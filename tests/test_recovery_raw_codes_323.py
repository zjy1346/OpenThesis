from types import SimpleNamespace

import pytest

from openthesis.financial_recovery import FinancialRecoveryController


@pytest.mark.parametrize(
    "raw_code",
    [
        "FINANCIAL_VALIDATION_REVIEW_REQUIRED",
        "MODEL_RUN_VERIFICATION_BUDGET_EXHAUSTED",
    ],
)
def test_unclassified_validation_and_budget_codes_keep_raw_code(raw_code):
    classification = FinancialRecoveryController.classify_error(raw_code, stage="validation")

    assert classification == "UNCLASSIFIED_ERROR"
    assert FinancialRecoveryController.raw_error_code(raw_code) == raw_code


def test_scope_confirmation_code_is_not_classified_as_a_filing_fetch_failure():
    raw_code = "FILING_RESEARCH_SCOPE_CONFIRMATION_REQUIRED"

    assert FinancialRecoveryController.classify_error(raw_code) == raw_code
    assert FinancialRecoveryController.raw_error_code(raw_code) == raw_code


def test_raw_code_is_preserved_exactly_when_read_from_an_exception():
    raw_code = "Model_Run_Custom_Budget_Code"
    error = SimpleNamespace(code=raw_code)

    assert FinancialRecoveryController.raw_error_code(error) == raw_code
    assert FinancialRecoveryController.classify_error(error) == "UNCLASSIFIED_ERROR"


def test_public_service_export_keeps_raw_code_separate_from_category(tmp_path, monkeypatch):
    from openthesis.service import AppService
    import openthesis.service as module
    from test_assertion_retention_312 import setup
    journal = setup(tmp_path)
    service = AppService(tmp_path)
    code = "MODEL_RUN_CUSTOM_VALIDATION_REQUIRED"
    monkeypatch.setattr(module, "_financial_status", lambda *args, **kwargs: {
        "recovery_cases": [{"error_code": code, "accession_number": "public-source", "stage": "validation"}]})
    row = service.financial_diagnostics(journal.run_id)["recovery_cases"][0]
    assert row["error_code"] == row["raw_error_code"] == code
    assert row["error_category"] == "UNCLASSIFIED_ERROR"
