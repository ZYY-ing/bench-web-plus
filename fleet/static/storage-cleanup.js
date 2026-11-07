"use strict";
let cleanupTask=null,cleanupScanId=null,cleanupItems=[],cleanupSelected=new Set(),cleanupTimer=null,cleanupRunning=false;
function cleanupSize(bytes){return bytes>=1073741824?(bytes/1073741824).toFixed(2)+' GiB':(bytes/1048576).toFixed(2)+' MiB'}
function cleanupSelection(){
 const count=cleanupSelected.size,total=cleanupItems.filter(r=>cleanupSelected.has(r.id)).reduce((sum,r)=>sum+r.bytes,0);
 $('cleanupSelection').textContent=`已选 ${count} 项 · ${cleanupSize(total)}`;
 $('cleanupDelete').disabled=cleanupRunning||!count;
 $('cleanupAll').checked=!!cleanupItems.length&&count===cleanupItems.length;
 $('cleanupAll').indeterminate=count>0&&count<cleanupItems.length;
 $('cleanupAll').disabled=cleanupRunning||!cleanupItems.length;
}
function cleanupDraw(){
 $('cleanupRows').innerHTML=cleanupItems.map(r=>`<tr><td><input aria-label="选择 ${esc(r.name)}" type="checkbox" data-cleanup-id="${esc(r.id)}" ${cleanupSelected.has(r.id)?'checked':''} ${cleanupRunning?'disabled':''}></td><td>${esc(r.host)}<small>${esc(r.container)}</small></td><td>${r.kind==='job'?'任务结果及日志':'性能数据集缓存'} · ${esc(r.name)}<small>${esc(r.path)}</small></td><td>${cleanupSize(r.bytes)}</td><td>${new Date(r.time*1000).toLocaleString('zh-CN')}</td></tr>`).join('')||'<tr><td colspan="5">没有可清理的过期项目。</td></tr>';
 cleanupSelection();
}
function cleanupBusy(value){cleanupRunning=value;$('cleanupScan').disabled=value;$('cleanupDays').disabled=value;cleanupDraw()}
async function cleanupPoll(){
 clearTimeout(cleanupTimer);if(!cleanupTask||!authenticated)return;
 try{
  const revision=authRevision;
  const task=await api('/api/storage/tasks/'+cleanupTask);
  if(!authenticated||revision!==authRevision)return;
  $('cleanupErrors').hidden=!task.errors.length;
  $('cleanupErrorText').textContent=task.errors.map(e=>[e.host||e.runner_id||'',e.path||'',e.reason||e.error||''].filter(Boolean).join(' · ')).join('\n');
  if(task.state==='running'){
   $('cleanupStatus').textContent=task.kind==='scan'?'正在扫描所有 Runner；忙碌或不可连接的 Runner 会跳过…':'正在清理；关闭窗口后仍会继续，请勿重复提交。';
   cleanupTimer=setTimeout(cleanupPoll,1500);return;
  }
  cleanupBusy(false);
  if(task.kind==='scan'){
   cleanupScanId=task.id;cleanupItems=task.items;cleanupSelected=new Set(cleanupItems.map(r=>r.id));cleanupDraw();
   $('cleanupStatus').textContent=`扫描完成：${task.days} 天前的项目共 ${task.items.length} 项。`;
  }else{
   const removed=new Set(task.items.filter(r=>r.status==='deleted').map(r=>r.runner_id+'\n'+r.path));
   cleanupItems=cleanupItems.filter(r=>!removed.has(r.runner_id+'\n'+r.path));cleanupSelected.clear();cleanupDraw();
   const skipped=task.items.filter(r=>r.status!=='deleted');
   if(skipped.length){$('cleanupErrors').hidden=false;$('cleanupErrorText').textContent+='\n'+skipped.map(r=>r.path+' · '+r.reason).join('\n')}
   $('cleanupStatus').textContent=`清理完成：已清理 ${removed.size} 项，约 ${cleanupSize(task.deleted_bytes)}；其余项目请查看跳过原因或重新扫描。`;
  }
 }catch(e){$('cleanupError').textContent=e.message;if(authenticated)cleanupTimer=setTimeout(cleanupPoll,4000)}
}
async function cleanupScan(){
 const days=Number($('cleanupDays').value);
 if(!Number.isInteger(days)||days<1||days>3650){$('cleanupError').textContent='保留天数请输入1–3650之间的整数';return}
 $('cleanupError').textContent='';cleanupItems=[];cleanupSelected.clear();cleanupScanId=null;cleanupBusy(true);
 try{const task=await api('/api/storage/scan',{days});cleanupTask=task.id;await cleanupPoll()}catch(e){cleanupBusy(false);$('cleanupError').textContent=e.message}
}
$('cleanupOpen').onclick=()=>{$('cleanupDialog').showModal();if(cleanupRunning)cleanupPoll();else if(!cleanupScanId)cleanupScan()};
$('cleanupScan').onclick=cleanupScan;
$('cleanupDays').oninput=()=>{if(cleanupRunning)return;cleanupScanId=null;cleanupItems=[];cleanupSelected.clear();cleanupDraw();$('cleanupStatus').textContent='天数已修改，请重新扫描。'};
$('cleanupAll').onchange=()=>{cleanupSelected=$('cleanupAll').checked?new Set(cleanupItems.map(r=>r.id)):new Set();cleanupDraw()};
$('cleanupRows').onchange=e=>{const id=e.target.dataset.cleanupId;if(!id)return;if(e.target.checked)cleanupSelected.add(id);else cleanupSelected.delete(id);cleanupSelection()};
$('cleanupDelete').onclick=async()=>{
 if(cleanupRunning||!cleanupSelected.size)return;
 $('cleanupError').textContent='';cleanupBusy(true);
 try{const task=await api('/api/storage/delete',{scan_id:cleanupScanId,selected:[...cleanupSelected]});cleanupTask=task.id;await cleanupPoll()}catch(e){cleanupBusy(false);$('cleanupError').textContent=e.message}
};
window.addEventListener('fleet-logout',()=>{clearTimeout(cleanupTimer);cleanupTask=null;cleanupScanId=null;cleanupItems=[];cleanupSelected.clear();cleanupBusy(false);$('cleanupStatus').textContent='';$('cleanupErrorText').textContent=''});
