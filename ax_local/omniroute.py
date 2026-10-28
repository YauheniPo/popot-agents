"""Apply repository-owned OmniRoute configuration through its management API."""

import argparse
import copy
import hashlib
import hmac
import http.cookiejar
import json
import os
from pathlib import Path
import sys
from urllib import error, request
from urllib.parse import quote, urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]


class ProvisionError(RuntimeError):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise error.HTTPError(req.full_url, code, 'Redirect refused', headers, fp)


class Api:
    def __init__(self, url):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname \
                or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProvisionError('OMNIROUTE_URL must be an HTTP(S) URL without credentials')
        self.url = url.rstrip('/')
        self.opener = request.build_opener(
            NoRedirect(), request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        call = request.Request(self.url + path, data=data, method=method,
                               headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(call, timeout=60) as response:
                return json.load(response)
        except error.HTTPError as exc:
            # Management responses can echo submitted credentials. Never log them.
            exc.close()
            raise ProvisionError(f'{method} {path} failed (HTTP {exc.code})') from None
        except OSError:
            raise ProvisionError(f'{method} {path} failed: connection unavailable') from None
        except ValueError:
            raise ProvisionError(f'{method} {path} returned invalid JSON') from None


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains(a, e) for a, e in zip(actual, expected))
    return actual == expected


def named(items, name):
    matches = [item for item in items if item.get('name') == name]
    if len(matches) > 1:
        raise ProvisionError('Duplicate managed name; resolve it before provisioning')
    return matches[0] if matches else None


def secret(environ, name):
    if not isinstance(name, str) or not name or not environ.get(name, '').strip():
        raise ProvisionError(f'Missing required secret: {name}')
    return environ[name]


def key_matches(key, value):
    return hmac.compare_digest(str(key.get('key', '')).encode(), value.encode()) or \
        hmac.compare_digest(str(key.get('keyHash', '')),
                            hashlib.sha256(value.encode()).hexdigest())


def configured_models(providers, combos):
    if not combos or not providers:
        raise ProvisionError('At least one provider and combo are required')
    if len({item['name'] for item in providers.values()}) != len(providers):
        raise ProvisionError('Managed provider names must be unique')
    used_providers, models = set(), []
    for name, combo in combos.items():
        if combo.get('name') != name or not combo.get('models'):
            raise ProvisionError('Each combo must have its configured name and model targets')
        for target in combo['models']:
            provider = target.get('providerId')
            if target.get('kind') != 'model' or provider not in providers \
                    or not target.get('model', '').startswith(provider + '/'):
                raise ProvisionError('Combo targets must reference configured providers')
            used_providers.add(provider)
            models.append(target['model'])
    if used_providers != set(providers):
        raise ProvisionError('Each configured provider must be used by a combo')

    return models


def existing_connections(before, providers, credentials):
    existing_providers = {}
    for provider, item in providers.items():
        existing = named(before['providerConnections'], item['name'])
        if existing and existing['provider'] != provider:
            raise ProvisionError('Managed connection name belongs to another provider')
        if existing is None:
            matches = [conn for conn in before['providerConnections']
                       if conn.get('provider') == provider and hmac.compare_digest(
                           str(conn.get('apiKey', '')).encode(), credentials[provider].encode())]
            if len(matches) > 1:
                raise ProvisionError('Several connections match a provider secret; select a unique name')
            if matches:
                existing = matches[0]
                item['name'] = existing['name']
        existing_providers[provider] = existing
    return existing_providers


def apply_settings(api, before, config):
    settings = config['settings']
    if not contains(before['settings'], settings):
        # The ordinary settings PATCH strips some persisted fields (cloudEnabled
        # in 3.8.51). The native partial JSON import preserves those settings.
        api.request('POST', '/api/settings/import-json', {'settings': settings})
    for section, path, method in (
            ('compression', '/api/settings/compression', 'PUT'),
            ('resilience', '/api/resilience', 'PATCH')):
        desired = config[section]
        if not contains(api.request('GET', path), desired):
            api.request(method, path, desired)
        if not contains(api.request('GET', path), desired):
            raise ProvisionError(f'{section} verification failed')


def apply_connections(api, providers, credentials, existing_providers):
    connections = {}
    for provider, item in providers.items():
        desired = {'provider': provider, 'name': item['name'], 'apiKey': credentials[provider]}
        existing = existing_providers[provider]
        if existing is None:
            existing = api.request('POST', '/api/providers', desired)['connection']
        elif not contains(existing, desired):
            api.request('PUT', '/api/providers/' + quote(existing['id'], safe=''),
                        {'name': item['name'], 'apiKey': credentials[provider]})
        connections[provider] = existing['id']

    return connections


def apply_combos(api, combos, connections, existing_combos):
    for name, combo in combos.items():
        for target in combo['models']:
            target['connectionId'] = connections[target['providerId']]
        existing = existing_combos[name]
        if existing is None:
            api.request('POST', '/api/combos', combo)
        elif not contains(existing, combo):
            api.request('PUT', '/api/combos/' + quote(existing['id'], safe=''), combo)


def provision(api, config, combos, environ):
    # Resolve every reference and secret before the first remote mutation.
    config, combos = copy.deepcopy(config), copy.deepcopy(combos)
    providers = config['providers']
    key_config = config['apiKey']
    password = secret(environ, 'OMNIROUTE_INITIAL_PASSWORD')
    gateway_key = secret(environ, key_config['keyEnv'])
    credentials = {provider: secret(environ, item['apiKeyEnv'])
                   for provider, item in providers.items()}
    models = configured_models(providers, combos)

    api.request('POST', '/api/auth/login', {'password': password})
    # The native export is kept only in memory. It lets us match an existing
    # gateway secret exactly, including legacy keys without a stored hash.
    before = api.request('GET', '/api/settings/export-json')
    key = named(before['apiKeys'], key_config['name'])
    if key is not None and not key_matches(key, gateway_key):
        raise ProvisionError('OMNIROUTE_API_KEY does not match the managed gateway key')
    if key and (key.get('isActive') is False or key.get('revokedAt') or key.get('isBanned')):
        raise ProvisionError('Managed gateway key is inactive, revoked or banned')
    if key is None and any(key_matches(item, gateway_key) for item in before['apiKeys']):
        raise ProvisionError('Gateway secret already belongs to another named API key')
    existing_providers = existing_connections(before, providers, credentials)
    existing_combos = {name: named(before['combos'], name) for name in combos}

    apply_settings(api, before, config)
    connections = apply_connections(api, providers, credentials, existing_providers)
    apply_combos(api, combos, connections, existing_combos)

    if key is None:
        # Native key creation generates its own secret. Import only the supplied
        # key, initially with deny-all model access; apply its full policy below.
        key = {'id': str(uuid.uuid4()), 'name': key_config['name'], 'key': gateway_key,
               'modelAccessMode': 'restricted', 'allowedModels': [], 'noLog': False}
        api.request('POST', '/api/settings/import-json', {'apiKeys': [key]})
    permissions = {
        **key_config['permissions'], 'modelAccessMode': 'restricted',
        'allowedModels': list(dict.fromkeys(models)), 'allowedCombos': list(combos),
        'allowedConnections': list(connections.values()),
    }
    if not contains(key, permissions):
        api.request('PATCH', '/api/keys/' + quote(key['id'], safe=''), permissions)

    after = api.request('GET', '/api/settings/export-json')
    checks = [('settings', after['settings'], config['settings']),
              ('key permissions', named(after['apiKeys'], key_config['name']), permissions)]
    checks.extend(('combo ' + name, named(after['combos'], name), combo)
                  for name, combo in combos.items())
    by_id = {item['id']: item for item in after['providerConnections']}
    checks.extend(('provider ' + provider, by_id.get(connections[provider]), {
        'id': connections[provider], 'provider': provider, 'apiKey': credentials[provider]})
        for provider in providers)
    for label, actual, expected in checks:
        for field, value in expected.items():
            if not contains(actual, {field: value}):
                raise ProvisionError(f'{label} verification failed for field {field}')
    return {'providers': len(connections), 'combos': len(combos), 'apiKeys': 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('OMNIROUTE_URL', 'http://127.0.0.1:20128'))
    parser.add_argument('--config', type=Path, default=ROOT / 'config/omniroute-settings.json')
    parser.add_argument('--combos', type=Path, default=ROOT / 'config/omniroute-combos.json')
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        combos = json.loads(args.combos.read_text())
        result = provision(Api(args.url), config, combos, os.environ)
    except ProvisionError as exc:
        print(f'OmniRoute provisioning failed: {exc}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print('OmniRoute provisioning failed: check configuration files and their schema',
              file=sys.stderr)
        return 1
    print('OmniRoute configuration verified: ' + json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
