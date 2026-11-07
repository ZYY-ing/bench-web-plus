const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/runner-overlay/scripts/runner-client.js','utf8');
const polling=source.slice(source.indexOf('let generationWake=null;'),source.indexOf("$('generate').onclick=async()=>"));
async function checkRetries(){
 const body={generation_id:'same-generation',count:32},updates=[],calls=[];let active=0,peak=0;
 const sandbox={AbortController,window:{addEventListener(){}},document:{addEventListener(){}},clearTimeout(){},setTimeout(fn,ms){if(ms===2000)queueMicrotask(fn);return 1},runnerAPI:async(path,sent)=>{
  calls.push(sent);active++;peak=Math.max(peak,active);
  try{
   assert.equal(path,'/api/datasets/generate');
   if(calls.length===1)throw new TypeError('Failed to fetch');
   if(calls.length===2)throw Object.assign(new Error('temporary gateway error'),{status:502});
   if(calls.length===3)return {state:'preparing',progress:{completed:8,total:32}};
   return {path:'/owned/data.jsonl',reused:false};
  }finally{active--}
 }};
 vm.createContext(sandbox);vm.runInContext(polling,sandbox);
 const result=await sandbox.waitForDatasetGeneration(body,(progress,error)=>updates.push({progress,error}));
 assert.equal(result.path,'/owned/data.jsonl');assert.equal(calls.length,4);assert(calls.every(sent=>sent===body));assert.equal(peak,1);
 assert.equal(updates.filter(x=>x.error).length,2);assert.equal(updates.at(-1).progress.progress.completed,8);
 sandbox.runnerAPI=async()=>{throw Object.assign(new Error('connection expired'),{status:400})};
 await assert.rejects(sandbox.waitForDatasetGeneration(body,()=>{}),/connection expired/);
}
async function checkFocusAndTimeout(){
 const events={},deadlines=[];let calls=0;
 const sandbox={AbortController,window:{addEventListener:(name,fn)=>events[name]=fn},document:{addEventListener(){}},clearTimeout(){},setTimeout(fn,ms){if(ms===20000)deadlines.push(fn);return 1},runnerAPI:async(path,body,options)=>{
  calls++;
  if(calls===1)return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('timeout'),{name:'AbortError'}))));
  return {path:'/owned/completed.jsonl'};
 }};
 vm.createContext(sandbox);vm.runInContext(polling,sandbox);
 const task=sandbox.waitForDatasetGeneration({generation_id:'unchanged'},()=>{});
 deadlines[0]();await new Promise(resolve=>setImmediate(resolve));assert.equal(calls,1);
 events.focus();assert.equal((await task).path,'/owned/completed.jsonl');assert.equal(calls,2);
}
function checkGenerationLocks(){
 const start=source.indexOf('function applyGate(){'),end=source.indexOf('function handleError',start);
 const elements=new Map(),events=[];
 const $=id=>{if(!elements.has(id))elements.set(id,{classList:{toggle(){}},disabled:false});return elements.get(id)};
 const sandbox={$: $,runnerConnected:true,connectionReady:true,jobBusy:false,datasetBusy:true,fleetPrefix:'/r/test',checkPending:false,setRealLabels(){},CustomEvent:class{constructor(name,options){this.detail=options.detail}},window:{fleetSelectionReady:true,dispatchEvent:event=>events.push(event)}};
 vm.createContext(sandbox);vm.runInContext(source.slice(start,end)+';applyGate()',sandbox);
 for(const id of ['host','port','model','path','connect','runnerSettings','run'])assert.equal($(id).disabled,true,id);
 assert.match($('readyText').textContent,/生成/);assert.equal(events.at(-1).detail.busy,true);

}
(async()=>{await checkRetries();await checkFocusAndTimeout();checkGenerationLocks();console.log('Generation UI: transient retry, same generation identity, focus resume, hard-error handling and field locks passed')})().catch(error=>{console.error(error);process.exitCode=1});
