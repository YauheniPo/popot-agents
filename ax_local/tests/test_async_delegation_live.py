"""Opt-in fork/join integration using real AX sandboxes and deterministic model replies."""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
import uuid

from ax_local.api.ax_runner import AxAgentRunner
from ax_local.api.worker_pool import KubernetesWorkerPool, WorkerPoolController
from ax_local.api.delegation import DelegationService, start_a2a_server
from ax_local.api.provider_proxy import network_address_toward
from ax_local.api.server import kind_control_plane_host
from ax_local.config import AX_CONFIG
from popot_agents.orchestrator.session_store import SessionStore
from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.progress import observe_progress
from popot_agents.workflow_config import POLICY

SCRIPT = r'''
import io,json,os
from popot_agents.worker import http_harness
from ax_local.worker.entrypoint import main
role=json.loads(os.environ['HARNESS_ROLE_JSON'])['_ax_role']
real_urlopen=http_harness.request.urlopen
def tool(name,args,identifier):
 return {'id':identifier,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}
def respond(request,**kwargs):
 if not request.full_url.endswith('/chat/completions'):
  return real_urlopen(request,**kwargs)
 body=json.loads(request.data)
 results={m.get('tool_call_id'):m.get('content') for m in body['messages'] if m.get('role')=='tool'}
 if role=='fixture_parent':
  if 'fork-a' not in results:
   message={'tool_calls':[tool('delegate_task',{'role':'worker_a','task':'perform independent fixture work'},'fork-a'),
                          tool('delegate_task',{'role':'worker_b','task':'perform independent fixture work'},'fork-b')]}
  elif 'parent-own' not in results:
   message={'tool_calls':[tool('bash',{'command':'python3 -c "print(5)"'},'parent-own')]}
  elif 'dependent' not in results:
   ids=[json.loads(results[key])['sessionId'] for key in ('fork-a','fork-b')]
   message={'tool_calls':[tool('delegate_task',{'role':'worker_c','task':'verify both dependency reports','depends_on':ids},'dependent')]}
  elif 'join' not in results:
   ids=[json.loads(results[key])['sessionId'] for key in ('fork-a','fork-b','dependent')]
   message={'tool_calls':[tool('await_tasks',{'session_ids':ids,'wait_for':'all'},'join')]}
  else:
   joined=json.loads(results['join'])
   assert len(joined['results'])==3 and not joined['pending'], 'incomplete join'
   assert '5' in results['parent-own'], 'parent evidence missing after restore'
   message={'content':json.dumps({'status':'completed','answer':'fork/join verified',
      'evidence':['parent tool result preserved','three child results accepted'], 'remaining_work':[],'errors':[]})}
 elif 'child-work' not in results:
  if role=='worker_c':
   context=' '.join(str(m.get('content','')) for m in body['messages'])
   assert 'result from worker_a' in context and 'result from worker_b' in context, 'dependency reports missing'
  delay=10 if role in ('worker_a','worker_b') else 0
  message={'tool_calls':[tool('bash',{'command':f'python3 -c "import time;time.sleep({delay});print(7)"'},'child-work')]}
 else:
  message={'content':json.dumps({'status':'completed','answer':'result from '+role,
      'evidence':['local fixture command succeeded'], 'remaining_work':[],'errors':[]})}
 return io.BytesIO(json.dumps({'choices':[{'message':message}]}).encode())
http_harness.request.urlopen=respond
main()
'''


class FixtureRunner(AxAgentRunner):
    def task_manifest(self,*args,**kwargs):
        manifest=super().task_manifest(*args,**kwargs)
        encoded=base64.b64encode(SCRIPT.encode()).decode()
        manifest['spec']['command']=['python','-u','-c',"import base64;exec(base64.b64decode('"+encoded+"'))"]
        return manifest


class TimedService(DelegationService):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.timings={};self.timing_lock=threading.Lock()

    def _record_progress(self,job,message):
        event=json.loads(message)
        if event.get('call_id')=='child-work' and event.get('event') in {'tool_call','tool_result'}:
            with self.timing_lock:self.timings.setdefault(job.config['_ax_role'],{})[event['event']]=time.monotonic()
        super()._record_progress(job,message)


