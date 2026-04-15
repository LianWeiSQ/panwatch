import json
import unittest

from src.core.market_data import market_data
from src.models.market import MarketCode
from src.core.tushare_futures import _safe_multiplier


class TestRuntimeQuoteSerialization(unittest.TestCase):
    def test_quote_response_converts_nan_to_none(self):
        row = market_data._quote_response(
            "FU2605",
            MarketCode.CN_FUT,
            {
                "name": "燃油2605",
                "contract_multiplier": float("nan"),
                "current_price": None,
                "change_pct": None,
            },
        )
        self.assertIsNone(row["contract_multiplier"])
        json.dumps(row, ensure_ascii=False, allow_nan=False)

    def test_safe_multiplier_falls_back_for_nan(self):
        self.assertEqual(_safe_multiplier(float("nan"), default=1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
