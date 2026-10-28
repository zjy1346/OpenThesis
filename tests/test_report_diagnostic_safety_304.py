from openthesis.service import _report_projection_failure


def test_projection_diagnostic_never_exposes_exception_body_or_locals():
    secret = "api-key-private-prompt-and-C:/Users/private"
    try:
        raise ValueError(secret)
    except ValueError as error:
        ordinary = _report_projection_failure(error)
        technical = _report_projection_failure(error, technical=True)
    assert ordinary == {"code": "REPORT_PROJECTION_FAILED", "location": "report"}
    assert technical["exception_type"] == "ValueError"
    assert secret not in str(technical)
    assert "Users" not in str(technical)
    assert len(technical["frames"]) <= 512
