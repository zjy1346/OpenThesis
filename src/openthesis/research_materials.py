"""Role-specific primary material contracts, shared by prompts and output gates.

The canonical ledger stays complete. Role routing changes presentation and
records adequacy, never silently drops evidence to fit a fixed item budget.
"""
from __future__ import annotations

from typing import Any

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
    'research-synthesizer': ('quantitative_facts', 'business_disclosures', 'mda_disclosures', 'risk_disclosures'),
    'research-synthesizer-repair': ('quantitative_facts', 'business_disclosures', 'mda_disclosures', 'risk_disclosures'),
}


def route_materials(context: dict[str, Any], agent_id: str) -> dict[str, Any]:
    partitions: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ('quantitative_facts', 'business_disclosures', 'mda_disclosures',
                           'risk_disclosures', 'audit_disclosures', 'other_disclosures')
    }
    # Accept already-routed context without duplicating its ledger.
    ledger = context.get('evidence', [])
    if not isinstance(ledger, list):
        ledger = []
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
            body = str(item.get('excerpt') or item.get('raw_text') or '').strip()
            # A topic label alone, a table cell or a source-less model narrative
            # cannot establish presence of an official disclosure body.
            bucket = TOPIC_PARTITION.get(topic, 'other_disclosures') if (
                body and item.get('source_url') and item.get('document_id')
                and not identity.startswith('table:')
            ) else 'other_disclosures'
        partitions[bucket].append(item)
    required = ROLE_REQUIRED.get(agent_id, ())
    missing = [key for key in required if not partitions[key]]
    priority = list(dict.fromkeys((*required, *partitions)))
    return {
        **{key: value for key, value in context.items() if key != 'evidence'},
        'evidence_partitions': {key: partitions[key] for key in priority},
        'material_adequacy': {
            'policy_version': 'research-materials-v1', 'agent_id': agent_id,
            'status': 'insufficient' if missing else 'ready',
            'required': list(required), 'missing': missing,
            'diagnostics': diagnostics,
            'counts': {key: len(items) for key, items in partitions.items()},
            'coverage_mode': 'topic_excerpts_not_full_document',
            'conclusion_scope': 'disclose_missing_material_only' if missing else 'supplied_material_only',
            'instruction': 'Analyze only supplied excerpts. Material presence is not a claim that a full filing was read. Never substitute numeric cells, issuer name or model memory for missing disclosure text.',
        },
    }


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
