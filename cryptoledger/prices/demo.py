"""Synthetic prices for demo mode only (clearly labelled, never used with real data)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from . import PriceResult

# Rough, rounded EUR levels per coin at a few dates; linear in between. Fictional.
CURVES: Dict[str, List[Tuple[str, float]]] = {
    "bitcoin": [("2022-01-01", 40000), ("2022-12-31", 15500), ("2023-12-31", 38500), ("2024-12-31", 90000),
                ("2025-12-31", 80000), ("2026-12-31", 88000)],
    "ethereum": [("2022-01-01", 3200), ("2022-12-31", 1150), ("2023-12-31", 2050), ("2024-12-31", 3200),
                 ("2025-12-31", 2900), ("2026-12-31", 3100)],
    "solana": [("2022-01-01", 150), ("2022-12-31", 9), ("2023-12-31", 92), ("2024-12-31", 180),
               ("2025-12-31", 120), ("2026-12-31", 150)],
    "binancecoin": [("2022-01-01", 450), ("2022-12-31", 230), ("2023-12-31", 285), ("2024-12-31", 670),
                    ("2025-12-31", 740), ("2026-12-31", 820)],
    "usd-coin": [("2022-01-01", 0.88), ("2022-12-31", 0.94), ("2023-12-31", 0.90), ("2024-12-31", 0.96),
                 ("2025-12-31", 0.85), ("2026-12-31", 0.86)],
    "ronin": [("2022-01-01", 4.5), ("2022-12-31", 0.25), ("2023-12-31", 2.6), ("2024-12-31", 2.3),
              ("2025-12-31", 0.6), ("2026-12-31", 0.7)],
    "polkadot": [("2022-01-01", 24), ("2022-12-31", 4.1), ("2023-12-31", 7.5), ("2024-12-31", 6.5),
                 ("2025-12-31", 2.5), ("2026-12-31", 3.0)],
    "cardano": [("2022-01-01", 1.2), ("2022-12-31", 0.23), ("2023-12-31", 0.53), ("2024-12-31", 0.82),
                ("2025-12-31", 0.40), ("2026-12-31", 0.50)],
}


def _ts(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


class DemoPrices:
    name = "demo"

    def can_price(self, asset: Dict) -> bool:
        return (asset.get("coingecko_id") or "") in CURVES

    def price_at(self, asset: Dict, ts: int) -> Optional[PriceResult]:
        curve = CURVES.get(asset.get("coingecko_id") or "")
        if not curve:
            return None
        pts = [(_ts(d), v) for d, v in curve]
        if ts <= pts[0][0]:
            value = pts[0][1]
        elif ts >= pts[-1][0]:
            value = pts[-1][1]
        else:
            for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
                if t0 <= ts <= t1:
                    value = v0 + (v1 - v0) * (ts - t0) / (t1 - t0)
                    break
        price = Decimal(str(round(value, 6 if value < 10 else 2)))
        return PriceResult(price, "DEMO synthetic price", ts, "demo data, not a market price")
