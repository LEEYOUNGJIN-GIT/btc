# UTC 일봉 OHLCV (GitHub 적재)

업비트 공개 REST `/v1/candles/days` 로 받은 **USDT-BTC·USDT-ETH** 일봉입니다.

| 항목 | 값 |
| --- | --- |
| 기준 시각 | `candle_date_time_utc` (UTC) |
| 권고 기간 | 최근 **1825일(~5년)** |
| 최대(CLI) | 3650일(~10년) |
| 생성 | `python backfill.py` (기본값) |

파일명 예: `ohlcv_USDT-BTC.csv`, `ohlcv_USDT-ETH.csv`

봇 실시간 동적 단가는 기동 시 **분봉 백필**을 씁니다. 이 폴더 일봉은 `python report.py` 로
장기 수익률·낙폭·그리드 요약에 연결됩니다. 분봉 오프라인 분석은 `data/minute/` 를 봅니다.
