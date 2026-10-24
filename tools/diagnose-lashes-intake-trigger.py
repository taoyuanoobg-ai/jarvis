#!/usr/bin/env python3
"""Read-only diagnostic for the production Drive Content Intake trigger/executions."""
from __future__ import annotations
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE='http://127.0.0.1:5678/api/v1'
SECRET='/opt/taoyuan/secrets/n8n-api-key'
WF_ID='SAjlpjx6cmwkfklv'
EXPECTED_NAME='Drive Content Intake V1'
FOLDER_ID='1HiK05nZ6oJEapWbMVmCtLZ530eOKuWVL'

def api(path):
    key=Path(SECRET).read_text(encoding='utf-8').strip()
    req=Request(BASE+path,headers={'X-N8N-API-KEY':key,'Accept':'application/json'})
    try:
        with urlopen(req,timeout=30) as r:
            raw=r.read().decode('utf-8')
            return r.status, json.loads(raw) if raw else None
    except HTTPError as e:
        raw=e.read().decode('utf-8',errors='replace')
        try: body=json.loads(raw) if raw else None
        except Exception: body={'raw':raw[:2000]}
        return e.code,body

def text_blob(x):
    try:return json.dumps(x,ensure_ascii=False).lower()
    except Exception:return str(x).lower()

def main():
    out={'ok':False,'workflow_id':WF_ID,'expected_name':EXPECTED_NAME,'folder_id':FOLDER_ID}
    s,wf=api(f'/workflows/{WF_ID}')
    out['workflow_get_status']=s
    if s!=200 or not isinstance(wf,dict):
        out['error']='workflow GET failed'; print(json.dumps(out,ensure_ascii=False,indent=2)); return 2
    out['workflow_name']=wf.get('name')
    out['active']=wf.get('active')
    nodes=wf.get('nodes',[]) or []
    triggers=[]
    folder_refs=[]
    for n in nodes:
        if not isinstance(n,dict): continue
        typ=str(n.get('type',''))
        blob=text_blob(n.get('parameters',{}))
        if 'trigger' in typ.lower() or 'schedule' in typ.lower() or 'cron' in typ.lower():
            triggers.append({'name':n.get('name'),'type':typ,'disabled':n.get('disabled',False),'parameters':n.get('parameters',{})})
        if FOLDER_ID.lower() in blob:
            folder_refs.append({'name':n.get('name'),'type':typ})
    out['trigger_nodes']=triggers
    out['folder_id_references']=folder_refs

    es,eb=api(f'/executions?workflowId={WF_ID}&limit=20')
    out['executions_list_status']=es
    executions=[]
    if es==200 and isinstance(eb,dict):
        data=eb.get('data',[]) or []
        for e in data[:20]:
            if not isinstance(e,dict): continue
            executions.append({k:e.get(k) for k in ('id','status','mode','startedAt','stoppedAt','finished','workflowId')})
        out['recent_executions']=executions
        if executions:
            exid=str(executions[0].get('id'))
            ds,db=api(f'/executions/{exid}?includeData=true')
            out['latest_execution_detail_status']=ds
            if ds==200 and isinstance(db,dict):
                out['latest_execution']={k:db.get(k) for k in ('id','status','mode','startedAt','stoppedAt','finished','workflowId')}
                run=(((db.get('data') or {}).get('resultData') or {}).get('runData') or {})
                out['latest_run_nodes']=list(run.keys())
                err=((db.get('data') or {}).get('resultData') or {}).get('error')
                if err:
                    out['latest_error']={'message':err.get('message'),'name':err.get('name'),'node':(err.get('node') or {}).get('name') if isinstance(err.get('node'),dict) else None}
    else:
        out['executions_list_body']=eb

    out['checks']={
        'name_ok':wf.get('name')==EXPECTED_NAME,
        'active_true':wf.get('active') is True,
        'has_trigger':bool(triggers),
        'folder_id_present':bool(folder_refs),
        'executions_endpoint_ok':es==200,
    }
    out['ok']=out['checks']['name_ok'] and out['checks']['has_trigger'] and out['checks']['executions_endpoint_ok']
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if out['ok'] else 1

if __name__=='__main__':
    raise SystemExit(main())
