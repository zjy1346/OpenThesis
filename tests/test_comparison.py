from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openthesis.comparison import compare_research_runs
from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchArtifact, ResearchRun
from openthesis.storage import Storage


class ComparisonTests(unittest.TestCase):
    def test_compares_common_and_one_sided_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            left = ResearchRun(
                run_id="left",
                company=DEMO_COMPANY,
                workflow_id="wf",
                research_pack_id="pack",
                research_pack_version="1",
                provider_id="p1",
                model_id="m1",
                data_as_of="2026-01-01",
            )
            right = ResearchRun(
                run_id="right",
                company=DEMO_COMPANY,
                workflow_id="wf",
                research_pack_id="pack",
                research_pack_version="1",
                provider_id="p2",
                model_id="m2",
                data_as_of="2026-01-01",
            )
            storage.save_run(left)
            storage.save_run(right)
            for run, claims in (
                (left, ["common", "left only"]),
                (right, ["common", "right only"]),
            ):
                storage.save_artifact(
                    ResearchArtifact(
                        artifact_id=f"{run.run_id}:report",
                        run_id=run.run_id,
                        artifact_type="research-report",
                        title="report",
                        content={
                            "report": {
                                "executive_summary": run.model_id,
                                "claims": [
                                    {"text": text, "kind": "inference"}
                                    for text in claims
                                ],
                            }
                        },
                        model_id=run.model_id,
                        agent_id="synthesis",
                    )
                )
            comparison = compare_research_runs(storage, left, right)
            self.assertEqual(comparison.content["common_claims"], ["common"])
            self.assertEqual(comparison.content["primary_only_claims"], ["left only"])
            self.assertEqual(
                comparison.content["secondary_only_claims"], ["right only"]
            )
            self.assertEqual(
                storage.get_artifacts(left.run_id)[-1]["artifact_type"],
                "model-comparison",
            )


if __name__ == "__main__":
    unittest.main()

