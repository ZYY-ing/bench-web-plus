"use strict";
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state={profiles:[],runners:[],deployments:[],inventory:{}},csrf='',activeTask=null,progressTimer=null,probed=null;
let authenticated=false,authRevision=0;
function setAdmin(enabled){authenticated=enabled;if(!enabled){authRevision++;csrf='';clearTimeout(progressTimer);activeTask=null;for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();state={profiles:[],runners:[],deployments:[],inventory:{}};$('runners').replaceChildren();$('history').replaceChildren();$('credential').value='';$('machinePassword').value='';$('navCount').textContent='0'}$('login').hidden=enabled;$('workspace').hidden=!enabled;$('logout').hidden=!enabled;$('deployOpen').hidden=!enabled;$('deployOpen').disabled=!enabled}
const labels={queued:'等待',checking:'环境检查',uploading:'校验 / 同步安装包',installing:'检查 / 安装',health:'健康检查',ready:'部署成功',unknown:'待核对',failed:'失败'};
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});const data=await r.json();if(!r.ok){if(r.status===401)setAdmin(false);throw Error(data.error||'请求失败')}return data}
function toast(s){$('toast').textContent=s;$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,4500)}
const profileFor=id=>state.profiles.find(p=>p.id===id);
function statusFor(r){const p=profileFor(r.spec.profile_id);if(p?.mode==='session'&&!p.connected)return ['Runner：待恢复 SSH 后刷新','warn'];if(r.error)return ['Runner：不可用','bad'];if(r.status.activity==='idle')return ['Runner：健康 · 空闲',''];if(r.status.activity==='running')return [r.status.active_job?.mode==='accuracy'?'Runner：精度测试中':'Runner：性能测试中','warn'];return [r.status.activity==='preparing'?'Runner：准备数据':'Runner：状态待核对','warn']}
function render(){
 if(!authenticated)return;
 $('deployOpen').disabled=false;$('emptyDeploy').disabled=false;$('empty').hidden=state.runners.length>0;$('configNotice').hidden=state.profiles.length>0;
 $('total').textContent=state.runners.length;$('navCount').textContent=state.runners.length;$('runnerCount').textContent=state.runners.length;
 $('idle').textContent=state.runners.filter(r=>statusFor(r)[0]==='Runner：健康 · 空闲').length;$('busy').textContent=state.runners.filter(r=>r.status.activity==='running').length;$('deployCount').textContent=state.deployments.length;
 $('runners').innerHTML=state.runners.map(r=>{const p=profileFor(r.spec.profile_id),[label,cls]=statusFor(r),s=r.status.active_job?.target||r.status.service||{},stale=Date.now()/1000-r.observed_at>300;return `<article class="runner-card"><div class="card-main"><div class="card-title"><div class="machine-icon">▤</div><div><h3>${esc(p?.name||r.spec.profile_id)}</h3><div class="subline">${esc(p?.host||'')} : ${r.spec.port}</div></div><span class="status ${cls}">${label}${stale?' · 缓存过期':''}</span></div><div class="connection-state"><span class="status ${p?.connected?'':'bad'}">${p?.connected?'SSH：已连接（管理通道）':'SSH：需重新连接'}</span></div><dl class="card-fields"><dt>测试容器</dt><dd>${esc(r.spec.container)}</dd><dt>安装目录</dt><dd>${esc(r.spec.base)}</dd><dt>Runner 版本</dt><dd>${esc(r.status.version||'未知')}</dd></dl><div class="service-block"><span>${r.status.active_job?'正在测试':'推理服务'}</span><b>${esc(s.model||'使用时配置')}${s.model&&s.port?' · '+esc(s.port):''}</b></div>${r.error?`<p class="card-error">${p?.connected?'SSH 管理通道正常，但 Runner 服务检查失败：':'Runner 检查失败：'}${esc(r.error)}</p>`:''}</div><div class="card-footer"><small>查询于 ${new Date(r.observed_at*1000).toLocaleTimeString('zh-CN')}</small><div><button data-machine="${r.spec.profile_id}">${p?.connected?'管理部署':'连接并部署'}</button><button data-refresh="${r.id}">↻ 刷新</button><button data-remove="${r.id}">删除</button><button class="use" data-use="${r.id}">使用 Runner ↗</button></div></div></article>`}).join('');
 $('history').innerHTML=state.deployments.length?state.deployments.map(t=>`<button class="history-row" data-task="${t.id}"><span class="status ${t.state==='ready'?'':t.state==='unknown'||t.state==='failed'?'bad':'warn'}">${labels[t.state]||t.state}</span><div><strong>${esc(profileFor(t.spec.profile_id)?.name||t.spec.profile_id)} / ${esc(t.spec.container)}</strong><small>${esc((t.detail||'').trim().split('\n').at(-1).replace(/^(ValueError|RuntimeError):\s*/, '')).slice(0,130)}</small></div><time>${new Date(t.created_at*1000).toLocaleString('zh-CN')}</time><span class="muted">↗</span></button>`).join(''):'<div class="history-empty">尚无部署记录。连接机器后即可一键部署。</div>';
}
async function bootstrap(){const revision=authRevision,data=await api('/api/bootstrap');if(revision!==authRevision)return;state=data;csrf=state.csrf;setAdmin(true);$('demoNotice').hidden=!state.demo;render()}
async function login(token){try{csrf=(await api('/api/login',{token})).csrf;await bootstrap();$('credential').value=''}catch(e){$('loginError').textContent=e.message}}
$('loginForm').onsubmit=e=>{e.preventDefault();login($('credential').value.trim())};$('demoLogin').onclick=()=>login('demo');
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
function chooseMachine(forceLogin=false){
 const p=profileFor($('machineChoice').value);$('machineError').textContent='';$('machinePassword').value='';probed=null;$('hostIdentity').hidden=true;$('trustHost').checked=false;
 $('machineHost').value=p?.host||'';$('machinePort').value=p?.ssh_port||22;$('machineForm').elements.user.value=p?.user||'root';$('machineForm').elements.name.value=p?.name||'';
 const reuse=!!p?.connected&&!forceLogin;$('machineFields').hidden=reuse;$('machineFields').disabled=reuse;$('connectedMachine').hidden=!reuse;$('reconnectSelected').hidden=!reuse;
 $('connectedMachine').textContent=p?`${p.name} · ${p.host} · SSH 已连接，下一步选择容器。`:'';
 $('machineSubmit').textContent=reuse?'下一步：选择容器 →':'连接并选择容器 →';
}
function openMachine(profile){
 options('machineChoice',[{value:'new',label:'添加新机器…'},...state.profiles.map(p=>({value:p.id,label:p.name+' · '+p.host+(p.connected?' · 已连接':' · 需登录')}))]);
 $('machineChoice').value=profile?.id||'new';chooseMachine();$('connectStep').hidden=false;$('installStep').hidden=true;if(!$('deployDialog').open)$('deployDialog').showModal();
}
$('machineChoice').onchange=()=>chooseMachine();$('reconnectSelected').onclick=()=>chooseMachine(true);
$('wizardBack').onclick=()=>openMachine(profileFor($('profile').value));
for(const id of ['machineHost','machinePort'])$(id).oninput=()=>{probed=null;$('hostIdentity').hidden=true;$('trustHost').checked=false};
$('probeHost').onclick=async()=>{$('probeHost').disabled=true;$('machineError').textContent='';try{probed=await api('/api/machines/probe',{host:$('machineHost').value.trim(),ssh_port:+$('machinePort').value});$('hostFingerprint').textContent=probed.fingerprint;$('hostIdentity').hidden=false;$('trustHost').checked=probed.trusted;$('hostTrustNote').textContent=probed.trusted?'已匹配工具内登记的服务器公钥。':'首次连接，请与管理员核对指纹后勾选确认。'}catch(e){$('machineError').textContent=e.message;probed=null}finally{$('probeHost').disabled=false}};
$('machineForm').onsubmit=async e=>{e.preventDefault();$('machineSubmit').disabled=true;$('machineError').textContent='';try{
 let id=$('machineChoice').value;
 if($('machineFields').disabled){state.inventory[id]=await api('/api/machines/inspect',{profile_id:id})}
 else{if(!probed||!$('trustHost').checked)throw Error('请先检查并确认服务器指纹');const body=Object.fromEntries(new FormData($('machineForm')));body.ssh_port=+body.ssh_port;body.fingerprint=probed.fingerprint;body.confirm_host_key=$('trustHost').checked;const result=await api('/api/machines/connect',body);id=result.profile_id;await bootstrap()}
 openDeploy(id);
 }catch(e){$('machineError').textContent=e.message;if($('machineFields').disabled)chooseMachine(true);$('machineError').textContent=e.message}finally{$('machinePassword').value='';$('machineSubmit').disabled=false}};
