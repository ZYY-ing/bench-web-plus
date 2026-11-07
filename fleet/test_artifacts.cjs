const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(__dirname + '/runner-overlay/website/artifacts.js', 'utf8'), context);
const config = {
  mode: 'performance', engine: 'aisbench', host: '192.0.2.1', port: 8000,
  model: 'test', tokenizer_path: '/models/test', stream: true,
  max_out_len: 1024, batch_size: 16, retry: 1, timeout: 600,
  generation_kwargs: {temperature: 0, ignore_eos: true},
  api: {endpoint: 'chat', abbr: 'test', request_rate: 0, use_timestamp: false, trust_remote_code: false, traffic_cfg: {}},
  execution: {mode: 'perf', work_dir: '/results', max_num_workers: 1, target: {kind: 'direct', aisbench_path: 'ais_bench'}},
  warmup: {enabled: true, count: 2, max_out_len: 1024}, workload: {count: 32},
};
context.config = config;
const artifacts = vm.runInContext('ExecutionArtifacts.make(config, "legacy-cli-test")', context);
assert.equal(artifacts.aisbench_commands.length, 2);
for (const command of artifacts.aisbench_commands) assert.equal(command.includes('--batch-size'), false);
for (const file of artifacts.files) {assert.match(file.content, /"batch_size": 16/);assert.match(file.content, /"retry": 1/);}
console.log('Execution artifacts: concurrency stays in model config; legacy CLI gets no batch-size flag');
