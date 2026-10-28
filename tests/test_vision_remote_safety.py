"""Offline contract checks for the free cloud transport, not live acceptance."""
from __future__ import annotations

import json
import unittest

from openthesis.vision_financials import (
    MINERU_UPLOAD_PROTOCOL_VERSION, MineruFlashAdapter, VisionAdapterError,
    VisionHttpResponse, VisionTaskCoordinator, _serialize_vision_result,
    vision_task_key,
)
from test_vision_financials import (
    BoundedVisionAdapter, MemoryVisionJournal, QueueVisionTransport, _company, _config,
    _explicit_financial_markdown, _filing, _page,
)


def _created():
    return VisionHttpResponse(200, json.dumps({"code": 0, "data": {
        "task_id": "existing-task", "file_url": "https://upload.example/page",
    }}).encode())


def _done():
    return VisionHttpResponse(200, json.dumps({"state": "done", "data": {
        "markdown_url": "https://download.example/result.md",
    }}).encode())


def _configuration(**changes):
    return _config(provider="mineru_flash", configured_model_id="", **changes)


def _key(page):
    return vision_task_key(f"mineru_flash:{MINERU_UPLOAD_PROTOCOL_VERSION}",
                           _filing().content_hash, (page,), namespace="page")


class FreeCloudTransportSafetyTests(unittest.TestCase):
    def test_scanned_page_submission_explicitly_enables_ocr(self):
        transport = QueueVisionTransport(_created(), VisionHttpResponse(200), _done(),
                                        VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")))
        result = MineruFlashAdapter(transport).extract(_company(), _filing(), [_page()], _configuration())
        self.assertIsNone(result.error_code)
        payload = json.loads(transport.calls[0]["body"])
        self.assertIs(payload["is_ocr"], True)
        self.assertIs(payload["enable_table"], True)

    def test_ambiguous_create_failure_is_not_reposted_on_restart(self):
        class LostCreateTransport:
            def __init__(self):
                self.calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                raise OSError("response lost after server accepted submission")

        transport = LostCreateTransport()
        journal = MemoryVisionJournal()
        page = _page()
        first = MineruFlashAdapter(transport, journal=journal, sleep=lambda _: None).extract(
            _company(), _filing(), [page], _configuration())
        second = MineruFlashAdapter(transport, journal=journal, sleep=lambda _: None).extract(
            _company(), _filing(), [page], _configuration())
        self.assertEqual(first.error_code, "VISION_NETWORK_ERROR")
        self.assertEqual(second.error_code, "VISION_RECOVERY_BLOCKED")
        self.assertEqual(transport.calls, 1)
        self.assertEqual(journal.rows[_key(page)]["status"], "submission_unknown")

    def test_server_failure_during_create_has_no_hidden_post_retry(self):
        transport = QueueVisionTransport(VisionHttpResponse(503), _created())
        result = MineruFlashAdapter(transport, sleep=lambda _: None).extract(
            _company(), _filing(), [_page()], _configuration())
        self.assertEqual(result.error_code, "VISION_HTTP_ERROR")
        self.assertEqual([call["method"] for call in transport.calls], ["POST"])

    def test_known_task_can_be_queried_after_uncertain_upload_or_poll_failure(self):
        for status in ("created", "failed", "uploaded", "running"):
            with self.subTest(status=status):
                page, journal = _page(), MemoryVisionJournal()
                journal.save_vision_task(_key(page), company_cik=_filing().company_cik,
                    document_id=_filing().document_id, provider="mineru_flash",
                    page_hashes=[page.content_hash], page_numbers=[page.original_page],
                    status=status, remote_task_id="existing-task")
                transport = QueueVisionTransport(_done(), VisionHttpResponse(
                    200, _explicit_financial_markdown("Revenue 1")))
                result = MineruFlashAdapter(transport, journal=journal).extract(
                    _company(), _filing(), [page], _configuration())
                self.assertIsNone(result.error_code)
                self.assertEqual([call["method"] for call in transport.calls], ["GET", "GET"])

    def test_all_http_calls_share_one_extraction_deadline(self):
        now = [0.0]

        class AdvancingTransport(QueueVisionTransport):
            def request(self, *args, **kwargs):
                response = super().request(*args, **kwargs)
                now[0] += 1.0
                return response

        transport = AdvancingTransport(_created(), VisionHttpResponse(200), _done(),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")))
        result = MineruFlashAdapter(transport, clock=lambda: now[0]).extract(
            _company(), _filing(), [_page()], _configuration(timeout_seconds=5))
        self.assertIsNone(result.error_code)
        self.assertEqual([call["timeout"] for call in transport.calls], [5, 4, 3, 2])

    def test_invalid_timeout_is_rejected_before_network(self):
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout):
                transport = QueueVisionTransport()
                result = MineruFlashAdapter(transport).extract(
                    _company(), _filing(), [_page()], _configuration(timeout_seconds=timeout))
                self.assertEqual(result.error_code, "VISION_TIMEOUT_INVALID")
                self.assertEqual(transport.calls, [])

    def test_json_rejects_duplicate_fields_and_nonfinite_numbers(self):
        for payload in (b'{"code":0,"code":1}', b'{"value":NaN}',
                        b'{"value":Infinity}', b'{"value":-Infinity}'):
            with self.subTest(payload=payload):
                with self.assertRaises(VisionAdapterError) as caught:
                    VisionHttpResponse(200, payload).json()
                self.assertEqual(caught.exception.code, "VISION_MALFORMED_RESPONSE")

    def test_recovery_usage_and_intent_precede_each_external_side_effect(self):
        events = []

        class Context:
            def checkpoint(self):
                events.append("checkpoint")

            def consume(self, usage):
                events.append(("consume", usage.calls, usage.pages))

            def record_external_intent(self):
                events.append("intent")

            def record_external_task(self, task_id):
                events.append(("task", task_id))

        class Transport(QueueVisionTransport):
            def request(self, method, *args, **kwargs):
                events.append(method)
                return super().request(method, *args, **kwargs)

        transport = Transport(_created(), VisionHttpResponse(200), _done(), VisionHttpResponse(
            200, _explicit_financial_markdown("Revenue 1")))
        result = MineruFlashAdapter(transport).extract(
            _company(), _filing(), [_page()], _configuration(), recovery_context=Context())
        self.assertIsNone(result.error_code)
        self.assertLess(events.index("intent"), events.index("POST"))
        self.assertLess(events.index(("task", "existing-task")), events.index("PUT"))
        self.assertEqual(sum(item[1] for item in events if isinstance(item, tuple)
                             and item[0] == "consume"), 4)
        self.assertEqual(sum(item[2] for item in events if isinstance(item, tuple)
                             and item[0] == "consume"), 1)
        for index, item in enumerate(events):
            if item in ("POST", "PUT", "GET"):
                self.assertEqual(events[index - 1], ("consume", 1, 0))

    def test_recovery_context_returns_pending_after_one_status_poll(self):
        class Context:
            def __init__(self):
                self.calls = 0
                self.pages = 0

            def checkpoint(self):
                pass

            def consume(self, usage):
                self.calls += usage.calls
                self.pages += usage.pages

            def record_external_intent(self):
                pass

            def record_external_task(self, _task_id):
                pass

        page, journal = _page(), MemoryVisionJournal()
        transport = QueueVisionTransport(
            _created(), VisionHttpResponse(200),
            VisionHttpResponse(200, json.dumps({"state": "running"}).encode()),
        )
        context = Context()
        result = MineruFlashAdapter(transport, journal=journal).extract(
            _company(), _filing(), [page], _configuration(), recovery_context=context,
        )
        row = journal.rows[_key(page)]
        self.assertEqual(result.error_code, "VISION_PENDING")
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PUT", "GET"])
        self.assertEqual((row["status"], row["remote_task_id"]), ("running", "existing-task"))
        self.assertEqual((context.calls, context.pages), (3, 1))

    def test_explicit_remote_task_id_is_quoted_and_query_only(self):
        transport = QueueVisionTransport(_done(), VisionHttpResponse(
            200, _explicit_financial_markdown("Revenue 1")))
        result = MineruFlashAdapter(transport).extract(
            _company(), _filing(), [_page()], _configuration(), remote_task_id="../opaque",
        )
        self.assertIsNone(result.error_code)
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "GET"])
        self.assertEqual(
            transport.calls[0]["url"],
            "https://mineru.net/api/v1/agent/parse/%2E%2E%2Fopaque",
        )

    def test_expired_deadline_never_submits_a_late_post(self):
        now = [0.0]
        transport, journal = QueueVisionTransport(_created()), MemoryVisionJournal()

        def slow_approval(_summary):
            now[0] = 1.1
            return True

        result = MineruFlashAdapter(transport, clock=lambda: now[0], journal=journal).extract(
            _company(), _filing(), [_page()],
            _configuration(timeout_seconds=1, approve_upload=slow_approval),
        )
        self.assertEqual(result.error_code, "VISION_TIMEOUT")
        self.assertEqual(transport.calls, [])
        self.assertEqual(journal.rows, {})

    def test_explicit_rate_limit_rejection_is_journaled_and_retry_after_survives(self):
        class Context:
            def __init__(self):
                self.rejections = []

            def checkpoint(self):
                pass

            def consume(self, _usage):
                pass

            def record_external_intent(self):
                pass

            def record_external_rejection(self, status):
                self.rejections.append(status)

        page, journal, context = _page(), MemoryVisionJournal(), Context()
        transport = QueueVisionTransport(VisionHttpResponse(429, headers={"Retry-After": "7.5"}))
        result = MineruFlashAdapter(transport, journal=journal).extract(
            _company(), _filing(), [page], _configuration(), recovery_context=context,
        )
        self.assertEqual(result.error_code, "VISION_RATE_LIMITED")
        self.assertEqual(result.retry_after_seconds, 7.5)
        self.assertEqual(context.rejections, [429])
        self.assertEqual(journal.rows[_key(page)]["status"], "rejected")

    def test_page_success_cache_revalidates_locally_without_http(self):
        page, journal = _page(), MemoryVisionJournal()
        first_transport = QueueVisionTransport(
            _created(), VisionHttpResponse(200), _done(),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
        )
        first = MineruFlashAdapter(first_transport, journal=journal).extract(
            _company(), _filing(), [page], _configuration(),
        )
        self.assertIsNone(first.error_code)
        second_transport = QueueVisionTransport()
        second = MineruFlashAdapter(second_transport, journal=journal).extract(
            _company(), _filing(), [page], _configuration(),
        )
        self.assertIsNone(second.error_code)
        self.assertIn("VISION_SOURCE_MARKDOWN_CACHE_HIT", second.diagnostics)
        self.assertEqual(first.facts, second.facts)
        self.assertEqual(first.evidence, second.evidence)
        self.assertEqual(second_transport.calls, [])

    def test_old_batch_ocr_cache_is_not_reused_for_current_protocol(self):
        page, journal = _page(), MemoryVisionJournal()
        legacy = BoundedVisionAdapter().extract(
            _company(), _filing(), [page], _config(),
        )
        old_batch_key = vision_task_key(
            "mineru_flash", _filing().content_hash, (page,), namespace="batch",
        )
        journal.save_vision_result(old_batch_key, _serialize_vision_result(legacy))
        transport = QueueVisionTransport(
            _created(), VisionHttpResponse(200), _done(),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
        )
        result = VisionTaskCoordinator(
            MineruFlashAdapter(transport, journal=journal), journal=journal,
        ).extract(_company(), _filing(), [page], _configuration())
        self.assertIsNone(result.error_code)
        self.assertNotIn("VISION_RESULT_CACHE_HIT", result.diagnostics)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PUT", "GET", "GET"])
