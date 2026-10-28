"""Offline wire-budget regressions for configured vision recovery.

These tests exercise the real DurableCloudVisionAdapter -> GatewayVisionAdapter
path.  The provider fake records the wire mode for every request and delegates
successful responses to the existing RecordingVisionProvider fixture.
"""

from pathlib import Path
import json
import tempfile
import unittest
from dataclasses import replace

from openthesis.domain import ResearchRun
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.providers import ProviderError
from openthesis.resilience.contracts import IncidentScope, RecoveryLimits, digest
from openthesis.storage import Storage
from openthesis.vision_financials import GatewayVisionAdapter

from test_durable_cloud_recovery import CountingCompiler
from test_resilience_core import Clock
from test_vision_financials import (
    RecordingVisionProvider,
    _company,
    _config,
    _filing,
    _page,
)


class WireModeProvider(RecordingVisionProvider):
    """Record one wire request per call and reuse the known-good response."""

    def __init__(self, *errors):
        super().__init__()
        self.errors = list(errors)
        self.json_modes = []

    def generate_vision_once(self, system_prompt, user_prompt, image, *, json_mode=True):
        self.json_modes.append(bool(json_mode))
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        return super().generate_vision(system_prompt, user_prompt, image)


class GatewayWireBudgetTests(unittest.TestCase):
    TEMP_ROOT = Path(__file__).resolve().parents[1] / "tmp"

    @classmethod
    def setUpClass(cls):
        # A clean checkout may not contain the generated workspace temp root.
        cls.TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=self.TEMP_ROOT
        )
        self.store = Storage(Path(self.temp.name))
        self.store.save_company(_company())
        self.store.save_run(
            ResearchRun("run", _company(), "wf", "pack", "1", "", "", "2026-10-06")
        )
        self.clock = Clock()
        self.compiler = CountingCompiler()
        self.config = _config(provider="configured_model", max_pages=2)
        self.provider = WireModeProvider()

    def tearDown(self):
        self.temp.cleanup()

    def wrapper(self, **kwargs):
        adapter = GatewayVisionAdapter(self.provider, image_renderer=lambda _: b"png")
        return DurableCloudVisionAdapter(
            adapter,
            self.store,
            "run",
            compiler=self.compiler,
            clock=self.clock,
            sleeper=self.clock.sleep,
            **kwargs,
        )

    def extract(self, wrapper, *, config=None):
        return wrapper.extract(_company(), _filing(), (_page(),), config or self.config)

    def incident_usage(self):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT usage_json FROM recovery_incidents ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def test_success_uses_one_json_wire_request(self):
        result = self.extract(self.wrapper())
        self.assertIsNone(result.error_code)
        self.assertEqual(self.provider.json_modes, [True])
        self.assertEqual(self.incident_usage()["calls"], 1)
        self.assertEqual(self.incident_usage()["pages"], 1)

    def test_format_400_gets_one_non_json_compensation_but_one_page(self):
        self.provider.errors = [
            ProviderError(
                "format unsupported",
                code="MODEL_RESPONSE_FORMAT_UNSUPPORTED",
                http_status=400,
            )
        ]
        result = self.extract(self.wrapper())
        self.assertIsNone(result.error_code)
        self.assertEqual(self.provider.json_modes, [True, False])
        usage = self.incident_usage()
        self.assertEqual((usage["calls"], usage["pages"]), (2, 1))

    def test_calls_one_stops_before_format_compensation(self):
        self.provider.errors = [
            ProviderError(
                "format unsupported",
                code="MODEL_RESPONSE_FORMAT_UNSUPPORTED",
                http_status=400,
            )
        ]
        result = self.extract(self.wrapper(limits=RecoveryLimits(calls=1, pages=1)))
        self.assertEqual(result.error_code, "RECOVERY_INCIDENT_BUDGET_EXHAUSTED")
        self.assertEqual(self.provider.json_modes, [True])
        usage = self.incident_usage()
        self.assertEqual((usage["calls"], usage["pages"]), (1, 1))
        self.assertEqual(
            [item for item in self.store.get_artifacts("run")
             if item["artifact_type"] == "vision-candidate-set"],
            [],
        )

    def test_unknown_first_timeout_is_not_reissued_after_restart(self):
        self.provider.errors = [
            ProviderError("timeout", code="MODEL_TIMEOUT", retryable=True)
        ]
        first = self.extract(self.wrapper())
        self.assertEqual(first.error_code, "RECOVERY_SUBMISSION_UNKNOWN")
        self.store = Storage(Path(self.temp.name))
        second = self.extract(self.wrapper())
        self.assertIsNotNone(second.error_code)
        self.assertEqual(self.provider.json_modes, [True])

    def test_unknown_timeout_is_not_reissued_after_configuration_revision_change(self):
        self.provider.errors = [
            ProviderError("timeout", code="MODEL_TIMEOUT", retryable=True)
        ]
        first = self.extract(self.wrapper())
        self.assertEqual(first.error_code, "RECOVERY_SUBMISSION_UNKNOWN")
        self.store = Storage(Path(self.temp.name))
        revised = replace(
            self.config,
            configuration_version=self.config.configuration_version + 1,
        )
        second = self.extract(self.wrapper(), config=revised)
        self.assertEqual(second.error_code, "RECOVERY_SUBMISSION_UNKNOWN")
        self.assertEqual(self.provider.json_modes, [True])
        self.assertEqual(
            [item for item in self.store.get_artifacts("run")
             if item["artifact_type"] == "vision-candidate-set"],
            [],
        )

    def test_format_then_second_timeout_is_not_reissued_after_restart(self):
        self.provider.errors = [
            ProviderError(
                "format unsupported",
                code="MODEL_RESPONSE_FORMAT_UNSUPPORTED",
                http_status=400,
            ),
            ProviderError("timeout", code="MODEL_TIMEOUT", retryable=True),
        ]
        first = self.extract(self.wrapper())
        self.assertEqual(first.error_code, "RECOVERY_SUBMISSION_UNKNOWN")
        self.store = Storage(Path(self.temp.name))
        second = self.extract(self.wrapper())
        self.assertIsNotNone(second.error_code)
        self.assertEqual(self.provider.json_modes, [True, False])
        usage = self.incident_usage()
        self.assertEqual((usage["calls"], usage["pages"]), (2, 1))

    def test_revocation_after_format_error_stops_before_compensation(self):
        authorized = {"value": True}

        class RevokingProvider(WireModeProvider):
            def generate_vision_once(inner, system_prompt, user_prompt, image, *, json_mode=True):
                inner.json_modes.append(bool(json_mode))
                if len(inner.json_modes) == 1:
                    authorized["value"] = False
                    raise ProviderError(
                        "format unsupported",
                        code="MODEL_RESPONSE_FORMAT_UNSUPPORTED",
                        http_status=400,
                    )
                return super().generate_vision_once(
                    system_prompt, user_prompt, image, json_mode=json_mode
                )

        self.provider = RevokingProvider()
        result = self.extract(
            self.wrapper(authorization_check=lambda: authorized["value"])
        )
        self.assertIsNotNone(result.error_code)
        self.assertEqual(self.provider.json_modes, [True])

    def test_5xx_never_gets_format_compensation(self):
        self.provider = WireModeProvider(
            ProviderError("server", code="MODEL_HTTP_500", http_status=500, retryable=True)
        )
        result = self.extract(self.wrapper())
        self.assertIsNotNone(result.error_code)
        self.assertEqual(self.provider.json_modes, [True])

    def test_ordinary_400_never_gets_format_compensation(self):
        self.provider = WireModeProvider(
            ProviderError("bad request", code="MODEL_HTTP_400", http_status=400)
        )
        result = self.extract(self.wrapper())
        self.assertIsNotNone(result.error_code)
        self.assertEqual(self.provider.json_modes, [True])

    def test_typed_format_error_with_500_never_gets_format_compensation(self):
        self.provider = WireModeProvider(
            ProviderError(
                "format rejected by server",
                code="MODEL_RESPONSE_FORMAT_UNSUPPORTED",
                http_status=500,
            )
        )
        result = self.extract(self.wrapper())
        self.assertIsNotNone(result.error_code)
        self.assertEqual(self.provider.json_modes, [True])

    def test_format_error_without_http_status_never_gets_format_compensation(self):
        self.provider = WireModeProvider(
            ProviderError("format rejected without status", code="MODEL_RESPONSE_FORMAT_UNSUPPORTED")
        )
        result = self.extract(self.wrapper())
        self.assertIsNotNone(result.error_code)
        self.assertEqual(self.provider.json_modes, [True])

    def test_repeated_format_400_stops_after_one_compensation(self):
        format_error = ProviderError(
            "format unsupported",
            code="MODEL_RESPONSE_FORMAT_UNSUPPORTED",
            http_status=400,
        )
        self.provider = WireModeProvider(format_error, format_error)
        result = self.extract(self.wrapper())
        self.assertEqual(result.error_code, "VISION_MODEL_FORMAT_UNSUPPORTED")
        self.assertEqual(self.provider.json_modes, [True, False])
        usage = self.incident_usage()
        self.assertEqual((usage["calls"], usage["pages"]), (2, 1))

    def test_shared_incident_budget_does_not_compensate_second_page(self):
        format_error = ProviderError(
            "format unsupported",
            code="MODEL_RESPONSE_FORMAT_UNSUPPORTED",
            http_status=400,
        )
        self.provider = WireModeProvider(None, format_error)
        scope = IncidentScope("financial-evidence", digest({"company": _company().cik}))
        first = self.wrapper(budget_scope=scope).extract(
            _company(), _filing(), (_page(130),), self.config
        )
        artifacts_after_first = [
            item for item in self.store.get_artifacts("run")
            if item["artifact_type"] == "vision-candidate-set"
        ]
        second = self.wrapper(
            budget_scope=scope,
            limits=RecoveryLimits(calls=2, pages=2),
        ).extract(_company(), _filing(), (_page(131, b"second-page"),), self.config)
        artifacts_after_second = [
            item for item in self.store.get_artifacts("run")
            if item["artifact_type"] == "vision-candidate-set"
        ]
        self.assertIsNone(first.error_code)
        self.assertIsNotNone(second.error_code)
        self.assertEqual(self.provider.json_modes, [True, True])
        self.assertEqual(artifacts_after_second, artifacts_after_first)

