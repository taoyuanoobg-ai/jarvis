#!/usr/bin/env python3
"""Guarded Group 001 workflow repair; never executes a Writer or changes WP."""
import copy, importlib.util, json, subprocess
from pathlib import Path
from datetime import datetime, timezone

spec=importlib.util.spec_from_file_location('helper','/opt/taoyuan/bin/n8n-deployment-helper.py')
h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
key=h.load_key()
ids=['SAjlpjx6cmwkfklv','lashesProdDraftV1','additivesDraftV1']
before={i:h.get_workflow(key,i) for i in ids}
out=Path('/opt/taoyuan/backups/n8n/group001-repair')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
out.mkdir(parents=True)
for i,w in before.items(): (out/(i+'-before.json')).write_text(json.dumps(w,ensure_ascii=False,indent=2))
candidate=copy.deepcopy(before)
intake=candidate[ids[0]]; writer=candidate[ids[1]]
nodes={n['name']:n for n in intake['nodes']}; wn={n['name']:n for n in writer['nodes']}
# HTTP nodes previously omitted the credential type, so requests never reached WP.
for n in intake['nodes']:
 if n['type']=='n8n-nodes-base.httpRequest' and n['name'].startswith('Bilingual'):
  n['parameters']['nodeCredentialType']='wordpressApi'
  n['parameters']['sendHeaders']=True
# Resolve current item rather than first item of a preceding loop iteration.
v=nodes['Verify Draft Writer Result']['parameters']['jsCode']
assert "$('Route NEW_ARTICLE by Site').first().json" in v
nodes['Verify Draft Writer Result']['parameters']['jsCode']=v.replace("$('Route NEW_ARTICLE by Site').first().json","$('Route NEW_ARTICLE by Site').item.json")
# Every existing draft recovery must still pass language mutation and GET verification.
prep=nodes['Bilingual Prepare Dedupe']['parameters']['jsCode']
prep=prep.replace("String(prior.last_error || '').includes('Polylang set-language verification failed')","Boolean(priorPostId > 0 && ['draft','not_created'].includes(String(prior.wordpress_status || '')))")
prep=prep.replace("bilingual_route:'skip'","needs_language_set:true,bilingual_route:'skip'")
prep=prep.replace("status:String(prior.wordpress_status || 'draft')","status:'draft'")
nodes['Bilingual Prepare Dedupe']['parameters']['jsCode']=prep
intake['connections']['Bilingual Writer Decision']['main'][1]=[{'node':'Bilingual Language Decision','type':'main','index':0}]
getnode=copy.deepcopy(nodes['Bilingual Verify EN Translation'])
getnode.update(id='group001-read-language',name='Bilingual Read Language',position=[2110,-250])
getnode['parameters']['url']="={{ 'https://pingdulashes.com/wp-json/pingdu-automation/v1/translations/' + Number($('Bilingual Language Decision').item.json.writer_result.post_id) }}"
assert not any(n['name']==getnode['name'] for n in intake['nodes'])
intake['nodes'].append(getnode)
intake['connections']['Bilingual Set Language']['main'][0]=[{'node':getnode['name'],'type':'main','index':0}]
intake['connections'][getnode['name']]={'main':[[{'node':'Bilingual Verify Language','type':'main','index':0}]]}
nodes['Bilingual Verify Language']['parameters']['jsCode']="""const base = $('Bilingual Language Decision').item.json;
const unwrap = x => { let r=x??{}; if(typeof r.body==='string') r=JSON.parse(r.body); else if(r.body && typeof r.body==='object') r=r.body; return r; };
const resp=unwrap($input.first().json);
const language=String(base.language||base.article?.language||'').toLowerCase();
const id=Number(base.writer_result?.post_id||0);
const ok=resp.ok===true && Number(resp.post_id)===id && resp.language===language && Number(resp.translations?.[language])===id;
return [{json:{...base,status:ok?'completed':'failed',error_code:ok?null:'POLYLANG_LANGUAGE_VERIFY_FAILED',failure_reason:ok?null:'Polylang set-language verification failed',polylang_language_verified:ok,polylang_language_response:resp}}];"""
link=nodes['Bilingual Verify Translation Link']['parameters']['jsCode']
link=link.replace("$('Persist Intake State').first().json","$('Persist Intake State').item.json").replace("$('Bilingual Link Translations').first().json","$('Bilingual Link Translations').item.json").replace("$('Bilingual Verify EN Translation').first().json","$('Bilingual Verify EN Translation').item.json")
link=link.replace('linkResp.ok === true && linkResp.verified === true &&','linkResp.ok === true &&')
nodes['Bilingual Verify Translation Link']['parameters']['jsCode']=link
for name in ['Bilingual Verify EN Translation','Bilingual Verify PT Translation']:
 nodes[name]['parameters']['url']=nodes[name]['parameters']['url'].replace("$('Persist Intake State').first()","$('Persist Intake State').item")
