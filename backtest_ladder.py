"""분할 구간(ladder) 전략 백테스트. ladder.py 의 실전 규칙과 같은 규칙을 일봉으로 재현합니다.

    python backtest_ladder.py data/daily/ohlcv_USDT-BTC.csv            # 기본값 성과 + 학습/검증 분리 탐색
    python backtest_ladder.py data/daily/ohlcv_USDT-BTC.csv --step 0.03 --slots 10 --lookback 120

규칙
- 앵커(anchor_mode)
  price(기본): 시작 시점의 가격. 보유 구간이 하나도 없을 때만 직전 종가를 따라 올라갑니다.
  high: 직전 lookback 일의 최고 종가(당일 봉은 쓰지 않음, 미래 참조 없음).
  fixed: 시작 시점의 가격에 고정.
- k 번째 구간 매수가 = 앵커 * (1 - step)^k, 구간당 투입액 = 예산 / slots (고정, 복리 없음).
- 체결된 구간은 매수가 * (1 + step) 에 지정가 매도. 매도되면 구간이 비고 다시 매수 대기.
- 손절 없음.

가정과 한계(결과는 실제보다 낙관적일 수 있습니다)
- 일봉이라 하루 안의 고가/저가 순서를 모릅니다. 매수한 날에는 그 구간을 매도하지 않고,
  매도한 날에는 그 구간을 다시 매수하지 않습니다(보수적).
- 시가가 매수가보다 낮으면 시가에 체결, 시가가 매도가보다 높으면 시가에 체결.
- 호가 대기 순서는 반영하지 않습니다. slip 은 체결마다 불리한 방향으로 주는 폭입니다.
- 롤링 창은 서로 겹쳐 독립 표본이 적습니다. 통계적 유의성은 검정하지 않습니다.
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
from dataclasses import dataclass
from typing import Sequence

Bar = tuple[float, float, float, float]  # open, high, low, close


def load_daily(path: str) -> list[Bar]:
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])) for r in rows]


@dataclass(frozen=True)
class LadderResult:
    quote_return: float      # 시작 예산 대비 USDT 평가 수익률
    base_return: float       # 같은 예산으로 시작가에 전량 매수했을 때 대비 BTC 환산 수량 변화
    sells: int
    exposure: float          # 기간 평균 BTC 비중
    max_drawdown: float
    slots_held: int


def simulate_ladder(
    bars: Sequence[Bar],
    start: int,
    end: int,
    *,
    step: float = 0.03,
    slots: int = 10,
    lookback: int = 120,
    fee: float = 0.0025,
    slip: float = 0.00125,
    anchor_mode: str = "price",
) -> LadderResult:
    """bars[start:end] 구간을 운용합니다. 앵커 계산에는 start 이전 봉도 씁니다."""
    budget = bars[start][3]
    cash = budget
    size = budget / slots
    held: list[tuple[float, float, int] | None] = [None] * slots  # qty, entry, day
    sells = 0
    peak = budget
    mdd = 0.0
    exposure_sum = 0.0
    days = 0
    price_anchor = budget  # 시작가
    for i in range(start + 1, end):
        o, h, low, c = bars[i]
        sold_today: set[int] = set()
        for k in range(slots):
            slot = held[k]
            if slot is None or slot[2] >= i:
                continue
            target = slot[1] * (1 + step)
            if h >= target:
                px = max(o, target) * (1 - slip)
                cash += slot[0] * px * (1 - fee)
                held[k] = None
                sold_today.add(k)
                sells += 1
        if anchor_mode == "high":
            anchor = max(b[3] for b in bars[max(0, i - lookback):i])
        else:
            # price: 시작가에서 출발. 보유 구간이 하나도 없을 때만 직전 종가를 따라 올립니다.
            if anchor_mode == "price" and all(s is None for s in held):
                price_anchor = max(price_anchor, bars[i - 1][3])
            anchor = price_anchor
        for k in range(slots):
            if held[k] is not None or k in sold_today:
                continue  # 같은 날 판 구간은 그날 다시 사지 않음(장중 순서를 모르므로 보수적)
            level = anchor * (1 - step) ** (k + 1)
            if low <= level and cash >= size * 0.999:
                px = min(o, level) * (1 + slip)
                held[k] = (size * (1 - fee) / px, px, i)
                cash -= size
        coin = sum(s[0] for s in held if s is not None) * c
        equity = cash + coin
        peak = max(peak, equity)
        mdd = max(mdd, 1 - equity / peak)
        exposure_sum += coin / equity if equity > 0 else 0.0
        days += 1
    last = bars[end - 1][3]
    equity = cash + sum(s[0] for s in held if s is not None) * last
    return LadderResult(
        quote_return=equity / budget - 1,
        base_return=equity / last - 1,
        sells=sells,
        exposure=exposure_sum / days if days else 0.0,
        max_drawdown=mdd,
        slots_held=sum(1 for s in held if s is not None),
    )


def rolling(bars: Sequence[Bar], lo: int, hi: int, window: int, stride: int, **params: float) -> dict[str, float]:
    """[lo, hi) 안에서 window 일 창을 stride 간격으로 굴려 요약합니다."""
    results: list[LadderResult] = []
    holds: list[float] = []
    for a in range(lo, hi - window, stride):
        results.append(simulate_ladder(bars, a, a + window, **params))
        holds.append(bars[a + window - 1][3] / bars[a][3] - 1)
    quote = [r.quote_return for r in results]
    # 평균 비중만큼만 단순 보유했을 때의 수익(노출 효과)을 빼서, 매매 규칙이 더한 몫을 따로 봅니다.
    excess = [r.quote_return - r.exposure * bh for r, bh in zip(results, holds)]
    return {
        "windows": len(results),
        "median": st.median(quote),
        "worst": min(quote),
        "positive": sum(x > 0 for x in quote) / len(quote),
        "beat_hold": sum(q > b for q, b in zip(quote, holds)) / len(quote),
        "hold_median": st.median(holds),
        "base_median": st.median(r.base_return for r in results),
        "excess_median": st.median(excess),
        "excess_positive": sum(x > 0 for x in excess) / len(excess),
        "exposure": st.mean(r.exposure for r in results),
        "sells": st.mean(r.sells for r in results),
        "mdd": st.mean(r.max_drawdown for r in results),
    }


GRID_STEPS = (0.015, 0.02, 0.03, 0.04, 0.05)
GRID_SLOTS = (5, 8, 10, 15)
GRID_LOOKBACK = (30, 60, 120, 250)


def _fmt(name: str, s: dict[str, float]) -> str:
    return (
        f"{name:<22} 중앙 {s['median']*100:+6.1f}% | 최악 {s['worst']*100:+6.1f}% | 양수 {s['positive']*100:3.0f}% | "
        f"보유초과 {s['beat_hold']*100:3.0f}% | 노출제외 초과 {s['excess_median']*100:+5.1f}% (양수 {s['excess_positive']*100:3.0f}%) | "
        f"평균비중 {s['exposure']*100:3.0f}% | 매도 {s['sells']:5.1f}회"
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--step", type=float, default=0.03)
    ap.add_argument("--slots", type=int, default=10)
    ap.add_argument("--lookback", type=int, default=120)
    ap.add_argument("--anchor", choices=("price", "high", "fixed"), default="price")
    ap.add_argument("--fee", type=float, default=0.0025)
    ap.add_argument("--slip", type=float, default=0.00125)
    ap.add_argument("--window", type=int, default=180)
    ap.add_argument("--stride", type=int, default=30)
    ap.add_argument("--split", type=float, default=0.6, help="학습 구간 비율")
    args = ap.parse_args(argv)
    bars = load_daily(args.csv)
    n = len(bars)
    warm = max(GRID_LOOKBACK)
    cut = int(n * args.split)
    cost = dict(fee=args.fee, slip=args.slip, anchor_mode=args.anchor)
    base = dict(step=args.step, slots=args.slots, lookback=args.lookback, **cost)

    print(f"앵커 {args.anchor} | 일봉 {n}개, 학습 [{warm}, {cut}) / 검증 [{cut}, {n}), 창 {args.window}일, 간격 {args.stride}일")
    for label, lo, hi in (("전체", warm, n), ("학습", warm, cut), ("검증", cut, n)):
        s = rolling(bars, lo, hi, args.window, args.stride, **base)
        print(f"[{label}] 단순보유 중앙 {s['hold_median']*100:+.1f}%, 창 {int(s['windows'])}개")
        print("  " + _fmt(f"step {args.step*100:g}% x{args.slots} L{args.lookback}", s))

    print("\n학습 구간에서 고른 상위 5개를 검증 구간에 그대로 적용 (선택 기준: 학습 중앙 수익)")
    scored = []
    for step in GRID_STEPS:
        for slots in GRID_SLOTS:
            for lookback in GRID_LOOKBACK:
                p = dict(step=step, slots=slots, lookback=lookback, **cost)
                scored.append((rolling(bars, warm, cut, args.window, args.stride, **p)["median"], step, slots, lookback))
    scored.sort(reverse=True)
    test_all = []
    for step in GRID_STEPS:
        for slots in GRID_SLOTS:
            for lookback in GRID_LOOKBACK:
                p = dict(step=step, slots=slots, lookback=lookback, **cost)
                test_all.append(rolling(bars, cut, n, args.window, args.stride, **p)["median"])
    for train_median, step, slots, lookback in scored[:5]:
        p = dict(step=step, slots=slots, lookback=lookback, **cost)
        s = rolling(bars, cut, n, args.window, args.stride, **p)
        rank = sum(x > s["median"] for x in test_all) + 1
        print(f"  학습 {train_median*100:+5.1f}% -> " + _fmt(f"step {step*100:g}% x{slots} L{lookback}", s) + f" | 검증 순위 {rank}/{len(test_all)}")
    print(f"  검증 구간 전체 조합 중앙 수익: 최저 {min(test_all)*100:+.1f}% / 중앙 {st.median(test_all)*100:+.1f}% / 최고 {max(test_all)*100:+.1f}%")


if __name__ == "__main__":
    main()
