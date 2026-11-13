# 실전 배포 가이드

이 문서는 서버에 봇을 올리고 업비트 API 키와 IP 를 넣어 실전 가동하기까지의 순서입니다.
코드는 업비트 실서버·실계좌로 검증되지 않았습니다. 단위 테스트는 모의 거래소 기준이며, 아래 3단계(preflight)와 5단계(소액 가동)가 그 공백을 메우는 절차입니다.

> 먼저 STRATEGY_REVIEW.md 를 읽으세요. USDT-BTC 마켓은 현재 스프레드가 커서 기본 설정으로는 매수하지 않습니다.

## 순서가 중요한 이유

업비트는 키를 발급할 때 허용 IP 를 먼저 정해야 합니다. 그래서 서버(고정 공인 IP)가 먼저입니다.

1. 서버 준비 (고정 공인 IP 필요. IP 가 바뀌면 주문이 `no_authorization_ip` 로 거부됩니다)
2. 서버에서 공인 IP 확인
3. 그 IP 를 허용 IP 로 넣어 업비트 키 발급, `.env` 에 입력, preflight 점검
4. DRY_RUN 으로 며칠 관찰
5. 소액으로 실전 시작

## 1. 서버 준비

- 무료 선택지는 가입·정책에 따라 달라집니다. Oracle Always Free 는 가입 승인이 필요하고(진행 중인 건), Always Free 유휴 회수 정책이 있습니다. GCP e2-micro 무료 티어는 미국 리전에서만 제공되며 업비트가 해외 IP 에서 오는 요청을 문제없이 받는지는 확인하지 못했습니다.
- 고정 IP 만 보장되면 사양은 중요하지 않습니다. 이 봇은 1 vCPU / 1GB 메모리로 충분한 수준의 경량 프로세스입니다(측정한 수치는 아닙니다).
- 집 PC 는 공인 IP 가 바뀔 수 있어 권장하지 않습니다.

Ubuntu/Debian 서버에서:

```bash
git clone https://github.com/LEEYOUNGJIN-GIT/btc.git
cd btc
sudo bash deploy/install.sh
```

설치 스크립트는 `btcbot` 전용 계정, `/opt/btc-bot`, 가상환경, systemd 유닛을 만듭니다. 서비스는 시작하지 않습니다. 이 스크립트는 실제 서버에서 실행해 검증하지 못했으니 처음에는 내용을 읽고 실행하세요.

## 2. 공인 IP 확인

```bash
sudo -u btcbot /opt/btc-bot/.venv/bin/python /opt/btc-bot/preflight.py --ip-only
```

출력되는 IP 가 업비트에 등록할 값입니다.

## 3. 키 발급과 점검

업비트 > 마이페이지 > Open API 관리에서:

- 허용 IP: 2단계에서 확인한 IP
- 권한: 자산조회, 주문조회, 주문하기만 체크. **출금하기는 절대 체크하지 마세요.** 키가 유출돼도 출금이 불가능해야 합니다.

발급받은 키를 서버의 `.env` 에 직접 입력합니다(채팅이나 저장소에 붙여넣지 마세요).

```bash
sudo nano /opt/btc-bot/.env
# UPBIT_ACCESS_KEY=...
# UPBIT_SECRET_KEY=...
```

점검:

```bash
sudo -u btcbot /opt/btc-bot/.venv/bin/python /opt/btc-bot/preflight.py
```

`자산조회` 와 `주문가능정보` 가 통과해야 합니다. 이어서 주문 권한을 확인합니다.

```bash
sudo -u btcbot /opt/btc-bot/.venv/bin/python /opt/btc-bot/preflight.py --order-test --yes
```

현재 호가의 절반 가격으로 최소금액의 약 1.5배짜리 지정가 매수를 넣고 바로 취소합니다. 체결될 수 없는 가격이지만 **실제 주문**이며 해당 USDT 가 잠깐 묶입니다. 취소에 실패하면 출력된 uuid 로 업비트 앱에서 직접 취소하세요.

오류 안내:

| 오류 이름 | 의미 |
|---|---|
| no_authorization_ip | 서버 공인 IP 가 허용 IP 에 없음 |
| invalid_access_key | 액세스 키 오타·공백 |
| jwt_verification | 시크릿 키가 액세스 키와 불일치 |
| out_of_scope | 키 권한 부족 |
| expired_access_key | 키 만료 |

## 4. DRY_RUN 관찰

`.env` 에 `DRY_RUN=true` 인 상태로 서비스를 시작합니다. 모의 체결이며 주문 API 는 호출하지 않습니다.

