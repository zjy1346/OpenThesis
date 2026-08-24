from __future__ import annotations

from io import BytesIO
import json
import unittest

from pypdf import PdfWriter

from openthesis.domain import Company, FilingDocument
from openthesis.providers import ProviderError
from openthesis.vision_financials import (
    GatewayVisionAdapter,
    MineruFlashAdapter,
    VisionHttpResponse,
    VisionFallbackConfig,
    VisionPageRequest,
    default_pdf_to_png,
    parse_vision_json,
    parse_vision_markdown,
)


def _company() -> Company:
    return Company(
        "HK:SEHK:00700",
        "00700.HK",
        "Tencent",
        "SEHK",
        market="HK",
        reporting_currency="CNY",
    )


def _filing() -> FilingDocument:
    return FilingDocument(
        "hkex:vision",
        _company().security_id,
        "vision",
        "ANNUAL_REPORT",
        "FY",
        "2025-12-31",
        "2026-04-01",
        "2025 Annual Report",
        "https://example.invalid/report.pdf",
    )


def _page(number: int = 130, payload: bytes = b"opaque-page-bytes") -> VisionPageRequest:
    return VisionPageRequest(number, payload)


def _config(**changes) -> VisionFallbackConfig:
    values = {
        "enabled": True,
        "consent": True,
        "configured_model_id": "vision.ready",
        "configuration_version": 4,
        "require_page_approval": True,
        "approve_upload": lambda _: True,
    }
    values.update(changes)
    return VisionFallbackConfig(**values)


class RecordingVisionProvider:
    def __init__(self, error: ProviderError | None = None):
        self.calls = []
        self.error = error

    def generate_vision(self, system_prompt: str, user_prompt: str, image: bytes):
        self.calls.append((system_prompt, user_prompt, image))
        if self.error is not None:
            raise self.error
        page = 131 if "original_page 131" in user_prompt else 130
        return {
            "facts": [
                {
                    "concept": "revenue",
                    "value": page,
                    "currency": "CNY",
                    "unit_scale": 1,
                    "statement": "income_statement",
                    "scope": "consolidated",
                    "period_end": "2025-12-31",
                    "original_page": page,
                    "raw_text": f"Revenue {page}",
                }
            ],
            "_response_meta": {
                "configured_model_id": "vision.ready",
                "configuration_version": 4,
            },
        }



class QueueVisionTransport:
    def __init__(self, *responses: VisionHttpResponse):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, timeout=60.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "body": body, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected vision HTTP request")
        return self.responses.pop(0)


