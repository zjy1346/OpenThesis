from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from openthesis.domain import ResearchRun
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.storage import Storage
from openthesis.vision_financials import (
    VisionExtractionResult,
    VisionTaskCoordinator,
    parse_vision_json,
    replay_saved_table_sources,
)
from test_durable_cloud_recovery import ScriptedAdapter
from test_resilience_core import Clock
from test_vision_financials import (
    BoundedVisionAdapter,
    MemoryVisionJournal,
    _company,
    _config,
    _filing,
    _page,
)


def _structure_candidate():
    return {
        "schema": "financial-structure-v1",
        "original_page": 130,
        "blocks": [
            {"type": "title", "text": "Consolidated Balance Sheet"},
            {"type": "text", "text": "Unit: CNY"},
            {
                "type": "table",
                "rows": [
                    [{"text": "Item"}, {"text": "2025-12-31"}],
                    [{"text": "Total assets"}, {"text": "500"}],
                    [{"text": "Total liabilities"}, {"text": "300"}],
                    [{"text": "Total equity"}, {"text": "200"}],
                    [{"text": "Unrecognized line"}, {"text": "17"}],
                ],
            },
        ],
    }


def _old_candidate():
    parsed = parse_vision_json(_structure_candidate(), _company(), _filing(), (_page(),))
    assert parsed.error_code is None
    assert {fact.concept for fact in parsed.facts} >= {"assets", "liabilities", "total_equity"}
    return replace(parsed, facts=tuple(fact for fact in parsed.facts if fact.concept != "total_equity"))


class _ReturningAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def extract(self, company, filing, pages, config, *, cancel_check=None):
        self.calls.append(tuple(page.original_page for page in pages))
        return self.result


