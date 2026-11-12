// Read only the baseline's request artifacts. Never export headers or bodies.
import {readdirSync, readFileSync, statSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

export function baselineCall(artifact, roots) {
  const raw = artifact.pipeline?.clientRawRequest;
  const headers = Array.isArray(raw?.headers) ? Object.fromEntries(raw.headers) : raw?.headers ?? {};
  const lower = Object.fromEntries(Object.entries(headers).map(([k,v]) => [k.toLowerCase(),v]));
  const sid = lower['x-popot-session-id'];
  const root = lower['x-popot-root-session-id'] ?? sid;
  if (!roots.has(root)) return null;
  const summary = artifact.summary ?? {};
  const response = artifact.pipeline?.clientResponse ?? artifact.responseBody;
  const usage = response?.usage;
  const safeUsage = usage ? Object.fromEntries(['prompt_tokens','completion_tokens','total_tokens']
    .filter(k => Number.isInteger(usage[k]) && usage[k] >= 0).map(k => [k,usage[k]])) : null;
  const tokens = Number.isFinite(usage?.total_tokens) ? usage.total_tokens :
    Number.isFinite(summary.tokens?.in) && Number.isFinite(summary.tokens?.out) ? summary.tokens.in + summary.tokens.out : null;
  return {id:summary.id, root_session_id:root, session_id:sid, run_id:lower['x-popot-run-id'],
    started_at:raw?.timestamp, duration_ms:summary.duration, status:summary.status,
    model:summary.model, requested_model:summary.requestedModel, provider:summary.provider,
    tokens, usage:safeUsage, error:Boolean(artifact.error) || summary.status >= 400};
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const report = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  const roots = new Set(report.results.map(r => r.session_id).filter(x => /^[a-f0-9]{16}$/.test(x)));
  const calls = [];
  function walk(directory) {
    for (const item of readdirSync(directory, {withFileTypes:true})) {
      const path = directory + '/' + item.name;
      if (item.isDirectory()) {walk(path);continue;}
      if (!item.name.endsWith('.json') || statSync(path).mtimeMs < Date.parse(report.started_at)) continue;
      const row = baselineCall(JSON.parse(readFileSync(path,'utf8')), roots);
      if (row) calls.push(row);
    }
  }
  walk('/app/data/call_logs');
  process.stdout.write(JSON.stringify(calls.sort((a,b) => Date.parse(a.started_at)-Date.parse(b.started_at))));
}
