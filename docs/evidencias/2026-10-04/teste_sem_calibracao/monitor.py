from pathlib import Path
import collections, datetime, json, os, re, shlex, subprocess, time
ROOT=Path(__file__).parent
DURATION=600
REMOTE=r"""
from pathlib import Path
import collections, datetime, hashlib, json, math, os, re, subprocess, time

def run(args, timeout=6):
 try:
  p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
  return {'code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
 except (OSError,subprocess.TimeoutExpired) as e:return {'error':str(e)}
def text(path):
 try:return Path(path).read_text()
 except OSError:return None
def load(path):
 value=text(path)
 try:return json.loads(value) if value else None
 except ValueError:return None
def rows(path):
 value=text(path)
 if value is None:return []
 result=[]
 for line in value.splitlines():
  try:result.append(json.loads(line))
  except ValueError:pass
 return result
now=datetime.datetime.now(datetime.timezone.utc)
logs=Path('/mnt/nvme/Monitoramento/FATIGUE/logs')
starts=list((logs/'runs'/now.strftime('%Y/%m/%d')).glob('*/run-start.json'))
start=max(starts,key=lambda p:p.stat().st_mtime) if starts else None
record={'remote_time_utc':now.isoformat(),'remote_time_unix':time.time()}
if start:
 record['run_path']=str(start.parent);record['run_start']=load(start)
 segments=[];frames=[];assessments=[]
 for folder in sorted((start.parent/'segments').iterdir()):
  f=rows(folder/'frames.jsonl');a=rows(folder/'assessments.jsonl');m=load(folder/'manifest.json')
  item={'name':folder.name,'partial':folder.name.endswith('.partial'),'frame_count':len(f),'assessment_count':len(a),'manifest':m}
  if m and not item['partial']:
   item['manifest_counts_ok']=m['frame_count']==len(f) and m['assessment_count']==len(a)
   item['sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
  segments.append(item);frames.extend(f);assessments.extend(a)
 frames.sort(key=lambda r:r['frame_idx']);assessments.sort(key=lambda r:r['frame_idx'])
 gaps=[{'from_frame':a['frame_idx'],'to_frame':b['frame_idx'],'seconds':b['timestamp_s']-a['timestamp_s']} for a,b in zip(assessments,assessments[1:])]
 record['runtime']={'segments':segments,'frames_logged':len(frames),'assessments_logged':len(assessments),'face_detected':sum(bool(r.get('face_detected')) for r in frames),'valid_eyes':dict(collections.Counter(str(r.get('valid_eye_count')) for r in assessments)),'states':dict(collections.Counter(r.get('fatigue_label') for r in assessments)),'observation_states':dict(collections.Counter(r.get('observation_state') for r in assessments)),'first_frame':frames[0] if frames else None,'last_frame':frames[-1] if frames else None,'last_assessment':assessments[-1] if assessments else None,'longest_sampled_gap':max(gaps,key=lambda r:r['seconds']) if gaps else None,'sampled_gaps_over_1s':[g for g in gaps if g['seconds']>1][-30:],'recent_assessments':assessments[-220:]}
 since=record['run_start']['ts_utc'] if record['run_start'] else now.isoformat()
 event_files=(logs/'events'/now.strftime('%Y/%m/%d')).glob('*.json')
 events=[load(p) for p in event_files]
 record['events']=[e for e in events if e and e.get('run_id')==start.parent.name]
else:since=now.isoformat()
inspect=run(['docker','inspect','salte-fatigue','monitor-motorista','gallery'])
record['containers']=[]
if inspect.get('code')==0:
 for c in json.loads(inspect['stdout']):
  config={'Config':c['Config'],'HostConfig':c['HostConfig'],'Mounts':sorted(c['Mounts'],key=lambda m:m['Destination'])}
  info={'name':c['Name'],'id':c['Id'],'image':c['Image'],'status':c['State']['Status'],'started_at':c['State']['StartedAt'],'restart_count':c['RestartCount'],'pid':c['State']['Pid'],'config_sha256':hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()}
  pid=c['State']['Pid'];status=text('/proc/'+str(pid)+'/status');info['process_state']=next((line.split(':',1)[1].strip() for line in (status or '').splitlines() if line.startswith('State:')),None);info['wchan']=text('/proc/'+str(pid)+'/wchan')
  record['containers'].append(info)
else:record['inspect_error']=inspect
record['fatigue_stdout']=run(['docker','logs','--since','45s','--tail','120','salte-fatigue'])
record['monitor_stdout']=run(['docker','logs','--since','45s','--tail','30','monitor-motorista'])
record['gallery_stdout']=run(['docker','logs','--since','45s','--tail','10','gallery'])
record['kernel_since_run']=run(['journalctl','-k','--since',since,'--no-pager','-o','short-iso'],timeout=7)
kernel=record['kernel_since_run'].get('stdout','')
record['nvme_write_timeouts_since_run']=len(re.findall(r'nvme .*timeout, aborting req_op:WRITE',kernel)) if record['kernel_since_run'].get('code')==0 else None
record['kernel_errors']=[line for line in kernel.splitlines() if re.search(r'nvme|I/O error|EXT4-fs error|Out of memory|oom-kill|under.?voltage',line,re.I)][-80:]
temp=text('/sys/class/thermal/thermal_zone0/temp');record['cpu_temp_c']=int(temp)/1000 if temp else None
record['throttled']=run(['vcgencmd','get_throttled'])
record['loadavg']=os.getloadavg();record['docker_stats']=run(['docker','stats','--no-stream','--format','{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.BlockIO}}'],timeout=8)
stat=os.statvfs(logs);record['nvme_free_bytes']=stat.f_bavail*stat.f_frsize
record['logs_mount']=run(['findmnt','-T',str(logs),'-no','SOURCE,TARGET,FSTYPE'])
record['gallery_http']=run(['curl','--head','--silent','--max-time','3','http://127.0.0.1:8080/'])
print(json.dumps(record,separators=(',',':')))
"""
started=time.monotonic();deadline=started+DURATION;index=0
(ROOT/'started.json').write_text(json.dumps({'started_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'duration_seconds':DURATION}))
while True:
 index+=1;begin=time.monotonic()
 item={'sample':index,'sampled_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
 try:
  p=subprocess.run(['ssh','-F','/tmp/codex-rpi-ssh/config','rpi-sala','python3 -'],input=REMOTE,capture_output=True,text=True,timeout=45)
  if p.returncode==0:item.update(json.loads(p.stdout))
  else:item['error']=p.stderr[-2000:]
 except (OSError,ValueError,subprocess.TimeoutExpired) as e:item['error']=str(e)
 item['collection_seconds']=round(time.monotonic()-begin,3)
 (ROOT/f'sample-{index:03d}.json').write_text(json.dumps(item,indent=2)+'\n')
 runtime=item.get('runtime',{});last=runtime.get('last_frame') or {};assessment=runtime.get('last_assessment') or {}
 print(json.dumps({'sample':index,'elapsed_s':round(time.monotonic()-started),'error':item.get('error'),'run':item.get('run_path','').split('/')[-1],'frame':last.get('frame_idx'),'states':runtime.get('states'),'last_label':assessment.get('fatigue_label'),'perclos':assessment.get('perclos_ear'),'coverage':assessment.get('perclos_coverage'),'temp_c':item.get('cpu_temp_c'),'nvme_timeouts_since_run':item.get('nvme_write_timeouts_since_run'),'collection_s':item['collection_seconds']},ensure_ascii=False),flush=True)
 if time.monotonic()>=deadline:break
 time.sleep(min(max(0,30-(time.monotonic()-begin)),max(0,deadline-time.monotonic())))
(ROOT/'completed.json').write_text(json.dumps({'completed_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sample_count':index,'elapsed_seconds':round(time.monotonic()-started,3)}))
