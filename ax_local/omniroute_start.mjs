// OmniRoute's native TLS fallback must never expose this deployment over HTTP.
import { readFileSync } from 'node:fs';
import { createSecureContext } from 'node:tls';

createSecureContext({
  cert: readFileSync(process.env.OMNIROUTE_TLS_CERT),
  key: readFileSync(process.env.OMNIROUTE_TLS_KEY),
  minVersion: 'TLSv1.2',
});
await import('/app/dev/run-standalone.mjs');