@unittest.skipUnless(os.getenv('AX_TEST_ASYNC_LIVE'),'requires local AX and a freshly built worker image')
class LiveAsyncDelegationTests(unittest.TestCase):
    def test_parent_and_children_overlap_and_dependent_starts_after_both_results(self):
        host=network_address_toward(kind_control_plane_host(AX_CONFIG['context']),6443)
        runner=FixtureRunner(image=os.environ['AX_TEST_WORKER_IMAGE'],model='fixture',base_url='http://localhost:1/v1')
        pool=WorkerPoolController(KubernetesWorkerPool(AX_CONFIG['context'], AX_CONFIG['atespace'],
            AX_CONFIG['worker_pool_name'], os.environ['KUBECONFIG']),
            warm_workers=AX_CONFIG['warm_workers'], max_workers=AX_CONFIG['max_workers'], idle_seconds=60)
        runner.worker_pool=pool
        pool.start()
        roles={r:{'agent':'fixture','_ax_role':r,'instructions':r,'tools':['bash'],'max_tool_rounds':8}
            for r in ('fixture_parent','worker_a','worker_b','worker_c')}
        roles['fixture_parent']['allowed_roles']=['worker_a','worker_b','worker_c']
        roles['fixture_parent']['_workflow_coordinator']=True
        policy={'max_depth':2,'max_calls_per_turn':8,'turn_timeout_seconds':180,'poll_interval_seconds':.1}
        sid=uuid.uuid4().hex[:16]
        with tempfile.TemporaryDirectory() as directory:
            service=TimedService(roles,policy,{'fixture':runner},SessionStore(directory),f'http://{host}:0')
            runner.delegation=service
            server=start_a2a_server(service,host,0)
            parent=None
            try:
                parent=runner.start_chat(sid,roles['fixture_parent'])
                workflow=WorkflowRun({**POLICY,'total_seconds':180},'controlled fork/join fixture')
                own=[]
                def observe(message):
                    event=json.loads(message)
                    if event.get('call_id')=='parent-own' and event.get('event')=='tool_result':own.append(time.monotonic())
                with service.workflow(sid,workflow.deadline,max_turns=4,metadata=workflow.meta),observe_progress(observe):
                    workflow.begin()
                    first=parent.send('controlled fork/join fixture',timeout_seconds=90)
                    self.assertEqual(first['outcome']['reason'],'child_pending',first)
                    checkpoint=parent.get_checkpoint()
                    self.assertIsNone(workflow.record(first,checkpoint))
                    history=[{'role':'user','content':'controlled fork/join fixture'},
                             {'role':'assistant','content':first['answer']}]
                    self.assertTrue(service.await_children(sid,parent,history,workflow.deadline))
                    continuation=workflow.continuation(compact=True)
                    workflow.begin()
                    final=parent.send(continuation,timeout_seconds=60)
                    self.assertEqual(final['outcome']['status'],'completed',final)
                    self.assertEqual(workflow.meta['turns'],1)
                a,b,c=[service.timings[r] for r in ('worker_a','worker_b','worker_c')]
                self.assertTrue(own)
                self.assertLess(own[0],min(a['tool_result'],b['tool_result']))
                self.assertLess(max(a['tool_call'],b['tool_call']),min(a['tool_result'],b['tool_result']))
                self.assertGreaterEqual(c['tool_call'],max(a['tool_result'],b['tool_result']))
                print(json.dumps({'nativeAXForkJoin':True,'parentWorkedBeforeChildCompletion':True,
                    'childrenOverlapped':True,'dependencyOrdering':True,'coordinatorTurns':1}),flush=True)
            finally:
                if parent:parent.close()
                service.close();server.server_close();runner.shutdown_worker_queue();runner.maintenance();pool.close()
                self.assertEqual(runner.worker_queue.status()['active'],0)


if __name__=='__main__':unittest.main()
