/* Explicit refresh and selection gate for the existing evaluation UI. */
(()=>{
 const panel=document.getElementById('fleetPicker');if(!panel)return;
 const style=document.createElement('style');style.textContent='#fleetPicker button:disabled{opacity:.45;cursor:not-allowed}#fleetPicker select:disabled{background:#f5f6f8;color:#9299a5}';document.head.append(style);
 const current=location.pathname.match(/^\/r\/([a-f0-9]{16})\//)?.[1];
 const select=panel.querySelector('select'),message=panel.querySelector('[data-message]'),refresh=panel.querySelector('[data-refresh]'),use=panel.querySelector('[data-select]'),editor=document.getElementById('editor');
 let context, fresh=new Set(),freshUntil=0,busy=false,refreshing=false,expiry;
 let ready;window.fleetReady=new Promise(resolve=>ready=resolve);window.fleetSelectionReady=false;
 function gate(enabled){window.fleetSelectionReady=enabled;if(editor){editor.inert=!enabled;editor.setAttribute('aria-disabled',String(!enabled))}window.dispatchEvent(new Event('fleet-selection-change'))}
 gate(false);
 async function request(path,body){const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':context.csrf},body:body===undefined?undefined:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result}
 function healthy(r){const p=context.profiles.find(p=>p.id===r.spec.profile_id);return p?.connected&&!r.error&&r.status.activity==='idle'}
 function controls(){select.disabled=busy||refreshing;refresh.disabled=busy||refreshing;use.disabled=busy||refreshing||Date.now()>freshUntil||!fresh.has(select.value)||!context?.runners.some(r=>r.id===select.value&&healthy(r))}
 function availability(r){const p=context.profiles.find(p=>p.id===r.spec.profile_id);return !p?.connected?'管理连接未建立，请联系管理员':r.error?'Runner 不可用，请联系管理员':r.status.activity==='idle'?'健康 · 空闲':r.status.activity==='running'?'测试中':'准备中 / 状态待核对'}
 const unavailable=document.createElement('ul');unavailable.setAttribute('aria-label','不可用 Runner 列表');unavailable.className='hint';panel.append(unavailable);
 function draw(){
  select.replaceChildren();const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent=!context.runners.length?'暂无已登记 Runner，请联系管理员':fresh.size?'请选择健康空闲的 Runner':'可查看全部 Runner；请先刷新状态再选择';select.append(placeholder);
  for(const r of context.runners){const p=context.profiles.find(p=>p.id===r.spec.profile_id);const option=document.createElement('option');const target=r.status.active_job?.target;const task=target?' · 测试 '+(target.host||'?')+':'+(target.port||'?')+' · '+(target.model||'未提供模型名'):'';option.value=r.id;option.textContent=(p?.host||'')+':'+r.spec.port+' · 容器 '+r.spec.container+' · '+availability(r)+(r.status.activity!=='idle'?task:'');option.disabled=!fresh.has(r.id)||!healthy(r);select.append(option)}
  unavailable.replaceChildren();unavailable.hidden=!context.runners.length||context.runners.some(healthy);if(!unavailable.hidden){for(const r of context.runners){const p=context.profiles.find(p=>p.id===r.spec.profile_id),item=document.createElement('li');item.textContent=(p?.host||'')+':'+r.spec.port+' · 容器 '+r.spec.container+' · '+availability(r);unavailable.append(item)}}
  controls();
 }
 if(!current){
  records=[];document.getElementById('count').textContent='0';document.querySelector('#history .muted').textContent='配置、实际命令、日志与结果保存在服务器';
  renderHistory=async function(){
   const list=document.getElementById('historyList');list.textContent='正在读取测试记录…';await window.fleetReady;
   if(!context){list.textContent='读取失败，请刷新页面后重试。';return}
   const replies=await Promise.allSettled(context.runners.map(async r=>({runner:r,rows:await request('/r/'+r.id+'/api/jobs')})));
   list.replaceChildren();records=[];let unavailable=0;
   for(const reply of replies){if(reply.status!=='fulfilled'){unavailable++;continue}const {runner,rows}=reply.value;const name=context.profiles.find(p=>p.id===runner.spec.profile_id)?.name||'Runner';for(const r of rows){records.push(r);const item=document.createElement('details');item.className='historyitem';const summary=document.createElement('summary');summary.textContent=name+' · '+(r.config?.mode==='accuracy'?'精度':'性能')+' · '+(r.config?.dataset?.name||r.config?.model||'测试')+' · '+r.created_at+' · '+({running:'执行中',completed:'已完成',failed:'失败',stopped:'已停止',interrupted:'已中断'}[r.status]||r.status);item.append(summary);const tools=document.createElement('div');tools.className='historytools';for(const [label,fn] of [['↓ 完整记录',()=>download(r.id+'-record.json',r)],['↓ 配置 JSON',()=>download(r.id+'-config.json',r.config)],['↓ 执行 log',()=>downloadText(r.id+'-execution.log',r.log||'')]]){const b=document.createElement('button');b.className='secondary';b.textContent=label;b.onclick=fn;tools.append(b)}item.append(tools);if(r.metrics?.accuracy){const text=document.createElement('p');text.textContent='Accuracy '+r.metrics.accuracy.score_percent+'%';item.append(text)}preBlock(item,'实际命令',r.command||'记录未包含命令','command.txt');preBlock(item,'实际结果',JSON.stringify({status:r.status,exit_code:r.exit_code,error:r.error,metrics:r.metrics,result_files:r.result_files},null,2),'result.json');for(const f of r.artifacts?.files||[])preBlock(item,f.name,f.content,f.name);list.append(item)}}
   document.getElementById('count').textContent=records.length;if(!records.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='暂无测试记录。';list.append(empty)}if(unavailable){const note=document.createElement('p');note.className='hint';note.textContent=unavailable+' 个 Runner 的记录暂不可读，请联系管理员检查连接。';list.append(note)}
  };
 }
 const baseSwitch=switchMode;switchMode=function(next){baseSwitch(next);panel.hidden=next==='history';if(next==='accuracy'||next==='performance')window.history.replaceState(null,'','#'+next)};
 window.addEventListener('fleet-job-state',event=>{busy=event.detail.busy;controls()});
 select.onchange=()=>{gate(false);if(Date.now()>freshUntil){fresh.clear();message.textContent='状态已过期，请刷新后再选择。'}else message.textContent=select.value?'请点击“使用所选 Runner”确认，之后配置推理服务。':'请选择一个健康空闲的 Runner。';controls()};
 refresh.onclick=async()=>{
  if(!context)return;refreshing=true;fresh.clear();gate(false);controls();message.textContent='正在刷新 Runner 状态…';let failed=0;
  try{for(const r of context.runners){try{await request('/api/user/refresh',{runner_id:r.id});fresh.add(r.id)}catch(e){failed++}}context=await request('/api/user/bootstrap');freshUntil=Date.now()+60000;draw();message.textContent=context.runners.some(r=>fresh.has(r.id)&&healthy(r))?'状态已刷新，请选择健康空闲的 Runner。'+(failed?' 部分 Runner 连接不可用，请联系管理员。':''):'暂无健康空闲 Runner，请联系管理员检查连接。';clearTimeout(expiry);expiry=setTimeout(()=>{fresh.clear();controls();message.textContent=window.fleetSelectionReady?'当前 Runner 已选择；切换前请重新刷新状态。':'状态已过期，请刷新后再选择。'},60000)}catch(e){message.textContent=e.message}finally{refreshing=false;controls()}
 };
 use.onclick=async()=>{
  try{if(Date.now()>freshUntil||!fresh.has(select.value))throw Error('请先刷新状态，再选择 Runner');const id=select.value;if(!id)throw Error('请选择 Runner');use.disabled=true;message.textContent='正在确认 Runner 仍健康空闲…';await request('/api/user/select',{runner_id:id});location.assign('/r/'+id+'/#'+(mode==='performance'?'performance':'accuracy'))}catch(e){message.textContent=e.message;gate(false);controls()}
 };
 (async()=>{try{context=await request('/api/user/bootstrap');draw();if(current&&context.selected_runner_id===current){const r=context.runners.find(r=>r.id===current);if(r&&healthy(r)){fresh.add(current);freshUntil=Date.now()+60000;draw();select.value=current;gate(true);message.textContent='已选择 Runner，继续配置推理服务。';controls()}}}catch(e){message.textContent=e.message}finally{ready()}})();
 async function syncRegistry(){if(!context||busy||refreshing)return;try{const selected=select.value;context=await request('/api/user/bootstrap');fresh=new Set([...fresh].filter(id=>context.runners.some(r=>r.id===id)));draw();if(context.runners.some(r=>r.id===selected))select.value=selected;if(current&&!context.runners.some(r=>r.id===current&&healthy(r))){gate(false);message.textContent='当前 Runner 已被移除或不可用，请重新选择。'}controls()}catch(e){message.textContent=e.message}}
 window.addEventListener('focus',syncRegistry);
 window.addEventListener('pageshow',syncRegistry);
 window.addEventListener('load',()=>{if(location.hash==='#performance')switchMode('performance')});
})();
