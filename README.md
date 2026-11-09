# USDT 왕복 스캘핑 봇

업비트 USDT 현물(기본 `USDT-BTC`)에서 **지정가 매수 → 목표가 지정가 매도**를 반복합니다. 현물이므로 숏은 하지 않고, 현금(USDT)과 코인 중 하나만 들고 있습니다.

기본값은 **모의거래**입니다. 실주문은 `.env`에서 `DRY_RUN=false`와 `LIVE_TRADING_CONFIRM=YES`를 같이 켜야 나갑니다.

이 프로그램은 투자 자문이 아닙니다. 수수료, 미체결, 슬리피지, 봇 중단으로 원금이 줄 수 있습니다.

## 동작

1. 시작 시 `BACKFILL_MARKETS`(기본 `USDT-BTC,USDT-ETH`)의 최근 `BACKFILL_HOURS`(기본 24시간, 최대 14일) **분봉**을 SQLite에 받습니다. 이미 있는 구간은 upsert로 갱신하고 창 밖 행은 prune 합니다. 봇이 켜져 있을 때만 `ANALYZE_REFRESH_SEC`마다 이어 받습니다. GitHub CSV는 봇과 별도로 **Actions 매일 갱신**(아래)합니다.
2. 그 데이터로 ATR과 평균 고저폭을 내고, 최근 `RANGE_LOOKBACK`개 봉의 하위/상위 백분위로 박스 하단·상단을 잡습니다. 변동폭은 `MIN_GRID_RATE`~`MAX_GRID_RATE`(기본 0.20%~0.40%)로 자르며, 왕복 수수료 0.10%보다 좁으면 수수료 위로 올립니다.
3. 현재가가 그 박스 **하단 구간**(`ENTRY_ZONE`, 기본 아래 40%)이고, 박스 하단을 이탈하지 않았을 때만 매수합니다.
4. 매수가는 최우선 매수호가를 넘지 않습니다. 박스 저가와 그리드 간격만큼 아래 가격 중 체결에 더 가까운 쪽을 USDT 호가 단위로 내림합니다. 백필이 없으면 고정 `BUY_OFFSET_RATE`를 씁니다.
5. 주문 가능 USDT에 `BALANCE_USE_RATIO`를 곱한 금액을 씁니다. 매수 수수료는 견적자산에서 빠지는 것으로 보고, `가격 × 수량 × (1 + 매수수수료)`가 잔고를 넘지 않게 수량을 내립니다. 비율이 1이면 사실상 전액입니다.
6. 체결되면 최소 매도가를 올립니다. 동적 그리드가 있으면 `매수가 × (1 + 그리드 간격)`과 수수료 손익분기 중 높은 쪽입니다. 그리드가 없으면 두 값 중 **더 높은 가격**을 호가 단위로 올립니다.
   - 정산 기준: `매수가 × (1 + 매수수수료) × (1 + 목표순수익률) / (1 - 매도수수료)`
   - 가산 기준: `매수가 × (1 + 매수수수료 + 매도수수료 + 목표순수익률)`  
     수수료가 각 0.05%이면 왕복 **0.10% + 목표 수익률**입니다.
7. 백필이 꺼져 있거나 실패하면 수수료 0.05%와 목표 순수익 0.25%를 쓰고, 매수가 100 USDT의 최소 매도가는 **100.36**입니다. 그리드가 0.30%이면 같은 매수가의 매도가는 **100.30**이며, 이 가격은 왕복 수수료 0.10%를 넘습니다. `ANALYZE_REFRESH_SEC`마다 캔들을 이어 받고 간격을 다시 계산합니다. 미체결 주문은 `DYNAMIC_REPRICE_SEC` 이후 새 단가로 바꿉니다.
8. 매수 체결 후 현재가가 손절가 이하로 내려가면 지정가 매도를 취소하고, 설정에 따라 시장가 또는 매수 1호가 지정가로 팝니다. 손절은 이익 하한을 지키지 않습니다. 손실을 자르는 주문입니다.
9. 매수가 `BUY_TIMEOUT_SEC` 동안 안 사지면 취소 후 다시 호가를 넣습니다. 매도는 `SELL_REPRICE_SEC` 뒤 호가에 더 붙이되, 수수료 손익분기 아래로는 내리지 않습니다.
10. 손절 뒤에는 `POST_STOP_COOLDOWN_SEC` 동안 새 매수를 쉬고, `MAX_DAILY_LOSS_QUOTE`가 0보다 크면 그날 실현손실이 한도를 넘는 순간 멈춥니다. 실전에서는 이 손익과 쿨다운이 `position_state.json`에 남아 재시작 후에도 유지됩니다. 모의거래는 상태 파일을 쓰지 않습니다.
11. 최근 봉이 한 주기보다 10분 이상 오래되면 동적 단가를 갱신하지 않습니다. 아직 한 번도 계산하지 못했으면 고정 퍼센트를 씁니다. 진행 중인 봉은 고저가 덜 열려 있어 ATR에서 뺍니다.
12. 평단을 모르는 미체결 매도는 그 가격 그대로 두고 손절과 정정을 하지 않습니다. 그 주문이 사라진 뒤 코인만 남으면 자동 매도 없이 멈춥니다. 손절 폭이 그리드(또는 수수료+목표 수익)보다 좁으면 시작 시 경고합니다. 현재가가 0이거나 손절가가 0이면 손절하지 않습니다.

