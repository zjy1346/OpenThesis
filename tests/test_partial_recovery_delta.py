from copy import deepcopy

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchRun, RunStatus
from openthesis.research import _supported_synthesis_sections
from openthesis.research_execution.durable import DurableStageVerifier
from openthesis.research_execution.executor import StageResult, StageValidation
from openthesis.research_execution.registry import StageSpec
from openthesis.resilience.contracts import digest
from openthesis.safe_report import SafeReportAssembler
from openthesis.storage import Storage


def setup(store):
    store.save_company(DEMO_COMPANY)
    store.save_run(ResearchRun('partial-delta', deepcopy(DEMO_COMPANY), 'wf', 'pack', '1',
                   'offline', 'fixture', '2025-12-31', RunStatus.RUNNING))


def test_independent_partial_nodes_commit_with_remaining_issues_and_read_after_restart(tmp_path):
    store = Storage(tmp_path)
    setup(store)
    spec = StageSpec('final-synthesis', 'v1', 'produce', 'verify')
    candidate = {'business_model': {'analysis': 'Operations serve industrial customers.',
                                   'evidence_ids': ['filing:business']},
                 'financial_quality': {'analysis': 'Revenue was USD 999 billion.',
                                       'evidence_ids': ['filing:business']}, 'claims': []}
    records = {'filing:business': {'kind': 'filing_text', 'evidence_id': 'filing:business',
        'company_cik': DEMO_COMPANY.cik, 'end_date': '2025-12-31', 'fiscal_year': 2025,
        'fiscal_period': 'FY', 'excerpt': 'Operations serve industrial customers.'}}
    identity = digest({'frozen_source': 'fixture'})
    calls = []

    def verify_local(value):
        calls.append('local-validation')
        safe, audit = _supported_synthesis_sections(value, set(records), records, 'en')
        assert 'financial_quality' not in safe
        return StageResult(spec.stage_id, spec.identity, identity, value,
            StageValidation(False, safe, 'completed_partial', ('unproved-financial-quality',),
                            {'verification': {'section_validation': audit}}),
            digest(value), execution_fingerprint='offline-model', has_candidate=True)

    result = DurableStageVerifier(store, 'partial-delta')(spec, identity, candidate, 'offline-model', verify_local)
    assert result.usable and not result.validation.passed
    with store.connect() as db:
        state = db.execute('SELECT state,outcome_json FROM recovery_plans').fetchone()
        assert state['state'] == 'partial_committed'
        import json
        outcome = json.loads(state['outcome_json'])
        assert outcome['accepted_delta'] and outcome['remaining_issue_ids']
        assert not outcome['resolved_issue_ids']
        assert outcome['next_actions']
    reopened = Storage(tmp_path)
    artifacts = reopened.get_artifacts('partial-delta')
    assert sum(row['artifact_type'] == 'stage-checkpoint' for row in artifacts) == 1
    assert sum(row['artifact_type'] == 'trusted-content-snapshot' for row in artifacts) == 1
    readable = SafeReportAssembler().assemble(run_id='partial-delta', company_name='Fixture', status='running',
        artifacts=artifacts, continuity={}, language='en', renderer_error='injected-renderer-failure')
    assert 'Operations serve industrial customers.' in readable.markdown
    assert 'Operations serve industrial customers.' in readable.html
    assert 'Revenue was USD 999 billion.' not in readable.markdown
    assert readable.nodes[0]['evidence_ids'] == ['filing:business']
    # An unchanged incident has no new information and must not produce or
    # commit another checkpoint. The saved candidate can be checked locally.
    again = DurableStageVerifier(reopened, 'partial-delta')(spec, identity, candidate, 'offline-model', verify_local)
    assert again.usable and not again.validation.passed
    assert len(reopened.get_artifacts('partial-delta')) == len(artifacts)
    assert len(calls) == 2
