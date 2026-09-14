import unittest
from datetime import date
from openthesis.markets import COMMON_MARKET_COMPANIES
from openthesis.research_readiness import readiness, primary_market_alternative


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.company = next(c for c in COMMON_MARKET_COMPANIES if c.ticker == '03750.HK')

    def test_catalogue_presence_does_not_establish_recommendation(self):
        self.assertFalse(readiness(self.company, [])['recommended'])
        self.assertEqual(primary_market_alternative(self.company, COMMON_MARKET_COMPANIES).ticker, '300750.SZ')

    def test_requires_consecutive_current_verified_same_scope_history(self):
        groups = [{'status': 'VERIFIED', 'fiscal_period': 'FY', 'consolidated_scope': 'consolidated',
                   'currency': 'CNY', 'period_end': f'{year}-12-31', 'issues': [],
                   'covered_concepts': ['revenue', 'assets', 'liabilities', 'equity']} for year in range(2021, 2026)]
        self.assertTrue(readiness(self.company, groups, today=date(2026, 9, 11))['recommended'])
        groups[2]['status'] = 'REJECTED'
        self.assertFalse(readiness(self.company, groups, today=date(2026, 9, 11))['recommended'])

    def test_freshness_tracks_fiscal_year_end_and_publication_window(self):
        groups = [{'status': 'VERIFIED', 'fiscal_period': 'FY', 'consolidated_scope': 'consolidated',
                   'currency': 'CNY', 'period_end': f'{year}-12-31', 'issues': [],
                   'covered_concepts': ['revenue', 'assets', 'liabilities', 'equity']} for year in range(2020, 2025)]
        for today in (date(2026, 1, 1), date(2026, 4, 30)):
            self.assertTrue(readiness(self.company, groups, today=today)['recommended'])
        self.assertFalse(readiness(self.company, groups, today=date(2026, 5, 1))['recommended'])
        for group in groups:
            group['period_end'] = group['period_end'].replace('-12-31', '-06-30')
        self.assertTrue(readiness(self.company, groups, today=date(2025, 10, 30))['recommended'])
        self.assertFalse(readiness(self.company, groups, today=date(2025, 10, 31))['recommended'])
