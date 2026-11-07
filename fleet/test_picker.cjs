// Exercise refresh failures without changing a real server or starting a job.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(){this.children=[];this.value='';this.disabled=false;this.hidden=false;}
  append(e){this.children.push(e);}
  replaceChildren(){this.children=[];this.value='';}
  setAttribute(){}
}
async function check(unavailable){
  const select=new Element(),message=new Element(),refresh=new Element(),use=new Element(),panel=new Element(),editor=new Element();
  panel.querySelector=s=>({'select':select,'[data-message]':message,'[data-refresh]':refresh,'[data-select]':use}[s]);
  const runner={id:'a'.repeat(16),spec:{profile_id:'host',container:'test-container',port:18787},status:{activity:'idle'},error:unavailable?'unavailable':''};
  const context={profiles:[{id:'host',host:'192.0.2.1',connected:true}],runners:[runner],csrf:'test-csrf'};
  const events={};const window={dispatchEvent(){},addEventListener(name,fn){events[name]=fn;}};
  let reloads=0;const timeouts=[];
  const sandbox={window,document:{getElementById:id=>id==='fleetPicker'?panel:editor,createElement:()=>new Element(),head:new Element()},location:{pathname:'/r/'+runner.id+'/',reload(){reloads++}},AbortController,Event:class {},switchMode(){},mode:'accuracy',setTimeout:(fn,ms)=>{if(ms===20000)timeouts.push(fn);return 1},clearTimeout(){},fetch:async path=>({ok:!unavailable||!path.endsWith('/refresh'),json:async()=>path.endsWith('/bootstrap')?context:{error:'offline'}})};
  vm.runInNewContext(readFileSync(__dirname+'/static/fleet-picker.js','utf8'),sandbox);
  await window.fleetReady;
  assert.equal(select.disabled,false);
  assert.equal(select.children.length,2);
  assert.equal(select.children[1].disabled,true); // refresh is still required
  await refresh.onclick();
  assert.equal(select.disabled,false);
  assert.equal(select.children.length,2);
  assert.equal(select.children[1].disabled,unavailable);
  assert.equal(panel.children[0].hidden,!unavailable);
  select.value=runner.id;select.onchange();
  assert.equal(use.disabled,unavailable);
  assert.equal(editor.inert,true); // selection alone does not authorize a job
  if(!unavailable){
    await use.onclick();
    assert.equal(reloads,1); // confirming the same Runner must reload, not navigate to the same hash
    assert.match(message.textContent,/已确认空闲/);
    const fetch=sandbox.fetch;
    sandbox.fetch=(path,options)=>path.endsWith('/select')?new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'})))):fetch(path,options);
    const attempt=use.onclick();
    assert.equal(use.disabled,true);
    timeouts.at(-1)();
    await attempt;
    assert.match(message.textContent,/超时/);
    assert.equal(use.disabled,false);
    assert.equal(editor.inert,true);
    sandbox.fetch=fetch;
  }
  context.runners=[];await events.focus();
  assert.equal(select.children.length,1);
  assert.equal(use.disabled,true);
  assert.equal(editor.inert,true);
}
(async()=>{await check(true);await check(false);console.log('Runner picker: unavailable list, selection, same-page reload, timeout recovery and removal checks passed');})().catch(e=>{console.error(e);process.exitCode=1;});
