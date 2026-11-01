import assert from 'node:assert/strict';
import {test} from 'node:test';
import {conversationStatus} from '../ax_local/omniroute_continuation_status.mjs';

const args={lastSeenAt:'2026-11-01T07:00:00Z',now:Date.parse('2026-11-01T07:10:00Z'),isActive:false};
function artifact(message,finish_reason='stop') {
  return {pipeline:{clientResponse:{choices:[{message,finish_reason}]}}};
}
test('Chat Completions stop is completed, tool_calls waits, length is incomplete',()=>{
  assert.equal(conversationStatus(args,artifact({content:'done'})).continuationState,'completed');
  assert.equal(conversationStatus(args,artifact({tool_calls:[{id:'one'}]},'tool_calls')).isStalled,true);
  assert.equal(conversationStatus({...args,isActive:true},artifact({},'length')).isStalled,false);
});
test('AX child, CI and tool waits are distinguished from an unowned stalled request',()=>{
  const a=artifact({tool_calls:[{id:'one'}]},'tool_calls');
  for(const [waitingFor,expected]of[['children','waiting_child'],['ci','waiting_ci'],['tool','waiting_tool']]) {
    const s=conversationStatus({...args,ax:{status:'running',waitingFor}},a);
    assert.equal(s.continuationState,expected);assert.equal(s.isStalled,false);
  }
  assert.equal(conversationStatus({...args,ax:{status:'blocked'}},a).continuationState,'blocked');
});
test('Responses output and unavailable artifacts retain honest states',()=>{
  assert.equal(conversationStatus(args,{pipeline:{clientResponse:{status:'completed',output:[{type:'message'}]}}}).continuationState,'completed');
  assert.equal(conversationStatus(args,null).continuationState,'unknown');
});

test('pinned route and browser transformations are guarded and idempotent',async()=>{
  const {patchConversationStatusRoute:route,patchConversationStatusPage:page}=await import('../ax_local/omniroute_continuation_status.mjs');
  const original='const x={isStalled:(0,i.ot)({artifactRelPath:c,lastSeenAt:a.lastSeenAt,isActive:f})};';
  const changed=route(original);
  assert.match(changed,/globalThis.__popotConversationStatus/);
  assert.equal(route(changed),changed);
  assert.throws(()=>route('unsupported route'));
  const browser='function v(e){return "old"}function g(){};const a={isStalled:e.isStalled},b={isStalled:e.isStalled}';
  const patched=page(browser);
  assert.match(patched,/continuationState:e.continuationState/);
  assert.match(patched,/waiting tool/);
  assert.equal(page(patched),patched);
  assert.throws(()=>page('unsupported page'));
});
