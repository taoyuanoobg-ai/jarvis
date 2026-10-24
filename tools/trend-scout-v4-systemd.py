#!/usr/bin/env python3
"""Guarded v4 patch with real scan using the existing systemd environment."""
import datetime, fcntl, hashlib, json, os, shutil, sqlite3, subprocess, time
from pathlib import Path
B=Path('/opt/youtube-trend-scout'); S=B/'youtube_trend_scout_v3.py'; C=B/'config_v3.json'
U='youtube-trend-scout.service'; T='youtube-trend-scout.timer'
V=dict(recent_uploads_per_channel=40,recent_long_target=15,min_recent_long_for_score=5)
def run(*a): return subprocess.run(a,check=True,capture_output=True,text=True).stdout.strip()
def replace(s,a,b):
    if s.count(a)!=1: raise RuntimeError('Anchor mismatch: '+a[:80])
    return s.replace(a,b,1)
def atomic(p,s):
    q=p.with_name(p.name+'.v4-tmp'); q.write_text(s,encoding='utf-8')
    os.chmod(q,p.stat().st_mode & 0o777); os.chown(q,p.stat().st_uid,p.stat().st_gid); os.replace(q,p)
def patch(s):
    if '# V4_SYSTEMD_SAMPLING' in s: return s
    s=replace(s,'    rows = []\n    for cid in shortlist:', '''    # V4_SYSTEMD_SAMPLING
    sampling = dict(uploads_inspected=0,recent_long_samples=0,score_eligible_channels=0,
                    insufficient_recent_long_data=0,v3_uploads_inspected=0,
                    v3_recent_long_samples=0,v3_score_eligible_channels=0)
    rows = []
    for cid in shortlist:''')
    s=replace(s,'        recent_long = []\n        for vid in uploads_map.get(cid, []):', '''        recent_long = []
        v3_count = 0
        recent_ids = uploads_map.get(cid, [])[:int(cfg['recent_uploads_per_channel'])]
        sampling['uploads_inspected'] += len(recent_ids)
        sampling['v3_uploads_inspected'] += min(15,len(recent_ids))
        for index, vid in enumerate(recent_ids):''')
    s=replace(s,'            recent_long.append(int(item.get("statistics", {}).get("viewCount", 0) or 0))\n\n        if len(recent_long) < 2:', '''            if index < 15: v3_count += 1
            if len(recent_long) < int(cfg.get('recent_long_target',15)):
                recent_long.append(int(item.get("statistics", {}).get("viewCount", 0) or 0))

        sampling['v3_recent_long_samples'] += v3_count
        sampling['v3_score_eligible_channels'] += int(v3_count >= 2)
        sampling['recent_long_samples'] += len(recent_long)
        if len(recent_long) < int(cfg.get('min_recent_long_for_score',5)):
            sampling['insufficient_recent_long_data'] += 1''')
    s=replace(s,'        cands = by_channel[cid]','        sampling["score_eligible_channels"] += 1\n        cands = by_channel[cid]')
    s=replace(s,'        "shortlisted_count": len(shortlist),','        "shortlisted_count": len(shortlist),\n        "sampling": sampling,')
    a='    key = os.environ.get("YOUTUBE_API_KEY", "").strip()'
    s=replace(s,a,'''    cfg.setdefault('recent_long_target',15)
    cfg.setdefault('min_recent_long_for_score',5)
    sampling_config={k:cfg[k] for k in ('recent_uploads_per_channel','recent_long_target','min_recent_long_for_score')}
    print('V4_SAMPLING '+json.dumps(sampling_config),flush=True)
'''+a)
    s=replace(s,'{"run_at":iso_z(captured),"english":en["rows"],"chinese":zh["rows"]}', '''{"run_at":iso_z(captured),"english":en["rows"],"chinese":zh["rows"],
                 "sampling_config":sampling_config,"market_metrics":{
                 "US":{k:v for k,v in en.items() if k!='rows'},
                 "TW":{k:v for k,v in zh.items() if k!='rows'}}}''')
    compile(s,str(S),'exec'); return s
def main():
    lock=open(B/'.v4-deploy.lock','a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if run('systemctl','show',U,'--property=ActiveState','--value') in ('active','activating'): raise RuntimeError('Scan already running')
    assert run('systemctl','is-active',T)=='active'
    assert run('systemctl','is-enabled',T)=='enabled'
    backup=B/'backups'/('v4-systemd-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')); backup.mkdir(parents=True)
    for p in (S,C): shutil.copy2(p,backup/p.name)
    for u in (U,T): (backup/(u+'.txt')).write_text(run('systemctl','cat',u))
    previous=json.loads((B/'reports/latest.json').read_text()); (backup/'previous-report.json').write_text(json.dumps(previous))
    old=json.loads(C.read_text()); new=dict(old,**V); source=patch(S.read_text())
    db=B/'data/trend_scout.sqlite3'; con=sqlite3.connect(db,timeout=15)
    assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    con.execute('BEGIN IMMEDIATE'); con.rollback()
    oldrun=con.execute('SELECT coalesce(max(id),0) FROM runs').fetchone()[0]; con.close()
    assert os.access(B/'reports',os.W_OK)
    r=dict(ok=False,backup=str(backup),old_config={k:old.get(k) for k in V},new_config=V)
    try:
        atomic(S,source); atomic(C,json.dumps(new,ensure_ascii=False,indent=2)+'\n')
        run(str(B/'.venv/bin/python'),'-m','py_compile',str(S)); json.loads(C.read_text())
        run('systemctl','start','--no-block',U)
        deadline=time.monotonic()+2400
        while run('systemctl','show',U,'--property=ActiveState','--value') in ('active','activating','deactivating'):
            if time.monotonic()>deadline: raise RuntimeError('Scan timeout')
            time.sleep(5)
        assert run('systemctl','show',U,'--property=Result','--value')=='success'
        assert run('systemctl','show',U,'--property=ExecMainStatus','--value')=='0'
        report=json.loads((B/'reports/latest.json').read_text()); assert report['sampling_config']==V
        assert set(report['market_metrics'])=={'US','TW'}
        files={e:(B/'reports'/('latest.'+e)).stat().st_size for e in ('html','csv','json')}; assert all(files.values())
        con=sqlite3.connect(db); newrun=con.execute('SELECT max(id) FROM runs').fetchone()[0]; assert newrun>oldrun
        hist=con.execute('SELECT market,count(*) FROM channel_opportunities_v3 WHERE run_id=? GROUP BY market',(newrun,)).fetchall()
        assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok'; con.close()
        assert run('systemctl','is-active',T)=='active'; assert run('systemctl','is-enabled',T)=='enabled'
        r.update(ok=True,real_scan='systemd success',reports=files,sqlite=dict(run_id=newrun,history=hist),
                 market_metrics=report['market_metrics'],previous_candidates={k:len(previous.get(k,[])) for k in ('english','chinese')},
                 final_candidates={k:len(report[k]) for k in ('english','chinese')},timer='active/enabled',rollback=False)
    except Exception as e:
        r['error']=str(e)
        if run('systemctl','show',U,'--property=ActiveState','--value') in ('active','activating'): run('systemctl','stop',U)
        for p in (S,C): atomic(p,(backup/p.name).read_text())
        r['rollback']=all(p.read_bytes()==(backup/p.name).read_bytes() for p in (S,C))
    (backup/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)); print(json.dumps(r,ensure_ascii=False,indent=2))
    return 0 if r['ok'] else 1
if __name__=='__main__': raise SystemExit(main())
