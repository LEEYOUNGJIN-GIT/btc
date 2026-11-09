# UTC 분봉 OHLCV (GitHub 적재)

업비트 `/v1/candles/minutes/1` 로 받은 **USDT-BTC·USDT-ETH** 1분봉입니다.

| 항목 | 값 |
| --- | --- |
| 기준 시각 | `candle_date_time_utc` (UTC) |
| 권고 기간 | 최근 **168시간(7일)** |
| 최대(CLI) | 336시간(14일) |
| 용도 | 봇 `analyzer.analyze` 와 동일한 ATR·박스·그리드 오프라인 분석 |

생성:

```bash
python backfill.py --timeframe minute
# 또는 일봉+분봉: python backfill.py --timeframe bundle
python report.py
```
