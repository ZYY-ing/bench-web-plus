/* Compact configuration UI; extras resolve to a typed, explicit snapshot. */
const environmentFields=document.createElement('div');
environmentFields.className='executiontarget';
environmentFields.innerHTML='<div class="sectionlabel">测试执行环境</div><label>执行方式<select id="executionTarget"><option value="direct">直接执行 · Runner 当前环境</option><option value="docker">容器内执行 · Docker</option></select></label><div id="containerFields" hidden><label>测试容器名称<input id="containerName" placeholder="如 bench-client" autocomplete="off"></label><p class="hint">填写 AISBench 所在的测试容器，不必是推理容器。Runner 若已在容器内运行，选择直接执行。</p></div><details class="compactadvanced"><summary>AISBench 命令路径（可选）</summary><label>执行文件<input id="aisbenchPath" value="ais_bench" placeholder="ais_bench 或 /path/to/venv/bin/ais_bench"></label><p class="hint">使用虚拟环境时填写 ais_bench 的绝对路径，不依赖手动激活环境。</p></details><p class="hint" id="environmentHint">数据、tokenizer 和结果目录均按 Runner 当前环境中的路径解释。</p>';
$('endpoint').closest('.compactapi').before(environmentFields);
$('executionTarget').onchange=()=>{const docker=$('executionTarget').value==='docker';$('containerFields').hidden=!docker;$('environmentHint').textContent=docker?'所有路径均为容器内路径；该容器需能访问 vLLM API。Runner 后续负责文件传入和结果归档。':'数据、tokenizer 和结果目录均按 Runner 当前环境中的路径解释。'};
const compactSwitch=switchMode;
switchMode=function(next){if(timer)return compactSwitch(next);compactSwitch(next);if(next==='history')return;const perf=next==='performance';$('stream').checked=perf;$('runMode').innerHTML=(perf?['perf']:['all']).map(m=>`<option>${m}</option>`).join('');$('pressureFields').hidden=!perf;$('aisSettings').hidden=$('engine').value!=='aisbench';$('warmupHint').textContent=perf?'独立预热使用不同尾部，不计入正式延迟和吞吐。':'预热不计入正式评分。';try{validateExtras()}catch{}};
$('engine').addEventListener('change',()=>{$('aisSettings').hidden=$('engine').value!=='aisbench';if($('engine').value!=='aisbench'){$('pressure').checked=false;$('pressureDuration').hidden=true}try{validateExtras()}catch{}});
$('pressure').onchange=()=>{$('pressureDuration').hidden=!$('pressure').checked};
function resetRequestTimeout(){$('timeoutMode').value='native';$('timeoutCustom').hidden=true}
$('timeoutMode').addEventListener('change',()=>{$('timeoutCustom').hidden=$('timeoutMode').value!=='custom'});
let warmupOutputCustom=false;
$('warmupOutput').addEventListener('input',()=>{warmupOutputCustom=true});
$('maxout').addEventListener('input',()=>{if(!warmupOutputCustom)$('warmupOutput').value=$('maxout').value});
$('warmupEnabled').onchange=()=>{$('warmupFields').hidden=!$('warmupEnabled').checked;if($('warmupEnabled').checked&&!warmupOutputCustom)$('warmupOutput').value=$('maxout').value};
function validateExtras(show=true){try{const values=ParameterEngine.parse($('extraParams').value);$('extraError').hidden=true;$('extraParams').removeAttribute('aria-invalid');$('extraStatus').textContent=values.length?`已识别 ${values.length} 项 · 额外参数写入执行快照，已有表单项请直接在表单填写`:'未填写额外参数，将使用表单值和默认值';return values}catch(e){if(show){$('extraError').textContent=e.message;$('extraError').hidden=false;$('extraParams').setAttribute('aria-invalid','true');$('extraStatus').textContent='请修改后再执行'}throw e}}
$('extraParams').oninput=()=>{try{validateExtras()}catch{}};
function readConfig(forRun=false){const numeric=['port','maxout','batch','temperature','retry','limit'];if($('timeoutMode').value==='custom')numeric.push('timeout');if(mode==='performance')numeric.push('inputTokens','requests','seed');if($('prefix').checked&&mode==='performance')numeric.push('ratio');if($('warmupEnabled').checked)numeric.push('warmups','warmupOutput');if(mode==='performance'&&$('pressure').checked)numeric.push('pressureTime');for(const id of numeric)if(!$(id).value.trim()||!$(id).checkValidity())throw Error(`请检查 ${$(id).closest('label')?.textContent.trim()||id} 的数值范围`);
  const perf=mode==='performance';let c={schema_version:3,mode,engine:$('engine').value,dataset:perf?null:structuredClone(currentDataset()),host:$('host').value.trim(),port:+$('port').value,model:$('model').value.trim(),tokenizer_path:$('path').value.trim(),max_out_len:+$('maxout').value,batch_size:+$('batch').value,generation_kwargs:{temperature:+$('temperature').value,ignore_eos:$('eos').checked},stream:$('stream').checked,api:{endpoint:$('endpoint').value,request_rate:0,use_timestamp:false,traffic_cfg:{},url:'',abbr:'vllm-api-eval',trust_remote_code:false},timeout:$('timeoutMode').value==='custom'?+$('timeout').value:null,retry:+$('retry').value,sample_limit:+$('limit').value||null,warmup:{enabled:$('warmupEnabled').checked,count:$('warmupEnabled').checked?+$('warmups').value:0,max_out_len:+$('warmupOutput').value,separate_from_measurement:true,source:perf?'independent-synthetic':'separate-dataset-requests'},execution:{mode:perf?'perf':'all',steady_state:perf&&$('steadyState').checked,pressure:perf&&$('pressure').checked,pressure_time:+$('pressureTime').value,work_dir:'/data/benchmarks/outputs',reuse:'',debug:true,max_num_workers:1,dump_eval_details:true,dump_extract_rate:false,merge_ds:false,response_anomaly:false,spec_decode:false},workload:perf?{input_tokens:+$('inputTokens').value,count:+$('requests').value,seed:+$('seed').value,prefix_ratio:$('prefix').checked?+$('ratio').value/100:0,chat_template_included:true}:null};
  c.execution.target={kind:$('executionTarget').value,container_name:$('executionTarget').value==='docker'?$('containerName').value.trim():null,aisbench_path:$('aisbenchPath').value.trim()||'ais_bench'};
  if(c.execution.target.kind==='docker'&&!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(c.execution.target.container_name))throw Error('请填写有效的测试容器名：字母或数字开头，仅含字母、数字、点、横线或下划线');
  if((c.execution.target.aisbench_path!=='ais_bench'&&!c.execution.target.aisbench_path.startsWith('/'))||/[\r\n\0]/.test(c.execution.target.aisbench_path))throw Error('AISBench 命令路径应为 ais_bench 或 Linux 绝对路径，不能填写整条 shell 命令');
  const entries=validateExtras();c=ParameterEngine.apply(c,entries);if(!warmupOutputCustom&&!entries.some(e=>e.target==='warmup.max_out_len'))c.warmup.max_out_len=c.max_out_len;c.extra_parameters_raw=$('extraParams').value;c.extra_parameters_resolved=entries.map(({key,target,value})=>({key,target,value}));c.temperature=c.generation_kwargs.temperature;c.ignore_eos=c.generation_kwargs.ignore_eos;c.warmup.enabled=c.warmup.count>0;
  const total=c.sample_limit||(perf?c.workload.count:c.dataset?.size);if(!Number.isSafeInteger(c.warmup.count)||c.warmup.count<0||c.warmup.count>total)throw Error('预热数量必须为 0–正式样本总数');if(!Number.isSafeInteger(c.workload?.seed??0))throw Error('随机种子需要安全整数');if(!c.host)throw Error('请填写服务 IP / Host');if(c.engine==='light'&&entries.some(e=>e.aisOnly))throw Error('额外参数包含 AISBench 专属项，请切换 AISBench 引擎');if(c.engine==='light'&&(c.api.use_timestamp||Object.keys(c.api.traffic_cfg).length||c.execution.reuse))throw Error('当前轻量 Runner 不支持时间戳流量或 AISBench 任务复用');if(!perf&&(c.execution.pressure||c.execution.spec_decode))throw Error('稳态压测与推测解码指标仅适用于性能测试');if(c.execution.response_anomaly&&c.api.endpoint!=='chat')throw Error('响应异常检测需要 chat 接口');if(c.execution.debug)c.execution.max_num_workers=1;
  if(c.execution.mode!==(perf?'perf':'all'))throw Error('精度仅支持 all，性能仅支持 perf');
  if(forRun&&(!c.model||!c.tokenizer_path))throw Error('请填写模型名称和 tokenizer / 权重路径');return c;
}
config=()=>readConfig(false);
$('export').onclick=()=>{try{download('ascend-test-config.json',readConfig())}catch(e){toast(e.message)}};
$('generate').onclick=()=>{try{const c=readConfig();$('generationStatus').textContent=`负载规格已生成（演示）：${c.workload.count} 条 × ${c.workload.input_tokens} tokens，共享前缀 ${c.workload.prefix_ratio*100}%`;toast('规格就绪；真实 JSONL 需 Runner 生成')}catch(e){toast(e.message)}};
$('reset').onclick=()=>{warmupOutputCustom=false;switchMode(mode);$('batch').value=4;$('timeout').value=600;resetRequestTimeout();$('retry').value=2;$('limit').value=0;$('warmupEnabled').checked=false;$('warmupFields').hidden=true;$('pressure').checked=false;$('pressureDuration').hidden=true;$('extraParams').value='';validateExtras();sync();toast('已恢复默认参数')};
const extraFormatHint=document.createElement('p');extraFormatHint.className='hint';extraFormatHint.textContent='选填，普通测试可以留空。已收录参数可直接填写，分类及最终配置位置见「格式与支持参数」。已有表单项不在这里重复配置。';$('extraParams').after(extraFormatHint);
function parameterHelpHTML(){
  const groups={models:[],generation:[],cli:[]};
  for(const [name,spec] of Object.entries(ParameterEngine.schema)){const target=spec.target||`${spec.group||'generation_kwargs'}.${name}`;groups[target.startsWith('generation_kwargs.')?'generation':target.startsWith('execution.')?'cli':'models'].push(name)}
  const list=names=>'<p>'+names.map(name=>'<code>'+escapeHtml(name)+'</code>').join('、')+'</p>';
  return '<p><b>网页已收录、可直接填写的完整列表</b></p>'+
    '<p><b>① 模型配置参数：最终放在 models 顶层</b></p>'+list(groups.models)+
    '<p><b>② 生成参数：最终放在 models 的 generation_kwargs 字典里面</b></p>'+list(groups.generation)+
    '<p><b>③ AISBench 执行参数：最终转为 AISBench 命令行参数</b></p>'+list(groups.cli)+
    '<p>若有其他未收录的生成参数，填写 <code>generation_kwargs.参数名=JSON值</code>，具体是否支持由模型服务校验。</p>'+
    '<p>温度、并发、最大输出、预热、超时等已有表单项，请直接在表单填写，不在额外参数中重复配置。</p>';
}
$('paramHelp').onclick=()=>modal('额外参数说明',parameterHelpHTML());
validateExtras();
