const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.textContent='';this.open=false;this.scrollTop=0;this.scrollLeft=0;this.parent=null}
 append(...nodes){for(const node of nodes)this.insertBefore(node,null)}
 insertBefore(node,before){if(node.parent){const a=node.parent.children;a.splice(a.indexOf(node),1)}const index=before?this.children.indexOf(before):this.children.length;this.children.splice(index,0,node);node.parent=this;return node}
 replaceChildren(){for(const n of this.children)n.parent=null;this.children=[]}
 remove(){if(this.parent){const a=this.parent.children;a.splice(a.indexOf(this),1);this.parent=null}}
 get firstElementChild(){return this.children[0]}
 querySelector(selector){return this.children.find(n=>selector==='pre'?n.tag==='pre':n.className==='secondary artifactdownload')}
}
const source=fs.readFileSync(__dirname+'/runner-overlay/scripts/runner-client.js','utf8');
const fn=source.slice(source.indexOf('function realArtifacts('),source.indexOf('\nfunction _w('));
const execution=fs.readFileSync(__dirname+'/runner-overlay/website/execution.js','utf8');
const preBlock=execution.slice(execution.indexOf('function preBlock('),execution.indexOf('\nfunction fillArtifacts('));
const downloads=[];const context=vm.createContext({document:{createElement:tag=>new Element(tag)},runnerInfo:{root:'/runner'},downloadText:(...args)=>downloads.push(args)});
vm.runInContext(preBlock+'\n'+fn,context);
const parent=new Element('div');const row={id:'job-a',status:'running',artifacts:{files:[{name:'config.py',content:'models = []'}],aisbench_commands:['ais_bench config.py']}};
context.realArtifacts(parent,row);
const config=parent.children[0],command=parent.children[1],result=parent.children.at(-1);
config.open=true;command.open=true;result.open=true;config.querySelector('pre').scrollLeft=120;result.querySelector('pre').scrollTop=80;
// Reproduce repeated status polls, including new artifacts and task completion.
for(let i=0;i<5;i++)context.realArtifacts(parent,structuredClone(row));
assert.equal(parent.children[0],config);assert.equal(parent.children[1],command);
assert(config.open&&command.open&&result.open);assert.equal(config.querySelector('pre').scrollLeft,120);
row.artifacts.files.push({name:'run.sh',content:'exec ais_bench config.py'});row.command='ais_bench config.py --debug';
context.realArtifacts(parent,row);assert.equal(parent.children[0],config);assert(parent.children.includes(command));assert(command.open);
row.status='completed';row.metrics={accuracy:{score_percent:75}};context.realArtifacts(parent,row);
assert.equal(parent.children.at(-1),result);assert(result.open);assert.equal(result.querySelector('pre').scrollTop,80);
assert(result.querySelector('pre').textContent.includes('completed'));
command.querySelector('.artifactdownload').onclick();assert.deepEqual(downloads.at(-1),['command.txt','ais_bench config.py --debug']);
context.realArtifacts(parent,{...row,id:'job-b'});assert.notEqual(parent.children[0],config);assert.equal(parent.children[0].open,false);
console.log('Artifact polling: expanded DOM nodes and scroll survive refresh/completion; downloads stay current and new jobs reset state.');
