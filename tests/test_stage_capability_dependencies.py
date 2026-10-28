from dataclasses import replace

from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec, standard_model_registry


def verified(_spec, value, _inputs):
    return StageValidation(True, value, 'completed_verified')


def test_optional_context_failure_settles_without_blocking_independent_producer():
    a = StageSpec('a', '1', 'produce', 'verify', output_contract='analysis')
    b = StageSpec('b', '1', 'produce', 'verify', ('a',), output_contract='analysis',
                  optional_dependencies=('a',), required_capabilities=('business_disclosures',))
    calls = []
    def produce(spec, inputs, dependencies):
        calls.append(spec.stage_id)
        if spec.stage_id == 'a':
            raise RuntimeError('injected provider failure')
        assert dependencies['a'] == {}
        assert dependencies['_dependency_gaps'][0]['stage_id'] == 'a'
        assert 'candidate' not in str(dependencies)
        return {'analysis': 'Independent source-bound analysis.'}
    results = StageExecutor({'produce': produce}, {'verify': verified}).execute(
        StageRegistry((a, b)), {'capabilities': ['business_disclosures']})
    assert calls == ['a', 'b']
    assert results['b'].usable


def test_missing_hard_input_capability_never_calls_producer():
    spec = StageSpec('a', '1', 'produce', 'verify', output_contract='analysis',
                     required_capabilities=('quantitative_facts',))
    calls = []
    result = StageExecutor({'produce': lambda *args: calls.append(args) or {}}, {'verify': verified}).execute(
        StageRegistry((spec,)), {'capabilities': ['business_disclosures']})
    assert not calls
    assert result['a'].error_code == 'STAGE_INPUT_CAPABILITY_UNAVAILABLE'


def test_official_graph_keeps_optional_views_but_requires_real_material():
    called = []
    def produce(spec, inputs, dependencies):
        called.append(spec.stage_id)
        if spec.stage_id == 'financial-analyst':
            raise RuntimeError('injected failure')
        return {'analysis': 'Independently verified disclosure analysis.'}
    registry = standard_model_registry()
    inputs = {'capabilities': ['company_identity', 'quantitative_facts', 'sourced_disclosures',
                               'business_disclosures', 'mda_disclosures', 'risk_disclosures',
                               'audit_disclosures']}
    result = StageExecutor({'model-analysis': produce}, {s.validator_id: verified for s in registry.stages}).execute(
        registry, inputs, max_workers=2)
    assert set(called) == {s.stage_id for s in registry.stages}
    assert result['final-synthesis'].usable
    assert result['financial-analyst'].validation.trusted_output == {}


def test_optional_partial_context_contains_only_validator_trusted_subset():
    spec = StageSpec('a', '1', 'produce', 'verify', output_contract='analysis')
    downstream = replace(spec, stage_id='b', depends_on=('a',), optional_dependencies=('a',))
    def verify(spec, candidate, inputs):
        return StageValidation(False, {'safe': 'body'}, 'completed_partial') if spec.stage_id == 'a' else verified(spec, candidate, inputs)
    def produce(spec, inputs, deps):
        if spec.stage_id == 'b':
            assert deps['a'] == {'safe': 'body'}
            assert 'unsafe' not in str(deps)
        return {'unsafe': 'raw candidate'}
    result = StageExecutor({'produce': produce}, {'verify': verify}).execute(StageRegistry((spec, downstream)), {})
    assert result['b'].usable
