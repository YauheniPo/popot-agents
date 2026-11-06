import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtempSync, writeFileSync, mkdirSync, readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {after, test} from 'node:test';
import {
  axIndexFrom,
  installConversationStatus,
  readSnapshot,
  requestAxStatus,
} from '../ax_local/omniroute_continuation_status.mjs';

const warnings = [];
const realWarn = console.warn;
console.warn = (...args) => warnings.push(args.join(' '));
after(() => { console.warn = realWarn; });

function fixtureRoot() {
  const root = mkdtempSync(join(tmpdir(), 'popot-omniroute-'));
  mkdirSync(join(root, 'server', 'chunks'), {recursive: true});
  const pageDir = join(root, 'static', 'chunks', 'app', '(dashboard)', 'dashboard', 'conversations');
  mkdirSync(pageDir, {recursive: true});
  mkdirSync(join(root, 'server', 'app', '(dashboard)', 'dashboard', 'conversations'), {recursive: true});
  writeFileSync(join(root, 'server', 'chunks', 'route.js'),
    'const a={isStalled:(0,i.ot)({artifactRelPath:c,lastSeenAt:a.lastSeenAt,isActive:f})};');
  writeFileSync(join(pageDir, 'page.js'),
    'function v(e){if((0,r.ot){isStalled:e.isStalled})return null;return {isStalled:e.isStalled}}function g(' +
    'x)"Latest turn didn\'t end in stop"');
  writeFileSync(join(root, 'app-build-manifest.json'), '{"pages":{"x/page.js":[]}}');
  writeFileSync(join(root, 'server', 'app', '(dashboard)', 'dashboard', 'conversations',
    'page_client-reference-manifest.js'), "self.__RSC_MANIFEST={'page.js':{}};");
  return root;
}

test('readSnapshot returns the contents and freshness stamp of one descriptor', () => {
  const file = join(mkdtempSync(join(tmpdir(), 'popot-snapshot-')), 'artifact.json');
  writeFileSync(file, '{"a":1}');
  const first = readSnapshot(file);
  assert.equal(first.text, '{"a":1}');
  assert.match(first.stamp, /^\d+(\.\d+)?:\d+$/);
  writeFileSync(file, '{"a":22}');
  const second = readSnapshot(file);
  assert.equal(second.text, '{"a":22}');
  assert.notEqual(second.stamp, first.stamp);
  assert.throws(() => readSnapshot(file + '.missing'), {code: 'ENOENT'});
});

test('requestAxStatus reads the gateway payload from host and port', async () => {
  const server = createServer((request, response) => {
    assert.equal(request.url, '/chats');
    response.writeHead(200, {'content-type': 'application/json'});
    response.end(JSON.stringify({chats: [{sessionId: 's1', status: 'running'}]}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const body = await requestAxStatus({host: '127.0.0.1', port: server.address().port});
    assert.deepEqual(body, {chats: [{sessionId: 's1', status: 'running'}]});
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('requestAxStatus reports gateway failures instead of returning a body', async () => {
  const server = createServer((request, response) => {
    response.writeHead(503, {'content-type': 'application/json'});
    response.end('{"chats":[]}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    await assert.rejects(requestAxStatus({host: '127.0.0.1', port: server.address().port}),
      /AX status HTTP 503/);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('requestAxStatus rejects a body that is not JSON', async () => {
  const server = createServer((request, response) => {
    response.writeHead(200);
    response.end('not json');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    await assert.rejects(requestAxStatus({host: '127.0.0.1', port: server.address().port}),
      SyntaxError);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('requestAxStatus settles when the response is truncated mid body', async () => {
  const server = createServer((request, response) => {
    response.writeHead(200, {'content-type': 'application/json'});
    response.write('{"chats":[');
    setTimeout(() => response.destroy(), 20);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    await assert.rejects(requestAxStatus({host: '127.0.0.1', port: server.address().port}),
      /AX status response closed before completion|aborted|socket hang up/);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('requestAxStatus applies one deadline even while bytes keep arriving', async () => {
  const server = createServer((request, response) => {
    response.writeHead(200, {'content-type': 'application/json'});
    const timer = setInterval(() => response.write(' '), 25);
    response.on('close', () => clearInterval(timer));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    await assert.rejects(requestAxStatus({host: '127.0.0.1', port: server.address().port, timeoutMs: 120}),
      /AX status timeout/);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('axIndexFrom keys sessions and their runs for browser lookups', () => {
  const index = axIndexFrom({
    chats: [
      {sessionId: 's1', workflow: {state: 'running', waitingFor: 'ci', runId: 'r1'}, runStates: {r2: 'blocked'}},
      {sessionId: 's2', status: 'running'},
      {sessionId: 's3', outcomeStatus: 'failed', outcomeReason: 'worker_startup_failed'},
      {sessionId: 's4', workflow: {state: 'blocked', reason: 'workflow_turn_limit'}},
    ],
  });
  assert.deepEqual(index.get('s1'), {status: 'running', reason: undefined, waitingFor: 'ci', runId: 'r1'});
  assert.deepEqual(index.get('s1:r2'), {status: 'blocked', runId: 'r2'});
  assert.deepEqual(index.get('s2'), {status: 'running', reason: undefined, waitingFor: undefined, runId: undefined});
  assert.deepEqual(index.get('s3'), {status: 'failed', reason: 'worker_startup_failed', waitingFor: undefined, runId: undefined});
  assert.deepEqual(index.get('s4'), {status: 'blocked', reason: 'workflow_turn_limit', waitingFor: undefined, runId: undefined});
  assert.equal(axIndexFrom({}).size, 0);
});

test('installConversationStatus patches the pinned bundles and manifests', () => {
  const root = fixtureRoot();
  const pageDir = join(root, 'static', 'chunks', 'app', '(dashboard)', 'dashboard', 'conversations');
  const before = readFileSync(join(pageDir, 'page.js'), 'utf8');
  installConversationStatus(root);
  const page = readFileSync(join(pageDir, 'page.js'), 'utf8');
  assert.notEqual(page, before);
  assert.match(page, /data-popot':'popot-continuation-badge'/);
  assert.match(page, /const state=e\.continuationState\|\|\(e\.isStalled\?'stalled':null\)/);
  assert.match(readFileSync(join(root, 'app-build-manifest.json'), 'utf8'), /page\.js\?popot-status=1/);
  assert.match(readFileSync(join(root, 'server', 'app', '(dashboard)', 'dashboard', 'conversations',
    'page_client-reference-manifest.js'), 'utf8'), /page\.js\?popot-status=1/);
});

test('installConversationStatus exposes a status reader that tolerates missing artifacts', () => {
  const root = fixtureRoot();
  installConversationStatus(root);
  const missing = globalThis.__popotConversationStatus({artifactRelPath: 'absent.json', isActive: false});
  assert.equal(missing.isStalled, false);
  const escaping = globalThis.__popotConversationStatus({artifactRelPath: '../../etc/passwd', isActive: false});
  assert.equal(escaping.isStalled, false);
});

test('installConversationStatus refuses unexpected bundles', () => {
  const root = fixtureRoot();
  writeFileSync(join(root, 'server', 'chunks', 'route.js'), 'const a={};');
  assert.throws(() => installConversationStatus(root), /Unsupported pinned OmniRoute conversation bundles/);
});
