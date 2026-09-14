import unittest

from openthesis.market_snapshot import EcbFxAdapter


class FxIntegrityTests(unittest.TestCase):
    def test_missing_observation_date_cannot_be_invented(self):
        class Transport:
            def get_json(self, url, *, timeout):
                return {'dataSets': [{'series': {'0': {'observations': {'0': [8.0]}}}}]}
        self.assertIsNone(EcbFxAdapter(Transport()).rate('HKD', 'CNY', '2026-09-10'))

    def test_cross_rate_uses_latest_common_observation_date_and_cache(self):
        class Transport:
            calls = 0
            def get_json(self, url, *, timeout):
                self.calls += 1
                if 'HKD.EUR' in url:
                    return 'CURRENCY,TIME_PERIOD,OBS_VALUE\nHKD,2026-09-09,8\nHKD,2026-09-10,10\n'
                return 'CURRENCY,TIME_PERIOD,OBS_VALUE\nCNY,2026-09-09,7\n'
        transport = Transport()
        adapter = EcbFxAdapter(transport)
        value = adapter.rate('HKD', 'CNY', '2026-09-10')
        self.assertEqual(value.rate, 7 / 8)
        self.assertEqual(value.as_of, '2026-09-09')
        self.assertEqual(adapter.rate('HKD', 'CNY', '2026-09-10'), value)
        self.assertEqual(transport.calls, 2)

    def test_normalized_payload_without_pair_and_date_is_not_a_cross_rate(self):
        class Transport:
            def get_json(self, url, *, timeout):
                return {'rate': .13}
        self.assertIsNone(EcbFxAdapter(Transport()).rate('HKD', 'CNY', '2026-09-10'))