손절은 업비트 조건부 주문이 아닙니다. **프로세스가 꺼지거나 네트워크가 끊기면 손절되지 않습니다.** 종료 시 미체결 매수는 취소하고, 이미 든 코인의 매도 주문은 거래소에 남겨 둡니다.

## 프로젝트 구조

| 파일 | 역할 |
| --- | --- |
| `main.py` | 상태 머신, 시작 시 백필, 동적 단가 갱신 |
| `config.py` | `.env` 설정. 백필 기간·그리드 범위 포함 |
| `exchange_api.py` | 잔고, 시세, 분봉 페이지, 주문, 현재가 WebSocket |
| `backfill.py` | 일봉 5년 + 분봉 7일 GitHub CSV 수집 CLI (`--timeframe bundle`) |
| `report.py` | `data/minute`·`data/daily` 분석 + 멀티TF 스캘핑 구간 |
| `scalp_zone.py` | 분봉+일봉 지표로 진입 구간·목표·손절·허용 여부 산출 |
| `data/daily/` | BTC·ETH UTC 일봉 CSV (권고 5년) |
| `data/minute/` | BTC·ETH UTC 1분봉 CSV (권고 7일, analyzer 동일 지표) |
| `data/reports/` | `report.py` 결과 JSON/Markdown |
| `analyzer.py` | ATR, 고저폭, 박스, 0.20%~0.40% 그리드 단가 |
| `strategy.py` | 동적 단가가 있을 때 그 가격으로, 없으면 고정 퍼센트로 주문 |
| `logger.py` | 콘솔 + `trading.log` 회전 로그 |

시세·주문은 업비트 공식 REST/WebSocket을 직접 호출합니다. `pyupbit.get_tick_size`는 원화 마켓 호가표라 USDT-BTC(1만 USDT 이상도 호가 0.01)에 쓰면 가격이 어긋납니다.

## 설치와 실행

Python 3.10 이상이 필요합니다.

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

macOS / Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

모의거래는 API 키 없이 공개 시세만 사용합니다. 가상 잔고는 `DRY_RUN_QUOTE`(기본 1000 USDT)입니다. 로그에 `[모의]`가 붙습니다.

```bash
python -m unittest discover -s tests -v
```

### 데이터 최신화 (봇이 꺼져 있어도)

| 대상 | 갱신 주체 | 주기 |
| --- | --- | --- |
| 봇 런타임 SQLite 분봉 | `main.py` (켜져 있을 때만) | `ANALYZE_REFRESH_SEC`(기본 60초) |
| GitHub `data/daily`·`data/minute`·`data/reports` | **GitHub Actions** `Refresh OHLCV` | 매일 00:15 UTC (+ 수동 `workflow_dispatch`) |

Actions는 공개 시세만 쓰며 API 키가 필요 없습니다. 워크플로: `.github/workflows/refresh-ohlcv.yml`.

로컬에서 같은 작업을 하려면:

```bash
python backfill.py          # bundle: 일봉 5년 + 분봉 7일 CSV
python report.py            # 스캘핑 구간 리포트
```

봇은 `MULTI_TF_ENABLED=true` 이면 분봉 백필과 `data/daily/ohlcv_{MARKET}.csv` 를 같이 보고
스캘핑 구간(`scalp_zone`)을 갱신합니다. `actionable=false` 이면 신규 매수를 건너뜁니다.

일봉만 / 분봉만:

```bash
python backfill.py --timeframe day --days 1825
python backfill.py --timeframe minute --hours 168
```

## .env

예시 전체는 `.env.example`에 있습니다. 실전에 최소한 필요한 값은 아래입니다.

