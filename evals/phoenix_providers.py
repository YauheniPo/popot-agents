"""Provision native Phoenix providers and adapt its pinned SDK integration."""
import argparse
from copy import deepcopy
from functools import wraps
import json
import os
from pathlib import Path
import re
from urllib import error, request
from urllib.parse import urlsplit


def load_settings(path=None):
    settings = json.loads(Path(path or Path(__file__).with_name('config.json')).read_text())['x-providers']
    names, endpoints = set(), set()
    for item in settings:
        url = urlsplit(item['base_url'])
        if (not item['name'] or item['name'] in names or item['base_url'] in endpoints
                or url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment
                or not re.fullmatch(r'[A-Z][A-Z0-9_]*', item['api_key_env'])
                or not item['models'] or not all(isinstance(m, str) and m for m in item['models'])
                or len(set(item['models'])) != len(item['models'])
                or not isinstance(item.get('extra_body', {}), dict)):
            raise ValueError('Invalid Phoenix provider configuration')
        names.add(item['name'])
        endpoints.add(item['base_url'])
    return settings


def configure_client(client, defaults):
    """Attach request defaults to one native client; explicit options win."""
    if not defaults:
        return
    create = client.chat.completions.create

    @wraps(create)
    async def with_defaults(*args, **kwargs):
        kwargs['extra_body'] = {**deepcopy(defaults), **(kwargs.get('extra_body') or {})}
        return await create(*args, **kwargs)

    client.chat.completions.create = with_defaults


def install_runtime(settings):
    """Two compatibility hooks for the Phoenix version pinned in pyproject.toml.

    Keep execution, permissions, encryption and client cleanup in native Phoenix.
    The stock custom-provider catalog uses OpenAI names and has no body defaults.
    """
    from openai import AsyncOpenAI
    from phoenix.server.api.types.GenerativeModelCustomProvider import GenerativeModelCustomProvider

    by_name = {item['name']: item for item in settings}
    by_url = {item['base_url'].rstrip('/'): item for item in settings}
    field = next(f for f in GenerativeModelCustomProvider.__strawberry_definition__.fields
                 if f.python_name == 'model_names')
    original_names = field.base_resolver.wrapped_func

    @wraps(original_names)
    async def model_names(self, info):
        configured = by_name.get(await self.name(info))
        return list(configured['models']) if configured else await original_names(self, info)

    field.base_resolver.wrapped_func = model_names
    # Playground and /v1/chat/completions construct clients through different
    # Phoenix factories. Configure each native SDK instance at their common seam.
    original_init = AsyncOpenAI.__init__

    @wraps(original_init)
    def initialize_client(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        defaults = by_url.get(str(self.base_url).rstrip('/'), {}).get('extra_body', {})
        configure_client(self, defaults)

    AsyncOpenAI.__init__ = initialize_client


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise error.HTTPError(req.full_url, code, 'Redirect refused', headers, fp)


class API:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip('/'), key
        self.opener = request.build_opener(NoRedirect())

    def query(self, query, variables=None):
        call = request.Request(self.url + '/graphql',
            data=json.dumps({'query': query, 'variables': variables or {}}).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.key})
        try:
            with self.opener.open(call, timeout=20) as response:
                payload = json.loads(response.read())
        except error.HTTPError as exc:
            exc.close()
            raise RuntimeError(f'Phoenix provider API failed (HTTP {exc.code})') from None
        except (OSError, ValueError):
            raise RuntimeError('Phoenix provider API unavailable or returned invalid JSON') from None
        if payload.get('errors'):
            # GraphQL errors can echo variables, including submitted API keys.
            raise RuntimeError('Phoenix provider GraphQL operation failed')
        return payload['data']


PROVIDERS_QUERY = '''query($after:String) {
  generativeModelCustomProviders(first:100,after:$after) {
    edges { node { id name provider modelNames config {
      __typename ... on OpenAICustomProviderConfig {
        openaiApiType openaiClientKwargs { baseUrl }
      }
    } } }
    pageInfo { hasNextPage endCursor }
  }
}'''


def read_providers(api):
    providers, cursors, after = {}, set(), None
    while True:
        page = api.query(PROVIDERS_QUERY, {'after': after})['generativeModelCustomProviders']
        providers.update((edge['node']['name'], edge['node']) for edge in page['edges'])
        if not page['pageInfo']['hasNextPage']:
            return providers
        after = page['pageInfo']['endCursor']
        if not after or after in cursors:
            raise RuntimeError('Phoenix provider API repeated a pagination cursor')
        cursors.add(after)


def provision(settings, environ, api):
    existing = read_providers(api)
    result, active = [], []
    for item in settings:
        name = item['name']
        key = environ.get(item['api_key_env'])
        if not key:
            result.append({'name': name, 'status': 'skipped_missing_key'})
            continue
        payload = {'name': name, 'provider': item['provider'],
            'description': 'Managed at deployment. Models: ' + ', '.join(item['models']) + '.',
            'clientConfig': {'openai': {
                'openaiAuthenticationMethod': {'apiKey': key},
                'openaiClientKwargs': {'baseUrl': item['base_url']},
                'openaiApiType': 'CHAT_COMPLETIONS'}}}
        if item.get('extra_body'):
            payload['description'] += ' Request defaults are applied automatically by the deployment.'
        operation, input_type = 'createGenerativeModelCustomProvider', 'CreateGenerativeModelCustomProviderMutationInput'
        if name in existing:
            payload['id'] = existing[name]['id']
            operation, input_type = 'patchGenerativeModelCustomProvider', 'PatchGenerativeModelCustomProviderMutationInput'
        api.query('mutation($input:' + input_type + '!){' + operation +
                  '(input:$input){provider{id name}}}', {'input': payload})
        active.append(item)
        result.append({'name': name, 'status': 'configured', 'models': item['models']})
    actual = read_providers(api)
    for item in active:
        provider = actual.get(item['name'], {})
        expected_config = {'__typename': 'OpenAICustomProviderConfig',
            'openaiApiType': 'CHAT_COMPLETIONS', 'openaiClientKwargs': {'baseUrl': item['base_url']}}
        if (provider.get('provider') != item['provider']
                or provider.get('modelNames') != item['models']
                or provider.get('config') != expected_config):
            raise RuntimeError('Phoenix provider verification failed: ' + item['name'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://phoenix:6006')
    args = parser.parse_args()
    try:
        settings = load_settings()
        key = os.environ.get('PHOENIX_ADMIN_SECRET')
        if not key:
            raise RuntimeError('PHOENIX_ADMIN_SECRET is required for provider setup')
        print(json.dumps(provision(settings, os.environ, API(args.url, key))))
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')
