import unittest
from unittest.mock import patch

from src.core.instrument_service import get_futures_quotes


class TestFuturesQuoteFallback(unittest.TestCase):
    @patch("src.core.instrument_service._build_contract_index")
    @patch("src.core.instrument_service.resolve_future_contract")
    def test_contract_index_primary_restores_quote_fields_without_contract_lookup(
        self,
        mock_resolve_contract,
        mock_build_contract_index,
    ):
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

        mock_resolve_contract.assert_not_called()
        self.assertIn("AG2606", result)
        self.assertEqual(result["AG2606"]["current_price"], 8123.0)
        self.assertEqual(result["AG2606"]["underlying_name"], "白银")
        self.assertEqual(result["AG2606"]["underlying_symbol"], "AG")


if __name__ == "__main__":
    unittest.main()
