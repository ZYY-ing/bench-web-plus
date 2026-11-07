const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/runner-overlay/scripts/runner-client.js','utf8');
const body=source.slice(source.indexOf('let jobPollEpoch='),source.indexOf("$('run').onclick=async()=>",source.indexOf('let jobPollEpoch=')));
function setup(api){
 const intervals=new Map(),deadlines=new Map(),events={},shown=[],notes=[];let next=0;const validation={textContent:''};
 const context=vm.createContext({timer:null,jobBusy:true,AbortController,runnerAPI:api,realResult:r=>shown.push(r.status),applyGate:()=>{},loadRealHistory:async()=>{},toast:s=>notes.push(s),$:()=>validation,setInterval:fn=>{const id=++next;intervals.set(id,fn);return id},clearInterval:id=>intervals.delete(id),setTimeout:fn=>{const id=++next;deadlines.set(id,fn);return id},clearTimeout:id=>deadlines.delete(id),window:{addEventListener:(name,fn)=>events[name]=fn},document:{hidden:false,addEventListener:(name,fn)=>events[name]=fn}});
 vm.runInContext(body,context);return {context,intervals,deadlines,events,shown,validation,notes,poll:()=>vm.runInContext('pollCurrentJob()',context)};
}
(async()=>{
 let calls=0;const state=setup(async()=>{if(++calls===1)throw Error('temporary gateway failure');return {status:'completed'}});
 vm.runInContext("startJobPolling('job')",state.context);await state.poll();
 assert.equal(state.context.jobBusy,true);assert.equal(state.intervals.size,1);assert(state.validation.textContent.includes('自动重试'));
 state.context.loadRealHistory=async()=>{throw Error('history unavailable')};await state.poll();await Promise.resolve();
 assert.deepEqual(state.shown,['completed']);assert.equal(state.context.jobBusy,false);assert.equal(state.context.timer,null);assert.equal(state.intervals.size,0);assert.equal(state.deadlines.size,0);assert.equal(state.notes.length,1);
 let release,requests=0;const overlap=setup(async()=>{requests++;return await new Promise(resolve=>release=resolve)});
 vm.runInContext("startJobPolling('old')",overlap.context);const first=overlap.poll();await overlap.poll();assert.equal(requests,1);release({status:'running'});await first;
 const stalled=setup(async(_path,_body,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(Error('timeout'),{name:'AbortError'})))));
 vm.runInContext("startJobPolling('job')",stalled.context);const pending=stalled.poll();[...stalled.deadlines.values()][0]();await pending;assert.equal(stalled.intervals.size,1);assert.equal(stalled.context.jobBusy,true);assert(stalled.validation.textContent.includes('超时'));
 let oldReply;const stale=setup(async path=>path.endsWith('old')?new Promise(resolve=>oldReply=resolve):{status:'completed'});
 vm.runInContext("startJobPolling('old')",stale.context);const old=stale.poll();vm.runInContext("startJobPolling('new')",stale.context);await stale.poll();oldReply({status:'running'});await old;assert.deepEqual(stale.shown,['completed']);
 console.log('Job polling: transient errors/timeouts retry, no overlapping requests, terminal result survives history errors, stale responses cannot overwrite completion.');
})().catch(e=>{console.error(e);process.exitCode=1});
