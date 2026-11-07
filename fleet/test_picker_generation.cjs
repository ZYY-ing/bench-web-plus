const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{constructor(){this.children=[];this.value=''}append(x){this.children.push(x)}replaceChildren(){this.children=[];this.value=''}setAttribute(){}}
(async()=>{
 const select=new Element(),message=new Element(),refresh=new Element(),use=new Element(),panel=new Element(),editor=new Element(),events={};
 panel.querySelector=s=>({'select':select,'[data-message]':message,'[data-refresh]':refresh,'[data-select]':use}[s]);
 const id='a'.repeat(16),runner={id,spec:{profile_id:'host',container:'own',port:18787},status:{activity:'idle'},access_configured:true,error:''};
 const context={selected_runner_id:'b'.repeat(16),selected_runner_ids:[id,'b'.repeat(16)],profiles:[{id:'host',host:'192.0.2.1',connected:false}],runners:[runner],csrf:'test'};
 let waiting=null,calls=0;
 const sandbox={window:{dispatchEvent(){},addEventListener:(name,fn)=>events[name]=fn},document:{getElementById:id=>id==='fleetPicker'?panel:editor,createElement:()=>new Element(),head:new Element()},location:{pathname:'/r/'+id+'/'},AbortController,Event:class{},switchMode(){},setTimeout(){return 1},clearTimeout(){},fetch:async()=>{calls++;return {ok:true,json:async()=>waiting?await waiting:JSON.parse(JSON.stringify(context))}}};
 vm.runInNewContext(fs.readFileSync(__dirname+'/static/fleet-picker.js','utf8'),sandbox);await sandbox.window.fleetReady;
 assert.equal(editor.inert,false);assert.equal(select.value,id);
 let resolve;waiting=new Promise(done=>resolve=done);
 const focus=events.focus();
 events['fleet-job-state']({detail:{busy:true}});runner.status.activity='preparing';
 resolve(JSON.parse(JSON.stringify(context)));await focus;
 assert.equal(editor.inert,false,'late registry response must not revoke a generation already started');
 const before=calls;await refresh.onclick();assert.equal(calls,before,'busy refresh cannot reset local operation');
 events['fleet-job-state']({detail:{busy:false}});waiting=null;await events.focus();
 assert.equal(editor.inert,false,'preparing is a busy state, not a broken connection');assert.equal(select.value,id);
 context.runners=[];await events.focus();assert.equal(editor.inert,true,'actual removal must still revoke the selection');
 console.log('Picker generation: pending-focus race, busy refresh guard, preparing-state preservation and removal passed');
})().catch(error=>{console.error(error);process.exitCode=1});