class VisionFinancialTests(unittest.TestCase):
    def test_missing_consent_stops_before_render_or_gateway(self):
        provider = RecordingVisionProvider()
        adapter = GatewayVisionAdapter(provider, image_renderer=lambda _: b"png")
        result = adapter.extract(
            _company(),
            _filing(),
            [_page()],
            _config(consent=False),
        )
        self.assertEqual(result.error_code, "VISION_CONSENT_REQUIRED")
        self.assertEqual(provider.calls, [])

    def test_mineru_flash_uses_no_token_and_preserves_page_provenance(self):
        transport = QueueVisionTransport(
            VisionHttpResponse(200, json.dumps({"code": 0, "data": {"task_id": "task-1", "file_url": "https://upload.example/page"}}).encode()),
            VisionHttpResponse(200),
            VisionHttpResponse(200, json.dumps({"state": "done", "data": {"markdown_url": "https://download.example/result.md"}}).encode()),
            VisionHttpResponse(200, b"Unit: RMB million\nRevenue 751,766\nTotal assets 2,038,986\nTotal liabilities 797,921\nTotal equity 1,241,065"),
        )
        seen = []
        config = _config(
            provider="mineru_flash",
            configured_model_id="",
            approve_upload=lambda summary: seen.append(summary) or True,
        )

        result = MineruFlashAdapter(transport, sleep=lambda _: None).extract(
            _company(), _filing(), [_page()], config
        )

        self.assertIsNone(result.error_code)
        self.assertIn("VISION_MINERU_FLASH_COMPLETED", result.diagnostics)
        self.assertEqual(seen[0]["provider"], "mineru_flash")
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PUT", "GET", "GET"])
        self.assertTrue(transport.calls[0]["url"].endswith("/api/v1/agent/parse/file"))
        self.assertTrue(all("Authorization" not in call["headers"] for call in transport.calls))
        self.assertFalse(hasattr(config, "token"))
        self.assertFalse(hasattr(config, "api_key"))
        self.assertEqual({fact.source_page for fact in result.facts}, {130})

    def test_mineru_flash_rate_limit_is_explicit_and_never_falls_back(self):
        transport = QueueVisionTransport(VisionHttpResponse(429, b"rate limited"))
        result = MineruFlashAdapter(transport, sleep=lambda _: None).extract(
            _company(),
            _filing(),
            [_page()],
            _config(provider="mineru_flash", configured_model_id=""),
        )

        self.assertEqual(result.error_code, "VISION_RATE_LIMITED")
        self.assertEqual(len(transport.calls), 1)

    def test_mineru_flash_rejects_insecure_upload_url(self):
        transport = QueueVisionTransport(
            VisionHttpResponse(200, json.dumps({"code": 0, "data": {"task_id": "task-1", "file_url": "http://upload.example/page"}}).encode()),
        )
        result = MineruFlashAdapter(transport, sleep=lambda _: None).extract(
            _company(),
            _filing(),
            [_page()],
            _config(provider="mineru_flash", configured_model_id=""),
        )

        self.assertEqual(result.error_code, "VISION_INSECURE_URL")
        self.assertEqual(len(transport.calls), 1)

    def test_mineru_flash_still_requires_consent_and_page_approval(self):
        transport = QueueVisionTransport()
        without_consent = MineruFlashAdapter(transport).extract(
            _company(),
            _filing(),
            [_page()],
            _config(provider="mineru_flash", configured_model_id="", consent=False),
        )
        declined = MineruFlashAdapter(transport).extract(
            _company(),
            _filing(),
            [_page()],
            _config(provider="mineru_flash", configured_model_id="", approve_upload=lambda _: False),
        )

        self.assertEqual(without_consent.error_code, "VISION_CONSENT_REQUIRED")
        self.assertEqual(declined.error_code, "VISION_UPLOAD_NOT_APPROVED")
        self.assertEqual(transport.calls, [])


    def test_mineru_flash_refuses_network_without_page_approval_callback(self):
        transport = QueueVisionTransport()
        result = MineruFlashAdapter(transport).extract(
            _company(),
            _filing(),
            [_page()],
            _config(
                provider="mineru_flash",
                configured_model_id="",
                approve_upload=None,
            ),
        )

        self.assertEqual(result.error_code, "VISION_UPLOAD_APPROVAL_REQUIRED")
        self.assertEqual(transport.calls, [])

    def test_model_reference_is_required_and_has_no_secret_fields(self):
        config = _config(configured_model_id="")
        self.assertFalse(hasattr(config, "token"))
        self.assertFalse(hasattr(config, "api_key"))
        self.assertFalse(hasattr(config, "endpoint"))
        result = GatewayVisionAdapter(
            RecordingVisionProvider(), image_renderer=lambda _: b"png"
        ).extract(_company(), _filing(), [_page()], config)
        self.assertEqual(result.error_code, "VISION_MODEL_REQUIRED")

    def test_page_and_byte_limits_run_before_gateway(self):
        provider = RecordingVisionProvider()
        adapter = GatewayVisionAdapter(provider, image_renderer=lambda _: b"png")
        page_limited = adapter.extract(
            _company(),
            _filing(),
            [_page()] * 21,
            _config(),
        )
        byte_limited = adapter.extract(
            _company(),
            _filing(),
            [_page(payload=b"x" * 20)],
            _config(max_bytes=10),
        )
        self.assertEqual(page_limited.error_code, "VISION_PAGE_LIMIT")
        self.assertEqual(byte_limited.error_code, "VISION_SIZE_LIMIT")
        self.assertEqual(provider.calls, [])

    def test_upload_approval_receives_metadata_only(self):
        seen = []
        config = _config(approve_upload=lambda summary: seen.append(summary) or False)
        result = GatewayVisionAdapter(
            RecordingVisionProvider(), image_renderer=lambda _: b"png"
        ).extract(_company(), _filing(), [_page()], config)
        self.assertEqual(result.error_code, "VISION_UPLOAD_NOT_APPROVED")
        self.assertEqual(seen[0]["provider"], "vision.ready")
        self.assertEqual(seen[0]["pages"], (130,))
        self.assertNotIn("pdf_bytes", seen[0])
        self.assertNotIn("local_path", seen[0])
        self.assertNotIn("source_url", seen[0])

    def test_cancel_stops_before_render_and_gateway(self):
        provider = RecordingVisionProvider()
        rendered = []
        result = GatewayVisionAdapter(
            provider, image_renderer=lambda value: rendered.append(value) or b"png"
        ).extract(
            _company(),
            _filing(),
            [_page()],
            _config(),
            cancel_check=lambda: True,
        )
        self.assertEqual(result.error_code, "VISION_CANCELLED")
        self.assertEqual(provider.calls, [])
        self.assertEqual(rendered, [])

    def test_gateway_processes_each_page_with_its_own_provenance(self):
        provider = RecordingVisionProvider()
        pages = [_page(130, b"one"), _page(131, b"two")]
        result = GatewayVisionAdapter(
            provider, image_renderer=lambda _: b"\x89PNG\r\n\x1a\n"
        ).extract(_company(), _filing(), pages, _config())
        self.assertIsNone(result.error_code)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual({fact.source_page for fact in result.facts}, {130, 131})
        self.assertEqual(len({fact.fact_id for fact in result.facts}), 2)
        self.assertTrue(
            all(
                ref.evidence_id == f"fact:{fact.fact_id}"
                for fact, ref in zip(result.facts, result.evidence)
            )
        )
        self.assertIn("VISION_MODEL_GATEWAY_COMPLETED", result.diagnostics)

    def test_gateway_errors_are_mapped_without_response_payloads(self):
        provider = RecordingVisionProvider(
            ProviderError("do not expose provider payload", code="MODEL_RATE_LIMITED")
        )
        result = GatewayVisionAdapter(
            provider, image_renderer=lambda _: b"png"
        ).extract(_company(), _filing(), [_page()], _config())
        self.assertEqual(result.error_code, "VISION_RATE_LIMITED")
        self.assertNotIn("provider payload", " ".join(result.diagnostics))

    def test_default_renderer_produces_a_bounded_png_in_memory(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buffer = BytesIO()
        writer.write(buffer)
        rendered = default_pdf_to_png(buffer.getvalue())
        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLessEqual(len(rendered), 8 * 1024 * 1024)

    def test_json_scales_raw_values_and_non_natural_fy_start(self):
        page = _page(183, b"page")
        filing = FilingDocument(
            "hkex:ali",
            _company().security_id,
            "ali",
            "ANNUAL_REPORT",
            "FY",
            "2026-03-31",
            "2026-06-18",
            "2026 Annual Report",
            "https://example.invalid/ali.pdf",
        )
        result = parse_vision_json(
            {
                "facts": [
                    {
                        "concept": "revenue",
                        "value": 1023670,
                        "currency": "CNY",
                        "unit_scale": 1000000,
                        "statement": "income_statement",
                        "scope": "consolidated",
                        "period_end": "2026-03-31",
                        "original_page": 183,
                        "raw_text": "Revenue 1,023,670",
                    }
                ]
            },
            _company(),
            filing,
            [page],
        )
        self.assertEqual(result.facts[0].value, 1_023_670_000_000)
        self.assertEqual(result.facts[0].start_date, "2025-04-01")

    def test_markdown_candidates_keep_page_hash_and_candidate_status(self):
        result = parse_vision_markdown(
            "Unit: RMB million\nRevenue 751,766\nTotal assets 2,038,986\n"
            "Total liabilities 797,921\nTotal equity 1,241,065",
            _company(),
            _filing(),
            [_page()],
        )
        self.assertIsNone(result.error_code)
        self.assertEqual(
            {fact.concept for fact in result.facts},
            {"revenue", "assets", "liabilities", "total_equity"},
        )
        self.assertTrue(
            all(fact.source_page == 130 and fact.unit_scale == 1_000_000 for fact in result.facts)
        )
        self.assertTrue(all(ref.content_hash for ref in result.evidence))
        self.assertIn("VISION_CANDIDATES_ONLY", result.diagnostics)


if __name__ == "__main__":
    unittest.main()
