import tempfile
import unittest
from pathlib import Path
from openthesis.domain import Company, ResearchRun, ResearchArtifact, utc_now_iso
from openthesis.service import AppService


class ThesisLineageTests(unittest.TestCase):
    def test_save_preserves_parent_source_and_rejects_other_company(self):
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))
            company = Company('issuer', 'TEST', 'Test')
            service.storage.save_company(company)
            run = ResearchRun('run', company, 'workflow', 'pack', '1', 'test', 'test', '2026-09-11')
            service.storage.save_run(run)
            source = {'evidence_id': 'filing:business', 'excerpt': 'Demand weakened.', 'source_url': 'https://example.test/report'}
            service.storage.save_artifact(ResearchArtifact('evidence', run.run_id, 'deterministic-financial-summary', 'Evidence', {'evidence': [source]}))
            base = service.storage.save_thesis_version(company.cik, {'thesis': 'Original'}, run_id=run.run_id, created_at=utc_now_iso())
            saved = service.save_thesis_version(company.cik, {'thesis': 'Revised', 'change_reason': 'Demand'}, base_thesis_version_id=base['thesis_version_id'])
            self.assertEqual(saved['run_id'], run.run_id)
            self.assertEqual(saved['sources'], [source])
            self.assertEqual(saved['content']['_parent_thesis_version_id'], base['thesis_version_id'])
            with self.assertRaises(ValueError):
                service.save_thesis_version('other', {}, base_thesis_version_id=base['thesis_version_id'])
