"""Role-specific primary material contracts, shared by prompts and output gates.

The canonical ledger stays complete. Role routing changes presentation and
records adequacy, never silently drops evidence to fit a fixed item budget.
"""
from __future__ import annotations

from typing import Any
from copy import deepcopy
import hashlib
import json
import re

from .research_source_context import route_repair_observations, source_context, observation_source_record

TOPIC_PARTITION = {
    'business': 'business_disclosures', 'competition': 'business_disclosures',
    'customers': 'business_disclosures', 'segments': 'business_disclosures',
    'management_discussion': 'mda_disclosures', 'growth': 'mda_disclosures',
    'capital_allocation': 'mda_disclosures', 'risk_factors': 'risk_disclosures',
    'audit': 'audit_disclosures',
}
ROLE_REQUIRED = {
    'financial-analyst': ('quantitative_facts',),
    'business-analyst': ('business_disclosures',),
    'growth-opportunity-analyst': ('mda_disclosures',),
    'growth-opportunity-analyst-repair': ('mda_disclosures',),
    'skeptical-analyst': ('risk_disclosures',),
    'accounting-risk-analyst': ('quantitative_facts', 'audit_disclosures'),
    'research-synthesizer': (),
    'research-synthesizer-repair': (),
}


def canonical_request_materials(materials: dict[str, Any], findings: dict[str, Any], *, compiled_sources=None) -> tuple[dict, dict, dict]:
    """One complete copy per source body and dependency object in this wire.

    References resolve to values physically present in this request. Source
    identity, period, currency, units, revisions and conflicting candidates
    remain on each owning record; only byte-identical body strings are shared.
    This is structural interning, never model summarization or text clipping.
    """
    registry: dict[str, Any] = deepcopy(compiled_sources[1]) if compiled_sources is not None else {}
    body_keys = {"raw_text", "excerpt", "source_text", "source_excerpt", "text", "quote"}
    body_counts: dict[str, int] = {}
    def count_bodies(value):
        if isinstance(value, dict):
            source = any(value.get(key) for key in ("evidence_id", "fact_id", "document_id", "source_block_id"))
            for key, child in value.items():
                if source and key in body_keys and isinstance(child, str) and child:
                    body_counts[child] = body_counts.get(child, 0) + 1
                else:
                    count_bodies(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                count_bodies(child)
    if compiled_sources is None:
        count_bodies(materials)
    else:
        body_counts.update({record["text"]: 2 for record in registry.values()})
    # The early source registry belongs solely to the frozen materials.
    # Newly produced findings cannot insert a hash before its stable prefix.
    findings_registry: dict[str, Any] = {}
    def visit(value, *, source_lane=True):
        if isinstance(value, dict):
            source = any(value.get(key) for key in ("evidence_id", "fact_id", "document_id", "source_block_id"))
            result = {}
            for key in sorted(value):
                child = value[key]
                if source and key in body_keys and isinstance(child, str) and (
                        body_counts.get(child, 0) > 1 or len(child.encode("utf-8")) > 256):
                    full_hash = hashlib.sha256(child.encode("utf-8")).hexdigest()
                    identity = "m:" + full_hash[:24]
                    if identity in registry and registry[identity]["text"] != child:
                        identity = "m:" + full_hash  # Never merge a digest-prefix collision.
                    if source_lane:
                        registry.setdefault(identity, {"text": child})
                        result[key] = {"material_ref": identity}
                    elif identity in registry and registry[identity]["text"] == child:
                        result[key] = {"material_ref": identity}
                    else:
                        dynamic_id = "finding:" + full_hash
                        findings_registry.setdefault(dynamic_id, {"text": child})
                        result[key] = {"finding_ref": dynamic_id}
                else:
                    result[key] = visit(child, source_lane=source_lane)
            return result
        if isinstance(value, (list, tuple)):
            return [visit(item, source_lane=source_lane) for item in value]
        return deepcopy(value)

    context = deepcopy(compiled_sources[0]) if compiled_sources is not None else visit(materials)
    dependency = visit(findings, source_lane=False)
    # Legacy aliases for the same dossier cannot multiply entire stage trees.
    # Keep the public field shape, but make its local resolution explicit.
    seen_dependencies = {}
    def alias(value, path):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                encoded = json.dumps(child, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                intern = bool(child) and isinstance(child, (dict, list, str)) and len(encoded.encode("utf-8")) > 128
                reference = {"request_path_ref": seen_dependencies[encoded]} if intern and encoded in seen_dependencies else None
                # An alias longer than its value is inflation, not lossless
                # compression. Keep the complete value in that case.
                if reference is not None and len(json.dumps(reference, separators=(",", ":")).encode()) + 8 < len(encoded.encode("utf-8")):
                    result[key] = reference
                else:
                    if intern:
                        seen_dependencies[encoded] = [*path, key]
                    result[key] = alias(child, [*path, key])
            return result
        if isinstance(value, list):
            return [alias(child, [*path, index]) for index, child in enumerate(value)]
        return value
    if compiled_sources is None:
        late = {key: context.pop(key) for key in ("material_adequacy", "reading_packet") if key in context}
        context = alias(context, ["research_context"])
        context.update(late)
    dependency = alias(dependency, ["prior_artifacts"])
    if findings_registry:
        dependency["findings_registry"] = dict(sorted(findings_registry.items()))
    return context, dependency, dict(sorted(registry.items()))


def question_repair_targets(context: dict[str, Any], agent_id: str) -> list[dict[str, Any]]:
    """Select current-run damaged cells for the question, not the user library.

    Only host-bound original rows qualify. The financial reader may interpret
    these observations but receives no calculator capability or admitted value.
    """
    if agent_id not in {'financial-analyst', 'accounting-risk-analyst'}:
        return []
    company = context.get('company', {})
    if not isinstance(company, dict):
        return []
    identities = {str(company.get(key)) for key in ('cik', 'security_id', 'issuer_id') if company.get(key)}
    observations = context.get('repair_observations', ())
    if not isinstance(observations, (list, tuple)):
        return []
    targets = []
    for item in observations:
        if (not isinstance(item, dict) or item.get('company_cik') not in identities
                or item.get('source_binding') != 'bound' or not item.get('raw_text')
                or not item.get('source_hash') or not item.get('source_bindings')
                or not item.get('fact_id') or not item.get('period_end')):
            continue
        targets.append({key: item[key] for key in ('fact_id', 'company_cik', 'accession_number',
            'period_end', 'scope', 'source_hash') if item.get(key) not in (None, '')})
    return targets


def usable_disclosure(item: dict[str, Any]) -> bool:
    """Body/source identity, not its retrieval label, establishes availability."""
    identity = str(item.get('evidence_id') or '')
    body = str(item.get('excerpt') or item.get('raw_text') or '').strip()
    unit_only = re.fullmatch(
        r'\s*(?:(?:单位|單位|unit|units|currency)\s*[:：]\s*)?'
        r'(?:人民币|人民幣|港币|港幣|美元|CNY|RMB|HKD|USD)\s*'
        r'(?:元|万元|萬元|亿元|億元|千元|百万元|百萬元|thousands?|millions?|billions?)?\s*[。.]?\s*',
        body, flags=re.IGNORECASE)
    heading_only = re.fullmatch(r"\s*(?:[一二三四五六七八九十0-9IVX.、\s]+)?(?:主营业务|主營業務|公司概况|公司概況|"
        r"业务概述|業務概述|审计报告|審計報告|风险因素|風險因素|管理层讨论与分析|管理層討論與分析|"
        r"business(?: overview| review)?|risk factors|audit opinion|independent auditor.?s? report|"
        r"management.?s? discussion and analysis)\s*[:：。.]?\s*", body, flags=re.IGNORECASE)
    return bool(body and item.get('source_url') and item.get('document_id')
                and not identity.startswith(('table:', 'fact:', 'meta:', 'gap:', 'observation:'))
                and item.get('kind') not in {'material_gap', 'financial_fact', 'unverified_visual_observation', 'metadata'}
                and str(item.get('locator', '')).casefold() not in {'metadata', 'filing-metadata'}
                and not unit_only and not heading_only)


def material_capabilities(context: dict[str, Any]) -> set[str]:
    partitions = route_materials(context, '')['evidence_partitions']
    result = {key for key, rows in partitions.items() if rows and key != 'other_disclosures'}
    if any(usable_disclosure(item) for rows in partitions.values() for item in rows):
        result.add('sourced_disclosures')
    selected, _catalog = route_repair_observations(context.get('repair_observations', ()),
        question_repair_targets(context, 'financial-analyst'))
    if any(observation_source_record(item) is not None for item in selected):
        result.add('sourced_financial_observations')
    return result


def has_sourced_disclosure(evidence: list[dict[str, Any]]) -> bool:
    """A gap notice, unit heading or numeric table is not disclosure body.

    Reuse the same role-routing semantics used by model admission instead of
    treating a nonempty evidence catalogue as usable qualitative material.
    """
    partitions = route_materials({'evidence': evidence}, '')['evidence_partitions']
    return any(usable_disclosure(item) for rows in partitions.values() for item in rows)


def route_materials(context: dict[str, Any], agent_id: str) -> dict[str, Any]:
    partitions: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ('quantitative_facts', 'business_disclosures', 'mda_disclosures',
                           'risk_disclosures', 'audit_disclosures', 'other_disclosures')
    }
    # Keep the canonical ledger intact while exposing derived source blocks and
    # repair material through separate, explicit channels.  Repair bodies are
    # never model-visible without a stable caller-supplied target.
    ledger = context.get('evidence', [])
    if not isinstance(ledger, list):
        ledger = []
    structured = source_context(context)
    targets = context.get('repair_targets', ())
    if not isinstance(targets, (list, tuple)):
        targets = ()
    if not targets:
        targets = question_repair_targets(context, agent_id)
    targeted_repairs, repair_catalog = route_repair_observations(
        structured['repair_observations'], targets,
    )
    for item in targeted_repairs:
        record = observation_source_record(item)
        if record is not None:
            item['analysis_source_id'] = record['evidence_id']
    diagnostics = []
    for item in ledger:
        if not isinstance(item, dict):
            continue
        if item.get('kind') == 'material_gap':
            diagnostics.append(item)
            continue
        identity = str(item.get('evidence_id', ''))
        if item.get('kind') == 'financial_fact' or identity.startswith('fact:'):
            bucket = 'quantitative_facts'
        else:
            topic = str(item.get('topic') or str(item.get('title', '')).split(' · ')[-1])
            # A topic label alone, a table cell or a source-less model narrative
            # cannot establish presence of an official disclosure body.
            bucket = TOPIC_PARTITION.get(topic, 'other_disclosures') if usable_disclosure(item) else 'other_disclosures'
        partitions[bucket].append(item)
    required = ROLE_REQUIRED.get(agent_id, ())
    topic_gaps = [key for key in required if not partitions[key]]
    disclosed = any(usable_disclosure(item) for rows in partitions.values() for item in rows)
    if agent_id == 'financial-analyst':
        missing = [] if partitions['quantitative_facts'] or targeted_repairs else ['quantitative_facts']
    elif agent_id == 'accounting-risk-analyst':
        missing = [] if partitions['quantitative_facts'] or disclosed else ['quantitative_facts', 'audit_disclosures']
    elif required:
        missing = [] if disclosed else list(required)
    else:
        missing = []
    if agent_id in {'research-synthesizer', 'research-synthesizer-repair', 'forecast-analyst'} and not any(
            partitions[key] for key in ('quantitative_facts',)) and not disclosed:
        missing.append('quantitative_facts')
    priority = list(dict.fromkeys((*required, *partitions)))
    return {
        **{
            key: value for key, value in context.items()
            if key not in {
                'evidence', 'repair_observations', 'repair_targets',
                'accepted_facts', 'source_blocks', 'repair_catalog',
                'targeted_repair_observations', 'repair_retrieval',
            }
        },
        # The original evidence ledger remains complete and independently
        # usable by later validators; source blocks are a derived view only.
        'evidence': list(ledger),
        'accepted_facts': structured['accepted_facts'],
        'source_blocks': structured['source_blocks'],
        'repair_observations': targeted_repairs,
        'repair_catalog': repair_catalog,
        'repair_retrieval': {
            'required': bool(structured['repair_observations']) and not bool(targets),
            'targets': [dict(item) for item in targets if isinstance(item, dict)],
            'selected_count': len(targeted_repairs),
        },
        'evidence_partitions': {key: partitions[key] for key in priority},
        'material_adequacy': {
            'policy_version': 'research-materials-v2-source-body', 'agent_id': agent_id,
            'status': 'insufficient' if missing else 'ready',
            'required': list(required), 'missing': missing,
            'retrieval_topic_gaps': topic_gaps,
            'diagnostics': diagnostics,
            'counts': {key: len(items) for key, items in partitions.items()},
            'coverage_mode': 'topic_excerpts_not_full_document',
            'source_interpretation_only': bool(targeted_repairs) and not bool(partitions['quantitative_facts']),
            'conclusion_scope': 'disclose_missing_material_only' if missing else 'supplied_material_only',
            'instruction': 'Analyze only supplied excerpts, including uncategorized official body text. Topic labels are retrieval hints, not evidence of relevance. Every assertion must be supported by its actual source. Material presence is not a claim that a full filing was read. Never substitute numeric cells, issuer name or model memory for missing disclosure text.',
        },
    }


def prompt_materials(context: dict[str, Any], agent_id: str, *, required_evidence_ids=()) -> dict[str, Any]:
    """One canonical body per source, with lossless typed-lane references.

    The validator's ledger remains unchanged. Prompt routing must not repeat
    every report in evidence, partitions and source blocks: references point
    to the complete records in evidence_partitions, not a summary or truncation.
    """
    routed = route_materials(context, agent_id)
    from .research_execution.reading_view import model_reading_view
    routed = model_reading_view(routed, agent_id, required_evidence_ids=required_evidence_ids)
    routed.pop('facts', None)  # identical admitted values live in accepted_facts
    # Publication generation is a local database lease, not source evidence.
    # It remains in the frozen host snapshot but cannot churn an identical
    # source/value prompt prefix every time the same financials are republished.
    routed['accepted_facts'] = [{key: value for key, value in fact.items() if key != 'generation_id'}
        if isinstance(fact, dict) else fact for fact in routed['accepted_facts']]
    facts_by_id = {}
    ambiguous = set()
    for fact in routed['accepted_facts']:
        if not isinstance(fact, dict) or not isinstance(fact.get('fact_id'), str):
            continue
        identity = fact['fact_id']
        if identity in facts_by_id and facts_by_id[identity] != fact:
            ambiguous.add(identity)
        facts_by_id[identity] = fact
    for partition, records in routed['evidence_partitions'].items():
        referred = []
        for record in records:
            if record.get('kind') == 'financial_fact':
                record = {key: value for key, value in record.items() if key != 'generation_id'}
            identity = str(record.get('evidence_id', '')).removeprefix('fact:')
            fact = facts_by_id.get(identity)
            if record.get('kind') == 'financial_fact' and fact is not None and identity not in ambiguous:
                # Reversible field inheritance, never a semantic merge of two
                # amounts/periods. Retain every differing field as an override.
                record = {'accepted_fact_ref': identity, **{key: value for key, value in record.items()
                    if key not in fact or json.dumps(value, ensure_ascii=False, sort_keys=True)
                    != json.dumps(fact[key], ensure_ascii=False, sort_keys=True)}}
            referred.append(record)
        routed['evidence_partitions'][partition] = referred
    routed['evidence'] = [item['evidence_id'] for item in routed['evidence']
        if isinstance(item, dict) and isinstance(item.get('evidence_id'), str)]
    routed['source_blocks'] = [{
        **{key: value for key, value in block.items() if key not in {'raw_text', 'evidence_id'}},
        'evidence_ref': block.get('evidence_id'),
        'body_from': 'evidence_partitions',
    } for block in routed['source_blocks']]
    routed['material_reference_contract'] = {
        'version': 'canonical-source-registry-v1',
        'instruction': 'Resolve accepted_fact_ref by exact fact_id in accepted_facts in THIS request and overlay all fields on the referring evidence record. This restores its complete financial_fact with the original evidence_id and source attribution. evidence and source_blocks refer to evidence_partitions, including original raw_text/excerpt. Follow references; they do not denote missing or summarized source material.',
    }
    return routed


def insufficient_material_result(gate: dict[str, Any], language: str) -> dict[str, Any]:
    labels = {
        'quantitative_facts': ('financial facts', '财务事实', '財務事實'),
        'business_disclosures': ('business disclosures', '主营业务披露', '主營業務披露'),
        'mda_disclosures': ('management discussion', '管理层讨论', '管理層討論'),
        'risk_disclosures': ('risk disclosures', '风险披露', '風險披露'),
        'audit_disclosures': ('audit disclosures', '审计披露', '審計披露'),
    }
    index = 0 if language == 'en' else 2 if language == 'zh-Hant' else 1
    missing = ', '.join(labels[key][index] for key in gate['missing'])
    message = (f'Required official material is missing: {missing}; this role has not formed a conclusion.' if index == 0
               else f'缺少必要的官方披露正文或事實：{missing}；此角色的結論尚未形成。' if index == 2
               else f'缺少必要的官方披露正文或事实：{missing}；此角色的结论尚未形成。')
    return {'kind': 'unknown', 'claims': [], 'opportunities': [], 'information_gaps': [message],
            'unresolved_questions': [message], '_material_adequacy': gate,
            '_response_error': 'insufficient_material', '_verification_state': 'failed_verification'}
