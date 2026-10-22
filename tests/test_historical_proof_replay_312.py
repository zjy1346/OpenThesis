from copy import deepcopy

from openthesis.research_assertions import AssertionSession, VERSION, validate_response
from openthesis.report_content import _replay_saved_assertion_proofs
from test_assertion_retention_312 import SOURCE


def test_host_saved_judgment_replay_restores_exact_body_but_never_rewritten_or_new_source():
    text = 'Equipment sales are the core operating activity.'
    session = AssertionSession()
    session.classify({'kind': 'inference', 'text': text}, [SOURCE])
    query = next(iter(session.queries.values()))
    proof = validate_response({'structured_output_valid': True, 'results': [{
        'assertion_id': query['assertion_id'], 'state': 'supported',
        'source_spans': [{'source_block_id': query['sources'][0]['source_block_id'],
                         'quote': SOURCE['raw_text']}]}]}, [query], 'offline-saved-host-check')
    node = {'role': 'analysis', 'validation_state': 'partial', 'body': text,
        'source_path': 'business_model.analysis', 'evidence_ids': [SOURCE['evidence_id']],
        'body_sha256': 'test-body', 'validator_version': 'test-version', 'context_sha256': 'test-context',
        'depends_on': [SOURCE['evidence_id']], 'node_id': 'original-node', 'issue_code': ''}
    artifacts = [{'artifact_type': 'stage-checkpoint', 'content': {'result': {'validation': {
        'record': {'assertion_protocol': VERSION, 'assertion_results': list(proof.values())}}}}}]
    before = deepcopy(node)
    recovered = _replay_saved_assertion_proofs((node,), artifacts, [SOURCE])[0]
    assert recovered['validation_state'] == 'verified'
    assert recovered['role'] == 'verified_analysis'
    assert recovered['body'] == text
    assert node == before
    assert _replay_saved_assertion_proofs(({**node, 'body': text + ' Insurance is also a major activity.'},),
        artifacts, [SOURCE])[0]['validation_state'] == 'partial'
    assert _replay_saved_assertion_proofs((node,), artifacts, [{**SOURCE, 'raw_text': 'The business ceased manufacturing.'}])[0]['validation_state'] == 'partial'
    assert _replay_saved_assertion_proofs((node,), [{'artifact_type': 'stage-candidate',
        'content': artifacts[0]['content']}], [SOURCE])[0]['validation_state'] == 'partial'
