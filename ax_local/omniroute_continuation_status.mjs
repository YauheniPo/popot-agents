// Continuation states for pinned OmniRoute 3.8.51; gateway observations and AX
// workflow status are separate inputs. No conversation contents are rewritten.
import {closeSync, fstatSync, openSync, readFileSync, readdirSync, realpathSync, writeFileSync} from 'node:fs';
import {request as httpRequest} from 'node:http';
import {resolve, sep} from 'node:path';
import {pathToFileURL} from 'node:url';
import {registerHooks} from 'node:module';

export function conversationStatus({isActive=false,lastSeenAt,now=Date.now(),ax,axUnavailable=false}, artifact) {
  let state='unknown', reason='No usable final response artifact is available.';
  const response=artifact?.pipeline?.clientResponse??artifact?.responseBody;
  const choice=response?.choices?.[0];
  if(choice) {
    const finish=choice.finish_reason;
    if(finish==='stop'&&!choice.message?.tool_calls?.length) state='completed';
    else if(finish==='tool_calls'||choice.message?.tool_calls?.length) state='waiting_tool';
    else if(finish==='length'||finish==='content_filter'||response?._truncated) state='incomplete';
  } else {
    const output=response?.summary?.output??response?.output;
    const status=response?.summary?.status??response?.status;
    if(response?._truncated||status==='incomplete') state='incomplete';
    else if(status==='failed'||response?.error) state='failed';
    else if(Array.isArray(output)&&output.length) state=output.some(x=>x?.type==='function_call')?'waiting_tool':'completed';
  }
  if(isActive) {state='active';reason='A model request is active in the gateway.';}
  else if(ax?.status==='running') {
    state=({children:'waiting_child',ci:'waiting_ci',model:'active'})[ax.waitingFor]??'waiting_tool';
    reason='AX task is running'+(ax.waitingFor?'; waiting for '+ax.waitingFor:'.');
  } else if(['failed','blocked'].includes(ax?.status)) {
    state=ax.status;reason='AX workflow reports '+ax.status+'.';
  } else if(ax?.status==='completed') {state='completed';reason='AX workflow completed.';}
  else if(ax?.status==='partial') {state='waiting_resume';reason='AX returned partial work; continuation requires its coordinator.';}
  else if(axUnavailable) {state='unknown';reason='AX status is unavailable; the gateway cannot determine worker progress.';}
  else if(['waiting_tool','incomplete'].includes(state)&&Number.isFinite(Date.parse(lastSeenAt))&&now-Date.parse(lastSeenAt)>300000) {
    state='stalled';reason='No active request or matching running AX task; the unfinished gateway conversation has no continuation for over 5 minutes.';
  } else if(state!=='unknown') reason=({completed:'The model response finished normally.',waiting_tool:'The model requested a tool; continuation has not been observed.',incomplete:'The response ended incomplete or was truncated.',failed:'The response failed.'})[state]??reason;
  return {isStalled:state==='stalled',continuationState:state,continuationReason:reason};
}

const statusExpression=/isStalled:\(0,\w+\.ot\)\((\{artifactRelPath:\w+,lastSeenAt:\w+\.lastSeenAt,isActive:\w+\})\)/g;
export function patchConversationStatusRoute(source) {
  if(source.includes('...globalThis.__popotConversationStatus(')) return source;
  let count=0;
  const patched=source.replace(statusExpression,(_,args)=>{count++;return '...globalThis.__popotConversationStatus('+args+')';});
  if(count!==1) throw new Error('Unsupported OmniRoute continuation route bundle');
  return patched;
}

