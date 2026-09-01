from datetime import date

from scripts.build_sec_derived_market_caps import _shares_asof


def test_sec_shares_asof_ignores_later_filing() -> None:
    payload = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2025-03-31",
                                "filed": "2025-04-20",
                                "form": "10-Q",
                                "val": 100,
                            },
                            {
                                "end": "2025-06-30",
                                "filed": "2025-07-20",
                                "form": "10-Q",
                                "val": 200,
                            },
                        ]
                    }
                }
            }
        }
    }

    result = _shares_asof(payload, date(2025, 6, 1))

    assert result is not None
    assert result[0] == 100
    assert result[1] == date(2025, 4, 20)
