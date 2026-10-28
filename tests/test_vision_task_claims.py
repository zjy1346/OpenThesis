from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from openthesis.domain import Company
from openthesis.storage import Storage


class VisionTaskClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp")
        self.store = Storage(Path(self.temp.name))
        self.store.save_company(Company("issuer", "EX", "Example"))
        self.identity = dict(company_cik="issuer", document_id="document", provider="mineru_flash",
                             page_hashes=("sha256-page",), page_numbers=(12,))

    def tearDown(self):
        self.temp.cleanup()

    def test_only_one_competing_worker_can_claim_submission(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.store.claim_vision_task("key", **self.identity), range(2)))
        self.assertEqual(sorted(claims), [False, True])
        self.assertEqual(self.store.get_vision_task("key")["status"], "submitting")

    def test_only_definitely_rejected_submission_may_be_claimed_again(self):
        for state in ("submitting", "submission_unknown", "created", "running", "failed", "completed"):
            self.store.save_vision_task(state, **self.identity, status=state)
            self.assertFalse(self.store.claim_vision_task(state, **self.identity))
        self.store.save_vision_task("rejected", **self.identity, status="rejected")
        self.assertTrue(self.store.claim_vision_task("rejected", **self.identity))
        self.store.save_vision_task("known-task", **self.identity, status="rejected", remote_task_id="task")
        self.assertFalse(self.store.claim_vision_task("known-task", **self.identity))

    def test_existing_task_cannot_be_rebound_to_another_source_or_task(self):
        self.store.save_vision_task("key", **self.identity, status="created", remote_task_id="original-task")
        with self.assertRaisesRegex(ValueError, "VISION_TASK_IDENTITY_CONFLICT"):
            self.store.save_vision_task("key", **{**self.identity, "document_id": "other"}, status="running")
        with self.assertRaisesRegex(ValueError, "VISION_TASK_IDENTITY_CONFLICT"):
            self.store.save_vision_task("key", **self.identity, status="running", remote_task_id="other-task")
        row = self.store.get_vision_task("key")
        self.assertEqual(row["document_id"], "document")
        self.assertEqual(row["remote_task_id"], "original-task")
