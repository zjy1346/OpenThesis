from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from openthesis.markets import build_company
from openthesis.official_network import OfficialNetworkError
from openthesis.service import AppService, PreferenceValidationError, ServiceConfigurationError


class _OfflineMarketData:
    def __init__(self, companies=(), *, error: Exception | None = None, adapter=None):
        self.companies = tuple(companies)
        self.error = error
        self.adapter = adapter
        self.resolve_calls: list[tuple[str, str, int]] = []
        self.network_policy = None

    def resolve(self, query, market, *, limit=15):
        self.resolve_calls.append((query, market.value, limit))
        if self.error is not None:
            raise self.error
        return [company for company in self.companies if company.market == market.value][:limit]

    def adapter_for(self, _company):
        return self.adapter

    def configure_network(self, policy):
        self.network_policy = policy


class CompanySearchServiceTests(unittest.TestCase):
    def test_official_empty_and_unavailable_are_distinct_and_echo_request_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            market_data = _OfflineMarketData()
            service = AppService(Path(directory), market_data=market_data)

            empty = service.search_companies(
                "  600000  ", market="CN_A", request_id="empty-search-1"
            )

            self.assertEqual(empty["contract"], "openthesis.company-search.v2")
            self.assertEqual(empty["state"], "empty")
            self.assertEqual(empty["query"], "600000")
            self.assertEqual(empty["market"], "CN_A")
            self.assertEqual(empty["request_id"], "empty-search-1")
            self.assertEqual(empty["companies"], [])
            self.assertEqual(empty["diagnostics"], [])

            market_data.error = OfficialNetworkError(
                "offline test source", code="SOURCE_NETWORK_UNAVAILABLE"
            )
            unavailable = service.search_companies(
                "600000", market="CN_A", request_id="unavailable-search-2"
            )

            self.assertEqual(unavailable["state"], "unavailable")
            self.assertEqual(unavailable["query"], "600000")
            self.assertEqual(unavailable["market"], "CN_A")
            self.assertEqual(unavailable["request_id"], "unavailable-search-2")
            self.assertEqual(unavailable["diagnostics"], [{
                "code": "SOURCE_NETWORK_UNAVAILABLE",
                "diagnostic_id": "unavailable-search-2",
            }])
            self.assertEqual(market_data.resolve_calls, [
                ("600000", "CN_A", 15),
                ("600000", "CN_A", 15),
            ])

    def test_identity_preflight_failure_creates_no_research_run_or_job(self) -> None:
        requested = build_company("300750.SZ", "CATL")
        wrong_issuer = build_company("002594.SZ", "BYD")
        provider_calls: list[str] = []

        with tempfile.TemporaryDirectory() as directory:
            service = AppService(
                Path(directory),
                market_data=_OfflineMarketData([wrong_issuer]),
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )
            jobs_before = set(service._jobs)

            with self.assertRaises(ServiceConfigurationError) as raised:
                service.start_research({
                    "mode": "company",
                    "company": requested.to_dict(),
                    "model": {
                        "configured_model_id": "test.primary",
                        "configuration_version": 1,
                        "role": "primary",
                    },
                })

            self.assertEqual(raised.exception.code, "SOURCE_IDENTITY_INVALID")
            self.assertEqual(provider_calls, [])
            self.assertEqual(service.list_research_runs(), [])
            self.assertEqual(set(service._jobs), jobs_before)

    def test_cached_official_identity_with_provenance_allows_offline_research(self) -> None:
        company = build_company("300750.SZ", "CATL")

        class NoFilingsAdapter:
            def list_financial_filings(self, _company, *, limit=5):
                return []

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            online = AppService(
                data_dir,
                market_data=_OfflineMarketData([company]),
            )
            cached = online.search_companies(
                company.ticker, market="CN_A", request_id="verified-directory-seed"
            )
            self.assertEqual(cached["state"], "ready")
            self.assertEqual(cached["companies"][0]["identity_state"], "official_verified")
            self.assertTrue(cached["companies"][0]["identity_source"])
            self.assertTrue(cached["companies"][0]["identity_verified_at"])

            offline_market_data = _OfflineMarketData(
                error=OfficialNetworkError(
                    "offline test source", code="SOURCE_NETWORK_UNAVAILABLE"
                ),
                adapter=NoFilingsAdapter(),
            )
            provider_calls: list[str] = []
            offline = AppService(
                data_dir,
                market_data=offline_market_data,
                provider_factory=lambda config: provider_calls.append(config.public_id),
            )

            started = offline.start_research({
                "mode": "company",
                "company": company.to_dict(),
                "download_filings": True,
                "model": {
                    "configured_model_id": "test.primary",
                    "configuration_version": 1,
                    "role": "primary",
                },
            })

            self.assertTrue(started["run_id"])
            self.assertIsNotNone(offline.storage.get_run(started["run_id"]))
            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "offline research did not settle")
                time.sleep(0.01)
                status = offline.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["error_code"], "NO_FILINGS_AVAILABLE")
            self.assertEqual(provider_calls, [])
            self.assertEqual(len(offline_market_data.resolve_calls), 1)

    def test_network_policy_rejects_invalid_profiles_without_overwriting_saved_policy(self) -> None:
        host = "www1.hkexnews.hk"
        allowed = {
            "version": "official-network-v1",
            "connect_proxy": "",
            "host_profiles": {host: ["10.0.0.0/8"]},
        }

        with tempfile.TemporaryDirectory() as directory:
            market_data = _OfflineMarketData()
            service = AppService(Path(directory), market_data=market_data)
            saved = service.update_preferences({
                "official_network_policy": json.dumps(allowed, sort_keys=True)
            })
            saved_value = saved["official_network_policy"]
            self.assertEqual(json.loads(saved_value), allowed)
            self.assertEqual(
                tuple(str(network) for network in market_data.network_policy.host_profiles[host]),
                ("10.0.0.0/8",),
            )

            invalid_values = (
                json.dumps({**allowed, "version": "wrong-version"}),
                json.dumps({**allowed, "host_profiles": {host: ["0.0.0.0/0"]}}),
                json.dumps({**allowed, "host_profiles": {host: ["::/0"]}}),
                json.dumps({**allowed, "host_profiles": {host: ["169.254.0.0/16"]}}),
                json.dumps({**allowed, "host_profiles": {"mirror.cninfo.com.cn": ["10.0.0.0/8"]}}),
            )
            for invalid in invalid_values:
                with self.subTest(invalid=invalid):
                    with self.assertRaises(PreferenceValidationError):
                        service.update_preferences({"official_network_policy": invalid})
                    self.assertEqual(
                        service.preferences()["official_network_policy"], saved_value
                    )


if __name__ == "__main__":
    unittest.main()
