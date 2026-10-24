#!/usr/bin/env python3
"""Build deterministic EN/PT bilingual candidates from existing n8n workflow backups.

Read-only with respect to n8n/WordPress. It reads the pre-change Intake and Lashes
workflow snapshots, validates exact anchors/fingerprints, applies minimal in-memory
patches, and writes candidate JSON + a patch report. It never calls any API.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

INTAKE_NAME = "Drive Content Intake V1"
LASHES_NAME = "Lashes - Production Draft"

EXPECTED_FP = {
    "Verify Additives Draft Writer Result": "2cf1532d5b6a537442b731c7d60489a0b8f7954b6f7d0f96e7c319f7f2e6448e",
    "Verify Draft Writer Result": "125a52f89c3eeb32b334533c19b7466b944dc2650101f8a78e14406febe25706",
    "Persist Intake State": "40e6b59e5547c50eefc0b0c97594be830acacb87b8dc9a84287823906b7e4cd5",
    "Validate Article JSON": "a1eb914a56e27f3240263a9fc9cea0d6ea84773c222443e3ba14dc5aba886f89",
    "Filter JSON and Dedupe": "e34a7d5b1c1140c5b8a7f7b30d40cc70b7a6ffea53590b08a32c8c17f59d43cd",
    "Route Intake Status": "2d150b5f098217310638e50c976a7e89494324354a7c57d6107bf3bd44a78078",
    "校验并标准化输入": "a7cdad39012a9c7fabdfd81b510da6fa5a3e71b07c2643d6cc46992caa3074dc",
    "保存草稿上下文": "97f29d6a22ae5076eade392db87282499ca01b80f341baf1e70ef99675eb6a6e",
    "先创建WordPress草稿": "632e918e4bf12f76f7dd044de680ca83a666ab9778e9012bd43ece084dfac0a7",
    "VPS Draft Guard": "74226ef33ccc1d6352300c21b7d9193e3854ab1e2ecd3b9d0d909ed929943013",
}


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def sanitized_credentials(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if isinstance(v, dict):
            out[k] = {"id": v.get("id"), "name": v.get("name")}
        else:
            out[k] = "<reference>"
    return out


def node_fp(node: dict) -> str:
    return sha({
        "name": node.get("name"),
        "type": node.get("type"),
        "parameters": node.get("parameters", {}),
        "credentials": sanitized_credentials(node.get("credentials", {})),
    })


def unwrap(raw: Any) -> dict:
    if isinstance(raw, dict) and isinstance(raw.get("body"), dict) and "nodes" in raw["body"]:
        return copy.deepcopy(raw["body"])
    if isinstance(raw, dict) and "nodes" in raw:
        return copy.deepcopy(raw)
    raise RuntimeError("input does not contain an n8n workflow")


def load_workflow(path: Path, expected_name: str) -> dict:
    wf = unwrap(json.loads(path.read_text(encoding="utf-8")))
    if wf.get("name") != expected_name:
        raise RuntimeError(f"wrong workflow: expected {expected_name!r}, got {wf.get('name')!r}")
    for key in ("nodes", "connections", "settings"):
        if key not in wf:
            raise RuntimeError(f"{expected_name}: missing {key}")
    return wf


def node_map(wf: dict) -> dict[str, dict]:
    return {str(n.get("name")): n for n in wf.get("nodes", []) if isinstance(n, dict)}


def require_anchor(wf: dict, name: str, expected_type: str | None = None) -> dict:
    n = node_map(wf).get(name)
    if not n:
        raise RuntimeError(f"missing anchor node: {name}")
    if expected_type and n.get("type") != expected_type:
        raise RuntimeError(f"{name}: type mismatch {n.get('type')!r}")
    expected = EXPECTED_FP.get(name)
    if expected:
        actual = node_fp(n)
        if actual != expected:
            raise RuntimeError(f"{name}: fingerprint mismatch actual={actual}")
    return n


def workflow_fp(wf: dict) -> str:
    return sha({k: wf[k] for k in ("name", "nodes", "connections", "settings")})


def main_targets(wf: dict, source: str) -> list[dict]:
    branches = wf["connections"].get(source, {}).get("main")
    if not isinstance(branches, list) or not branches:
        raise RuntimeError(f"{source}: no main outgoing connection")
    if len(branches) != 1:
        raise RuntimeError(f"{source}: expected exactly one output branch, found {len(branches)}")
    targets = branches[0]
    if not isinstance(targets, list) or not targets:
        raise RuntimeError(f"{source}: empty main target list")
    return copy.deepcopy(targets)


def edge(name: str) -> dict:
    return {"node": name, "type": "main", "index": 0}


def set_main(wf: dict, source: str, branches: list[list[dict]]) -> None:
    wf["connections"][source] = {"main": branches}


def pos_after(anchor: dict, dx: float, dy: float = 0) -> list[float]:
    p = anchor.get("position") or [0, 0]
    return [float(p[0]) + dx, float(p[1]) + dy]


def stable_id(wf: dict, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis:n8n:{wf.get('id')}:{name}"))


def add_node(wf: dict, node: dict) -> None:
    names = {n.get("name") for n in wf["nodes"]}
    if node["name"] in names:
        raise RuntimeError(f"candidate node already exists: {node['name']}")
    wf["nodes"].append(node)


def code_node(wf: dict, name: str, js: str, position: list[float]) -> dict:
    return {
        "id": stable_id(wf, name), "name": name, "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": position, "parameters": {"jsCode": js.strip()},
    }


def switch_node(wf: dict, name: str, expression: str, match: str, output_key: str, position: list[float]) -> dict:
    return {
        "id": stable_id(wf, name), "name": name, "type": "n8n-nodes-base.switch",
        "typeVersion": 3.2, "position": position,
        "parameters": {
            "rules": {"values": [{
                "conditions": {
                    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                    "conditions": [{"leftValue": expression, "rightValue": match, "operator": {"type": "string", "operation": "equals"}}],
                    "combinator": "and",
                },
                "renameOutput": True, "outputKey": output_key,
            }]},
            "options": {"fallbackOutput": "extra"},
        },
    }


def http_node(wf: dict, name: str, method: str, url: str, credential: dict, position: list[float], json_body: str | None = None) -> dict:
    params = {
        "url": url, "method": method,
        "options": {"timeout": 30000, "response": {"response": {"responseFormat": "json"}}},
        "headerParameters": {"parameters": [{"name": "Accept", "value": "application/json"}, {"name": "Cache-Control", "value": "no-cache"}]},
        "authentication": "predefinedCredentialType",
    }
    if json_body is not None:
        params.update({"jsonBody": json_body, "sendBody": True, "specifyBody": "json", "contentType": "json"})
    return {
        "id": stable_id(wf, name), "name": name, "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.5, "position": position, "parameters": params,
        "credentials": copy.deepcopy(credential), "onError": "continueRegularOutput",
    }


INTAKE_VALIDATE_JS = r"""
const item = items[0] ?? {json:{}};
const meta = item.json ?? {};
const fail = (code, reason) => [{json:{...meta,status:'failed',error_code:code,failure_reason:reason,validated_at:new Date().toISOString()}}];
if (item.json?.error) return fail('DRIVE_DOWNLOAD_FAILED', String(item.json.error.message ?? item.json.error));
if (!item.binary?.data) return fail('MISSING_BINARY', 'Drive download returned no binary data');
let doc;
try { const bytes = await this.helpers.getBinaryDataBuffer(0, 'data'); doc = JSON.parse(bytes.toString('utf8')); }
catch (e) { return fail('INVALID_JSON', e.message); }
const errors = [];
const required = ['schema_version','article_id','site','article_type','action','content_type','title','content','slug','language','focus_keyword','seo_title','meta_description'];
for (const key of required) if (doc[key] === undefined || doc[key] === null || doc[key] === '') errors.push(`${key} is required`);
for (const key of required) if (doc[key] !== undefined && typeof doc[key] !== 'string') errors.push(`${key} must be a string`);
if (!['pingdulashes.com','zxsflavors.com'].includes(doc.site)) errors.push('only pingdulashes.com and zxsflavors.com are enabled in this stage');
if (doc.action !== 'NEW_ARTICLE') errors.push('only NEW_ARTICLE is enabled in this stage');
if (!['ARTICLE','article'].includes(doc.content_type)) errors.push('content_type must be ARTICLE');
if (!['blog_post','article'].includes(doc.article_type)) errors.push('article_type must be blog_post');
if (doc.categories !== undefined && !Array.isArray(doc.categories)) errors.push('categories must be an array');
if (doc.tags !== undefined && !Array.isArray(doc.tags)) errors.push('tags must be an array');
if (doc.images !== undefined && !Array.isArray(doc.images)) errors.push('images must be an array');
if (doc.seo !== undefined && (typeof doc.seo !== 'object' || Array.isArray(doc.seo) || doc.seo === null)) errors.push('seo must be an object');
const language = String(doc.language || '').trim().toLowerCase();
if (doc.site === 'pingdulashes.com' && !['en','pt'].includes(language)) errors.push('pingdulashes.com language must be en or pt');
doc.language = language;
if (doc.translation_group_id !== undefined && doc.translation_group_id !== null) {
  if (typeof doc.translation_group_id !== 'string') errors.push('translation_group_id must be a string');
  else { doc.translation_group_id = doc.translation_group_id.trim(); if (!doc.translation_group_id) errors.push('translation_group_id cannot be empty when provided'); }
}
if (doc.site === 'pingdulashes.com') {
  if (doc.category !== undefined && doc.category !== null && typeof doc.category !== 'string') errors.push('category must be a string');
  if (typeof doc.category !== 'string' || !doc.category.trim()) doc.category = language === 'pt' ? 'Guia de Cílios' : 'Lash Guide';
} else {
  if (typeof doc.category !== 'string' || !doc.category.trim()) errors.push('category is required');
}
if (errors.length) return fail('SCHEMA_VALIDATION_FAILED', errors.join('; '));
return [{json:{...meta,status:'validated',article:doc,validated_at:new Date().toISOString()}}];
"""

INTAKE_PREPARE_DEDUPE_JS = r"""
const state = $getWorkflowStaticData('global');
state.translation_groups ??= {};
return items.map(item => {
  const r = item.json ?? {};
  if (r.status !== 'validated') return {json:{...r,bilingual_route:'write',bilingual_dedupe_hit:false}};
  const article = r.article ?? {};
  const site = String(article.site || '').toLowerCase();
  const language = String(article.language || '').toLowerCase();
  const groupId = typeof article.translation_group_id === 'string' ? article.translation_group_id.trim() : '';
  if (site !== 'pingdulashes.com' || !groupId || !['en','pt'].includes(language)) return {json:{...r,bilingual_route:'write',bilingual_dedupe_hit:false}};
  const groupKey = `${site}::${groupId}`;
  const group = state.translation_groups[groupKey] ?? {};
  const existingPostId = Number(group[`${language}_post_id`] ?? 0);
  if (existingPostId > 0) {
    return {json:{file_id:r.file_id,file_name:r.file_name,version:r.version,checksum:r.checksum,article,status:'completed',error_code:null,failure_reason:null,writer_result:{success:true,post_id:existingPostId,status:'draft',dedupe_hit:true,language,translation_group_id:groupId},requested_publish:false,completed_at:new Date().toISOString(),bilingual_route:'skip',bilingual_dedupe_hit:true,translation_group_id:groupId,language}};
  }
  return {json:{...r,bilingual_route:'write',bilingual_dedupe_hit:false,translation_group_id:groupId,language}};
});
"""

INTAKE_VERIFY_WRITER_JS = r"""
const route = $('Route NEW_ARTICLE by Site').first().json;
const out = $input.first().json;
const ok = out.success === true && Number(out.post_id) > 0 && out.status === 'draft';
const article = route.article ?? {};
const language = String(article.language || 'en').toLowerCase();
const translationGroupId = typeof article.translation_group_id === 'string' ? article.translation_group_id.trim() : '';
return [{json:{file_id:route.file_id,file_name:route.file_name,version:route.version,checksum:route.checksum,article,status:ok?'completed':'failed',error_code:ok?null:'WRITER_FAILED',failure_reason:ok?null:String(out.error || 'Lashes Writer did not return a valid draft result'),writer_result:out,requested_publish:route.requested_publish,completed_at:new Date().toISOString(),language,translation_group_id:translationGroupId,needs_language_set:Boolean(ok && article.site === 'pingdulashes.com' && ['en','pt'].includes(language))}}];
"""

INTAKE_VERIFY_LANGUAGE_JS = r"""
const base = $('Verify Draft Writer Result').first().json;
const resp = $input.first().json ?? {};
const expectedLanguage = String(base.language || base.article?.language || '').toLowerCase();
const expectedPostId = Number(base.writer_result?.post_id || 0);
const ok = resp.ok === true && resp.verified === true && Number(resp.post_id) === expectedPostId && String(resp.language || '').toLowerCase() === expectedLanguage;
return [{json:{...base,status:ok ? base.status : 'failed',error_code:ok ? base.error_code : 'POLYLANG_LANGUAGE_VERIFY_FAILED',failure_reason:ok ? base.failure_reason : String(resp.error?.message || resp.message || 'Polylang set-language verification failed'),polylang_language_verified:ok}}];
"""

INTAKE_PERSIST_JS = r"""
const state = $getWorkflowStaticData('global');
state.files ??= {};
state.translation_groups ??= {};
const now = new Date().toISOString();
return items.map(item => {
  const r = item.json;
  const prior = state.files[r.file_id] ?? {};
  const failed = r.status === 'failed';
  const attempts = failed ? Number(prior.attempts ?? 0) + 1 : Number(prior.attempts ?? 0);
  let status = r.status;
  if (failed && attempts >= 3) status = 'manual_review';
  const article = r.article ?? {};
  const site = String(article.site || '').toLowerCase();
  const language = String(r.language || article.language || '').toLowerCase();
  const groupId = String(r.translation_group_id || article.translation_group_id || '').trim();
  const groupKey = site === 'pingdulashes.com' && groupId ? `${site}::${groupId}` : null;
  const postId = Number(r.writer_result?.post_id || 0);
  if (groupKey) {
    const group = state.translation_groups[groupKey] ?? {translation_group_id:groupId,site,en_post_id:null,pt_post_id:null,en_status:null,pt_status:null,linked:false,link_verified:false,updated_at:now};
    if (status === 'completed' && ['en','pt'].includes(language) && postId > 0) { group[`${language}_post_id`] = postId; group[`${language}_status`] = String(r.writer_result?.status || 'draft'); }
    group.updated_at = now;
    state.translation_groups[groupKey] = group;
  }
  state.files[r.file_id] = {file_name:r.file_name,version:r.version,checksum:r.checksum,status,attempts,article_id:r.article?.article_id ?? prior.article_id ?? null,wordpress_post_id:r.writer_result?.post_id ?? prior.wordpress_post_id ?? null,wordpress_status:r.writer_result?.status ?? prior.wordpress_status ?? null,requested_publish:r.requested_publish === true,translation_group_id:groupId || prior.translation_group_id || null,language:language || prior.language || null,last_error:r.failure_reason ?? null,updated_at:now};
  const group = groupKey ? state.translation_groups[groupKey] : null;
  const needsTranslationLink = Boolean(status === 'completed' && group && Number(group.en_post_id || 0) > 0 && Number(group.pt_post_id || 0) > 0 && group.linked !== true);
  return {json:{file_id:r.file_id,file_name:r.file_name,status,attempts,error:r.failure_reason ?? null,article:r.article ?? null,writer_result:r.writer_result ?? null,translation_group_id:groupId || null,language:language || null,translation_group_key:groupKey,en_post_id:group ? Number(group.en_post_id || 0) || null : null,pt_post_id:group ? Number(group.pt_post_id || 0) || null : null,linked:group?.linked === true,link_verified:group?.link_verified === true,needs_translation_link:needsTranslationLink}};
});
"""

INTAKE_VERIFY_LINK_JS = r"""
const base = $('Persist Intake State').first().json;
const linkResp = $('Bilingual Link Translations').first().json ?? {};
const enResp = $('Bilingual Verify EN Translation').first().json ?? {};
const ptResp = $input.first().json ?? {};
const enId = Number(base.en_post_id || 0);
const ptId = Number(base.pt_post_id || 0);
const enTranslations = enResp.translations ?? {};
const ptTranslations = ptResp.translations ?? {};
const verified = Boolean(linkResp.ok === true && linkResp.verified === true && Number(linkResp.translations?.en || 0) === enId && Number(linkResp.translations?.pt || 0) === ptId && enResp.ok === true && String(enResp.language || '') === 'en' && Number(enTranslations.en || 0) === enId && Number(enTranslations.pt || 0) === ptId && ptResp.ok === true && String(ptResp.language || '') === 'pt' && Number(ptTranslations.en || 0) === enId && Number(ptTranslations.pt || 0) === ptId);
const state = $getWorkflowStaticData('global');
state.translation_groups ??= {};
if (base.translation_group_key && state.translation_groups[base.translation_group_key]) { const group = state.translation_groups[base.translation_group_key]; group.linked = verified; group.link_verified = verified; group.updated_at = new Date().toISOString(); }
if (verified) return [{json:{...base,status:'completed',linked:true,link_verified:true,needs_translation_link:false}}];
state.files ??= {};
const prior = state.files[base.file_id] ?? {};
const attempts = Number(prior.attempts ?? 0) + 1;
const status = attempts >= 3 ? 'manual_review' : 'failed';
state.files[base.file_id] = {...prior,status,attempts,last_error:'Polylang translation link verification failed',updated_at:new Date().toISOString()};
return [{json:{...base,status,attempts,linked:false,link_verified:false,needs_translation_link:false,error:'Polylang translation link verification failed'}}];
"""

LASHES_NORMALIZE_JS = r"""
const source = $input.first().json;
const input = source && typeof source.body === 'object' && source.body !== null ? source.body : source;
const expectedSite = 'pingdulashes.com';
const normalizeSite = (value) => String(value || '').trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/+$/, '');
const required = ['title', 'content', 'slug', 'focus_keyword'];
const missing = required.filter((field) => typeof input[field] !== 'string' || input[field].trim() === '');
const receivedSite = normalizeSite(input.site);
const siteValid = !receivedSite || receivedSite === expectedSite || receivedSite === `www.${expectedSite}`;
const contentType = typeof input.content_type === 'string' && input.content_type.trim() ? input.content_type.trim().toLowerCase() : 'post';
const contentTypeValid = contentType === 'post' || contentType === 'article';
const language = typeof input.language === 'string' && input.language.trim() ? input.language.trim().toLowerCase() : 'en';
const languageValid = ['en','pt'].includes(language);
const translationGroupId = typeof input.translation_group_id === 'string' ? input.translation_group_id.trim() : '';
const categoryDefault = language === 'pt' ? 'Guia de Cílios' : 'Lash Guide';
const category = typeof input.category === 'string' && input.category.trim() ? input.category.trim() : categoryDefault;
const validationErrors = [];
if (missing.length) validationErrors.push(`Missing required field(s): ${missing.join(', ')}`);
if (!siteValid) validationErrors.push(`Wrong target site: ${receivedSite}; expected ${expectedSite}`);
if (!contentTypeValid) validationErrors.push(`Unsupported content_type: ${contentType}; only post/article is allowed`);
if (!languageValid) validationErrors.push(`Unsupported language: ${language}; only en/pt are allowed`);
if (input.translation_group_id !== undefined && typeof input.translation_group_id !== 'string') validationErrors.push('translation_group_id must be a string when provided');
const rawTags = Array.isArray(input.tags) ? input.tags : [];
const rawSecondaryKeywords = Array.isArray(input.secondary_keywords) ? input.secondary_keywords : [];
const seen = new Set();
const tags = [];
for (const value of rawTags) { if (typeof value !== 'string' || !value.trim()) continue; const clean = value.trim(); const key = clean.toLocaleLowerCase('en-US'); if (!seen.has(key)) { seen.add(key); tags.push(clean); } }
const secondaryKeywords = [...new Set(rawSecondaryKeywords.filter((value) => typeof value === 'string' && value.trim()).map((value) => value.trim()))];
return [{ json: {input_valid: validationErrors.length === 0,validation_error: validationErrors.join('; '),site: expectedSite,content_type: contentType,language,translation_group_id: translationGroupId || null,title: typeof input.title === 'string' ? input.title.trim() : '',slug: typeof input.slug === 'string' ? input.slug.trim() : '',content: typeof input.content === 'string' ? input.content : '',excerpt: typeof input.excerpt === 'string' ? input.excerpt : '',category,category_defaulted: !(typeof input.category === 'string' && input.category.trim()),tags,focus_keyword: typeof input.focus_keyword === 'string' ? input.focus_keyword.trim() : '',secondary_keywords: secondaryKeywords,seo_title: typeof input.seo_title === 'string' ? input.seo_title.trim() : '',meta_description: typeof input.meta_description === 'string' ? input.meta_description.trim() : '',featured_image_url: typeof input.featured_image_url === 'string' ? input.featured_image_url.trim() : '',featured_image_alt: typeof input.featured_image_alt === 'string' && input.featured_image_alt.trim() ? input.featured_image_alt.trim() : (typeof input.title === 'string' ? input.title.trim() : ''),publish_requested: input.publish === true,publish: false,required_fields_valid: missing.length === 0 && siteValid && contentTypeValid && languageValid,draft_only_layer: true} }];
"""


def build(intake: dict, lashes: dict) -> tuple[dict, dict, dict]:
    require_anchor(intake, "Validate Article JSON", "n8n-nodes-base.code")
    require_anchor(intake, "Verify Draft Writer Result", "n8n-nodes-base.code")
    require_anchor(intake, "Persist Intake State", "n8n-nodes-base.code")
    require_anchor(intake, "Verify Additives Draft Writer Result", "n8n-nodes-base.code")
    require_anchor(intake, "Route Intake Status", "n8n-nodes-base.switch")
    normalizer = require_anchor(lashes, "校验并标准化输入", "n8n-nodes-base.code")
    require_anchor(lashes, "VPS Draft Guard", "n8n-nodes-base.code")
    create_wp = require_anchor(lashes, "先创建WordPress草稿", "n8n-nodes-base.httpRequest")
    wp_cred = copy.deepcopy(create_wp.get("credentials", {}))
    if not wp_cred:
        raise RuntimeError("WordPress credential reference is missing")

    report = {"intake_before_fingerprint": workflow_fp(intake), "lashes_before_fingerprint": workflow_fp(lashes), "changed_nodes": [], "added_nodes": [], "untouched_additives_anchor": node_fp(node_map(intake)["Verify Additives Draft Writer Result"]), "untouched_draft_guard_anchor": node_fp(node_map(lashes)["VPS Draft Guard"])}

    for name, js in (("Validate Article JSON", INTAKE_VALIDATE_JS), ("Verify Draft Writer Result", INTAKE_VERIFY_WRITER_JS), ("Persist Intake State", INTAKE_PERSIST_JS)):
        n = node_map(intake)[name]; before = node_fp(n); n["parameters"]["jsCode"] = js.strip(); report["changed_nodes"].append({"workflow":"intake","name":name,"before":before,"after":node_fp(n)})
    before = node_fp(normalizer); normalizer["parameters"]["jsCode"] = LASHES_NORMALIZE_JS.strip(); report["changed_nodes"].append({"workflow":"lashes","name":normalizer["name"],"before":before,"after":node_fp(normalizer)})

    validate_anchor = node_map(intake)["Validate Article JSON"]
    validate_old_targets = main_targets(intake, "Validate Article JSON")
    prep = code_node(intake, "Bilingual Prepare Dedupe", INTAKE_PREPARE_DEDUPE_JS, pos_after(validate_anchor, 220, -120))
    decision = switch_node(intake, "Bilingual Writer Decision", "={{ $json.bilingual_route }}", "write", "write", pos_after(validate_anchor, 440, -120))
    add_node(intake, prep); add_node(intake, decision)
    set_main(intake, "Validate Article JSON", [[edge(prep["name"])]])
    set_main(intake, prep["name"], [[edge(decision["name"])]])
    set_main(intake, decision["name"], [validate_old_targets, [edge("Persist Intake State")]])
    report["added_nodes"] += [{"workflow":"intake","name":prep["name"]},{"workflow":"intake","name":decision["name"]}]

    verify_anchor = node_map(intake)["Verify Draft Writer Result"]
    verify_old_targets = main_targets(intake, "Verify Draft Writer Result")
    lang_decision = switch_node(intake, "Bilingual Language Decision", "={{ $json.needs_language_set ? 'set' : 'skip' }}", "set", "set", pos_after(verify_anchor, 220, -140))
    set_lang = http_node(intake, "Bilingual Set Language", "POST", "https://pingdulashes.com/wp-json/pingdu-automation/v1/set-language", wp_cred, pos_after(verify_anchor, 440, -140), "={{ JSON.stringify({ post_id: Number($json.writer_result.post_id), language: String($json.language || $json.article?.language || 'en').toLowerCase() }) }}")
    verify_lang = code_node(intake, "Bilingual Verify Language", INTAKE_VERIFY_LANGUAGE_JS, pos_after(verify_anchor, 660, -140))
    for n in (lang_decision, set_lang, verify_lang): add_node(intake, n)
    set_main(intake, "Verify Draft Writer Result", [[edge(lang_decision["name"])]])
    set_main(intake, lang_decision["name"], [[edge(set_lang["name"])], verify_old_targets])
    set_main(intake, set_lang["name"], [[edge(verify_lang["name"])]])
    set_main(intake, verify_lang["name"], [verify_old_targets])
    report["added_nodes"] += [{"workflow":"intake","name":n["name"]} for n in (lang_decision,set_lang,verify_lang)]

    persist_anchor = node_map(intake)["Persist Intake State"]
    persist_old_targets = main_targets(intake, "Persist Intake State")
    link_decision = switch_node(intake, "Bilingual Link Decision", "={{ $json.needs_translation_link ? 'link' : 'skip' }}", "link", "link", pos_after(persist_anchor, 220, 160))
    link = http_node(intake, "Bilingual Link Translations", "POST", "https://pingdulashes.com/wp-json/pingdu-automation/v1/link-translations", wp_cred, pos_after(persist_anchor, 440, 160), "={{ JSON.stringify({ en_post_id: Number($json.en_post_id), pt_post_id: Number($json.pt_post_id) }) }}")
    get_en = http_node(intake, "Bilingual Verify EN Translation", "GET", "={{ 'https://pingdulashes.com/wp-json/pingdu-automation/v1/translations/' + Number($('Persist Intake State').first().json.en_post_id) }}", wp_cred, pos_after(persist_anchor, 660, 120))
    get_pt = http_node(intake, "Bilingual Verify PT Translation", "GET", "={{ 'https://pingdulashes.com/wp-json/pingdu-automation/v1/translations/' + Number($('Persist Intake State').first().json.pt_post_id) }}", wp_cred, pos_after(persist_anchor, 880, 120))
    verify_link = code_node(intake, "Bilingual Verify Translation Link", INTAKE_VERIFY_LINK_JS, pos_after(persist_anchor, 1100, 160))
    for n in (link_decision, link, get_en, get_pt, verify_link): add_node(intake, n)
    set_main(intake, "Persist Intake State", [[edge(link_decision["name"])]])
    set_main(intake, link_decision["name"], [[edge(link["name"])], persist_old_targets])
    set_main(intake, link["name"], [[edge(get_en["name"])]])
    set_main(intake, get_en["name"], [[edge(get_pt["name"])]])
    set_main(intake, get_pt["name"], [[edge(verify_link["name"])]])
    set_main(intake, verify_link["name"], [persist_old_targets])
    report["added_nodes"] += [{"workflow":"intake","name":n["name"]} for n in (link_decision,link,get_en,get_pt,verify_link)]

    report["intake_after_fingerprint"] = workflow_fp(intake); report["lashes_after_fingerprint"] = workflow_fp(lashes)
    report["intake_node_count_after"] = len(intake["nodes"]); report["lashes_node_count_after"] = len(lashes["nodes"])
    report["intake_connection_sources_after"] = len(intake["connections"]); report["lashes_connection_sources_after"] = len(lashes["connections"])
    return intake, lashes, report


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--intake", required=True); ap.add_argument("--lashes", required=True); ap.add_argument("--output-dir", required=True); args = ap.parse_args()
    intake_path = Path(args.intake); lashes_path = Path(args.lashes); out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    intake = load_workflow(intake_path, INTAKE_NAME); lashes = load_workflow(lashes_path, LASHES_NAME)
    before_counts = {"intake_nodes":len(intake["nodes"]),"lashes_nodes":len(lashes["nodes"]),"intake_connections":len(intake["connections"]),"lashes_connections":len(lashes["connections"])}
    intake_candidate, lashes_candidate, report = build(intake, lashes); report["before_counts"] = before_counts; report["source_files"] = {"intake":str(intake_path),"lashes":str(lashes_path)}
    intake_out = out / "intake-bilingual.json"; lashes_out = out / "lashes-bilingual.json"; report_out = out / "patch-report.json"
    intake_out.write_text(json.dumps(intake_candidate, ensure_ascii=False, indent=2), encoding="utf-8"); lashes_out.write_text(json.dumps(lashes_candidate, ensure_ascii=False, indent=2), encoding="utf-8"); report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok":True,"intake_candidate":str(intake_out),"lashes_candidate":str(lashes_out),"patch_report":str(report_out),"intake_nodes_before":before_counts["intake_nodes"],"intake_nodes_after":len(intake_candidate["nodes"]),"lashes_nodes_before":before_counts["lashes_nodes"],"lashes_nodes_after":len(lashes_candidate["nodes"])}, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok":False,"error":str(exc)}, ensure_ascii=False)); raise SystemExit(1)
