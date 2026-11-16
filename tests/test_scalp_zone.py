import unittest
from decimal import Decimal

from backfill import OhlcvBar
from scalp_zone import compute_scalping_zone


def _series(
    count: int,
    *,
    start_ms: int,
    step_ms: int,
    base: Decimal,
    swing: Decimal,
) -> list[OhlcvBar]:
    bars: list[OhlcvBar] = []
    for index in range(count):
        level = base + swing * Decimal(index) / Decimal(max(count - 1, 1))
        bars.append(
            OhlcvBar(
                timestamp_ms=start_ms + index * step_ms,
                open_time=f"t{index}",
                open=level,
                high=level + swing,
                low=level - swing,
                close=level,
                volume=Decimal("1"),
            )
        )
    return bars


class ScalpZoneTests(unittest.TestCase):
    def test_long_bias_near_daily_support(self) -> None:
        daily = _series(150, start_ms=0, step_ms=86_400_000, base=Decimal("100"), swing=Decimal("2"))
        last = daily[-1]
        daily[-1] = OhlcvBar(
            last.timestamp_ms,
            last.open_time,
            Decimal("98"),
            Decimal("99"),
            Decimal("97"),
            Decimal("98"),
            Decimal("1"),
        )
        minute = _series(150, start_ms=last.timestamp_ms, step_ms=60_000, base=Decimal("98"), swing=Decimal("0.2"))
        zone = compute_scalping_zone(
            minute,
            daily,
            market="USDT-BTC",
            entry_zone=Decimal("0.40"),
        )
        self.assertEqual(zone.market, "USDT-BTC")
        self.assertIn(zone.bias, {"long_bias", "neutral", "avoid"})
        self.assertLess(zone.zone_low, zone.zone_high)
        self.assertGreater(zone.target_rate, 0)
        self.assertGreater(zone.target_sell, zone.reference_buy)
        self.assertEqual(zone.stop_price, Decimal("0"))
        self.assertEqual(zone.stop_rate, Decimal("0"))
        enabled = compute_scalping_zone(
            minute,
            daily,
            market="USDT-BTC",
            entry_zone=Decimal("0.40"),
            stop_loss_enabled=True,
            stop_loss_rate=Decimal("0.005"),
        )
        self.assertGreater(enabled.stop_price, Decimal("0"))
        self.assertEqual(enabled.stop_rate, Decimal("0.005"))

    def test_avoid_on_daily_breakdown(self) -> None:
        daily = _series(150, start_ms=0, step_ms=86_400_000, base=Decimal("100"), swing=Decimal("1"))
        last = daily[-1]
        daily[-1] = OhlcvBar(
            last.timestamp_ms,
            last.open_time,
            Decimal("80"),
            Decimal("81"),
            Decimal("79"),
            Decimal("80"),
            Decimal("1"),
        )
        minute = _series(150, start_ms=last.timestamp_ms, step_ms=60_000, base=Decimal("80"), swing=Decimal("0.1"))
        zone = compute_scalping_zone(minute, daily, market="USDT-ETH")
        self.assertEqual(zone.bias, "avoid")
        self.assertFalse(zone.actionable)


if __name__ == "__main__":
    unittest.main()
