from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.research_execution.requests import ModelRequestJournal
from openthesis.domain import ResearchRun
from openthesis.demo import DEMO_COMPANY
from openthesis.storage import Storage


def registry():
    return StageRegistry([
        StageSpec('a', '1', 'produce', 'verify', output_contract='analysis'),
        StageSpec('b', '1', 'produce', 'verify', ('a',), output_contract='analysis'),
        StageSpec('independent', '1', 'produce', 'verify', output_contract='analysis'),
    ])


def execute(value='unchanged', previous=None, fingerprints=None, invalidated=()):
    calls = []
    def produce(spec, inputs, dependencies):
        calls.append(spec.stage_id)
        return {'body': value} if spec.stage_id == 'a' else {'body': spec.stage_id, 'inputs': dependencies}
    executor = StageExecutor({'produce': produce}, {'verify': lambda _s, candidate, _i:
        StageValidation(True, candidate, 'completed_verified')})
    result = executor.execute(registry(), {}, previous=previous, execution_fingerprint='model-1',
        execution_fingerprints=fingerprints, invalidated=invalidated, propagate_invalidation=False)
    return result, calls


def test_local_model_change_with_unchanged_trusted_body_does_not_rerun_other_nodes():
    first, _ = execute()
    result, calls = execute(previous=first, fingerprints={'a': 'model-2', 'b': 'model-1', 'independent': 'model-1'},
        invalidated=('a',))
    assert calls == ['a']
    assert result['a'].execution_fingerprint == 'model-2'
    assert result['b'].candidate == first['b'].candidate
    assert result['independent'].input_digest == first['independent'].input_digest


def test_local_model_change_with_changed_body_invalidates_actual_consumer_only():
    first, _ = execute()
    result, calls = execute(value='new trusted body', previous=first,
        fingerprints={'a': 'model-2', 'b': 'model-1', 'independent': 'model-1'}, invalidated=('a',))
    assert calls == ['a', 'b']
    assert result['b'].candidate['inputs']['a']['body'] == 'new trusted body'
    assert result['independent'].candidate == first['independent'].candidate


def test_journal_changes_only_selected_stage_head_and_retains_other_config_identity(tmp_path):
    store = Storage(tmp_path)
    store.save_company(DEMO_COMPANY)
    store.save_run(ResearchRun('local-model-run', DEMO_COMPANY, 'workflow', 'pack', '1',
        'offline', 'model-1', '2025-12-31'))
    first = ModelRequestJournal(store, 'local-model-run', 'model-1', timeout_seconds=2, cancel_check=lambda: False)
    initial = first.prepare(registry(), 'material')
    changed = ModelRequestJournal(store, 'local-model-run', 'model-2', timeout_seconds=2, cancel_check=lambda: False)
    heads = changed.prepare(registry(), 'material', ('a',),
        stage_configurations={'a': 'model-2', 'b': 'model-1', 'independent': 'model-1'})
    assert heads == {'a': initial['a'] + 1, 'b': initial['b'], 'independent': initial['independent']}
