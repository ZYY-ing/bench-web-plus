/* Explicit parameter registry. No eval, shell interpolation, or arbitrary code. */
var ParameterEngine = (() => {
  const schema = Object.create(null);
  const add=(names,type,target,opts={})=>names.split(' ').forEach(name=>schema[name]={type,target,...opts});
  add('temperature','number','generation_kwargs.temperature',{min:0});
  add('ignore_eos','boolean','generation_kwargs.ignore_eos');
  add('top_p','number',null,{min:0,max:1,exclusiveMin:true});add('min_p','number',null,{min:0,max:1});
  add('chat_template_kwargs','object','generation_kwargs.chat_template_kwargs');
  add('num_return_sequences','integer','generation_kwargs.num_return_sequences',{min:1});
  add('warmup_max_out_len','integer','warmup.max_out_len',{min:1,max:225280});
  add('top_k','integer',null,{min:-1});add('seed','integer','generation_kwargs.seed');
  add('repetition_penalty','number',null,{min:0,exclusiveMin:true});
  add('frequency_penalty presence_penalty','number',null,{min:-2,max:2});
  add('stop bad_words','strings');add('stop_token_ids allowed_token_ids','integers');
  add('min_tokens','integer',null,{min:0});
  add('logprobs skip_special_tokens spaces_between_special_tokens detokenize include_stop_str_in_output','boolean');
  add('top_logprobs','integer',null,{min:0,max:20});
  add('request_rate','number','api.request_rate',{min:0,max:64000});
  add('retry','integer','retry',{min:0,max:1000});
  add('trust_remote_code','boolean','api.trust_remote_code');
  add('use_timestamp','boolean','api.use_timestamp');add('traffic_cfg','object','api.traffic_cfg');
  add('url abbr','string',null,{group:'api'});
  add('host_ip','string','host');add('host_port','integer','port',{min:1,max:65535});
  add('model','string','model');add('path tokenizer_path','string','tokenizer_path');
  add('max_out_len','integer','max_out_len',{min:1});add('batch_size','integer','batch_size',{min:1,max:64000});
  add('stream','boolean','stream');
  add('timeout REQUEST_TIME_OUT','nullableNumber','timeout',{min:0,exclusiveMin:true});
  add('num_prompts','integer','sample_limit',{min:1});add('num_warmups','integer','warmup.count',{min:0});
  add('work_dir','string','execution.work_dir');add('reuse','string','execution.reuse');
  add('max_num_workers','integer','execution.max_num_workers',{min:1});
  add('debug dump_eval_details dump_extract_rate merge_ds response_anomaly spec_decode','boolean',null,{group:'execution',aisOnly:true});
  add('response_anomaly_payload_retention','string','execution.response_anomaly_payload_retention',{enum:['all','anomalies','none'],aisOnly:true});
  add('steady_state','boolean','execution.steady_state',{aisOnly:true});add('pressure','boolean','execution.pressure',{aisOnly:true});add('pressure_time','integer','execution.pressure_time',{min:1,max:86400,aisOnly:true});
  add('enable_ssl verbose','boolean',null,{group:'api'});
  add('meta_template','object','api.meta_template');
  add('api_key','string','api.api_key');
  add('max_workers_per_gpu','integer','execution.max_workers_per_gpu',{min:1,aisOnly:true});
  const formFields={temperature:'温度',ignore_eos:'忽略结束符',warmup_max_out_len:'预热输出上限',retry:'重试次数',host_ip:'服务 IP / Host',host_port:'端口号',model:'模型名称',path:'模型权重路径',tokenizer_path:'模型权重路径',max_out_len:'最大输出长度',max_tokens:'最大输出长度',batch_size:'并发请求数',stream:'流式响应',timeout:'请求超时',REQUEST_TIME_OUT:'请求超时',num_prompts:'样本数',num_warmups:'预热请求数',mode:'运行模式',steady_state:'稳态指标',pressure:'按时长持续压测',pressure_time:'压测时长'};
  for(const key of Object.keys(formFields))delete schema[key];
  function split(text){const out=[];let start=0,quote=null,escaped=false,stack=[];for(let i=0;i<text.length;i++){const c=text[i];if(quote){if(escaped)escaped=false;else if(c==='\\')escaped=true;else if(c===quote)quote=null;continue}if(c==='"'||c==="'"){quote=c;continue}if(c==='['||c==='{')stack.push(c);else if(c===']'||c==='}'){if(stack.pop()!==(c===']'?'[':'{'))throw Error('额外参数的括号不匹配')}else if(c===','&&!stack.length){out.push(text.slice(start,i));start=i+1}}if(quote||stack.length)throw Error('额外参数的引号或括号未闭合');out.push(text.slice(start));return out}
  function value(raw,name,type){const s=raw.trim();if(!s)throw Error(`参数 ${name} 缺少值`);if(type==='string'){if(s.startsWith('"')){try{const v=JSON.parse(s);if(typeof v!=='string')throw Error();return v}catch{throw Error(`参数 ${name} 的字符串格式不正确`)}}if(s.startsWith("'")){if(!s.endsWith("'"))throw Error(`参数 ${name} 引号未闭合`);return s.slice(1,-1).replace(/\\'/g,"'").replace(/\\\\/g,'\\')}return s}
    if(type==='boolean'){if(/^(true|false)$/i.test(s))return /^true$/i.test(s);throw Error(`参数 ${name} 需要 true 或 false`)}
    if(type==='nullableNumber'&&/^(none|null)$/i.test(s))return null;
    if(['integer','number','nullableNumber'].includes(type)){if(!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(s))throw Error(`参数 ${name} 需要数字`);const n=Number(s);if(!Number.isFinite(n)||(type==='integer'&&!Number.isSafeInteger(n)))throw Error(`参数 ${name} 需要${type==='integer'?'安全整数':'有限数字'}`);return n}
    let parsed;try{parsed=JSON.parse(s)}catch{throw Error(`参数 ${name} 需要有效的 JSON ${type==='object'?'对象':'数组'}`)}
    if(type==='object'&&(!parsed||Array.isArray(parsed)||typeof parsed!=='object'))throw Error(`参数 ${name} 需要 JSON 对象`);
    if(type==='strings'&&(!Array.isArray(parsed)||parsed.some(x=>typeof x!=='string')))throw Error(`参数 ${name} 需要字符串数组，例如 ["END","STOP"]`);
    if(type==='integers'&&(!Array.isArray(parsed)||parsed.some(x=>!Number.isSafeInteger(x)||x<0)))throw Error(`参数 ${name} 需要非负整数数组`);
    return parsed;
  }
  function parse(text){if(!text.trim())return[];const seen=new Set();return split(text).map((item,i)=>{const eq=item.indexOf('=');if(eq<1)throw Error(`第 ${i+1} 项应为 参数名=值`);let key=item.slice(0,eq).trim().replace(/^--/,'').replace(/-/g,'_');const generation=key.startsWith('generation_kwargs.');if(generation)key=key.slice(18);if(!/^[A-Za-z][A-Za-z0-9_]*$/.test(key)||['constructor','prototype','__proto__'].includes(key))throw Error('参数名无效');if(formFields[key])throw Error(`参数 ${key} 已有表单配置，请在「${formFields[key]}」中填写`);const spec=generation?{type:'json',target:'generation_kwargs.'+key}:(schema[key]||null);if(!spec)throw Error(`未识别参数「${key}」；服务支持的其他生成参数请使用 generation_kwargs.${key}=JSON值`);const target=spec.target||`${spec.group||'generation_kwargs'}.${key}`;if(seen.has(target))throw Error(`参数「${key}」重复配置`);seen.add(target);let v;if(spec.type==='json'){try{v=JSON.parse(item.slice(eq+1))}catch{throw Error(`参数 ${key} 需要有效的 JSON 值`)}}else v=value(item.slice(eq+1),key,spec.type);if(v!==null&&typeof v==='number'&&((spec.min!==undefined&&(spec.exclusiveMin?v<=spec.min:v<spec.min))||(spec.max!==undefined&&v>spec.max)))throw Error(`参数 ${key} 超出允许范围`);if(spec.enum&&!spec.enum.includes(v))throw Error(`参数 ${key} 只允许 ${spec.enum.join(' / ')}`);return{key,target,value:v,aisOnly:!!spec.aisOnly}})}
  function apply(base,entries){const out=JSON.parse(JSON.stringify(base));for(const {target,value} of entries){const path=target.split('.');let obj=out;for(const p of path.slice(0,-1)){if(!obj[p])obj[p]={};obj=obj[p]}obj[path.at(-1)]=value}return out}
  return{parse,apply,schema};
})();
if(typeof module!=='undefined')module.exports=ParameterEngine;
