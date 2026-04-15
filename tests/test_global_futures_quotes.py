import unittest

from src.collectors.akshare_collector import _parse_sina_global_futures_line


class TestGlobalFuturesQuotes(unittest.TestCase):
    def test_parse_spot_gold_line(self):
        row = _parse_sina_global_futures_line(
            'var hq_str_hf_XAU="4721.03,4749.050,4721.03,4721.38,4744.42,4639.65,22:17:00,4749.05,4639.65,0,0,0,2026-04-13,伦敦金（现货黄金）";'
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["symbol"], "hf_XAU")
        self.assertEqual(row["name"], "伦敦金（现货黄金）")
        self.assertEqual(row["trade_date"], "2026-04-13")
        self.assertEqual(row["tick_time"], "22:17:00")
        self.assertAlmostEqual(row["current_price"], 4721.03, places=2)
        self.assertAlmostEqual(row["prev_close"], 4749.05, places=2)
        self.assertAlmostEqual(row["open_price"], 4721.03, places=2)
        self.assertAlmostEqual(row["high_price"], 4744.42, places=2)
        self.assertAlmostEqual(row["low_price"], 4639.65, places=2)
        self.assertAlmostEqual(row["change_amount"], -28.02, places=2)
        self.assertAlmostEqual(row["change_pct"], -0.59, places=2)

    def test_parse_wti_line(self):
        row = _parse_sina_global_futures_line(
            'var hq_str_hf_CL="91.598,,91.530,91.670,92.380,86.960,13:54:45,91.280,94.010,0,2,2,2026-04-15,纽约原油,2505";'
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["symbol"], "hf_CL")
        self.assertEqual(row["name"], "纽约原油")
        self.assertAlmostEqual(row["current_price"], 91.598, places=3)
        self.assertAlmostEqual(row["prev_close"], 91.28, places=2)
        self.assertAlmostEqual(row["open_price"], 91.53, places=2)
        self.assertAlmostEqual(row["high_price"], 92.38, places=2)
        self.assertAlmostEqual(row["low_price"], 86.96, places=2)
        self.assertAlmostEqual(row["change_pct"], 0.35, places=2)

    def test_parse_empty_line_returns_none(self):
        row = _parse_sina_global_futures_line('var hq_str_hf_BRT="";')
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