class VisionSavedTableReplayTests(unittest.TestCase):
    def setUp(self):
        self.company = _company()
        self.filing = _filing()
        self.page = _page()
        self.old = _old_candidate()

    def test_in_memory_cache_replays_structure_and_restores_previously_omitted_equity(self):
        adapter = _ReturningAdapter(self.old)
        coordinator = VisionTaskCoordinator(adapter)
        first = coordinator.extract(self.company, self.filing, (self.page,), _config())
        self.assertNotIn("total_equity", {fact.concept for fact in first.facts})

        cached = coordinator.extract(self.company, self.filing, (self.page,), _config())
        self.assertIn("total_equity", {fact.concept for fact in cached.facts})
        self.assertIn("VISION_SAVED_SOURCE_REVALIDATED", cached.diagnostics)
        self.assertEqual(len(adapter.calls), 1)
        source = next(ref for ref in cached.evidence if ref.evidence_id.startswith("vision-table:"))
        self.assertIn("Unrecognized line", source.excerpt)
        self.assertTrue({ref.evidence_id for ref in self.old.evidence}.issubset(
            {ref.evidence_id for ref in cached.evidence}
        ))

    def test_new_coordinator_replays_persistent_cache_without_adapter_call(self):
        journal = MemoryVisionJournal()
        first_adapter = _ReturningAdapter(self.old)
        first = VisionTaskCoordinator(first_adapter, journal=journal).extract(
            self.company, self.filing, (self.page,), _config()
        )
        self.assertNotIn("total_equity", {fact.concept for fact in first.facts})

        next_adapter = _ReturningAdapter(self.old)
        restored = VisionTaskCoordinator(next_adapter, journal=journal).extract(
            self.company, self.filing, (self.page,), _config()
        )
        self.assertIn("total_equity", {fact.concept for fact in restored.facts})
        self.assertIn("VISION_RESULT_CACHE_HIT", restored.diagnostics)
        self.assertIn("VISION_SAVED_SOURCE_REVALIDATED", restored.diagnostics)
        self.assertEqual(next_adapter.calls, [])

    def test_corrupt_proof_binding_is_controlled_and_does_not_call_adapter(self):
        journal = MemoryVisionJournal()
        first_adapter = _ReturningAdapter(self.old)
        VisionTaskCoordinator(first_adapter, journal=journal).extract(
            self.company, self.filing, (self.page,), _config()
        )
        task_key, stored = next(iter(journal.results.items()))
        proof = next(item for item in stored["evidence"] if item["evidence_id"].startswith("vision-table:"))
        payload = json.loads(proof["excerpt"])
        payload["page_hash"] = "0" * 64
        proof["excerpt"] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        journal.results[task_key] = stored

        next_adapter = _ReturningAdapter(self.old)
        restored = VisionTaskCoordinator(next_adapter, journal=journal).extract(
            self.company, self.filing, (self.page,), _config()
        )
        self.assertEqual(restored.error_code, "VISION_CACHED_SOURCE_INVALID")
        self.assertEqual(restored.diagnostics, ("VISION_CACHED_SOURCE_INVALID",))
        self.assertEqual(next_adapter.calls, [])

    def test_altered_normalized_table_amount_is_rejected_before_raw_source_replay(self):
        adapter = _ReturningAdapter(self.old)
        coordinator = VisionTaskCoordinator(adapter)
        first = coordinator.extract(self.company, self.filing, (self.page,), _config())
        self.assertIsNone(first.error_code)
        changed = replace(first,
            facts=tuple(replace(fact, value=fact.value * 999) for fact in first.facts))
        coordinator._completed_results[next(iter(coordinator._completed_results))] = changed
        replayed = coordinator.extract(self.company, self.filing, (self.page,), _config())
        self.assertEqual(replayed.error_code, "VISION_CACHED_SOURCE_INVALID")
        self.assertIn("VISION_CANDIDATE_INTEGRITY_FAILED", replayed.diagnostics)
        self.assertEqual(adapter.calls, [(self.page.original_page,)])

        # The pure helper uses the same classification, allowing DurableCloudVisionAdapter
        # to distinguish altered normalized candidates from malformed source proofs.
        replayed = replay_saved_table_sources(changed, self.company, self.filing, (self.page,))
        self.assertEqual(replayed.error_code, "VISION_CACHED_SOURCE_INVALID")
        self.assertIn("VISION_CANDIDATE_INTEGRITY_FAILED", replayed.diagnostics)
        self.assertEqual(replayed.facts, ())

    def test_durable_committed_candidate_replays_after_restart_without_second_model_call(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Storage(Path(temp.name))
        store.save_company(self.company)
        store.save_run(ResearchRun("run", self.company, "wf", "pack", "1", "", "", "2026-10-26"))
        clock = Clock()
        calls = []

        def produce(_context, _task):
            calls.append("model")
            return self.old

        wrapper = DurableCloudVisionAdapter(
            ScriptedAdapter(produce), store, "run", clock=clock, sleeper=clock.sleep
        )
        initial = wrapper.extract(self.company, self.filing, (self.page,), _config(provider="configured_model"))
        self.assertIsNone(initial.error_code)
        self.assertIn("total_equity", {fact.concept for fact in initial.facts})
        self.assertEqual(calls, ["model"])

        artifacts = [item for item in store.get_artifacts("run") if item["artifact_type"] == "vision-candidate-set"]
        self.assertEqual(len(artifacts), 1)
        restarted_store = Storage(Path(temp.name))
        no_call_adapter = ScriptedAdapter(lambda _context, _task: self.fail("committed candidate must be replayed"))
        restarted = DurableCloudVisionAdapter(
            no_call_adapter, restarted_store, "run", clock=clock, sleeper=clock.sleep
        ).extract(self.company, self.filing, (self.page,), _config(provider="configured_model"))
        self.assertIsNone(restarted.error_code)
        self.assertIn("total_equity", {fact.concept for fact in restarted.facts})
        self.assertEqual(calls, ["model"])
        self.assertEqual(no_call_adapter.calls, [])

    def test_flat_legacy_cache_without_table_proof_is_unchanged(self):
        legacy = BoundedVisionAdapter().extract(
            self.company, self.filing, (self.page,), _config()
        )
        legacy = VisionExtractionResult(
            legacy.facts, legacy.evidence, (*legacy.diagnostics, "legacy-marker"),
            legacy.error_code, legacy.retry_after_seconds,
        )
        self.assertFalse(any(ref.evidence_id.startswith("vision-table:") for ref in legacy.evidence))
        self.assertIs(replay_saved_table_sources(legacy, self.company, self.filing, (self.page,)), legacy)

    def test_replay_accepts_column_currency_and_scale_overrides_from_bound_structure(self):
        for header_suffix, scale, expected_current, expected_prior in (
            ("RMB", 1_000_000, 100_000_000, 80_000_000),
            ("RMB thousand", 1_000, 100_000, 80_000),
        ):
            with self.subTest(header_suffix=header_suffix):
                payload = {
                    "schema": "financial-structure-v1",
                    "original_page": self.page.original_page,
                    "blocks": [
                        {"type": "title", "text": "Consolidated Income Statement"},
                        {"type": "text", "text": "Amounts in USD millions"},
                        {"type": "table", "rows": [
                            [{"text": "Item"}, {"text": f"2025 {header_suffix}"},
                                {"text": f"2024 {header_suffix}"}],
                            [{"text": "Revenue"}, {"text": "100"}, {"text": "80"}],
                        ]},
                    ],
                }
                source = parse_vision_json(payload, self.company, self.filing, (self.page,))
                self.assertIsNone(source.error_code)
                expected = {
                    (2025, expected_current, "CNY", float(scale)),
                    (2024, expected_prior, "CNY", float(scale)),
                }
                self.assertEqual({(fact.fiscal_year, fact.value, fact.currency, fact.unit_scale)
                    for fact in source.facts}, expected)
                compiler = FinancialFactCompiler()
                compiled = compiler.compile_facts(self.company, (self.filing,), source.facts,
                    evidence=source.evidence, required_concepts=frozenset({"revenue"}))
                self.assertEqual({(fact.fiscal_year, fact.value, fact.currency, fact.unit_scale)
                    for fact in compiled.accepted_facts}, expected)
                self.assertFalse(compiled.quarantined_facts)

                adapter = _ReturningAdapter(source)
                coordinator = VisionTaskCoordinator(adapter)
                first = coordinator.extract(self.company, self.filing, (self.page,), _config())
                self.assertIsNone(first.error_code)
                replayed = coordinator.extract(self.company, self.filing, (self.page,), _config())
                self.assertIsNone(replayed.error_code)
                self.assertIn("VISION_SAVED_SOURCE_REVALIDATED", replayed.diagnostics)
                self.assertEqual({(fact.fiscal_year, fact.value, fact.currency, fact.unit_scale)
                    for fact in replayed.facts}, expected)
                recompiled = compiler.compile_facts(self.company, (self.filing,), replayed.facts,
                    evidence=replayed.evidence, required_concepts=frozenset({"revenue"}))
                self.assertEqual({(fact.fiscal_year, fact.value, fact.currency, fact.unit_scale)
                    for fact in recompiled.accepted_facts}, expected)
                self.assertFalse(recompiled.quarantined_facts)
                self.assertEqual(adapter.calls, [(self.page.original_page,)])


if __name__ == "__main__":
    unittest.main()
