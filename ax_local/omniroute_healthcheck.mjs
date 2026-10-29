import { get } from 'node:https';

const call = get(`https://127.0.0.1:${process.env.PORT || '20128'}/healthz`, response => {
  response.resume();
  if (response.statusCode < 200 || response.statusCode >= 300) process.exitCode = 1;
});
call.setTimeout(4000, () => call.destroy(new Error('healthcheck timeout')));
call.on('error', () => {
  console.error('OmniRoute HTTPS healthcheck failed');
  process.exitCode = 1;
});
