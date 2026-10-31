// Compatibility fix for the pinned OmniRoute 3.8.51 standalone route.
// Transform only its module at load time; stored artifacts and the image stay intact.
import {readFileSync, realpathSync} from 'node:fs';
import {registerHooks} from 'node:module';
import {pathToFileURL} from 'node:url';

export function loggingEnvironment(config) {
  const env = {};
  for (const [key, variable] of [['pipelineMaxSizeKB', 'CALL_LOG_PIPELINE_MAX_SIZE_KB'],
                               ['textLimitChars', 'CHAT_LOG_TEXT_LIMIT']]) {
    const value = config?.logging?.[key];
    if (!Number.isSafeInteger(value) || value <= 0) {
      throw new Error('OmniRoute logging.' + key + ' must be a positive integer');
    }
    env[variable] = String(value);
  }
  return env;
}

const replacements = [
  ['i(b,h(g(j?.clientRawRequest)?.body))',
   'i(b,h(g(j?.clientRawRequest)?.body)),i(b,h(c?.requestBody))'],
  ['}(g(j?.providerRequest)?.body))',
   '}(g(j?.providerRequest)?.body??c?.requestBody))'],
];

export function patchConversationRoute(source) {
  if (replacements.every(([, after]) => source.includes(after))) return source;
  if (replacements.some(([, after]) => source.includes(after))) {
    throw new Error('OmniRoute conversation fallback: unsupported partially patched route bundle');
  }
  for (const [before, after] of replacements) {
    if (source.split(before).length !== 2) {
      throw new Error('OmniRoute conversation fallback: unsupported route bundle; review the pinned-version patch');
    }
    source = source.replace(before, after);
  }
  return source;
}

export function installConversationFallback(routePath = '/app/.build/next/server/app/api/conversations/[id]/tree/route.js') {
  routePath = realpathSync(routePath);
  // Check compatibility before accepting traffic, rather than silently ignoring drift.
  patchConversationRoute(readFileSync(routePath, 'utf8'));
  const target = pathToFileURL(routePath).href;
  return registerHooks({
    load(url, context, nextLoad) {
      const result = nextLoad(url, context);
      if (url !== target) return result;
      return {...result, source: patchConversationRoute(
        result.source == null ? readFileSync(routePath, 'utf8') : Buffer.from(result.source).toString('utf8'))};
    },
  });
}

if (process.env.POPOT_OMNIROUTE_CONVERSATION_FALLBACK === '1') {
  const version = JSON.parse(readFileSync('/app/package.json', 'utf8')).version;
  if (version !== '3.8.51') throw new Error('OmniRoute conversation fallback: unsupported version ' + version);
  installConversationFallback();
}
