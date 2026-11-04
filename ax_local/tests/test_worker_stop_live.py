"""Opt-in cancellation test that inspects actual gVisor processes after AX deletion."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
import uuid

API_FIXTURE = r'''
import importlib.util,json,os,tempfile,time,uuid
from pathlib import Path
from ax_local.api.delegation import start_a2a_server
from ax_local.api.provider_proxy import network_address_toward
from ax_local.api.server import kind_control_plane_host
from ax_local.config import AX_CONFIG
from popot_agents.orchestrator.session_store import SessionStore
from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.workflow_config import POLICY
spec=importlib.util.spec_from_file_location('fixture',os.environ['STOP_FIXTURE_MODULE'])
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
fixture.SCRIPT=fixture.SCRIPT.replace('delay=10 if','delay=90 if')
host=network_address_toward(kind_control_plane_host(AX_CONFIG['context']),6443)
runner=fixture.FixtureRunner(image=os.environ['AX_TEST_WORKER_IMAGE'],model='fixture',base_url='http://localhost:1/v1')
roles={r:{'agent':'fixture','_ax_role':r,'instructions':r,'tools':['bash'],'max_tool_rounds':8}
 for r in ('fixture_parent','worker_a','worker_b','worker_c')}
roles['fixture_parent']['allowed_roles']=['worker_a','worker_b','worker_c']
roles['fixture_parent']['_workflow_coordinator']=True
policy={'max_depth':2,'max_calls_per_turn':8,'turn_timeout_seconds':180,'poll_interval_seconds':.1}
sid=uuid.uuid4().hex[:16]
with tempfile.TemporaryDirectory() as directory:
 service=fixture.TimedService(roles,policy,{'fixture':runner},SessionStore(directory),f'http://{host}:0')
 runner.delegation=service;server=start_a2a_server(service,host,0);parent=None;jobs=[]
 try:
  parent=runner.start_chat(sid,roles['fixture_parent'])
  workflow=WorkflowRun({**POLICY,'total_seconds':240},'controlled cancellation fixture')
  with service.workflow(sid,workflow.deadline,max_turns=4,metadata=workflow.meta):
   workflow.begin();result=parent.send('controlled cancellation fixture',timeout_seconds=90)
   assert result['outcome']['reason']=='child_pending',result
   deadline=time.monotonic()+45
   while not all('tool_call' in service.timings.get(r,{}) for r in ('worker_a','worker_b')):
    assert time.monotonic()<deadline,'children did not start';time.sleep(.1)
   jobs=list(service.jobs.values())
   ids=[sid]+[job.session_id for job in jobs]
   Path(os.environ['STOP_FIXTURE_READY']).write_text(json.dumps({'sessionIds':ids,'physicalTasks':[
    'popot-chat-'+sid]+['popot-chat-'+job.session_id for job in jobs if job.chat is not None]}))
   deadline=time.monotonic()+60
   while not Path(os.environ['STOP_FIXTURE_RELEASE']).exists():
    assert time.monotonic()<deadline,'host never acknowledged actor identities';time.sleep(.1)
  parent.close()
  for job in jobs:
   job.thread.join(15);assert not job.thread.is_alive(),'child thread survived cancellation'
   assert job.canceled.is_set()
  runner.maintenance()
  assert runner.worker_queue.status()['active']==0,runner.worker_queue.status()
  print(json.dumps({'parentAndChildrenCanceled':True,'leasesReleased':True,'childJobs':len(jobs)}),flush=True)
 finally:
  if parent:parent.close()
  service.close();server.server_close();runner.shutdown_worker_queue();runner.maintenance()
'''


@unittest.skipUnless(os.getenv('AX_TEST_STOP_LIVE'),'requires local Docker, AX, and deployed atelet lifecycle fence')
class LiveWorkerStopTests(unittest.TestCase):
    def test_parent_stop_removes_tasks_and_their_actual_sandbox_processes(self):
        root=Path(__file__).resolve().parents[2]
        tag=uuid.uuid4().hex
        api='popot-ax-local-api-1';node='popot-ax-control-plane'
        ready='/tmp/popot-stop-'+tag+'.json';release='/tmp/popot-stop-'+tag+'.release'
        module='/tmp/popot-stop-'+tag+'.py'
        image=(root/'ax_local/.local/worker-image').read_text().strip()
        def run(args,**kwargs):return subprocess.run(args,capture_output=True,text=True,check=True,timeout=45,**kwargs).stdout
        run(['docker','cp',str(root/'ax_local/tests/test_async_delegation_live.py'),api+':'+module])
        def processes(uids):
            script='for p in /proc/[0-9]*; do case "$(cat "$p/comm" 2>/dev/null)" in gvisor_sentry|exe) for uid do if grep -zq "$uid" "$p/cmdline" 2>/dev/null; then printf "%s %s\\n" "$uid" "${p##*/}"; fi; done;; esac; done'
            return run(['docker','exec',node,'sh','-c',script,'sh',*uids]).splitlines()
        with tempfile.TemporaryDirectory() as tmp:
            log=Path(tmp)/'native.log'
            with log.open('w') as output:
                command=['docker','exec','-i','-e','KUBECONFIG=/run/ax-local/kubeconfig',
                    '-e','AX_TEST_WORKER_IMAGE='+image,'-e','STOP_FIXTURE_MODULE='+module,
                    '-e','STOP_FIXTURE_READY='+ready,'-e','STOP_FIXTURE_RELEASE='+release,api,'python','-u','-']
                proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=output,stderr=subprocess.STDOUT,text=True)
                proc.stdin.write(API_FIXTURE);proc.stdin.close()
                try:
                    deadline=time.monotonic()+90;metadata=None
                    while time.monotonic()<deadline:
                        status=subprocess.run(['docker','exec',api,'cat',ready],capture_output=True,text=True,timeout=10)
                        if status.returncode==0:metadata=json.loads(status.stdout);break
                        if proc.poll() is not None:break
                        time.sleep(.5)
                    self.assertIsNotNone(metadata,log.read_text()[-3000:])
                    ate=[str(root/'ax_local/.local/bin/kubectl-ate'),'--kubeconfig='+str(root/'ax_local/.local/kubeconfig'),
                        '--context=kind-popot-ax','get','actors','-a','ate-demo-counter','-o','json']
                    actors=json.loads(run(ate))['actors']
                    uids=[a['metadata']['uid'] for a in actors if a['metadata']['name'] in metadata['physicalTasks']]
                    self.assertEqual(len(uids),3,metadata)  # parent + two running children; dependent has no worker.
                    self.assertTrue(all(re.fullmatch(r'[a-f0-9-]{36}',uid) for uid in uids))
                    before=processes(uids)
                    self.assertEqual({row.split()[0] for row in before},set(uids))
                    run(['docker','exec',api,'touch',release])
                    proc.wait(timeout=60)
                    self.assertEqual(proc.returncode,0,log.read_text()[-4000:])
                    self.assertEqual(processes(uids),[],'AX tasks deleted but gVisor processes survived')
                    for uid in uids:
                        run(['docker','exec',node,'test','-f','/var/lib/ateom-gvisor/actors/'+uid+'/popot-terminated'])
                    time.sleep(25)  # > dashboard's 20-second active-process window.
                    self.assertEqual(processes(uids),[],'a delayed start resurrected a terminated actor')
                    kube=['kubectl','--kubeconfig='+str(root/'ax_local/.local/kubeconfig'),'--context=kind-popot-ax']
                    query='count(time() - popot_sandbox_process_sampled_at_seconds{task=~"'+ '|'.join(metadata['physicalTasks']) +'"} < 20)'
                    from urllib.parse import urlencode
                    metrics=json.loads(run([*kube,'-n','otel-system','exec','deployment/prometheus','--','wget','-qO-',
                        'http://127.0.0.1:9090/api/v1/query?'+urlencode({'query':query})]))
                    samples=metrics['data']['result']
                    self.assertTrue(not samples or all(float(row['value'][1])==0 for row in samples),samples)
                    print(json.dumps({'nativeParentCancellation':True,'physicalSandboxesStopped':len(uids),
                        'dependentNeverStarted':True,'noResurrectionAfterSeconds':25,'freshMetrics':0}),flush=True)
                finally:
                    subprocess.run(['docker','exec',api,'touch',release],capture_output=True,timeout=10)
                    if proc.poll() is None:
                        try:proc.wait(timeout=60)
                        except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
                    run(['docker','exec',api,'rm','-f',ready,release,module])


if __name__=='__main__':unittest.main()
