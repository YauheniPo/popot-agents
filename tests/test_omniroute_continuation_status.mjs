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
  const blocked=conversationStatus({...args,ax:{status:'blocked',reason:'workflow_turn_limit'}},a);
  assert.equal(blocked.continuationState,'blocked');
  assert.equal(blocked.continuationCode,'workflow_turn_limit');
  assert.match(blocked.continuationReason,/workflow_turn_limit/);
});
test('Non-string and malformed AX reasons are not exposed as codes',()=>{
  const s=conversationStatus({...args,ax:{status:'blocked',reason:42}},artifact({}));
  assert.equal(s.continuationState,'blocked');
  assert.equal(s.continuationCode,null);
  assert.equal(s.continuationReason,'AX workflow blocked.');
  const bad=conversationStatus({...args,ax:{status:'failed',reason:'Not a code!'}},artifact({}));
  assert.equal(bad.continuationCode,null);
  assert.equal(bad.continuationReason,'AX workflow failed: Not a code!');
  const ok=conversationStatus({...args,ax:{status:'failed',reason:'workflow_turn_limit'}},artifact({}));
  assert.equal(ok.continuationCode,'workflow_turn_limit');
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
  assert.match(patched,/popot-continuation-reason/);
  assert.equal(page(patched),patched);
  assert.throws(()=>page('unsupported page'));
});
test('AX index derives running, outcome, fallback and workflow states',async()=>{
  const {axIndexFrom}=await import('../ax_local/omniroute_continuation_status.mjs');
  const index=axIndexFrom({chats:[
    {sessionId:'running',status:'running'},
    {sessionId:'outcome',status:'idle',outcomeStatus:'completed'},
    {sessionId:'fallback',status:'idle'},
    {sessionId:'workflow',workflow:{state:'blocked',reason:'limit',runId:'r'},runStates:{'1':'running'}},
  ]});
  assert.equal(index.get('running').status,'running');
  assert.equal(index.get('outcome').status,'completed');
  assert.equal(index.get('fallback').status,'idle');
  assert.equal(index.get('workflow').status,'blocked');
  assert.equal(index.get('workflow').reason,'limit');
  assert.equal(index.get('workflow:1').status,'running');
});
