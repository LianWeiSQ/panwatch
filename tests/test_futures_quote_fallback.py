import unittest
from unittest.mock import patch

from src.core.instrument_service import get_futures_quotes
from src.core.tushare_futures import TushareUnavailable


class TestFuturesQuoteFallback(unittest.TestCase):
    @patch("src.core.instrument_service._build_contract_index")
    @patch("src.core.instrument_service.resolve_future_contract")
    @patch("src.core.instrument_service.get_tushare_futures_quotes")
    def test_contract_index_fallback_restores_quote_fields(
        self,
        mock_tushare_quotes,
        mock_resolve_contract,
        mock_build_contract_index,
    ):
        mock_tushare_quotes.side_effect = TushareUnavailable("permission denied")
        mock_resolve_contract.return_value = {
            "symbol": "AG2606",
            "market": "CN_FUT",
            "instrument_type": "future",
            "name": "沪银2606",
            "exchange": "SHFE",
            "underlying_symbol": "AG",
            "underlying_name": "沪银2606",
            "product_name": "沪银2606",
            "contract_multiplier": 1.0,
            "tushare_ts_code": "AG2606.SHF",
        }
        mock_build_contract_index.return_value = {
            "AG2606": {
                "symbol": "AG2606",
                "market": "CN_FUT",
                "instrument_type": "future",
                "name": "白银2606",
                "exchange": "SHFE",
                "underlying_symbol": "",
                "underlying_name": "白银",
                "product_name": "白银",
                "current_price": 8123.0,
                "change_pct": 1.23,
                "contract_multiplier": 1.0,
            }
        }

        result = get_futures_quotes(["AG2606"])

        self.assertIn("AG2606", result)
        self.assertEqual(result["AG2606"]["current_price"], 8123.0)
        self.assertEqual(result["AG2606"]["underlying_name"], "白银")
        self.assertEqual(result["AG2606"]["underlying_symbol"], "AG")
        self.assertEqual(result["AG2606"]["tushare_ts_code"], "AG2606.SHF")


if __name__ == "__main__":
    unittest.main()
