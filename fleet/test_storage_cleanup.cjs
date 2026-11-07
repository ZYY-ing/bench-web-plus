const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements={};const el=id=>elements[id]||=( {value:'7',disabled:false,hidden:false,checked:false,textContent:'',innerHTML:'',dataset:{},showModal(){this.open=true}} );
const items=[{id:'a',runner_id:'r1',host:'14',container:'runner',kind:'job',name:'real-old-a',path:'/home/runner/results/jobs/real-old-a',bytes:1024,time:1},{id:'b',runner_id:'r2',host:'22',container:'runner',kind:'cache',name:'cache-b',path:'/home/runner/datasets/perf/cache-b',bytes:2048,time:1}];
let selectedBody,calls=[],events={},currentTask={id:'scan1',state:'completed',kind:'scan',days:7,items,errors:[]};
const context=vm.createContext({$:el,esc:s=>String(s).replace(/</g,'&lt;'),Set,Date,Number,authenticated:true,authRevision:0,clearTimeout(){},setTimeout(){},window:{addEventListener(n,cb){events[n]=cb}},api:async(path,body)=>{calls.push(path);if(path==='/api/storage/scan')return {id:'scan1'};if(path==='/api/storage/delete'){selectedBody=body;currentTask={id:'delete1',state:'completed',kind:'delete',items:[{runner_id:'r1',path:items[0].path,status:'deleted'}],errors:[],deleted_bytes:1024};return{id:'delete1'}}return currentTask}});
vm.runInContext(fs.readFileSync(__dirname+'/static/storage-cleanup.js','utf8'),context);
(async()=>{
 await el('cleanupScan').onclick();assert.equal(el('cleanupAll').checked,true);assert.equal(el('cleanupDelete').disabled,false);assert.match(el('cleanupRows').innerHTML,/real-old-a/);
 el('cleanupRows').onchange({target:{dataset:{cleanupId:'b'},checked:false}});assert.match(el('cleanupSelection').textContent,/已选 1 项/);
 await el('cleanupDelete').onclick();assert.deepEqual(Array.from(selectedBody.selected),['a']);assert.equal(selectedBody.scan_id,'scan1');assert.doesNotMatch(el('cleanupRows').innerHTML,/real-old-a/);assert.match(el('cleanupRows').innerHTML,/cache-b/);assert.equal(el('cleanupDelete').disabled,true);
 el('cleanupDays').value='0';const count=calls.length;await el('cleanupScan').onclick();assert.equal(calls.length,count);assert.match(el('cleanupError').textContent,/1–3650/);
 el('cleanupDays').value='14';el('cleanupDays').oninput();assert.equal(el('cleanupDelete').disabled,true);assert.match(el('cleanupStatus').textContent,/重新扫描/);
 events['fleet-logout']();assert.match(el('cleanupRows').innerHTML,/没有可清理/);
 console.log('Cleanup UI: default all, deselection, exact request, removal, day validation and logout passed');
})().catch(e=>{console.error(e);process.exitCode=1});
