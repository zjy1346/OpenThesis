from openthesis.providers import RustModelGatewayProvider, ModelConfig


def test_output_forecast_uses_observed_reasoning_contract_without_disabling_it():
    provider = RustModelGatewayProvider(ModelConfig(configured_model_id="fixture"))
    assert provider.output_allowance(4096, 1024) == 5120
    provider.observe_output_usage(output_tokens=10000, reasoning_tokens=8000, reasoning_in_output=True)
    assert provider.output_allowance(4096, 1024) == 20480


def test_separate_reasoning_usage_and_unknown_contract_are_not_conflated():
    provider = RustModelGatewayProvider(ModelConfig(configured_model_id="fixture"))
    provider.observe_output_usage(output_tokens=2000, reasoning_tokens=8000, reasoning_in_output=False)
    assert provider.output_allowance(4096, 1024) == 20480
    other = RustModelGatewayProvider(ModelConfig(configured_model_id="another-route"))
    other.observe_output_usage(output_tokens=10000, reasoning_tokens=8000)
    assert other.output_allowance(4096, 1024) == 5120


def test_no_visible_answer_still_increases_the_next_distinct_work_allowance():
    provider = RustModelGatewayProvider(ModelConfig(configured_model_id="fixture"))
    provider.observe_output_usage(output_tokens=6000, reasoning_tokens=6000, reasoning_in_output=True)
    assert provider.output_allowance(4096, 1024) == 10096