```bash
sudo systemctl enable --now btc-bot
journalctl -u btc-bot -f      # 또는 /opt/btc-bot/trading.log
```

모의 체결은 슬리피지와 체결 우선순위를 단순화했기 때문에 실제 성과를 보장하지 않습니다. 이 단계의 목적은 프로세스가 오류 없이 도는지, 알림과 재시작이 동작하는지 보는 것입니다.

## 5. 소액 실전 시작

`.env` 에서 다음을 설정합니다.

```
DRY_RUN=false
LIVE_TRADING_CONFIRM=YES
MAX_ORDER_QUOTE=20          # 1회 매수 상한(USDT). 처음엔 최소 수준으로
MAX_DAILY_LOSS_QUOTE=3      # 당일 누적 실현손실이 이 값에 닿으면 HALT
```

`MAX_DAILY_LOSS_QUOTE` 가 0 이면 실전 모드는 시작하지 않습니다(의도적으로 끄려면 `ALLOW_NO_LOSS_LIMIT=true`). 위 숫자는 예시이며 권장값이 아닙니다. 잃어도 되는 금액을 기준으로 직접 정하세요.

```bash
sudo systemctl restart btc-bot
```

## 운영

| 상황 | 방법 |
|---|---|
| 신규 매수만 멈추고 싶다 | `touch /opt/btc-bot/STOP` (대기 중인 매수 취소, 보유분은 계속 관리·손절). 풀려면 파일 삭제 |
| 보유 코인을 전량 정리하고 멈추고 싶다 | `touch /opt/btc-bot/FLATTEN` (시장가 전량 청산 후 신규 매수 중단) |
| 상태 확인 | `journalctl -u btc-bot -n 100`, `cat /opt/btc-bot/bot.heartbeat` |
| 즉시 종료 | `sudo systemctl stop btc-bot` (**보유 중이면 손절이 동작하지 않습니다**) |

시작 단계의 키/IP/권한 오류는 종료 코드 2 로 끝나며 systemd 가 재시작을 반복하지 않습니다. 원인을 고친 뒤 `systemctl restart` 하세요.

### 알림 (권장)

- 텔레그램: BotFather 로 봇을 만들어 `TELEGRAM_BOT_TOKEN`, 본인 채팅 ID 를 `TELEGRAM_CHAT_ID` 에 넣습니다. 매수 체결, 청산 손익, 손절, HALT, 오류 연속 발생을 보냅니다.
- 생존 신호: healthchecks.io 같은 서비스의 핑 URL 을 `HEALTHCHECK_URL` 에 넣으면 봇이 정상 루프를 도는 동안만 약 60초마다 핑을 보냅니다. HALT 이거나 서버가 죽으면 핑이 끊겨 외부에서 알려줍니다. 텔레그램 알림은 프로세스가 살아 있어야 가는 반면, 이 방식은 서버 전체 장애도 잡습니다.

## 남아 있는 위험 (코드로 해결되지 않은 것)

1. **손절은 기본적으로 꺼져 있습니다(`STOP_LOSS_ENABLED=false`).** 매수 후 가격이 내려가면 목표 매도가에 닿을 때까지 보유하며, 하락이 이어지면 손실이 한도 없이 커지고 USDT 가 묶여 새 매수도 못 합니다. `MAX_DAILY_LOSS_QUOTE` 는 실현 손익만 보므로 미실현 손실은 막지 못합니다. 켜더라도 손절은 거래소 조건부 주문이 아니라 프로세스가 살아 있을 때만 동작합니다. 수동 정리는 `FLATTEN` 파일로 합니다.
2. **전략은 검증되지 않았습니다.** 수수료 0.05% 왕복 0.1% 를 넘는 순이익 폭이 작은 스캘핑이라, 슬리피지와 체결 지연이 기대수익을 쉽게 잠식합니다. 이 저장소의 백테스트·모의 결과는 실거래 수익을 보장하지 않습니다.
3. **실계좌에서 처음 도는 코드입니다.** 주문 파라미터, 호가 단위, 최소주문금액은 업비트 문서 기준으로 구현했지만 모든 경우를 실거래로 확인하지는 못했습니다. 그래서 소액으로 시작하는 단계가 필요합니다.
4. 서버 IP 가 바뀌면 주문이 거부됩니다. 업비트에서 허용 IP 를 다시 등록해야 합니다.
5. 이 문서와 코드는 투자 자문이 아닙니다. 손익의 책임은 운영자에게 있습니다.