$('deployDialog').addEventListener('close',()=>$('machinePassword').value='');
function options(id,values){$(id).replaceChildren(...values.map(v=>{const o=document.createElement('option');o.value=typeof v==='object'?v.value:v;o.textContent=typeof v==='object'?v.label:v;o.disabled=!!v.disabled;return o}))}
function updateMode(){const isNew=$('containerMode').value==='new';$('newFields').hidden=!isNew;$('existingFields').hidden=isNew;$('newContainer').required=isNew;$('image').required=isNew;$('hostPath').required=isNew;$('container').required=!isNew}
function updateProfile(){
 const p=profileFor($('profile').value);if(!p)return;const inventory=state.inventory?.[p.id];
 const containers=[...(inventory?.containers||p.containers.map(name=>({name})))];
 function group(c){if(c.known_runner||c.runner_installed){if(c.state!=='running')return 2;return c.runner_running?0:1}return 3}
 containers.sort((a,b)=>group(a)-group(b)||(group(a)===3?Number(b.state==='running')-Number(a.state==='running'):0)||a.name.localeCompare(b.name));
 options('container',[{value:'',label:'请选择测试容器'},...containers.map(c=>({value:c.name,label:c.name+' · '+(c.state||'待检查')+' · '+(group(c)===0?'已安装 · Runner运行中':group(c)===1?'已安装 · Runner停止/待检查':group(c)===2?'已安装 · 容器停止/退出':'未检测到Runner')}))]);
 $('container').value=containers.find(c=>group(c)<3)?.name||'';
 options('image',inventory?.images||[]);$('containerMode').value='existing';$('base').value=state.runners.find(r=>r.spec.profile_id===p.id)?.spec.base||(p.mode==='session'?'/home/ascend_bench/runner':p.base_roots[0]);$('hostPath').value=$('base').value;$('packageDir').value=state.runners.find(r=>r.spec.profile_id===p.id)?.spec.package_dir||'/mnt/share/ascend_bench/packages';for(const id of ['installProxy','installPipIndex','installExtraHosts'])$(id).value='';$('port').value=18787;$('newContainer').value='ascend-bench-runner';$('deployError').textContent='';updateMode();
}
function openDeploy(profileId){const available=state.profiles.filter(p=>p.connected);if(!available.length){openMachine();return}options('profile',available.map(p=>({value:p.id,label:p.name+' · '+p.host})));if(profileId)$('profile').value=profileId;updateProfile();$('connectStep').hidden=true;$('installStep').hidden=false;if(!$('deployDialog').open)$('deployDialog').showModal()}
$('profile').onchange=updateProfile;$('containerMode').onchange=updateMode;$('deployOpen').onclick=()=>openMachine();$('emptyDeploy').onclick=()=>openMachine();
$('deployForm').onsubmit=async e=>{e.preventDefault();$('deploySubmit').disabled=true;$('deployError').textContent='';try{const body={profile_id:$('profile').value,container_mode:$('containerMode').value,container:$('containerMode').value==='new'?$('newContainer').value.trim():$('container').value,base:$('base').value.trim(),host_path:$('hostPath').value.trim(),image:$('image').value,port:+$('port').value,package_dir:$('packageDir').value.trim(),network:{proxy:$('installProxy').value.trim(),pip_index:$('installPipIndex').value.trim(),extra_hosts:$('installExtraHosts').value.trim()}};const task=await api('/api/deployments',body);$('deployDialog').close();state.deployments.unshift(task);render();openProgress(task.id)}catch(e){$('deployError').textContent=e.message}finally{$('deploySubmit').disabled=false}};
function drawProgress(t){$('reconcile').hidden=t.state!=='unknown';$('progressTitle').textContent=labels[t.state]||'部署进度';$('progressDetail').textContent=t.detail+' · 已耗时 '+Math.floor((Date.now()/1000-t.created_at)/60)+' 分钟'+(t.state==='installing'?'（首次依赖下载期间日志可能暂不变化）':'');$('logs').textContent=t.logs||'等待执行…';$('logs').scrollTop=$('logs').scrollHeight;const steps=['queued','checking','uploading','installing','health','ready'],index=steps.indexOf(t.state);$('steps').innerHTML=steps.map((s,i)=>`<div class="step ${i<index||t.state==='ready'?'done':i===index?'current':''}"><b>${i<index||t.state==='ready'?'✓':i+1}</b>${labels[s]}</div>`).join('')}
async function openProgress(id){activeTask=id;$('progressDialog').showModal();await pollProgress()}
async function pollProgress(){clearTimeout(progressTimer);if(!$('progressDialog').open||!activeTask)return;try{const t=await api('/api/deployments/'+activeTask);drawProgress(t);const i=state.deployments.findIndex(x=>x.id===t.id);if(i>=0)state.deployments[i]=t;render();if(['ready','failed','unknown'].includes(t.state))await bootstrap();else progressTimer=setTimeout(pollProgress,1500)}catch(e){toast(e.message)}}
$('progressDialog').addEventListener('close',()=>{clearTimeout(progressTimer);activeTask=null});
$('reconcile').onclick=async()=>{$('reconcile').disabled=true;try{drawProgress(await api('/api/deployments/'+activeTask+'/reconcile',{}));await bootstrap();toast('已核对接入')}catch(e){toast(e.message)}finally{$('reconcile').disabled=false}};
async function refreshRunner(id){const r=await api('/api/refresh',{runner_id:id});state.runners[state.runners.findIndex(x=>x.id===id)]=r;render();return r}
$('runners').onclick=async e=>{const b=e.target.closest('button');if(!b)return;b.disabled=true;try{if(b.dataset.remove){removeTarget=b.dataset.remove;$('removeError').textContent='';$('removeDialog').showModal()}if(b.dataset.machine)openMachine(profileFor(b.dataset.machine));if(b.dataset.refresh){await refreshRunner(b.dataset.refresh);toast('状态已更新')}if(b.dataset.use){if(state.demo)throw Error('演示模式不连接真实 Runner');const r=await refreshRunner(b.dataset.use);if(r.error)throw Error(r.error);if(r.status.activity!=='idle')throw Error('Runner 非空闲');location.assign('/r/'+r.id+'/')}}catch(e){toast(e.message)}finally{b.disabled=false}};
$('history').onclick=e=>{const b=e.target.closest('[data-task]');if(b)openProgress(b.dataset.task)};
$('refreshAll').onclick=async()=>{$('refreshAll').disabled=true;try{for(const r of state.runners)await refreshRunner(r.id);toast('已完成一次性状态刷新')}catch(e){toast(e.message)}finally{$('refreshAll').disabled=false}};
(async()=>{try{const mode=await api('/api/mode');$('demoLogin').hidden=!mode.demo;if(mode.demo){$('loginForm').hidden=true;$('login').querySelector('.hint').hidden=true}await bootstrap()}catch(e){if(e.message!=='请登录本机控制台')$('loginError').textContent=e.message}})();

$('inspectMachine').onclick=async()=>{$('inspectMachine').disabled=true;try{const id=$('profile').value;state.inventory[id]=await api('/api/machines/inspect',{profile_id:id});updateProfile();toast('容器与本地镜像列表已刷新')}catch(e){$('deployError').textContent=e.message}finally{$('inspectMachine').disabled=false}};

$('logout').onclick=async()=>{try{await api('/api/logout',{});setAdmin(false)}catch(e){toast(e.message)}};

let removeTarget=null;
$('removeConfirm').onclick=async()=>{if(!removeTarget)return;$('removeConfirm').disabled=true;try{const result=await api('/api/runners/'+removeTarget+'/remove',{});$('removeDialog').close();await bootstrap();toast(result.detail)}catch(e){$('removeError').textContent=e.message}finally{$('removeConfirm').disabled=false}};
