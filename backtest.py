"""분봉 CSV 로 '지정가 매수 -> 목표가 지정가 매도' 규칙을 단순 재현하는 점검 도구.

    python backtest.py data/minute/ohlcv_USDT-BTC.csv --fee 0.0025 --target 0.0025 --grid 0.002
    python backtest.py data/minute/ohlcv_USDT-BTC.csv --fee 0.0025 --stop 0.005

한계(결과는 실제보다 낙관적입니다):
- 호가 이력이 없어 체결은 '캔들 저가가 매수가에 닿으면 체결, 고가가 목표가에 닿으면 매도 체결'로 가정합니다.
  호가창 대기 순서와 스프레드는 반영하지 않습니다.
- 손절은 손절가에 그대로 체결된다고 가정합니다(시장가 슬리피지 미반영).
- 진입 시도를 step 분마다 반복해 표본이 서로 겹칩니다. 독립 표본 수는 체결 건수보다 훨씬 적습니다.
- 표본 기간의 추세(하락장이면 저가 매수 전략이 불리)에 크게 좌우됩니다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from analyzer import fill_gaps
from backfill import OhlcvBar, load_ohlcv_csv


@dataclass(frozen=True)
class BacktestResult:
    attempts: int
    fills: int
    mean_net: float
    win_rate: float
    worst: float
    take_profits: int
    stops: int
    open_at_horizon: int


def simulate(
    bars: Sequence[OhlcvBar],
    *,
    fee: float,
    target: float,
    grid: float,
    stop: float | None = None,
    horizon: int = 1440,
    step: int = 30,
    timeout: int = 45,
    period_ms: int = 60_000,
) -> BacktestResult:
    filled = fill_gaps(bars, period_ms)
    close = [float(b.close) for b in filled]
    high = [float(b.high) for b in filled]
    low = [float(b.low) for b in filled]
    n = len(close)
    nets: list[float] = []
    tp = stops = opened = attempts = 0
    for i in range(0, n - horizon - timeout, step):
        attempts += 1
        price = close[i] * (1 - grid)
        fill_at = next((k for k in range(i + 1, i + 1 + timeout) if low[k] <= price), None)
        if fill_at is None:
            continue
        take = price * (1 + fee) / (1 - fee) * (1 + target)
        stop_price = price * (1 - stop) if stop else None
        net = None
        for k in range(fill_at, min(n, fill_at + horizon)):
            if stop_price is not None and low[k] <= stop_price:
                net = stop_price * (1 - fee) / (price * (1 + fee)) - 1
                stops += 1
                break
            if high[k] >= take:
                net = take * (1 - fee) / (price * (1 + fee)) - 1
                tp += 1
                break
        if net is None:
            net = close[min(n - 1, fill_at + horizon - 1)] * (1 - fee) / (price * (1 + fee)) - 1
            opened += 1
        nets.append(net)
    if not nets:
        return BacktestResult(attempts, 0, 0.0, 0.0, 0.0, 0, 0, 0)
    return BacktestResult(
        attempts=attempts,
        fills=len(nets),
        mean_net=sum(nets) / len(nets),
        win_rate=sum(1 for x in nets if x > 0) / len(nets),
        worst=min(nets),
        take_profits=tp,
        stops=stops,
        open_at_horizon=opened,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv")
    parser.add_argument("--fee", type=float, default=0.0025, help="편도 수수료율 (USDT 마켓 기본 0.0025)")
    parser.add_argument("--target", type=float, default=0.0025, help="수수료 회수 후 목표 순이익률")
    parser.add_argument("--grid", type=float, default=0.002, help="현재가 아래 매수 지정가 거리")
    parser.add_argument("--stop", type=float, default=None, help="손절률. 없으면 손절 안 함")
    parser.add_argument("--horizon", type=int, default=1440, help="진입 후 최대 보유 분")
    args = parser.parse_args(argv)
    bars = load_ohlcv_csv(args.csv)
    result = simulate(bars, fee=args.fee, target=args.target, grid=args.grid, stop=args.stop, horizon=args.horizon)
    print(f"시도 {result.attempts} | 체결 {result.fills} | 익절 {result.take_profits} | 손절 {result.stops} | 기간내 미청산 {result.open_at_horizon}")
    print(f"건당 평균 순손익 {result.mean_net * 100:+.3f}% | 승률 {result.win_rate * 100:.0f}% | 최악 {result.worst * 100:.2f}%")
    print("주의: 체결·슬리피지를 낙관적으로 가정한 값입니다. 모듈 설명(--help)의 한계를 확인하세요.")


if __name__ == "__main__":
    main()
