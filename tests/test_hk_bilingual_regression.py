"""Source-derived HKEX CAS geometry through the production PDF AST boundary."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from openthesis.domain import FilingDocument
from openthesis.markets import build_company
from openthesis.financial_ingestion import FinancialIngestionEngine

FIXTURE = Path(__file__).parent / 'fixtures' / 'hk_cas_statement_geometry.json'


class Page:
    def __init__(self, value): self.value = value
    def extract_text(self): return self.value.get('text', '')
    def extract_words(self, **kwargs): return self.value.get('words', [])


class Pdf:
    def __init__(self, pages): self.pages = [Page(pages.get(str(number), {})) for number in range(1, 132)]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class HkBilingualRegressionTests(unittest.TestCase):
    def extract(self, period='2025-12-31'):
        fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        company = build_company('01234.HK', 'Generic CAS issuer', reporting_currency='CNY', accounting_standard='CAS')
        filing = FilingDocument('source-cas', company.security_id, 'source-cas', 'ANNUAL_REPORT', 'FY', period,
                                '2026-03-11', '2025 Annual Report', fixture['source_url'],
                                'source.pdf', fixture['sha256'])
        with patch('pdfplumber.open', return_value=Pdf(fixture['pages'])), patch(
            'openthesis.financial_ingestion._candidate_financial_pages', return_value=frozenset(map(int, fixture['pages']))
        ):
            return FinancialIngestionEngine().extract_pdf_candidates('source.pdf', company, filing)

    def test_official_closing_balance_and_cas_operating_revenue_are_not_lost(self):
        facts, refs = self.extract()
        current = {f.concept: f for f in facts if f.fiscal_year == 2025 and f.scope == 'consolidated'}
        for concept, value in {'revenue': 423701834000, 'assets': 974827544000,
                               'liabilities': 603801220000, 'equity': 337107747000}.items():
            self.assertIn(concept, current)
            self.assertEqual(current[concept].value, value)
            self.assertEqual(current[concept].currency, 'CNY')
            self.assertEqual(current[concept].unit_scale, 1000)
            self.assertIsNotNone(current[concept].source_bbox)
        self.assertEqual(len(facts), len(refs))

    def test_absent_provider_period_uses_document_date(self):
        facts, _ = self.extract('')
        self.assertTrue(any(f.concept == 'assets' and f.end_date == '2025-12-31' for f in facts))
