from __future__ import annotations

from io import BytesIO
import json
import zipfile
import unittest
from pypdf import PdfWriter

from openthesis.domain import Company, FilingDocument
from openthesis.vision_financials import (
    CustomVisionAdapter,
    MineruLiteAdapter,
    MineruPrecisionAdapter,
    VisionFallbackConfig,
    VisionHttpResponse,
    VisionPageRequest,
    parse_vision_markdown,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, timeout=60.0):
        self.calls.append((method, url, headers or {}, body or b""))
        response = self.responses.pop(0)
        return response


def _company() -> Company:
    return Company("HK:SEHK:00700", "00700.HK", "Tencent", "SEHK", market="HK", reporting_currency="CNY")


def _filing() -> FilingDocument:
    return FilingDocument("hkex:vision", _company().security_id, "vision", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-04-01", "2025 Annual Report", "https://example.invalid/report.pdf")


def _page() -> VisionPageRequest:
    return VisionPageRequest(130, b"opaque-page-bytes")


class VisionFinancialTests(unittest.TestCase):
    def test_disabled_or_missing_consent_does_not_call_transport(self):
        transport = FakeTransport([])
        adapter = MineruLiteAdapter(transport, sleep=lambda _: None)
        result = adapter.extract(_company(), _filing(), [_page()], VisionFallbackConfig(enabled=True, consent=False))
        self.assertEqual(result.error_code, "VISION_CONSENT_REQUIRED")
        self.assertEqual(transport.calls, [])

    def test_lite_signed_upload_poll_and_markdown_are_in_memory(self):
        transport = FakeTransport([
            VisionHttpResponse(200, json.dumps({"data": {"task_id": "task-1", "file_url": "https://signed.invalid/u"}}).encode()),
            VisionHttpResponse(200, b""),
            VisionHttpResponse(200, json.dumps({"data": {"state": "succeeded", "markdown_url": "https://signed.invalid/m"}}).encode()),
            VisionHttpResponse(200, b"Revenue 100"),
        ])
        result = MineruLiteAdapter(transport, sleep=lambda _: None).extract(
            _company(), _filing(), [_page()], VisionFallbackConfig(enabled=True, consent=True)
        )
        self.assertIsNone(result.error_code)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "PUT", "GET", "GET"])
        request_payload = json.loads(transport.calls[0][3].decode())
        self.assertEqual(request_payload["enable_table"], True)
        self.assertEqual(request_payload["enable_formula"], False)
        self.assertTrue(transport.calls[0][1].endswith("/parse/file"))
        self.assertNotIn("signed.invalid", " ".join(result.diagnostics))

    def test_precision_reads_only_safe_full_markdown_zip(self):
        data = BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("full.md", "Revenue 100")
        transport = FakeTransport([
            VisionHttpResponse(200, json.dumps({"data": {"batch_id": "batch-1", "file_urls": [{"file_url": "https://signed.invalid/u"}]}}).encode()),
            VisionHttpResponse(200, b""),
            VisionHttpResponse(200, json.dumps({"data": {"state": "completed", "extract_result": [{"data_id": "page-130-" + _page().content_hash[:12], "state": "done", "full_zip_url": "https://signed.invalid/z"}]}}).encode()),
            VisionHttpResponse(200, data.getvalue()),
        ])
        result = MineruPrecisionAdapter(transport, sleep=lambda _: None).extract(
            _company(), _filing(), [_page()], VisionFallbackConfig(enabled=True, consent=True, provider="mineru_precision", token="session-token")
        )
        self.assertIsNone(result.error_code)
        self.assertNotIn("session-token", " ".join(result.diagnostics))

    def test_custom_vision_requires_https_and_never_uploads_pdf_without_renderer(self):
        transport = FakeTransport([])
        adapter = CustomVisionAdapter(transport)
        result = adapter.extract(
            _company(), _filing(), [_page()],
            VisionFallbackConfig(enabled=True, consent=True, provider="custom_vision", endpoint="http://insecure", model="vision", api_key="session-key"),
        )
        self.assertEqual(result.error_code, "VISION_HTTPS_REQUIRED")
        self.assertEqual(transport.calls, [])

    def test_custom_vision_uses_https_image_endpoint_and_returns_candidates(self):
        transport = FakeTransport([
            VisionHttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({"facts": [{"concept": "revenue", "value": 100, "currency": "CNY", "unit_scale": 1, "statement": "income_statement", "scope": "consolidated", "period_end": "2025-12-31", "original_page": 130, "raw_text": "Revenue 100"}, {"concept": "assets", "value": 200, "currency": "CNY", "unit_scale": 1, "statement": "balance_sheet", "scope": "consolidated", "period_end": "2025-12-31", "original_page": 130, "raw_text": "Total assets 200"}]})}}]}).encode()),
        ])
        adapter = CustomVisionAdapter(transport, image_renderer=lambda _: b"png")
        result = adapter.extract(
            _company(), _filing(), [_page()],
            VisionFallbackConfig(enabled=True, consent=True, provider="custom_vision", endpoint="https://vision.invalid/v1/chat/completions", model="vision", api_key="session-key"),
        )
        self.assertIsNone(result.error_code)
        self.assertEqual({fact.concept for fact in result.facts}, {"revenue", "assets"})
        self.assertNotIn("session-key", " ".join(result.diagnostics))
        self.assertTrue(transport.calls[0][1].startswith("https://"))

    def test_custom_default_renderer_is_available_for_one_page_pdf(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buffer = BytesIO()
        writer.write(buffer)
        page = VisionPageRequest(130, buffer.getvalue())
        transport = FakeTransport([VisionHttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({"facts": []})}}]}).encode())])
        result = CustomVisionAdapter(transport).extract(
            _company(), _filing(), [page], VisionFallbackConfig(enabled=True, consent=True, provider="custom_vision", endpoint="https://vision.invalid/v1/chat/completions", model="vision", api_key="session-key")
        )
        self.assertEqual(result.error_code, "VISION_NO_CANDIDATES")
        self.assertEqual(len(transport.calls), 1)

    def test_custom_vision_uploads_each_page_with_its_own_provenance(self):
        pages = [VisionPageRequest(130, b"page-one"), VisionPageRequest(131, b"page-two")]
        rows = []
        for page in pages:
            rows.append({"concept": "revenue", "value": page.original_page, "currency": "CNY", "unit_scale": 1, "statement": "income_statement", "scope": "consolidated", "period_end": "2025-12-31", "original_page": page.original_page, "raw_text": f"Revenue {page.original_page}"})
        transport = FakeTransport([
            VisionHttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({"facts": [rows[0]]})}}]}).encode()),
            VisionHttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps({"facts": [rows[1]]})}}]}).encode()),
        ])
        result = CustomVisionAdapter(transport, image_renderer=lambda _: b"png").extract(
            _company(), _filing(), pages,
            VisionFallbackConfig(enabled=True, consent=True, provider="custom_vision", endpoint="https://vision.invalid/v1/chat/completions", model="vision", api_key="session-key"),
        )
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual({fact.source_page for fact in result.facts}, {130, 131})
        self.assertEqual(len({fact.fact_id for fact in result.facts}), 2)
        self.assertTrue(all(ref.evidence_id == f"fact:{fact.fact_id}" for fact, ref in zip(result.facts, result.evidence)))

    def test_json_scales_raw_values_and_non_natural_fy_start(self):
        from openthesis.vision_financials import parse_vision_json
        page = VisionPageRequest(183, b"page")
        filing = FilingDocument("hkex:ali", _company().security_id, "ali", "ANNUAL_REPORT", "FY", "2026-03-31", "2026-06-18", "2026 Annual Report", "https://example.invalid/ali.pdf")
        result = parse_vision_json({"facts": [{"concept": "revenue", "value": 1023670, "currency": "CNY", "unit_scale": 1000000, "statement": "income_statement", "scope": "consolidated", "period_end": "2026-03-31", "original_page": 183, "raw_text": "Revenue 1,023,670"}]}, _company(), filing, [page])
        self.assertEqual(result.facts[0].value, 1_023_670_000_000)
        self.assertEqual(result.facts[0].start_date, "2025-04-01")

    def test_markdown_candidates_keep_original_page_hash_and_are_candidates(self):
        result = parse_vision_markdown(
            "Unit: RMB million\nRevenue 751,766\nTotal assets 2,038,986\nTotal liabilities 797,921\nTotal equity 1,241,065",
            _company(), _filing(), [_page()],
        )
        self.assertEqual(result.error_code, None)
        self.assertEqual({fact.concept for fact in result.facts}, {"revenue", "assets", "liabilities", "total_equity"})
        self.assertTrue(all(fact.source_page == 130 and fact.unit_scale == 1_000_000 for fact in result.facts))
        self.assertTrue(all(ref.content_hash for ref in result.evidence))
        self.assertIn("VISION_CANDIDATES_ONLY", result.diagnostics)

    def test_cancel_stops_lite_polling(self):
        transport = FakeTransport([
            VisionHttpResponse(200, json.dumps({"data": {"task_id": "task-1", "file_url": "https://signed.invalid/u"}}).encode()),
            VisionHttpResponse(200, b""),
        ])
        result = MineruLiteAdapter(transport, sleep=lambda _: None).extract(
            _company(), _filing(), [_page()], VisionFallbackConfig(enabled=True, consent=True), cancel_check=lambda: True
        )
        self.assertEqual(result.error_code, "VISION_CANCELLED")
        self.assertEqual(len(transport.calls), 0)

    def test_rate_limit_is_safe_and_page_size_limits_happen_before_http(self):
        limited = MineruLiteAdapter(FakeTransport([]), sleep=lambda _: None).extract(
            _company(), _filing(), [_page()] * 21,
            VisionFallbackConfig(enabled=True, consent=True),
        )
        self.assertEqual(limited.error_code, "VISION_PAGE_LIMIT")
        rate_limited = MineruLiteAdapter(FakeTransport([VisionHttpResponse(429, b"secret-token")]), sleep=lambda _: None).extract(
            _company(), _filing(), [_page()], VisionFallbackConfig(enabled=True, consent=True)
        )
        self.assertEqual(rate_limited.error_code, "VISION_RATE_LIMITED")
        self.assertNotIn("secret-token", " ".join(rate_limited.diagnostics))

    def test_lite_business_error_code_rejects_http_200(self):
        transport = FakeTransport([VisionHttpResponse(200, json.dumps({"code": 1001, "msg": "bad"}).encode())])
        result = MineruLiteAdapter(transport, sleep=lambda _: None).extract(
            _company(), _filing(), [_page()], VisionFallbackConfig(enabled=True, consent=True)
        )
        self.assertEqual(result.error_code, "VISION_REMOTE_FAILED")

    def test_upload_approval_receives_metadata_only(self):
        seen = []
        result = MineruLiteAdapter(FakeTransport([]), sleep=lambda _: None).extract(
            _company(), _filing(), [_page()], VisionFallbackConfig(
                enabled=True, consent=True, approve_upload=lambda summary: seen.append(summary) or False
            )
        )
        self.assertEqual(result.error_code, "VISION_UPLOAD_NOT_APPROVED")
        self.assertEqual(seen[0]["pages"], (130,))
        self.assertNotIn("pdf_bytes", seen[0])
        self.assertNotIn("local_path", seen[0])
        self.assertNotIn("source_url", seen[0])
