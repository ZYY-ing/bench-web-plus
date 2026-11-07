const {readFileSync}=require('node:fs');
const vm=require('node:vm'),assert=require('node:assert/strict');
class Element{constructor(){this.children=[];this.value='';}append(x){this.children.push(x)}replaceChildren(){this.children=[];this.value=''}setAttribute(){}}
(async()=>{
 const select=new Element(),message=new Element(),refresh=new Element(),use=new Element(),panel=new Element();
 panel.querySelector=s=>({'select':select,'[data-message]':message,'[data-refresh]':refresh,'[data-select]':use}[s]);
 const runners=Array.from({length:7},(_,i)=>({id:String(i).repeat(16),spec:{profile_id:String(i),port:18787,container:'own-runner'},status:{activity:'idle'},error:i<4?'offline':'old failed observation',access_configured:true}));
 const context={runners,profiles:runners.map((r,i)=>({id:String(i),host:i<4?'141.offline':'80.online',connected:false})),csrf:'test'};
 const pending=[];let selected;
 const sandbox={window:{dispatchEvent(){},addEventListener(){}},document:{getElementById:id=>id==='fleetPicker'?panel:new Element(),createElement:()=>new Element(),head:new Element()},location:{pathname:'/r/4444444444444444/',reload(){},assign(){}},AbortController,Event:class{},switchMode(){},setTimeout(){return 1},clearTimeout(){},fetch:async(path,options)=>{
  if(path.endsWith('/bootstrap'))return {ok:true,json:async()=>context};
  const body=JSON.parse(options.body);
  if(path.endsWith('/select')){selected=body.runner_id;return {ok:true,json:async()=>({})}}
  const r=runners.find(r=>r.id===body.runner_id);
  if(Number(r.spec.profile_id)<4)return new Promise(resolve=>pending.push(()=>resolve({ok:false,json:async()=>({error:'offline'})})));
  return {ok:true,json:async()=>({runner:{...r,error:''}})};
 }};
 vm.runInNewContext(readFileSync(__dirname+'/static/fleet-picker.js','utf8'),sandbox);
 await sandbox.window.fleetReady;
 const refreshing=refresh.onclick();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(pending.length,4);
 assert.equal(select.disabled,false);
 assert.equal(select.children.filter(o=>o.value&&!o.disabled).length,3,'healthy results appear while four offline calls remain pending');
 select.value=runners[4].id;select.onchange();assert.equal(use.disabled,false);
 await use.onclick();assert.equal(selected,runners[4].id,'selection does not wait for offline hosts or require SSH');
 pending.forEach(resolve=>resolve());await refreshing;
 assert.equal(select.value,runners[4].id,'later replies preserve selection');
 assert.equal(select.children.filter(o=>o.value&&!o.disabled).length,3);
 console.log('Outage: four pending/offline hosts do not block three online hosts, selection or partial rendering');
})().catch(e=>{console.error(e);process.exitCode=1});
