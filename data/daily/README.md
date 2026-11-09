# UTC 일봉 OHLCV (GitHub 적재)

업비트 공개 REST `/v1/candles/days` 로 받은 **USDT-BTC·USDT-ETH** 일봉입니다.

| 항목 | 값 |
| --- | --- |
| 기준 시각 | `candle_date_time_utc` (UTC) |
| 권고 기간 | 최근 **1825일(~5년)** |
| 최대(CLI) | 3650일(~10년) |
| 생성 | `python backfill.py` (기본값) |

파일명 예: `ohlcv_USDT-BTC.csv`, `ohlcv_USDT-ETH.csv`

봇 동적 단가(ATR·박스)는 이 폴더가 아니라 **최근 분봉**(`BACKFILL_HOURS`)을 사용합니다.
