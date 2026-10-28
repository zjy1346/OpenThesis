from __future__ import annotations

import unittest

from openthesis.research_execution.registry import StageRegistry, StageSpec, standard_model_registry


class StageRegistryTests(unittest.TestCase):
    def test_recovery_invalidates_descendants_not_independent_sibling_roles(self):
        registry = standard_model_registry()
        stages = registry.descendants(("business-analyst",))
        self.assertEqual(stages, ("business-analyst", "growth-opportunities", "counter-analysis", "forecast-scenarios", "final-synthesis"))
        self.assertNotIn("financial-analyst", stages)
        self.assertNotIn("accounting-risk-analyst", stages)

    def test_ot_compiles_to_the_same_stage_contract_with_dependency_order(self):
        registry = StageRegistry.from_ot({"steps": [
            {"id": "write", "role": "synthesis", "prompt": "write.md", "depends_on": ["read"]},
            {"id": "read", "role": "research", "prompt": "read.md", "depends_on": []},
        ]})
        self.assertEqual(tuple(spec.stage_id for spec in registry.stages), ("read", "write"))
        self.assertEqual(registry.ancestors(("write",)), ("read", "write"))
        self.assertTrue(all(spec.validator_id for spec in registry.stages))

    def test_missing_dependency_duplicate_and_cycle_are_explicit(self):
        def spec(stage, *deps):
            return StageSpec(stage, "1", "handler", "validator", tuple(deps), output_contract="output")
        for stages, code in (([spec("a", "b")], "MISSING"),
                             ([spec("a"), spec("a")], "DUPLICATE"),
                             ([spec("a", "b"), spec("b", "a")], "CYCLE")):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                StageRegistry(stages)

    def test_extra_registered_adapter_does_not_require_core_graph_edits(self):
        base = StageSpec("parse", "1", "pdf", "financial-v1", output_contract="candidates-v1")
        additional = StageSpec("cloud", "1", "vision", "financial-v1", ("parse",), output_contract="candidates-v1")
        registry = StageRegistry((base, additional))
        self.assertEqual(registry.descendants(("parse",)), ("parse", "cloud"))
        self.assertNotEqual(registry.graph_hash, StageRegistry((base,)).graph_hash)
