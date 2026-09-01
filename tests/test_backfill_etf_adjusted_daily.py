from datetime import UTC, datetime

from scripts.backfill_etf_adjusted_daily import _parse_chart


def test_parse_chart_uses_adjusted_close_and_skips_null_rows() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1_704_110_400, 1_704_196_800],
                    "indicators": {
                        "quote": [
                            {
                                "close": [101.0, 102.0],
                                "volume": [10, 20],
                            }
                        ],
                        "adjclose": [{"adjclose": [100.0, None]}],
                    },
                }
            ],
            "error": None,
        }
    }

    rows = _parse_chart("SPY", payload)

    assert rows == [
        {
            "symbol": "SPY",
            "ts_utc": datetime.fromtimestamp(1_704_110_400, UTC),
            "close": 101.0,
            "adjusted_close": 100.0,
            "volume": 10,
            "adjustment": "split_dividend_adjusted",
        }
    ]
