import assert from 'node:assert/strict';
import {test} from 'node:test';
import {baselineCall} from '../evals/gateway.mjs';

test('gateway export selects owned roots and never exports credentials or prompts', () => {
  const artifact = {summary:{id:'call',model:'fixed',provider:'cloud',tokens:{in:10,out:2},status:200},
    requestBody:{messages:[{content:'private prompt'}]},
    pipeline:{clientRawRequest:{headers:{Authorization:'Bearer private-key','X-Popot-Session-Id':'child',
      'X-Popot-Root-Session-Id':'root'},body:{messages:[{content:'private prompt'}]}},
      clientResponse:{usage:{total_tokens:13,raw_prompt:'private prompt'}}}};
  assert.equal(baselineCall(artifact,new Set(['other'])),null);
  const exported = baselineCall(artifact,new Set(['root']));
  assert.equal(exported.tokens,13);
  assert.equal(exported.root_session_id,'root');
  assert.ok(!JSON.stringify(exported).includes('private'));
});

test('gateway export preserves unknown usage and provider errors', () => {
  const row=baselineCall({summary:{status:429},pipeline:{clientRawRequest:{headers:[['x-popot-session-id','root']]}}},new Set(['root']));
  assert.equal(row.tokens,null);
  assert.equal(row.error,true);
});
