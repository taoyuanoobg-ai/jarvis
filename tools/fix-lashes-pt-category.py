#!/usr/bin/env python3
"""Guarded category assignment after verified Polylang language; no test execution."""
import copy, importlib.util, json, subprocess
from pathlib import Path
from datetime import datetime,timezone
sp=importlib.util.spec_from_file_location('h','/opt/taoyuan/bin/n8n-deployment-helper.py');h=importlib.util.module_from_spec(sp);sp.loader.exec_module(h)
key=h.load_key(); iid='SAjlpjx6cmwkfklv'; lid='lashesProdDraftV1'; aid='additivesDraftV1'
before={i:h.get_workflow(key,i) for i in [iid,lid,aid]}
backup=Path('/opt/taoyuan/backups/n8n/pt-category')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');backup.mkdir(parents=True)
for i,w in before.items(): (backup/(i+'.json')).write_text(json.dumps(w,ensure_ascii=False,indent=2))
w=copy.deepcopy(before[iid]);nodes={n['name']:n for n in w['nodes']}
assert 'Bilingual Canonical Category Decision' not in nodes
cred=copy.deepcopy(nodes['Bilingual Set Language']['credentials'])
def add(name,typ,version,parameters,x):
 n={'id':'pt-category-'+str(x),'name':name,'type':'n8n-nodes-base.'+typ,'typeVersion':version,'parameters':parameters,'position':[x,400]}
 if typ=='httpRequest': n['credentials']=cred;n['onError']='continueRegularOutput'
 w['nodes'].append(n);return n
def edge(name):return {'node':name,'type':'main','index':0}
gate='Bilingual Canonical Category Decision';update='Bilingual Assign Canonical Category';get='Bilingual Read Canonical Category';verify='Bilingual Verify Canonical Category'
add(gate,'if',2.2,{'conditions':{'options':{'caseSensitive':True,'typeValidation':'strict','version':2},'conditions':[{'leftValue':"={{ $json.status === 'completed' && $json.polylang_language_verified === true && ['lash guide','guia de cílios'].includes(String($json.article?.category || '').trim().toLowerCase()) }}",'rightValue':True,'operator':{'type':'boolean','operation':'true','singleValue':True}}],'combinator':'and'},'options':{}},2400)
common={'authentication':'predefinedCredentialType','nodeCredentialType':'wordpressApi','sendHeaders':True,'headerParameters':{'parameters':[{'name':'Accept','value':'application/json'},{'name':'Cache-Control','value':'no-cache'}]},'options':{'timeout':30000,'response':{'response':{'responseFormat':'json'}}}}
u=copy.deepcopy(common);u.update(method='POST',url="={{ 'https://pingdulashes.com/wp-json/wp/v2/posts/' + Number($json.writer_result.post_id) }}",sendBody=True,contentType='json',specifyBody='json',jsonBody="={{ JSON.stringify({categories:[$json.language === 'pt' ? 42 : 1]}) }}")
add(update,'httpRequest',4.5,u,2620)
g=copy.deepcopy(common);g.update(method='GET',url="={{ 'https://pingdulashes.com/wp-json/wp/v2/posts/' + Number($('Bilingual Canonical Category Decision').item.json.writer_result.post_id) + '?context=edit&_fields=id,status,categories' }}")
add(get,'httpRequest',4.5,g,2840)
js="""const base=$('Bilingual Canonical Category Decision').item.json;
const raw=$input.first().json;const r=typeof raw.body==='string'?JSON.parse(raw.body):(raw.body??raw);
const expected=base.language==='pt'?42:1;
const ok=Number(r.id)===Number(base.writer_result.post_id)&&r.status==='draft'&&Array.isArray(r.categories)&&r.categories.length===1&&Number(r.categories[0])===expected;
return [{json:{...base,status:ok?'completed':'failed',error_code:ok?null:'CATEGORY_VERIFY_FAILED',failure_reason:ok?null:'Canonical category readback failed',category_verified:ok,writer_result:{...base.writer_result,categories:r.categories??[],category_verified:ok}}}];"""
add(verify,'code',2,{'jsCode':js},3060)
w['connections']['Bilingual Verify Language']={'main':[[edge(gate)]]}
w['connections'][gate]={'main':[[edge(update)],[edge('Persist Intake State')]]}
w['connections'][update]={'main':[[edge(get)]]};w['connections'][get]={'main':[[edge(verify)]]};w['connections'][verify]={'main':[[edge('Persist Intake State')]]}
names={n['name'] for n in w['nodes']}
for c in w['connections'].values():
 for b in c.get('main',[]): assert all(e['node'] in names for e in b)
subprocess.run(['docker','exec','taoyuan-n8n','node','-e','new Function('+json.dumps(js)+')'],check=True,capture_output=True)
# An isolated response test covers the actual failure (146) and canonical success (42).
test="const code="+json.dumps(js)+";const base={language:'pt',writer_result:{post_id:4615}};for(const category of [146,42]){const result=new Function('$','$input',code)(()=>({item:{json:base}}),{first:()=>({json:{id:4615,status:'draft',categories:[category]}})});if(result[0].json.category_verified!==(category===42))throw Error('category verification');}"
subprocess.run(['docker','exec','taoyuan-n8n','node','-e',test],check=True,capture_output=True)
(backup/'candidate.json').write_text(json.dumps(w,ensure_ascii=False,indent=2))
r={'ok':False,'backup':str(backup)}
try:
 assert h.canonical_hash(h.get_workflow(key,iid))==h.canonical_hash(before[iid])
 status,_=h.api('PUT','/workflows/'+iid,key,h.writable(w));assert status==200
 saved=h.get_workflow(key,iid);assert h.canonical_hash(saved)==h.canonical_hash(w);assert saved['active']==before[iid]['active']
 assert h.canonical_hash(h.get_workflow(key,lid))==h.canonical_hash(before[lid])
 assert h.canonical_hash(h.get_workflow(key,aid))==h.canonical_hash(before[aid])
 r.update(ok=True,same_id_verified=True,writer_unchanged=True,additives_unchanged=True,draft_guard_unchanged=True,static_tests=True,new_posts_created=0)
except Exception as e:
 h.api('PUT','/workflows/'+iid,key,h.writable(before[iid]));r.update(error=str(e),rollback=h.canonical_hash(h.get_workflow(key,iid))==h.canonical_hash(before[iid]))
(backup/'result.json').write_text(json.dumps(r,indent=2));print(json.dumps(r,indent=2))