# Canonical IDs/slugs for the two default categories; preserve legacy explicit categories.
p=wn['查询已有分类']['parameters']
p['url']="={{ ['lash guide','guia de cílios'].includes(String($json.category).toLowerCase()) ? 'https://pingdulashes.com/wp-json/wp/v2/categories/' + ($json.language === 'pt' ? 42 : 1) + '?context=edit' : 'https://pingdulashes.com/wp-json/wp/v2/categories?context=edit&per_page=100&search=' + encodeURIComponent($json.category) }}"
wn['匹配分类名称或Slug']['parameters']['jsCode']="""const original=$('校验并标准化输入').first().json;
const r=$input.first().json; const body=r.body??r; const terms=Array.isArray(body)?body:(body.id?[body]:[]);
const canonical=['lash guide','guia de cílios'].includes(String(original.category).toLowerCase());
const wanted=original.language==='pt'?{id:42,slug:'guia-de-cilios'}:{id:1,slug:'lash-guide'};
const matches=terms.filter(t=>canonical ? Number(t.id)===wanted.id && t.slug===wanted.slug : String(t.name||'').trim().toLowerCase()===original.category.toLowerCase());
const match=matches.length===1?matches[0]:null;
return [{json:{...original,category_found:!!match,category_id:match?Number(match.id):null,category_error:match?'':'Canonical category missing or ambiguous'}}];"""
assert wn['VPS Draft Guard']=={n['name']:n for n in before[ids[1]]['nodes']}['VPS Draft Guard']
# Compile changed JavaScript and verify graph before deployment.
for i in ids[:2]:
 oldnodes={n['name']:n for n in before[i]['nodes']}
 names={n['name'] for n in candidate[i]['nodes']}
 for branches in candidate[i]['connections'].values():
  for branch in branches.get('main',[]):
   assert all(e['node'] in names for e in branch)
 for n in candidate[i]['nodes']:
  if 'jsCode' in n.get('parameters',{}) and n!=oldnodes.get(n['name']):
   subprocess.run(['node','-e','new Function('+json.dumps(n['parameters']['jsCode'])+');'],check=True,capture_output=True)
 (out/(i+'-candidate.json')).write_text(json.dumps(candidate[i],ensure_ascii=False,indent=2))
done=[]; report={'ok':False,'backup':str(out),'rollback':{}}
try:
 for i in ids[:2]:
  assert h.canonical_hash(h.get_workflow(key,i))==h.canonical_hash(before[i])
  done.append(i)
  status,_=h.api('PUT','/workflows/'+i,key,h.writable(candidate[i]))
  assert status==200
  saved=h.get_workflow(key,i)
  assert h.canonical_hash(saved)==h.canonical_hash(candidate[i])
  assert saved['active']==before[i]['active']
 assert h.canonical_hash(h.get_workflow(key,ids[2]))==h.canonical_hash(before[ids[2]])
 report.update(ok=True,additives_unchanged=True,draft_guard_unchanged=True,static_validation=True,workflow_readback=True)
except Exception as e:
 report['error']=str(e)
 for i in reversed(done):
  h.api('PUT','/workflows/'+i,key,h.writable(before[i]))
  report['rollback'][i]=h.canonical_hash(h.get_workflow(key,i))==h.canonical_hash(before[i])
(out/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))

