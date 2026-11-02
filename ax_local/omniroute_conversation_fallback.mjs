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

// Tool receipts originate from Python json.dumps, but gateway logging compacts
// their JSON strings after the conversation identity has already been hashed.
function spacedToolJson(text) {
  if (typeof text !== 'string' || !/^[\s]*[\[{]/.test(text)) return null;
  try { JSON.parse(text); } catch { return null; }
  let result = '', quoted = false, escaped = false;
  for (const char of text) {
    if (quoted) {
      result += char;
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === '"') quoted = false;
    } else if (char === '"') {
      quoted = true;
      result += char;
    } else if (char === ',' || char === ':') result += char + ' ';
    else if (!/\s/.test(char)) result += char;
  }
  return result;
}

const originalIndexer = 'function i(a,b){for(let c of b){let b=(0,f.vm)(c);a.has(b)||a.set(b,{textPreview:c.text,blockKind:c.blockKind,toolName:c.toolName})}}';
const receiptIndexer = spacedToolJson.toString() + `
function i(a,b){for(const turn of b){
  const variants=[turn];
  if(turn.blockKind==='tool_result'){
    const spaced=spacedToolJson(turn.text);
    if(spaced!==null&&spaced!==turn.text)variants.push({...turn,text:spaced});
  }
  for(const c of variants){const key=(0,f.vm)(c);
    if(!a.has(key))a.set(key,{textPreview:c.text,blockKind:c.blockKind,toolName:c.toolName});
  }
}}`;

const replacements = [
  ['i(b,h(g(j?.clientRawRequest)?.body))',
   'i(b,h(g(j?.clientRawRequest)?.body)),i(b,h(c?.requestBody))'],
  ['}(g(j?.providerRequest)?.body))',
   '}(g(j?.providerRequest)?.body??c?.requestBody))'],
  [originalIndexer, receiptIndexer],
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
