import pathlib,json,subprocess,shlex,time,datetime,sys,re
base=pathlib.Path(sys.argv[1]); config=json.loads((base/"monitor-config.json").read_text())
deadline=datetime.datetime.fromisoformat(config["deadline_utc"]).timestamp()
iteration=0
while True:
 iteration+=1
 captured=datetime.datetime.now(datetime.timezone.utc).isoformat()
 try:
  result=subprocess.run(["ssh","-F","/tmp/codex-rpi-ssh/config","-o","ConnectTimeout=10","rpi-sala","python3 -c "+shlex.quote(config["remote_script"])],text=True,capture_output=True,timeout=45)
  if result.returncode: record={"ts_utc":captured,"ssh_rc":result.returncode,"stdout":result.stdout,"stderr":result.stderr}
  else: record=json.loads(result.stdout)
 except Exception as e: record={"ts_utc":captured,"error":str(e)}
 (base/f"sample-{iteration:03d}.json").write_text(json.dumps(record,indent=2)+"\n")
 run=record.get("run",{}); frames=run.get("frames_tail",{}).get("records",[]); assessments=run.get("assessments_tail",{}).get("records",[])
 last=frames[-1] if frames else {}
 fat=record.get("containers",{}).get("salte-fatigue",{})
 mon=record.get("containers",{}).get("monitor-motorista",{})
 health=[line for stream in ("stdout","stderr") for line in record.get("fatigue_logs",{}).get(stream,"").splitlines() if "health:" in line]
 mt=record.get("monitor_logs",{}); monitor_lines=(mt.get("stdout","")+mt.get("stderr","")).splitlines()
 summary={"sample":iteration,"ts_utc":record.get("ts_utc"),"frame_idx":last.get("frame_idx"),"frame_timestamp_s":last.get("timestamp_s"),"face":last.get("face_detected"),"ear_left":last.get("ear_left"),"ear_right":last.get("ear_right"),"fatigue_state":assessments[-1].get("fatigue_state") if assessments else None,"segments":[{"name":x["name"],"manifest":x["manifest_exists"],"bytes":x["frames_bytes"]+x["assessments_bytes"]} for x in run.get("segments",[])],"fatigue_restarts":fat.get("restart_count"),"fatigue_proc":fat.get("proc"),"monitor_restarts":mon.get("restart_count"),"monitor_proc":mon.get("proc"),"last_health":health[-1] if health else None,"last_monitor":monitor_lines[-1] if monitor_lines else None,"nvme_write_timeout_count":len(record.get("nvme_write_timeouts",[])),"error":record.get("error") or record.get("run_error") or record.get("ssh_rc")}
 print(json.dumps(summary),flush=True)
 remaining=deadline-time.time()
 if remaining<=0: break
 time.sleep(min(30,remaining))
print(json.dumps({"complete":True,"samples":iteration,"evidence_dir":str(base)}),flush=True)
