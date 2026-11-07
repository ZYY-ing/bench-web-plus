"""Administrator-only manual storage operations over the management channel."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import threading
import time
import uuid


def days_value(value):
    if type(value) is not int or not 1 <= value <= 3650:
        raise ValueError('保留天数请输入1–3650之间的整数')
    return value


def remote_request(session, spec, action, body):
    # Deliberately outside the public evaluation gateway allowlist. Only a
    # centre administrator with the existing management authorization can call.
    inner = r'''
import json,sys,urllib.request,urllib.error
from pathlib import Path
spec,action,body=json.loads(sys.argv[1])
base=Path(spec['base']);token=(base/'runner-token').read_text().strip()
op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
root='http://127.0.0.1:'+str(spec['port'])
headers={'Cookie':'runner_session_'+str(spec['port'])+'='+token,'Content-Type':'application/json'}
with op.open(urllib.request.Request(root+'/api/status',headers=headers),timeout=5) as response:status=json.load(response)
assert status['root']==spec['base'] and status['container']==spec['container'],'Runner identity changed'
request=urllib.request.Request(root+'/api/maintenance/storage/'+action,data=json.dumps(body).encode(),headers=headers)
try:response=op.open(request,timeout=270)
except urllib.error.HTTPError as exc:response=exc
with response:
 data=json.load(response)
 if response.status!=200:raise ValueError(data.get('error','存储操作失败'))
print(json.dumps(data,ensure_ascii=False))
'''
    payload=json.dumps([spec,action,body])
    script='import subprocess\nprint(subprocess.check_output('+repr(['docker','exec','-w','/',spec['container'],'python3','-I','-S','-c',inner,payload])+',stderr=subprocess.STDOUT,timeout=285).decode())'
    return json.loads(session.execute(script,timeout=295))


class StorageCleanup:
    def __init__(self, app):
        self.app=app;self.lock=threading.RLock();self.tasks={}

    def view(self, task_id):
        with self.lock:
            task=self.tasks.get(task_id)
            if not task:raise ValueError('清理扫描已失效，请重新扫描')
            return json.loads(json.dumps({k:v for k,v in task.items() if k!='plans'}))

    def _start(self, task, work):
        with self.lock:
            if any(t['state']=='running' for t in self.tasks.values()):
                raise ValueError('已有管理员扫描或清理正在进行，请稍后重试')
            completed=[k for k,v in self.tasks.items() if v['state']!='running']
            for key in completed[:-8]:self.tasks.pop(key,None)
            self.tasks[task['id']]=task
        def run():
            try:work(task)
            except Exception as exc:
                with self.lock:task['errors'].append({'error':str(exc)})
            finally:
                with self.lock:task['state']='completed'
        threading.Thread(target=run,daemon=True).start()
        return self.view(task['id'])

    def scan(self, days):
        days=days_value(days)
        task={'id':uuid.uuid4().hex,'kind':'scan','state':'running','days':days,'created_at':time.time(),'items':[],'errors':[],'plans':{}}
        runners=self.app.store.list('runner')
        def one(runner):
            profile=self.app.transport.profiles[runner['spec']['profile_id']]
            session=self.app.transport.session(profile['id'])
            return remote_request(session,runner['spec'],'scan',{'days':days})
        def work(task):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures={pool.submit(one,r):r for r in runners}
                for future in as_completed(futures):
                    runner=futures[future];rid=runner['id'];profile=self.app.transport.profiles[runner['spec']['profile_id']]
                    try:
                        data=future.result()
                        with self.lock:
                            task['plans'][rid]={'runner':runner,'items':{}}
                            for row in data['items']:
                                item_id=uuid.uuid4().hex
                                task['plans'][rid]['items'][item_id]=row
                                task['items'].append({**{k:row[k] for k in ('kind','name','path','bytes','time')},'id':item_id,'runner_id':rid,'host':profile['host'],'container':runner['spec']['container']})
                            for row in data.get('skipped',[]):task['errors'].append({'runner_id':rid,'host':profile['host'],**row})
                    except Exception as exc:
                        with self.lock:task['errors'].append({'runner_id':rid,'host':profile['host'],'error':str(exc)})
        return self._start(task,work)

    def delete(self, scan_id, selected):
        with self.lock:
            scan=self.tasks.get(scan_id)
            if not scan or scan['kind']!='scan' or scan['state']!='completed' or time.time()-scan['created_at']>3600:
                raise ValueError('请先完成扫描；扫描清单有效期为1小时')
            if not isinstance(selected,list) or not selected or len(selected)>10000 or any(not isinstance(i,str) for i in selected):
                raise ValueError('请勾选要清理的项目')
            selected=set(selected)
            available={row['id'] for row in scan['items']}
            if not selected<=available:raise ValueError('清理项不在本次扫描清单中')
            plans={rid:{'runner':p['runner'],'items':[row for key,row in p['items'].items() if key in selected]} for rid,p in scan['plans'].items()}
        task={'id':uuid.uuid4().hex,'kind':'delete','state':'running','created_at':time.time(),'items':[],'errors':[],'deleted_bytes':0}
        def work(task):
            # One Runner at a time limits disk deletion bursts. A centre claim
            # blocks deploy/tests; the Runner lock also blocks local generation.
            for rid,plan in plans.items():
                if not plan['items']:continue
                request_id=uuid.uuid4().hex;claimed=False
                try:
                    runner=self.app.store.get('runner',rid)
                    if runner['spec']!=plan['runner']['spec']:raise ValueError('Runner 登记已变化，请重新扫描')
                    with self.app.mutex:
                        if any(t['runner_id']==rid and t['state'] not in ('ready','failed') for t in self.app.store.list('deployment')):
                            raise ValueError('Runner 正在部署或等待核对，已跳过')
                        self.app.store.claim(rid,'maintenance-storage:'+rid,request_id);claimed=True
                    session=self.app.transport.session(runner['spec']['profile_id'])
                    for index in range(0,len(plan['items']),50):
                        result=remote_request(session,runner['spec'],'delete',{'items':plan['items'][index:index+50]})
                        with self.lock:
                            task['items'].extend({**row,'runner_id':rid} for row in result['results'])
                            task['deleted_bytes']+=result['deleted_bytes']
                except Exception as exc:
                    with self.lock:task['errors'].append({'runner_id':rid,'error':str(exc)+'；通信中断时操作结果可能未知，请重新扫描核对，不自动重试删除'})
                finally:
                    if claimed:self.app.store.release(rid,request_id)
        return self._start(task,work)
