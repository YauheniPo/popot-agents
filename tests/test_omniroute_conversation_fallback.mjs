import assert from 'node:assert/strict';
import {readFileSync, mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createRequire} from 'node:module';
import {runInNewContext} from 'node:vm';
import {test} from 'node:test';
import {patchConversationRoute, installConversationFallback, loggingEnvironment} from '../ax_local/omniroute_conversation_fallback.mjs';

const fixture = readFileSync(new URL('./fixtures/omniroute-3.8.51-turn-resolver.cjs', import.meta.url), 'utf8');
test('logging limits come from validated repository configuration', () => {
  assert.deepEqual(loggingEnvironment({logging:{pipelineMaxSizeKB:32768,textLimitChars:8388608}}),
    {CALL_LOG_PIPELINE_MAX_SIZE_KB:'32768',CHAT_LOG_TEXT_LIMIT:'8388608'});
  for(const value of [null, true, 0, -1, 1.5, '32768', Infinity]) {
    assert.throws(() => loggingEnvironment({logging:{pipelineMaxSizeKB:value,textLimitChars:8388608}}), /logging/);
    assert.throws(() => loggingEnvironment({logging:{pipelineMaxSizeKB:32768,textLimitChars:value}}), /logging/);
  }
  assert.throws(() => loggingEnvironment({}), /logging/);
});
function resolve(source, artifact) {
  const module = {exports: {}};
  runInNewContext(source, {module});
  return runFactory(module.exports, artifact);
}
function runFactory(factory, artifact) {
  const exports = {};
  function dependency(id) {
    if(id === 33653) return {getDbInstance: () => ({prepare: () => ({all: () => [{correlation_id:'id', artifact_relpath:'log.json'}]})})};
    if(id === 766025) return {readCallArtifact: () => ({state:'ready', artifact})};
    if(id === 348438) return {
      OI: body => (body.messages || body.input || []).flatMap(m => typeof m.content === 'string' && m.content ? [{role:m.role, text:m.content, blockKind:m.role==='tool'?'tool_result':'text', toolName:null}] : []),
      vm: turn => turn.role + ' ' + turn.text,
    };
    throw new Error('Unexpected dependency ' + id);
  }
  dependency.d = (target, properties) => {for(const [key, getter] of Object.entries(properties)) Object.defineProperty(target, key, {get:getter});};
  factory({}, exports, dependency);
  return exports.z([{lastCorrelationId:'id'}]);
}
const artifact = {
  pipeline: {error:{_omniroute_truncated:true, reason:'call_log_artifact_size_limit_exceeded'}},
  requestBody: {messages:[
    {role:'user', content:'hello'},
    {role:'assistant', content:null, tool_calls:[{function:{name:'read_file', arguments:'{"path":"a"}'}}]},
    {role:'tool', content:'file text'},
  ]},
};
test('recovers text and tool calls from a size-limited real resolver artifact', () => {
  assert.equal(resolve(fixture, artifact).size, 0);
  const turns=resolve(patchConversationRoute(fixture), artifact);
  assert.equal(turns.get('user hello').textPreview, 'hello');
  assert.equal(turns.get('tool {"path":"a"}').blockKind, 'tool_use');
  assert.equal(turns.get('tool {"path":"a"}').toolName, 'read_file');
  assert.equal(turns.get('tool file text').blockKind, 'tool_result');
});
test('keeps native pipeline content and handles omitted bodies without invented text', () => {
  const source=patchConversationRoute(fixture);
  const turns=resolve(source, {...artifact, pipeline:{clientRawRequest:{body:{messages:[{role:'user',content:'native'}]}}}});
  assert.equal(turns.get('user native').textPreview, 'native');
  assert.equal(turns.get('user hello').textPreview, 'hello');
  assert.equal(resolve(source, {requestBody:'[omitted]'}).size,0);
  assert.equal(patchConversationRoute(source),source);
  assert.throws(() => patchConversationRoute('changed upstream bundle'), /unsupported/);
  assert.throws(() => patchConversationRoute(fixture.replace(
    'i(b,h(g(j?.clientRawRequest)?.body))',
    'i(b,h(g(j?.clientRawRequest)?.body)),i(b,h(c?.requestBody))')), /unsupported/);
});
test('loader changes only the selected module in memory', () => {
  const dir=mkdtempSync(join(tmpdir(),'popot-conversation-'));
  const path=join(dir,'route.cjs');
  writeFileSync(path,fixture);
  const hooks=installConversationFallback(path);
  try {
    const factory=createRequire(import.meta.url)(path);
    assert.equal(runFactory(factory,artifact).get('user hello').textPreview,'hello');
    assert.equal(readFileSync(path,'utf8'),fixture);
  } finally {hooks.deregister();rmSync(dir,{recursive:true,force:true});}
});
