"""A damaged attempt cannot shadow an earlier proof for the same input."""
from copy import deepcopy

from openthesis.domain import ResearchArtifact
from openthesis.report_content import read_trusted_snapshots
from openthesis.storage import Storage
from test_trusted_emergency_content import seed, commit_report


def _stage(artifact, artifact_id, input_digest):
    report = artifact.content['report']
    proof = artifact.content['verification']['section_validation']
    return ResearchArtifact(artifact_id, artifact.run_id, 'stage-checkpoint', 'Business', {
        'result': {'stage_id': 'business-analyst', 'input_digest': input_digest,
            'validation': {'state': 'completed_verified', 'trusted_output': deepcopy(report),
                'record': {'verification': {'section_validation': deepcopy(proof)}}}}})


def _corrupt_snapshot(artifacts, artifact_id):
    snapshot = next(row for row in artifacts if row['artifact_type'] == 'trusted-content-snapshot'
        and row['content']['source_artifact_id'] == artifact_id)
    snapshot['content']['nodes'][0]['minimal_content']['text'] = 'Unproven changed content.'


def test_corrupt_latest_stage_snapshot_keeps_prior_valid_same_input(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    artifact = commit_report(store)
    store.save_artifact(_stage(artifact, 'stage:good', 'same-stage-input'))
    store.save_artifact(_stage(artifact, 'stage:damaged', 'same-stage-input'))
    artifacts = Storage(tmp_path).get_artifacts(artifact.run_id)
    _corrupt_snapshot(artifacts, 'stage:damaged')
    # Exclude the report so this asserts the stage-head behavior alone.
    artifacts = [row for row in artifacts if row['artifact_type'] != 'research-report'
        and row.get('content', {}).get('target') != 'report']
    nodes = read_trusted_snapshots(artifacts, run_id=artifact.run_id)
    assert len(nodes) == 1
    assert nodes[0]['source_artifact_id'] == 'stage:good'
    assert nodes[0]['body'] == 'Operations serve industrial customers.'


def test_corrupt_changed_stage_input_does_not_reuse_older_proof(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    artifact = commit_report(store)
    store.save_artifact(_stage(artifact, 'stage:old', 'old-input'))
    store.save_artifact(_stage(artifact, 'stage:new', 'new-input'))
    artifacts = Storage(tmp_path).get_artifacts(artifact.run_id)
    _corrupt_snapshot(artifacts, 'stage:new')
    artifacts = [row for row in artifacts if row['artifact_type'] != 'research-report'
        and row.get('content', {}).get('target') != 'report']
    assert not read_trusted_snapshots(artifacts, run_id=artifact.run_id)


def test_corrupt_report_snapshot_reuses_only_same_explicit_publication_input(tmp_path):
    store = Storage(tmp_path)
    seed(store)
    artifact = commit_report(store)
    for artifact_id, input_digest in (
            ('report:same-good', 'same-input'), ('report:same-damaged', 'same-input')):
        store.save_artifact(ResearchArtifact(artifact_id, artifact.run_id, 'research-report', 'Attempt',
            {**deepcopy(artifact.content), 'publication_input_digest': input_digest}))
    artifacts = Storage(tmp_path).get_artifacts(artifact.run_id)
    _corrupt_snapshot(artifacts, 'report:same-damaged')
    nodes = read_trusted_snapshots(artifacts, run_id=artifact.run_id)
    assert len(nodes) == 1 and nodes[0]['source_artifact_id'] == 'report:same-good'
    store.save_artifact(ResearchArtifact('report:changed-input', artifact.run_id, 'research-report', 'Attempt',
        {**deepcopy(artifact.content), 'publication_input_digest': 'different-input'}))
    artifacts = Storage(tmp_path).get_artifacts(artifact.run_id)
    _corrupt_snapshot(artifacts, 'report:changed-input')
    assert not read_trusted_snapshots(artifacts, run_id=artifact.run_id)


def test_partial_attempt_for_same_input_preserves_independent_prior_nodes(tmp_path):
    """An omitted sibling is not a source correction or revocation."""
    from openthesis.research import _supported_synthesis_sections
    store = Storage(tmp_path)
    seed(store)
    artifact = commit_report(store)
    content = deepcopy(artifact.content)
    content['report']['unresolved_questions'] = [
        {'kind': 'question', 'text': 'What drives customer demand?'}]
    records = {'filing:business': {'kind': 'filing_text', 'company_cik': '0000320193',
        'end_date': '2025-12-31', 'fiscal_year': 2025, 'fiscal_period': 'FY',
        'excerpt': 'Operations serve industrial customers.'}}
    safe, proof = _supported_synthesis_sections(content['report'], set(records), records, 'en')
    older = ResearchArtifact('report:two-independent', artifact.run_id, 'research-report', 'Two',
        {'report': safe, 'verification': {'section_validation': proof}})
    store.save_artifact(_stage(older, 'stage:two-independent', 'same-stage-input'))
    store.save_artifact(_stage(artifact, 'stage:one-only', 'same-stage-input'))
    artifacts = [row for row in Storage(tmp_path).get_artifacts(artifact.run_id)
        if row['artifact_type'] != 'research-report' and row.get('content', {}).get('target') != 'report']
    nodes = read_trusted_snapshots(artifacts, run_id=artifact.run_id)
    assert {node['body'] for node in nodes} == {
        'Operations serve industrial customers.', 'What drives customer demand?'}
    assert next(node for node in nodes if node['body'].startswith('Operations'))[
        'source_artifact_id'] == 'stage:one-only'
    assert next(node for node in nodes if node['body'].startswith('What'))[
        'source_artifact_id'] == 'stage:two-independent'
    from openthesis.service import AppService
    from openthesis.safe_report import SafeReportAssembler
    formal = AppService(tmp_path, provider_factory=lambda _: None).get_report(artifact.run_id, language='en')
    emergency = SafeReportAssembler().assemble(run_id=artifact.run_id, company_name='Fixture',
        status='partial', artifacts=Storage(tmp_path).get_artifacts(artifact.run_id),
        continuity={}, language='en')
    for body in ('Operations serve industrial customers.', 'What drives customer demand?'):
        assert body in formal['markdown'] and body in formal['html']
        assert body in emergency.markdown and body in emergency.html
    assert formal['research_complete'] is False
