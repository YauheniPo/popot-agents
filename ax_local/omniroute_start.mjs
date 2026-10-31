// OmniRoute's native TLS fallback must never expose this deployment over HTTP.
import { readFileSync } from 'node:fs';
import { createSecureContext } from 'node:tls';

createSecureContext({
  cert: readFileSync(process.env.OMNIROUTE_TLS_CERT),
  key: readFileSync(process.env.OMNIROUTE_TLS_KEY),
  minVersion: 'TLSv1.2',
});
const {loggingEnvironment} = await import('./popot-conversation-fallback.mjs');
Object.assign(process.env, loggingEnvironment(
  JSON.parse(readFileSync('/app/popot-omniroute-settings.json', 'utf8'))));
process.env.POPOT_OMNIROUTE_CONVERSATION_FALLBACK = '1';
process.env.NODE_OPTIONS = [process.env.NODE_OPTIONS,
  '--import=/app/popot-conversation-fallback.mjs'].filter(Boolean).join(' ');
await import('./dev/run-standalone.mjs');