export function patchConversationStatusPage(source) {
  if(source.includes('popot-continuation-badge')) return source;
  const component=/function v\(e\)\{[\s\S]*?\}function g\(/;
  if(!component.test(source)||source.split('isStalled:e.isStalled}').length!==3) throw new Error('Unsupported OmniRoute continuation page bundle');
  return source.replace(component,`function v(e){
    const state=e.continuationState||(e.isStalled?'stalled':null);
    const labels={stalled:'stalled',waiting_tool:'waiting tool',waiting_child:'waiting child',waiting_ci:'waiting CI',waiting_resume:'waiting resume',failed:'failed',blocked:'blocked'};
    if(!labels[state])return null;
    const problem=['stalled','failed','blocked'].includes(state);
    return(0,l.jsxs)('span',{'data-popot':'popot-continuation-badge',title:e.continuationReason||'Gateway continuation status',
      className:'inline-flex items-center gap-0.5 px-2 py-0.5 rounded-full text-[9px] font-bold border '+(problem?'bg-red-500/15 text-red-500 border-red-500/25':'bg-amber-500/15 text-amber-600 border-amber-500/25'),
      children:[(0,l.jsx)('span',{className:'material-symbols-outlined text-[11px] leading-none',children:problem?'error':'hourglass_empty'}),labels[state]]});
  }function g(`).replaceAll('isStalled:e.isStalled}', 'isStalled:e.isStalled,continuationState:e.continuationState,continuationReason:e.continuationReason}');
}

function header(headers,name) {
  if(Array.isArray(headers))return headers.find(([k])=>String(k).toLowerCase()===name)?.[1];
  return Object.entries(headers??{}).find(([k])=>k.toLowerCase()===name)?.[1];
}

function compactArtifact(artifact) {
  const response=artifact?.pipeline?.clientResponse??artifact?.responseBody;
  const choice=response?.choices?.[0];
  const minimal=choice?{_truncated:!!response?._truncated,choices:[{finish_reason:choice.finish_reason,
    message:{tool_calls:choice.message?.tool_calls?.length?[{}]:undefined}}]}:
    {status:response?.summary?.status??response?.status,
      output:(response?.summary?.output??response?.output)?.map(x=>({type:x?.type})),
      error:!!response?.error,_truncated:!!response?._truncated};
  const headers=artifact?.pipeline?.clientRawRequest?.headers;
  return {pipeline:{clientResponse:minimal,clientRawRequest:{headers:Object.fromEntries(
    ['x-popot-session-id','x-popot-run-id','x-popot-root-session-id'].map(k=>[k,header(headers,k)]))}}};
}

// Read one bounded snapshot through a single descriptor so the freshness stamp
// and the contents describe the same file even while a writer replaces it.
export function readSnapshot(path) {
  const descriptor=openSync(path,'r');
  try {
    const stat=fstatSync(descriptor);
    return {stamp:stat.mtimeMs+':'+stat.size,text:readFileSync(descriptor,'utf8')};
  } finally {closeSync(descriptor);}
}

// The gateway is only reachable inside the compose network, which speaks plain
// HTTP. Naming host and port builds that request without a clear-text URL.
export function requestAxStatus({host='api',port=8002,path='/chats',timeoutMs=2000}={}) {
  return new Promise((succeed,fail)=>{
    const request=httpRequest({host,port,path,method:'GET'},response=>{
      const chunks=[];
      response.on('data',chunk=>chunks.push(chunk));
      response.on('end',()=>{
        if(response.statusCode<200||response.statusCode>299){fail(new Error('AX status HTTP '+response.statusCode));return;}
        try {succeed(JSON.parse(Buffer.concat(chunks).toString('utf8')));} catch(error) {fail(error);}
      });
    });
    request.setTimeout(timeoutMs,()=>request.destroy(new Error('AX status timeout')));
    request.on('error',fail);
    request.end();
  });
}

// AX gateway rows collapse into the session and run keys the browser looks up.
export function axIndexFrom(body) {
  const next=new Map();
  for(const row of body.chats??[]) {
    next.set(row.sessionId,{status:row.workflow?.state??(row.status==='running'?'running':row.outcomeStatus??row.status),waitingFor:row.waitingFor??row.workflow?.waitingFor,runId:row.workflow?.runId??row.runId});
    for(const [id,state]of Object.entries(row.runStates??{}))next.set(row.sessionId+':'+id,{status:state,runId:id});
  }
  return next;
}

export function installConversationStatus(root='/app/.build/next') {
  const ax=new Map();let fetchedAt=0, refreshing=false, unavailable=false;
  async function refresh() {
    if(refreshing)return;refreshing=true;
    try {
      const next=axIndexFrom(await requestAxStatus());
      ax.clear();for(const [k,v]of next)ax.set(k,v);fetchedAt=Date.now();
      unavailable=false;
    } catch(error) {
      if(!unavailable)console.warn('OmniRoute AX status unavailable:',error.name);
      unavailable=true;
    } finally {refreshing=false;}
  }
  void refresh();const timer=setInterval(()=>void refresh(),5000);timer.unref();
  const artifacts=new Map(),dataRoot=resolve('/app/data/call_logs');
  globalThis.__popotConversationStatus=args=>{
    let artifact=null;
    const path=typeof args.artifactRelPath==='string'?resolve(dataRoot,args.artifactRelPath):'';
    if(path.startsWith(dataRoot+sep)) {
      try {
        const snapshot=readSnapshot(path),cached=artifacts.get(path);
        artifact=cached?.stamp===snapshot.stamp?cached.artifact:compactArtifact(JSON.parse(snapshot.text));
        artifacts.set(path,{stamp:snapshot.stamp,artifact});
        if(artifacts.size>100)artifacts.delete(artifacts.keys().next().value);
      } catch {artifact=null;}
    }
    const request=artifact?.pipeline?.clientRawRequest;
    const sid=header(request?.headers,'x-popot-session-id'),runId=header(request?.headers,'x-popot-run-id');
    let owner=Date.now()-fetchedAt<15000?ax.get(sid):undefined;
    if(owner?.runId&&runId&&owner.runId!==runId)owner=ax.get(sid+':'+runId)??ax.get(header(request?.headers,'x-popot-root-session-id')+':'+runId);
    return conversationStatus({...args,ax:owner,axUnavailable:!!sid&&Date.now()-fetchedAt>=15000},artifact);
  };
  const routes=readdirSync(root+'/server/chunks').filter(f=>f.endsWith('.js')).map(f=>root+'/server/chunks/'+f)
    .filter(p=>/isStalled:\(0,\w+\.ot\)\(\{artifactRelPath:/.test(readFileSync(p,'utf8')));
  const pageDir=root+'/static/chunks/app/(dashboard)/dashboard/conversations';
  const pages=readdirSync(pageDir).filter(f=>f.endsWith('.js')).map(f=>pageDir+'/'+f)
    .filter(p=>{const s=readFileSync(p,'utf8');return s.includes("Latest turn didn't end in stop")||s.includes('popot-continuation-badge');});
  if(!routes.length||pages.length!==1)throw new Error('Unsupported pinned OmniRoute conversation bundles');
  const transforms=new Map();
  for(const p of routes){patchConversationStatusRoute(readFileSync(p,'utf8'));transforms.set(pathToFileURL(realpathSync(p)).href,patchConversationStatusRoute);}
  // Browser bundles use the container's disposable writable image layer.
  // Cache-bust the page URL in Next's manifests after transforming its contents.
  for(const p of pages) {
    const source=readFileSync(p,'utf8'),patched=patchConversationStatusPage(source);
    if(source!==patched)writeFileSync(p,patched);
    const name=p.split('/').at(-1);
    for(const manifest of [root+'/app-build-manifest.json',root+'/server/app/(dashboard)/dashboard/conversations/page_client-reference-manifest.js']) {
      let text;
      try {text=readFileSync(manifest,'utf8');} catch {continue;}
      const changed=text.replaceAll(name+'"',name+'?popot-status=1"').replaceAll(name+"'",name+"?popot-status=1'");
      if(text!==changed)writeFileSync(manifest,changed);
    }
  }
  return registerHooks({
    load(url,context,next) {
      const result=next(url,context),patch=transforms.get(url);
      if(!patch)return result;
      return {...result,source:patch(result.source==null?readFileSync(new URL(url),'utf8'):Buffer.from(result.source).toString('utf8'))};
    }
  });
}

if(process.env.POPOT_OMNIROUTE_CONVERSATION_FALLBACK==='1') {
  const version=JSON.parse(readFileSync('/app/package.json','utf8')).version;
  if(version!=='3.8.51')throw new Error('Unsupported OmniRoute continuation version '+version);
  installConversationStatus();
}
