"""Configured image models cross the same durable financial-source gate."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openthesis.domain import ResearchRun
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.model_scheduler import ModelRequestScheduler
from openthesis.providers import ProviderError
from openthesis.resilience.contracts import IncidentScope, RecoveryLimits, digest
from openthesis.storage import Storage
from openthesis.vision_financials import GatewayVisionAdapter
from test_durable_cloud_recovery import CountingCompiler
from test_resilience_core import Clock
from test_vision_financials import RecordingVisionProvider, _company, _config, _filing, _page


class DurableGatewayVisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp")
        self.store = Storage(Path(self.temp.name))
        self.store.save_company(_company())
        self.store.save_run(ResearchRun("run", _company(), "wf", "pack", "1", "", "", "2026-10-06"))
        self.clock, self.compiler = Clock(), CountingCompiler()
        self.config = _config(provider="configured_model", max_pages=2)
        self.provider = RecordingVisionProvider()
        self.rendered = []

    def tearDown(self):
        self.temp.cleanup()

    def wrapper(self, **kwargs):
        def render(contents):
            self.rendered.append(contents)
            return b"\x89PNG\r\n\x1a\n"
        adapter = GatewayVisionAdapter(self.provider, image_renderer=render)
        return DurableCloudVisionAdapter(adapter, self.store, "run", compiler=self.compiler,
            clock=self.clock, sleeper=self.clock.sleep, **kwargs)

    def extract(self, wrapper, *, page=None, config=None):
        return wrapper.extract(_company(), _filing(), (page or _page(),), config or self.config)

    def incidents(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM recovery_incidents")]

    def test_committed_model_page_is_revalidated_after_restart_without_generating_again(self):
        self.assertIsNone(self.extract(self.wrapper()).error_code)
        prior_validations = self.compiler.calls
        self.store = Storage(Path(self.temp.name))
        self.assertIsNone(self.extract(self.wrapper()).error_code)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertGreater(self.compiler.calls, prior_validations)
        artifacts = [item for item in self.store.get_artifacts("run")
                     if item["artifact_type"] == "vision-candidate-set"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["model_id"], "configured_model:vision.ready")
        self.assertEqual(json.loads(self.incidents()[0]["usage_json"])["calls"], 1)

    def test_another_model_page_cannot_reset_the_current_incident_page_cap(self):
        scope = IncidentScope("financial-evidence", digest({"company": _company().cik}))
        explicit_scope = self.wrapper(budget_scope=scope)
        self.assertFalse(explicit_scope.automatic_budget_scope)
        self.assertIsNone(self.extract(explicit_scope).error_code)
        denied = self.extract(self.wrapper(budget_scope=scope), page=_page(131, b"second"),
                              config=replace(self.config, max_pages=1))
        self.assertEqual(denied.error_code, "RECOVERY_INCIDENT_BUDGET_EXHAUSTED")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.rendered), 1)

    def test_application_generated_adaptive_scope_keeps_per_page_recovery_behavior(self):
        scope = IncidentScope("financial-evidence", digest({"company": _company().cik}))
        automatic_scope = self.wrapper(budget_scope=scope, automatic_budget_scope=True)
        self.assertIsNone(self.extract(automatic_scope).error_code)
        second = self.extract(automatic_scope, page=_page(131, b"second"),
                              config=replace(self.config, max_pages=1))
        self.assertIsNone(second.error_code)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(len(self.rendered), 2)

    def test_explicit_limits_remain_authoritative_with_automatic_scope(self):
        scope = IncidentScope("financial-evidence", digest({"company": _company().cik}))
        limited = self.wrapper(budget_scope=scope, automatic_budget_scope=True,
                               limits=RecoveryLimits(calls=1, pages=1))
        self.assertIsNone(self.extract(limited).error_code)
        denied = self.extract(limited, page=_page(131, b"second"),
                              config=replace(self.config, max_pages=1))
        self.assertEqual(denied.error_code, "RECOVERY_INCIDENT_BUDGET_EXHAUSTED")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.rendered), 1)

    def test_unknown_synchronous_submission_does_not_generate_again_after_restart(self):
        self.provider.error = ProviderError("timeout", code="MODEL_TIMEOUT", retryable=True)
        first = self.extract(self.wrapper())
        self.assertEqual(first.error_code, "RECOVERY_SUBMISSION_UNKNOWN")
        self.store = Storage(Path(self.temp.name))
        self.extract(self.wrapper())
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(json.loads(self.incidents()[0]["usage_json"])["calls"], 1)

    def test_explicit_rate_limit_retries_in_the_same_budget_and_preserves_retry_after(self):
        ordinary = self.provider.generate_vision
        attempts = []
        def limited_once(*args):
            attempts.append(self.clock.now)
            if len(attempts) == 1:
                raise ProviderError("limited", code="MODEL_RATE_LIMITED", retry_after_seconds=7)
            return ordinary(*args)
        self.provider.generate_vision = limited_once
        scheduler = ModelRequestScheduler(clock=lambda: self.clock.now)
        with patch("openthesis.vision_financials.shared_model_scheduler", return_value=scheduler):
            self.assertIsNone(self.extract(self.wrapper()).error_code)
        self.assertEqual(attempts, [1000.0, 1007.0])
        self.assertEqual(len(self.incidents()), 1)
        used = json.loads(self.incidents()[0]["usage_json"])
        self.assertEqual((used["calls"], used["pages"]), (2, 2))

    def test_revoked_scope_stops_before_render_or_model(self):
        result = self.extract(self.wrapper(authorization_check=lambda: False))
        self.assertEqual(result.error_code, "RECOVERY_CANCELLED")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.rendered, [])

    def test_explicit_authentication_failure_requires_configuration_and_has_no_retry(self):
        self.provider.error = ProviderError("denied", code="MODEL_UNAUTHORIZED")
        result = self.extract(self.wrapper())
        self.assertEqual(result.error_code, "VISION_UNAUTHORIZED")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(json.loads(self.incidents()[0]["usage_json"])["calls"], 1)
