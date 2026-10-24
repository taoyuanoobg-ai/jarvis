#!/usr/bin/env python3
import json, re, shutil, subprocess, sys, hashlib, datetime, sqlite3, os
from pathlib import Path
BASE=Path('/opt/youtube-trend-scout')
CFG=BASE/'config_v3.json'; SRC=BASE/'youtube_trend_scout_v3.py'
STAMP=datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
BACK=BASE/'backups'/('v4-'+STAMP)
def fail(msg):
 print(json.dumps({'ok':False,'error':msg},ensure_ascii=False)); raise SystemExit(1)
try:
 BACK.mkdir(parents=True,exist_ok=False)
 shutil.copy2(CFG,BACK/'config_v3.json'); shutil.copy2(SRC,BACK/'youtube_trend_scout_v3.py')
 old=json.loads(CFG.read_text())
 text=SRC.read_text()
 if 'recent_long_target' not in old:
  old['recent_uploads_per_channel']=40; old['recent_long_target']=15; old['min_recent_long_for_score']=5
 else:
  old.update(recent_uploads_per_channel=40,recent_long_target=15,min_recent_long_for_score=5)
 if 'cfg.get("recent_long_target", 15)' not in text:
  text=text.replace('recent_long = []','recent_long = []\n        recent_long_target = cfg.get("recent_long_target", 15)',1)
  text=text.replace('recent_long.append(int(item.get("statistics", {}).get("viewCount", 0) or 0))','recent_long.append(int(item.get("statistics", {}).get("viewCount", 0) or 0))\n            if len(recent_long) >= recent_long_target:\n                break',1)
  text=text.replace('if len(recent_long) < 2:','if len(recent_long) < cfg.get("min_recent_long_for_score", 5):',1)
 if 'recent_long_target' not in text or 'min_recent_long_for_score' not in text:
  fail('source anchors not found')
 CFG.write_text(json.dumps(old,ensure_ascii=False,indent=2)+'\n')
 SRC.write_text(text)
 json.loads(CFG.read_text()); compile(text,str(SRC),'exec')
 subprocess.run([sys.executable,'-m','py_compile',str(SRC)],check=True)
 db=BASE/'data'/'trend_scout.sqlite3'
 con=sqlite3.connect(db); con.execute('create table if not exists _v4_probe(x int)'); con.rollback(); con.close()
 subprocess.run(['systemctl','is-active','--quiet','youtube-trend-scout.timer'],check=True)
 p=subprocess.run([sys.executable,str(SRC)],cwd=BASE,text=True,capture_output=True,timeout=1800)
 if p.returncode!=0: raise RuntimeError('scan failed: '+p.stderr[-1000:])
 report={'ok':True,'backup':str(BACK),'old_config':{'recent_uploads_per_channel':old.get('recent_uploads_per_channel')},'new_config':{k:old[k] for k in ('recent_uploads_per_channel','recent_long_target','min_recent_long_for_score')},'scan_returncode':p.returncode,'stdout_tail':p.stdout[-2000:],'stderr_tail':p.stderr[-1000:]}
 (BACK/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps(report,ensure_ascii=False))
except Exception as e:
 try:
  if (BACK/'config_v3.json').exists(): shutil.copy2(BACK/'config_v3.json',CFG)
  if (BACK/'youtube_trend_scout_v3.py').exists(): shutil.copy2(BACK/'youtube_trend_scout_v3.py',SRC)
 except: pass
 fail(str(e))
