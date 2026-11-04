/* Authenticated, same-origin real Runner UI. Public preview keeps its separate demo mode. */
let runnerConnected=false, realRecord=null, runnerInfo=null;
records=[];$('count').textContent='0';
const fleetPrefix=location.pathname.match(/^\/r\/[a-f0-9]{16}/)?.[0]||'';
const fleetSession=fleetPrefix?fetch('/api/user/bootstrap').then(r=>r.json()):Promise.resolve({});
let connectionReady=false, connectionId=null, verifiedSignature='', checkEpoch=0, checkPending=false, jobBusy=false;
const DATASET_API={'GSM8K':'GSM8K','GPQA Diamond':'GPQA','GPQA':'GPQA','AIME2026':'AIME2026'};
const REAL_DATASETS=[
 {name:'GSM8K',task:'gsm8k_gen',size:1319,kind:'数学推理 · 公开',state:'unchecked'},
 {name:'GPQA Diamond',task:'gpqa_gen_0_shot_cot_chat_prompt',size:198,kind:'科学推理 · 需授权文件',state:'unchecked'},
 {name:'AIME2026',task:'aime2026_gen',size:30,kind:'数学竞赛 · 公开',state:'unchecked'}
];
function serviceValues(){return {host:$('host').value.trim(),port:+$('port').value,model:$('model').value.trim(),tokenizer_path:$('path').value.trim()}}
function signature(s){return JSON.stringify([s.host,s.port,s.model,s.tokenizer_path])}
function applyDefaultLimit(){if(mode==='accuracy'){const d=currentDataset();if(d&&d.size)$('limit').value=d.size}else if(mode==='performance'){$('limit').value=$('requests').value}}
$('requests').addEventListener('input',()=>{if(mode==='performance')$('limit').value=$('requests').value});
async function runnerAPI(path,body){
 const session=await fleetSession;
 const r=await fetch(fleetPrefix+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json',...(fleetPrefix?{'X-CSRF-Token':session.csrf}:{})},body:body===undefined?undefined:JSON.stringify(body)});
 const v=await r.json();
 if(!r.ok){const e=new Error(v.error||r.statusText);e.status=r.status;e.connectionVerified=v.connection_verified;throw e}
 return v;
}
function ensureVerified(){if(!runnerConnected||!connectionReady||signature(serviceValues())!==verifiedSignature)throw Error('请先通过服务连接检查，再配置和执行测试')}
function withConnection(body){ensureVerified();return {...body,connection_id:connectionId}}
function lockFieldset(parent,id,nodes){if($(id))return;const fieldset=document.createElement('fieldset');fieldset.id=id;fieldset.className='workflow-fields';nodes.forEach(n=>fieldset.append(n));parent.append(fieldset)}
function installWorkflowGates(){
 const datasetPanel=$('datasetPanel');lockFieldset(datasetPanel,'datasetFields',[$('accuracyData'),$('performanceData')]);
 const parameters=$('maxout').closest('section');parameters.id='parametersPanel';
 lockFieldset(parameters,'parameterFields',[...parameters.children].filter(n=>!n.classList.contains('panelhead')));
 const summary=$('run').closest('section');lockFieldset(summary,'summaryFields',[...summary.children]);
 for(const id of ['host','port','model','path'])$(id).addEventListener('input',()=>{checkEpoch++;connectionReady=false;connectionId=null;verifiedSignature='';$('connectionStatus').textContent='○ 服务信息已变化，请重新检查连接';applyGate()});
}
function setRealLabels(){
 $('engineTitle').textContent='AISBench · 测试';$('engineDesc').textContent='当前环境执行 AISBench，保留官方评分和原始性能指标。';
 const lightOpt=$('engine').querySelector('option[value="light"]');if(lightOpt)lightOpt.remove();
 const limitLabel=$('limit').closest('label');limitLabel.firstChild.textContent=mode==='performance'?'样本数（默认=请求数量）':'样本数（默认=数据集全量）';$('limit').min=mode==='accuracy'?'1':'0';
 $('warmupEnabled').closest('.switchrow').style.display='';
 $('stop').textContent='停止任务';$('downloadResult').textContent='↓ 导出测试记录';
 document.querySelector('#history .muted').textContent='配置、实际命令、日志与结果保存在服务器';
 document.querySelector('#results .badge').style.display='none';
 document.querySelector('#results .sectionlabel span').textContent='REAL LOG';
 document.querySelector('#results .panelhead h2').firstChild.textContent='执行与结果 ';
 $('run').innerHTML=jobBusy?'任务执行中…':'▷　开始测试 <span>→</span>';
 if(!realRecord){$('runTitle').textContent='等待测试';$('runDesc').textContent='连接验证后可提交任务';$('validation').textContent='等待结果…'}
}
function applyGate(){
 const locked=!runnerConnected||!connectionReady||jobBusy||(fleetPrefix&&!window.fleetSelectionReady);
 if(fleetPrefix)window.dispatchEvent(new CustomEvent('fleet-job-state',{detail:{busy:jobBusy}}));
 for(const id of ['datasetFields','parameterFields','summaryFields'])if($(id))$(id).disabled=locked;
 $('run').disabled=locked;$('export').disabled=locked;$('reset').disabled=locked;
 $('datasetPanel').classList.toggle('gated',!connectionReady);$('parametersPanel')?.classList.toggle('gated',!connectionReady);
 $('datasetGate').hidden=connectionReady;
 for(const id of ['host','port','model','path'])$(id).disabled=jobBusy;
 $('connect').disabled=jobBusy||checkPending;$('runnerSettings').disabled=jobBusy||checkPending;
 $('readyText').textContent=jobBusy?'任务正在运行':connectionReady?'服务连接已验证，可以执行测试':'请先通过服务连接检查';
 setRealLabels();
}
function handleError(e){if(e.connectionVerified===false||e.status===401){connectionReady=false;connectionId=null;verifiedSignature='';applyGate()}toast(e.message)}
function realArtifacts(parent,r){
 parent.replaceChildren();
 if(r.artifacts){for(const f of r.artifacts.files)preBlock(parent,f.name,f.content,f.name);preBlock(parent,'实际 AISBench 执行命令',r.command||r.artifacts.aisbench_commands.join('\n'),'command.txt')}
 const archDir=`${runnerInfo.root}/results/jobs/${r.id}`;
 const arch=r.result_files?.length?`${archDir}\n\n原始文件:\n${r.result_files.join('\n')}`:archDir;
 preBlock(parent,'文件归档路径',arch,'archive.txt');
 preBlock(parent,'结果及原始文件索引',JSON.stringify({id:r.id,status:r.status,exit_code:r.exit_code,error:r.error,metrics:r.metrics,raw_tables:r.raw_tables,result_files:r.result_files},null,2),'result.json');
}
function _w(s){let w=0;for(const ch of s){const c=ch.codePointAt(0);w+=(c>=0x1100&&(c<=0x115f||c===0x2329||c===0x232a||(c>=0x2e80&&c<=0xa4cf&&c!==0x303f)||(c>=0xac00&&c<=0xd7a3)||(c>=0xfe10&&c<=0xfe19)||(c>=0xfe30&&c<=0xfe6f)||(c>=0xff00&&c<=0xff60)||(c>=0xffe0&&c<=0xffe6)||(c>=0x20000&&c<=0x3fffd)))?2:1}return w}
function _kv(label,value){return '  '+label+' '.repeat(Math.max(1,12-_w(label)))+String(value)}
function _fmtTime(v){if(!v)return '-';try{return new Date(v).toLocaleString('zh-CN',{hour12:false})}catch(e){return String(v)}}
// 成功：从最后一个进度 100% 起；失败/停止/中断：完整日志
function _tailLog(r){const log=r.log||'';if(r.status!=='completed')return log;const cut=i=>{const s=Math.max(log.lastIndexOf('\n',i),log.lastIndexOf('\r',i));return log.slice(s+1)};const i=log.lastIndexOf('100%');if(i>=0)return cut(i);for(const m of ['Inference tasks completed','Summarizing']){const j=log.lastIndexOf(m);if(j>=0)return cut(j)}return log}
function recordText(r){
 const c=r.config||{},acc=c.mode==='accuracy';
 const L=['='.repeat(64),'  Ascend Bench · 测试记录','='.repeat(64)];
 L.push(_kv('任务 ID',r.id||''), _kv('测试类型',acc?'精度测试（AISBench）':'性能测试（AISBench）'));
 L.push(_kv('状态',({running:'执行中',completed:'已完成',stopped:'已停止',interrupted:'已中断'})[r.status]||r.status||''));
 if(r.created_at)L.push(_kv('开始时间',_fmtTime(r.created_at)));
 if(r.completed_at)L.push(_kv('完成时间',_fmtTime(r.completed_at)));
 L.push(_kv('退出码',r.exit_code==null?'-':r.exit_code), _kv('失败请求',r.failed_requests==null?'-':r.failed_requests));
 if(r.error)L.push(_kv('错误',r.error));
 L.push('','-'.repeat(64),'  测试配置','-'.repeat(64));
 L.push('【推理服务】',_kv('服务地址',`${c.host||''}:${c.port||''}`),_kv('模型名称',c.model||''),_kv('tokenizer',c.tokenizer_path||''),'');
 if(acc){
  L.push('【数据集】',_kv('名称',c.dataset?.name||''),_kv('题目数',(r.count==null?'-':r.count)+(c.dataset?.size?`（数据集共 ${c.dataset.size} 题）`:'')));
 }else{
  const wl=c.workload||{},pr=wl.prefix_ratio||0;
  L.push('【负载规格】',_kv('输入长度',`${wl.input_tokens} tokens`),_kv('请求数量',wl.count),_kv('随机种子',wl.seed),_kv('共享前缀',pr>0?`启用 · ${Math.round(pr*100)}%`:'未启用'));
 }
 const g=c.generation_kwargs||{},wm=c.warmup||{};
 L.push('','【生成参数】',_kv('最大输出',`max_out_len = ${c.max_out_len}`),_kv('并发请求',`batch_size  = ${c.batch_size}`),_kv('温度',`temperature = ${g.temperature}`),_kv('忽略结束符',`ignore_eos  = ${g.ignore_eos}`),_kv('流式响应',`stream = ${c.stream?'是':'否'}${acc?'':'（性能测试必须）'}`),_kv('超时 / 重试',`${c.timeout} 秒 / ${c.retry} 次`),_kv('运行模式',c.execution?.mode||''));
 L.push('','【预热】',_kv('启用',wm.enabled?`是 · ${wm.count} 条 · 输出上限 ${wm.max_out_len}`:'否'));
 L.push('','-'.repeat(64),'  执行日志'+(r.status==='completed'?'（自进度 100% 起）':'（完整）'),'-'.repeat(64));
 L.push((_tailLog(r)||'(无日志)').replace(/\r\n/g,'\n').replace(/\r/g,'\n'));
 L.push('='.repeat(64));
 return L.join('\n')+'\n';
}
function exportRecord(r){if(r)downloadText((r.id||'record')+'-record.log',recordText(r),'text/plain;charset=utf-8')}
function realResult(r){
 realRecord=r;report=r;$('results').hidden=false;$('editor').hidden=true;$('metrics').innerHTML='';$('detailResults').innerHTML='';
 const accuracy=r.config.mode==='accuracy';$('metrics').classList.toggle('accuracyMetric',accuracy);$('logs').textContent=r.log||'';
 $('runTitle').textContent=r.status==='running'?'测试执行中':r.status==='completed'?'测试完成':r.status==='stopped'?'任务已停止':'测试未通过';
 $('runDesc').textContent=r.error||`任务 ${r.id} · 归档目录 ${runnerInfo.root}/results/jobs/${r.id}`;
 $('results').querySelector('.resultgrid').hidden=false;$('stop').hidden=r.status!=='running';$('downloadResult').hidden=r.status==='running';$('progressBar').style.width=r.status==='running'?'45%':'100%';
 if(r.metrics?.accuracy){const a=r.metrics.accuracy;$('metrics').innerHTML=`<div class="metric"><span>${escapeHtml(r.config.dataset?.name||'数据集')} ·  Accuracy</span><b>${escapeHtml(a.score_percent)}%</b><small>正确 ${a.correct===null?'待核对':escapeHtml(a.correct)} / ${escapeHtml(a.total)}</small></div>`}
 if(!accuracy)for(const t of r.raw_tables||[]){const title=document.createElement('div');title.className='sectionlabel';title.textContent=t.file;const div=document.createElement('div');div.innerHTML=table(t.headers,t.rows);$('detailResults').append(title,div)}
 $('validation').textContent=r.status==='completed'?`AISBench 退出码 ${r.exit_code}，原始结果已归档。`:'请结合完整日志检查实际状态。';
 realArtifacts($('executionArtifacts'),r);setRealLabels();
}
async function loadRealHistory(){
 if(!runnerInfo)runnerInfo=await runnerAPI('/api/info');
 const rows=await runnerAPI('/api/jobs');records=rows;$('count').textContent=rows.length;$('historyList').replaceChildren();
 for(const r of rows){const item=document.createElement('details');item.className='historyitem';const summary=document.createElement('summary');summary.textContent=` · ${r.config.mode==='accuracy'?'精度':'性能'} · ${r.config.dataset?.name||'合成负载'} · ${r.created_at} · ${r.status}`;item.append(summary);const button=document.createElement('button');button.className='secondary';button.textContent='↓ 导出测试记录';button.onclick=()=>exportRecord(r);item.append(button);const body=document.createElement('div');realArtifacts(body,r);item.append(body);$('historyList').append(item)}
}
const previousRealSwitch=switchMode;
switchMode=function(next){if(timer)return toast('请等待任务完成或先停止当前任务');previousRealSwitch(next);if(!runnerConnected)return;$('engine').value='aisbench';$('engine').dispatchEvent(new Event('change'));if(next==='history')loadRealHistory().catch(handleError);else{applyDefaultLimit();sync()}applyGate()};
$('run').onclick=async()=>{
 try{ensureVerified();const c=readConfig(true);if(fleetPrefix){c.execution.target={kind:'direct',container_name:null}}if(signature({host:c.host,port:c.port,model:c.model,tokenizer_path:c.tokenizer_path})!==verifiedSignature)throw Error('额外参数覆盖了已验证的服务信息，请直接修改服务面板并重新检查');jobBusy=true;applyGate();const r=await runnerAPI('/api/jobs',withConnection(c));realResult(r);
 timer=setInterval(async()=>{try{const current=await runnerAPI('/api/jobs/'+r.id);realResult(current);if(current.status!=='running'){clearInterval(timer);timer=null;jobBusy=false;applyGate();await loadRealHistory()}}catch(e){clearInterval(timer);timer=null;jobBusy=false;applyGate();handleError(e)}},1500);
 }catch(e){jobBusy=false;applyGate();handleError(e)}
};
$('stop').onclick=async()=>{if(!realRecord)return;try{await runnerAPI('/api/jobs/'+realRecord.id+'/stop',{});toast('已发送停止请求，等待保存日志')}catch(e){handleError(e)}};
$('connect').onclick=async()=>{
 const epoch=++checkEpoch;const service=serviceValues();connectionReady=false;connectionId=null;verifiedSignature='';checkPending=true;applyGate();$('connectionStatus').textContent='正在验证 /health、模型名称与 tokenizer 路径…';
 try{const v=await runnerAPI('/api/check-service',{service});if(epoch!==checkEpoch)return;connectionId=v.connection_id;verifiedSignature=signature(service);connectionReady=true;$('connectionStatus').textContent='✓ 连接已验证并保存：'+v.data.map(m=>m.id).join(', ');await refreshDatasetState()}
 catch(e){if(epoch===checkEpoch){connectionReady=false;$('connectionStatus').textContent='连接失败：'+e.message}}
 finally{checkPending=false;applyGate()}
};
const originalPreparation=renderPreparation;
renderPreparation=function(){if(!runnerConnected)return originalPreparation();$('selectedDatasetName').textContent=currentDataset().name;applyDefaultLimit();if(connectionReady)refreshDatasetState();else{$('datasetStatus').textContent='请先验证服务连接';$('prepareDetail').hidden=true}};
async function refreshDatasetState(){
 if(!runnerConnected||!connectionReady)return;const d=currentDataset(),name=d.name,api=DATASET_API[name];
 if(!api){$('datasetStatus').textContent='尚未适配该数据集';$('prepare').hidden=true;return}
 try{const r=await runnerAPI('/api/datasets/check',{name:api});if(currentDataset().name!==name||!connectionReady)return;$('prepare').hidden=false;$('prepareDetail').hidden=true;
 if(r.state==='ready'){$('datasetStatus').textContent=`已校验 ${r.size??''} 题 · ${r.path}/${r.file}`;$('prepare').textContent='重新检查 ↗';d.state='ready'}
 else if(r.state==='invalid'){$('datasetStatus').textContent='数据校验失败：'+r.error;$('prepare').textContent='重新检查 ↗';d.state='missing'}
 else{$('datasetStatus').textContent=r.downloadable?'服务器未下载，可下载到项目数据目录':`缺少 ${r.file}，请将已授权文件放入 ${r.path}`;$('prepare').textContent=r.downloadable?'下载到服务器 ↗':'重新检查 ↗';d.state='missing'}
 renderDatasets();document.querySelectorAll('.state-ready').forEach(n=>n.textContent='已就绪');
 }catch(e){$('datasetStatus').textContent=e.message;handleError(e)}
}
$('prepare').onclick=async()=>{try{ensureVerified();const api=DATASET_API[currentDataset().name];if(!api)throw Error('该数据集尚未适配');$('prepare').disabled=true;$('datasetStatus').textContent='Runner 正在校验 / 准备数据…';const r=await runnerAPI('/api/datasets/prepare',withConnection({name:api}));await refreshDatasetState();toast('数据就绪：'+r.path)}catch(e){$('datasetStatus').textContent=e.message;handleError(e)}finally{$('prepare').disabled=false;applyGate()}};
$('generate').onclick=async()=>{try{ensureVerified();$('generate').disabled=true;$('generationStatus').textContent='正在精确分词生成并独立校验…';const r=await runnerAPI('/api/datasets/generate',withConnection({input_tokens:+$('inputTokens').value,seed:+$('seed').value,count:(+$('limit').value)||(+$('requests').value),max_out_len:+$('maxout').value,prefix_ratio:$('prefix').checked?+$('ratio').value/100:0}));$('generationStatus').textContent=(r.reused?'已校验并复用：':'已生成并校验：')+r.path}catch(e){$('generationStatus').textContent=e.message;handleError(e)}finally{$('generate').disabled=false;applyGate()}};
$('downloadResult').onclick=()=>{if(realRecord)exportRecord(realRecord)};
$('back').onclick=()=>{if(timer)return toast('请先停止任务');$('results').hidden=true;$('editor').hidden=false;applyGate()};
$('reset').onclick=()=>{switchMode(mode);$('batch').value=1;$('timeout').value=600;$('retry').value=2;$('limit').value=mode==='accuracy'?(currentDataset()?.size||1):+$('requests').value;$('warmupEnabled').checked=false;$('warmupFields').hidden=true;$('pressure').checked=false;$('pressureDuration').hidden=true;$('extraParams').value='';validateExtras();sync();applyGate();toast('已恢复默认参数')};
$('export').onclick=()=>{try{ensureVerified();download('test-config.json',readConfig(false))}catch(e){handleError(e)}};
async function activateRunner(){
 if(fleetPrefix){await window.fleetReady;if(!window.fleetSelectionReady)return}
 runnerInfo=await runnerAPI('/api/info');runnerConnected=true;connectionReady=false;connectionId=null;verifiedSignature='';
 for(const [id,key] of [['model','model'],['path','tokenizer_path'],['host','host'],['port','port']])$(id).value=runnerInfo[key];
 $('aisbenchPath').value=runnerInfo.aisbench_path;$('executionTarget').value='direct';$('executionTarget').dispatchEvent(new Event('change'));$('executionTarget').disabled=true;
 $('executionTarget').closest('.executiontarget').hidden=true;if(fleetPrefix)$('runnerSettings').hidden=true;
 $('maxout').value=2048;$('batch').value=1;$('engine').value='aisbench';$('engine').dispatchEvent(new Event('change'));
 document.querySelector('.demo').textContent=' Runner';document.querySelector('.agentdot').innerHTML='● <b> Runner 已连接</b>';document.querySelector('.summary .hint.center').textContent='测试将向已验证的推理服务发送请求';
 datasets.length=0;datasets.push(...structuredClone(REAL_DATASETS));dataset='GSM8K';renderDatasets();renderPreparation();
 $('addDataset').hidden=true;document.querySelector('#accuracyData > .hint').textContent='GSM8K / GPQA Diamond / AIME2026 使用 AISBench 官方评分适配；GPQA 只使用已有授权文件。';
 $('connectionStatus').textContent='○ 请先检查连接';sync();applyGate();await loadRealHistory();
}
function loginRunner(){modal('登录内网测试 Runner','<p>这是内网测试入口，任务与结果保存在服务器。</p><label>Runner 访问凭据<input id="runnerCredential" type="password" autocomplete="off"></label><button class="primary" id="loginRunner" style="margin-top:18px">连接 Runner</button><p id="loginError" class="parametererror" hidden></p>');$('loginRunner').onclick=async()=>{try{await runnerAPI('/api/login',{token:$('runnerCredential').value});$('dialog').close();await activateRunner()}catch(e){$('loginError').hidden=false;$('loginError').textContent=e.message}}}
$('runnerSettings').onclick=async()=>{
 try{const cfg=(await runnerAPI('/api/config')).config;const context=runnerInfo?.execution_context;modal('Runner 配置',`<p>AISBench 直接在 Runner 当前环境执行。容器名是环境标识，不会把任务转发到另一个容器；需要换执行环境时，应把 Runner 安装到对应环境。</p><div class="formgrid"><label>AISBench 可执行路径<input id="cfgAis" value="${escapeHtml(cfg.ais_bench_path)}"></label><label>Runner 所在环境（自动识别）<input id="cfgContainer" value="${escapeHtml(context?.container||cfg.container||'当前主机')}" readonly></label></div><p class="hint">当前工作目录：<code>${escapeHtml(runnerInfo.root)}</code>。以上路径均按 Runner 所在环境解释；若运行在容器里，宿主机只有配置了目录挂载才能直接看到它。</p><p id="cfgError" class="parametererror" hidden></p><button class="primary" id="cfgSave" style="margin-top:14px">保存配置并重新验证连接</button>`);
 $('cfgSave').onclick=async()=>{try{await runnerAPI('/api/config',{...cfg,ais_bench_path:$('cfgAis').value.trim()});checkEpoch++;$('dialog').close();await activateRunner();toast('配置已保存，请重新检查连接')}catch(e){$('cfgError').hidden=false;$('cfgError').textContent=e.message}};
 }catch(e){handleError(e)}
};
$('architecture').onclick=()=>modal('接入架构',`<p>本机浏览器 → SSH 隧道 → Runner 当前环境 → vLLM OpenAI API。</p><p>工作目录：${escapeHtml(runnerInfo?.root||'登录后可见')}。Runner 与 AISBench 在同一环境执行；容器名不等于远端服务的寻址方式，推理服务由 IP 和端口指定。</p>`);
$('compare').onclick=()=>modal('执行引擎','<p>当前模式使用 AISBench，精度走官方评分器，性能保留原始指标表。轻量 Runner 尚未适配，不会用演示值替代结果。</p>');
if(fleetPrefix)window.addEventListener('fleet-selection-change',()=>{if(!window.fleetSelectionReady){connectionReady=false;connectionId=null;verifiedSignature=''}applyGate()});
installWorkflowGates();applyGate();activateRunner().catch(e=>{if(fleetPrefix){modal('Runner 连接失败','<p id="fleetConnectError"></p>');$('fleetConnectError').textContent=e.message}else loginRunner()});
