# UTC 일봉 OHLCV (GitHub 적재)

업비트 `/v1/candles/days` **USDT-BTC·USDT-ETH** 일봉.

| 항목 | 값 |
| --- | --- |
| 시각 | `candle_date_time_utc` (UTC) |
| 권고 | 1825일(~5년), CLI 최대 3650일 |
| 생성 | `python backfill.py` |

용도: `report.py` 장기 요약 + 봇 `MULTI_TF_ENABLED` 스캘핑 필터 (`ohlcv_{MARKET}.csv`).
