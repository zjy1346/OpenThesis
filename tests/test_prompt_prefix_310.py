import json
from openthesis.research import ResearchContext, _agent_prompt_bundle
from openthesis.demo import DEMO_COMPANY, demo_facts


def _resolve_request_path(payload, path):
    value = payload
    for component in path:
        if isinstance(value, dict) and value.get("record_table") == "columns-data-v1":
            from openthesis.research_execution.source_compiler import expand_record_tables
            value = expand_record_tables(value)
        value = value[component]
    return value


def test_public_prefix_is_stable_without_removing_data_or_cutoff():
    context = ResearchContext(DEMO_COMPANY, [], [], [], [], research_as_of="2026-10-16")
    first = json.loads(context.compact_json())
    first["company"] = {**first["company"], "extension": {"z":3,"a":{"y":2,"b":1}}}
    second = {**first, "company": {**first["company"], "extension": {"a":{"b":1,"y":2},"z":3}}}
    prompts = [_agent_prompt_bundle("business-analyst", "role", "en", json.dumps(value), {})
               for value in (first, second)]
    assert prompts[0] == prompts[1]
    system, user = prompts[0]
    data = json.loads(user)
    assert data["research_context"]["research_as_of"] == "2026-10-16"
    assert data["research_context"]["company"]["extension"] == first["company"]["extension"]
    assert user.index('"claim_contract"') < user.index('"research_context"') < user.index('"agent"')
    assert data["task_instructions"] == "role"


def test_valuation_gap_uses_latest_annual_inputs_and_not_false_missing_capex():
    context = ResearchContext(DEMO_COMPANY, [], [
        {"year":2023,"operating_cash_flow":10},
        {"year":2025,"operating_cash_flow":20,"capital_expenditure":5,"free_cash_flow":15}], [], [],
        valuation={"status":"ok","equity_market_value":100,"base_free_cash_flow":15})
    gaps = context.valuation_gaps()
    assert gaps["reasons"] == []
    assert set(gaps["available_financial_inputs"]) == {"operating_cash_flow","capital_expenditure","free_cash_flow"}


def test_shared_primary_material_precedes_role_suffix_for_independent_financial_agents():
    from os.path import commonprefix
    original = "Public filing evidence with exact amounts, periods and sources. " * 100
    evidence = [{"evidence_id": f"filing:{index}", "document_id": f"original:{index}",
        "source_url": "https://example.test/annual", "title": "Annual · audit",
        "raw_text": original, "excerpt": original} for index in (1, 2)]
    context = ResearchContext(DEMO_COMPANY, demo_facts(), [], [], evidence)
    first = _agent_prompt_bundle("financial-analyst", "Financial instructions", "en", context.compact_json(), {})
    second = _agent_prompt_bundle("accounting-risk-analyst", "Independent accounting instructions", "en", context.compact_json(), {})
    assert first[0] == second[0]
    shared = commonprefix([first[1], second[1]])
    assert original in shared
    assert first[1].count(original) == second[1].count(original) == 1
    first_payload = json.loads(first[1])
    second_payload = json.loads(second[1])
    first_context = first_payload["research_context"]
    second_context = second_payload["research_context"]
    assert first_payload["task_instructions"] != second_payload["task_instructions"]
    for key in ("material_adequacy", "reading_packet"):
        assert first_context[key] == second_context[key] == {
            "request_path_ref": ["reading_assignment", key]
        }
    financial_assignment = first_payload["reading_assignment"]
    accounting_assignment = second_payload["reading_assignment"]
    assert financial_assignment["material_adequacy"]["agent_id"] == "financial-analyst"
    assert accounting_assignment["material_adequacy"]["agent_id"] == "accounting-risk-analyst"
    assert financial_assignment["reading_packet"]["reader"] == "financial-analyst"
    assert accounting_assignment["reading_packet"]["reader"] == "accounting-risk-analyst"
    # Shared source bytes are identical until the role-specific agent field;
    # its independent instruction and metadata remain in the request tail.
    first_agent = first[1].index('"agent":')
    second_agent = second[1].index('"agent":')
    assert first[1][:first_agent] == second[1][:second_agent]
    assert first[1].index('"reading_assignment":') > first_agent
    assert second[1].index('"reading_assignment":') > second_agent
    assert _resolve_request_path(first_payload, first_context["material_adequacy"]["request_path_ref"]) == financial_assignment["material_adequacy"]
    assert _resolve_request_path(second_payload, second_context["reading_packet"]["request_path_ref"]) == accounting_assignment["reading_packet"]
