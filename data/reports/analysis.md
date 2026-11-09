# OHLCV 백필 분석

분봉은 봇 `analyzer.analyze` 와 동일한 동적 단가 지표입니다.
일봉은 같은 지표에 더해 장기 수익률·최대낙폭·BTC/ETH 상관을 붙입니다.

- 일봉 BTC↔ETH 종가 상관: `0.662851`

## 분봉 (data/minute)

### USDT-BTC

- 파일: `data/minute/ohlcv_USDT-BTC.csv`
- 구간(UTC): `2026-11-02T03:37:00` ~ `2026-11-09T03:30:00`
- 봉 수: `2733`
- 동적 단가: support `74335.0600000000`, resistance `75912.0510000000`, grid `0.2000%`, ATR `0.0605%`
- 히스토리: return `-4.4579%`, MDD `8.3556%`, mean range `0.0342%`

### USDT-ETH

- 파일: `data/minute/ohlcv_USDT-ETH.csv`
- 구간(UTC): `2026-11-02T03:41:00` ~ `2026-11-09T03:19:00`
- 봉 수: `858`
- 동적 단가: support `2130.0000000000`, resistance `2241.6400000000`, grid `0.2000%`, ATR `0.1719%`
- 히스토리: return `-7.0799%`, MDD `13.6693%`, mean range `0.0748%`

## 일봉 (data/daily)

### USDT-BTC

- 파일: `data/daily/ohlcv_USDT-BTC.csv`
- 구간(UTC): `2021-11-11T00:00:00` ~ `2026-11-09T00:00:00`
- 봉 수: `1825`
- 동적 단가: support `63089.8880000000`, resistance `84954.5120000000`, grid `0.4000%`, ATR `2.8384%`
- 히스토리: return `16.1274%`, MDD `76.1297%`, mean range `4.1563%`

### USDT-ETH

- 파일: `data/daily/ohlcv_USDT-ETH.csv`
- 구간(UTC): `2021-11-11T00:00:00` ~ `2026-11-09T00:00:00`
- 봉 수: `1825`
- 동적 단가: support `1856.5430000000`, resistance `2717.1350000000`, grid `0.4000%`, ATR `3.9119%`
- 히스토리: return `-54.9133%`, MDD `79.1134%`, mean range `5.9626%`