```env
UPBIT_ACCESS_KEY=발급받은_액세스_키
UPBIT_SECRET_KEY=발급받은_시크릿_키
MARKET=USDT-BTC
DRY_RUN=false
LIVE_TRADING_CONFIRM=YES
BALANCE_USE_RATIO=0.05
TARGET_PROFIT_RATE=0.0025
STOP_LOSS_RATE=0.005
```

- API 키는 **거래 권한만** 켜고 출금 권한은 끄세요. IP 제한을 거세요. `.env`는 git에 넣지 마세요.
- `BALANCE_USE_RATIO=1`은 주문 가능 USDT를 한 번에 씁니다. 첫 실전은 `0.05`~`0.1`처럼 작게 두세요.
- `QUOTE_RESERVE` 기본값은 0입니다. 거래소가 반올림으로 잔고 부족을 반복해서 돌려주면 `0.01` 정도로 올리세요. 잔고 부족이 한 번 나면 수량을 0.1% 줄여 다시 넣습니다.
- 실전 기동 시 `/v1/orders/chance`의 `bid_fee`, `ask_fee`를 읽을 수 있으면 그 수수료로 최소 매도가를 다시 계산합니다.
- 재시작 시 `position_state.json`과 미체결 주문, USDT 마켓 코인 잔고를 맞춥니다. KRW 마켓에서 산 코인(`unit_currency=KRW`)은 건드리지 않습니다.
- 같은 계정으로 이 봇을 두 개 띄우지 마세요. 잔고를 서로 잡아먹습니다.

`MAX_RUNTIME_SEC=0`이면 종료 신호(Ctrl+C)까지 반복합니다.

## 실전에서 볼 슬리피지와 API 제한

- USDT-BTC 호가 간격은 0.01 USDT로 좁아 보여도 잔량이 KRW-BTC보다 훨씬 얇은 경우가 많습니다. 지정가 매수·매도는 그 가격 이상으로 체결되지만, **시장가 손절은 빈 호가를 따라 내려가며** 손절 비율보다 더 잃을 수 있습니다. 주문 전에 호가 잔량과 주문금액을 비교하세요. 로그에 잔량 부족 경고가 나오면 비중을 줄이세요.
- 목표 0.25%는 왕복 수수료 0.10%를 뺀 **순수익**입니다. 스프레드와 미체결 재호가가 이 폭을 먹으면 체결이 안 되거나 손익이 거의 없습니다. `BUY_OFFSET_RATE`를 키우면 더 싸게 사지만 체결이 줄고, 0에 가깝게 두면 수수료만 내고 되팔 자리가 없을 수 있습니다.
- 손절 폭이 목표 수익보다 크면(기본 0.50% 손절, 0.25% 목표) 승률이 높아도 기대값이 마이너스일 수 있습니다. 과거 데이터로 간격을 맞추기 전에는 소액만 쓰세요.
- 업비트 한도(포켓·IP 기준, 정책이 바뀌면 공식 문서가 우선입니다): 시세 REST 그룹당 초당 10회, 주문 생성 초당 12회, 잔고·주문 조회 초당 30회, WebSocket 연결 초당 5회, 연결당 메시지 초당 5회·분당 100회. 429가 쌓이면 418로 일시 차단되고 차단 시간은 길어집니다. 이 봇은 시세 8회/초, 조회 15회/초, 주문 6회/초 아래로 간격을 두고 `Remaining-Req`가 0이면 1초 쉽니다. `Origin` 헤더는 시세 한도를 더 세게 만들므로 보내지 않습니다. 현재가는 WebSocket을 쓰고, 5초 이상 끊기면 REST로 보완합니다.
- 주문 요청이 타임아웃 나면 같은 `identifier`로 주문이 이미 있는지 확인한 뒤 재시도합니다. 키가 틀리거나 잔고·최소금액 오류(400)는 같은 주문으로 재시도하지 않습니다.
- 취소 후 재주문은 `/v1/orders/cancel_and_new` 한 번으로 보냅니다. 이 엔드포인트도 주문 생성 한도를 같이 씁니다.
- 최소 주문금액은 USDT 마켓 **0.5 USDT**입니다. 그 미만 잔량은 주문할 수 없어 로그에 남기고, 반복되면 봇이 중지 상태로 들어갑니다.
- 서버 시각이 크게 어긋나도 주문 인증 nonce는 UUID라 바로 거절되지는 않지만, 프로세스 다운은 곧 손절 공백입니다. 재시작 후에도 매도 주문은 복구하지만, 다운된 동안의 급락은 막지 못합니다.

## 로그

콘솔과 `trading.log`에 주문 요청, 접수, 체결, 수수료, 청산 순손익, 오류가 남습니다. 순손익은 `매도금액 - 매도수수료 - 매수금액 - 매수수수료`입니다.
