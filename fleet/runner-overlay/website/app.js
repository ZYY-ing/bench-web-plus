const $=id=>document.getElementById(id);
let mode='accuracy',dataset='GPQA Diamond',timer=null,report=null,records=[];
try{records=JSON.parse(localStorage.getItem('ascend-demo-history')||'[]')}catch{}
$('count').textContent=records.length;
function toast(s){$('toast').textContent=s;$('toast').style.display='block';setTimeout(()=>$('toast').style.display='none',4500)}
function modal(title,body){$('dialogTitle').textContent=title;$('dialogBody').innerHTML=body;$('dialog').showModal()}
$('close').onclick=()=>$('dialog').close();
function sync(){$('sumType').textContent=mode==='accuracy'?'精度评测':'性能评测';$('sumDataset').textContent=mode==='accuracy'?dataset:`${$('inputTokens').value} tokens × ${$('requests').value}`;$('sumEngine').textContent=$('engine').value==='aisbench'?'AISBench':'轻量 HTTP Runner';$('sumHost').textContent=`${$('host').value}:${$('port').value}`;$('sumParams').textContent=`${$('batch').value} / ${$('maxout').value} tokens`}
function switchMode(next){if(timer){toast('请先停止演示任务');return}document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('active',n.dataset.nav===next));$('history').hidden=next!=='history';$('results').hidden=true;$('editor').hidden=next==='history';if(next==='history'){$('crumb').textContent='测试记录';$('title').textContent='每一轮测试，都有迹可循。';$('subtitle').textContent='查看每轮配置文件、执行命令、日志与对应结果。';renderHistory();return}mode=next;const perf=next==='performance';$('crumb').textContent=perf?'性能测试':'精度测试';$('title').innerHTML=perf?'从首个 Token，到整体吞吐<span>。</span>':'让每一次精度，清晰可见<span>。</span>';$('subtitle').textContent=perf?'定义输入负载与缓存条件，测量服务的推理表现。':'选择数据、配置参数，获取清晰的精度结果。';$('accuracyData').hidden=perf;$('performanceData').hidden=!perf;$('dataTitle').textContent=perf?'生成性能数据集':'选择精度数据集';$('dataMeta').textContent=perf?'SYNTHETIC WORKLOAD':'BENCHMARK DATASETS';$('engine').innerHTML=perf?'<option value="light">轻量 HTTP Runner · 推荐</option><option value="aisbench">AISBench · 对照测试</option>':'<option value="aisbench">AISBench · 标准评分</option>';$('engineTitle').textContent=perf?'流式请求，更轻的采集端':'成熟的精度评分流程';$('engineDesc').textContent=perf?'仅加载 tokenizer，流式采集延迟与吞吐。稳态压测可切换到 AISBench。':'复用 AISBench 的数据适配、答案提取与评分器。';$('maxout').value=perf?1024:4096;$('temperature').value=perf?0:0.6;$('eos').checked=perf;$('eosHint').textContent=perf?'定长输出默认开启，仍需核对真实输出 token 数':'精度测试默认关闭，保留模型自然终止行为';sync()}
document.querySelectorAll('.nav').forEach(n=>n.onclick=()=>switchMode(n.dataset.nav));
document.querySelectorAll('input,select').forEach(n=>n.addEventListener('input',sync));
$('prefix').onchange=()=>{$('prefixOptions').hidden=!$('prefix').checked};
$('connect').onclick=()=>modal('检查服务连接','<p>当前为演示原型，未连接服务器 Runner。接入后，Runner 将检查服务健康和模型名称。</p><p>127.0.0.1 指 Runner 所在机器。</p>');
function download(name,data){downloadText(name,JSON.stringify(data,null,2),'application/json')}
function downloadText(name,text,type='text/plain;charset=utf-8'){const u=URL.createObjectURL(new Blob([text],{type}));const a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}
$('architecture').onclick=()=>modal('服务器 Runner 接入','<p>浏览器 → 控制 API → 服务器 Runner → 已启动的 vLLM API。</p><p>Runner 负责数据准备、任务调度、参数版本校验与结果归档。当前演示只生成执行计划，不向服务器发送请求。</p>');
$('compare').onclick=()=>modal('执行引擎','<p>精度优先使用 AISBench 的评分适配。性能可使用轻量 HTTP Runner 按并发发送流式请求；稳态压测使用 AISBench。</p><p>当前两种引擎均未接入服务器，执行文件和命令是待验证的计划。</p>');
