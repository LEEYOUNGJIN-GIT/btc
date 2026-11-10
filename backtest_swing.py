"""일봉 OHLC 로 '직전 종가 대비 grid 만큼 아래 지정가 매수 -> 매수가 대비 target 지정가 매도'를 연속 반복하는 점검 도구.

    python backtest_swing.py data/daily/ohlcv_USDT-BTC.csv --fee 0.0025

가정(결과는 실제보다 낙관적일 수 있음):
- 전액 투입, 손절 없음, 종료 시점 미청산분은 종가로 평가.
- 매수 체결일에는 매도 체결을 허용하지 않음(일봉 내 순서를 모르므로 보수적).
- 매수 지정가는 매일 직전 종가 기준으로 재설정. 시가가 지정가보다 낮으면 시가에 체결.
- 호가 스프레드와 대기 순서는 반영하지 않음(--slip 으로 편도 불리한 체결폭을 줄 수 있음).
"""
from __future__ import annotations
import argparse, csv


def load(path):
    rows = list(csv.DictReader(open(path)))
    return [(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])) for r in rows]


def run(bars, *, fee, grid, target, slip=0.0, grid_abs=None, target_abs=None):
    cash, qty, buy_px = 1.0, 0.0, None
    sell_px, bought_i = None, -1
    trades = wins = 0
    eq_curve, hold_days = [], 0
    prev_close = bars[0][3]
    for i, (o, h, l, c) in enumerate(bars):
        if i == 0:
            eq_curve.append(1.0); continue
        if qty > 0:
            hold_days += 1
            if i > bought_i and h >= sell_px:
                px = max(o, sell_px) * (1 - slip)
                cash = qty * px * (1 - fee)
                trades += 1
                wins += px * (1 - fee) > buy_px * (1 + fee)
                qty = 0.0
        if qty == 0 and cash > 0:
            lim = prev_close - grid_abs if grid_abs else prev_close * (1 - grid)
            if l <= lim:
                px = min(o, lim) * (1 + slip)
                qty = cash * (1 - fee) / px
                buy_px = px
                sell_px = px + target_abs if target_abs else px * (1 + target)
                cash, bought_i = 0.0, i
        eq_curve.append(cash + qty * c)
        prev_close = c
    peak, mdd = 0.0, 0.0
    for e in eq_curve:
        peak = max(peak, e); mdd = max(mdd, 1 - e / peak)
    return dict(ret=eq_curve[-1] - 1, trades=trades, win=wins / trades if trades else float("nan"),
                mdd=mdd, inmkt=hold_days / max(1, len(bars) - 1), open=qty > 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv"); ap.add_argument("--fee", type=float, default=0.0025)
    ap.add_argument("--slip", type=float, default=0.0)
    a = ap.parse_args()
    bars = load(a.csv)
    cfgs = [("A 1.0%/1.0%", dict(grid=.01, target=.01)), ("A 1.5%/1.5%", dict(grid=.015, target=.015)),
            ("A 2.0%/2.0%", dict(grid=.02, target=.02)), ("B 3%/3%", dict(grid=.03, target=.03)),
            ("B 5%/5%", dict(grid=.05, target=.05)), ("B $5,000 고정", dict(grid=0, target=0, grid_abs=5000, target_abs=5000))]
    for name, days in (("전체 5년", len(bars)), ("최근 365일", 365), ("최근 180일", 180), ("최근 90일", 90)):
        seg = bars[-days:]
        bh = seg[-1][3] / seg[0][3] - 1
        print(f"\n[{name}] 단순보유 {bh*100:+.1f}%")
        print(f"{'설정':<16}{'수익률':>9}{'거래':>6}{'승률':>7}{'최대낙폭':>9}{'보유비중':>9}  미청산")
        for n, k in cfgs:
            r = run(seg, fee=a.fee, slip=a.slip, **k)
            print(f"{n:<16}{r['ret']*100:>+8.1f}%{r['trades']:>6}{r['win']*100:>6.0f}%{r['mdd']*100:>8.1f}%{r['inmkt']*100:>8.0f}%  {'O' if r['open'] else '-'}")

if __name__ == "__main__":
    main()
