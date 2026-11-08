"""Data types shared by adapters, the sync engine and the price service."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


class AdapterError(Exception):
    """A readable error shown to the user next to the wallet (bad key, API down...)."""


@dataclass
class Movement:
    """One balance change of one asset in one wallet."""
    uid: str                 # unique and stable per wallet: re-syncing never duplicates
    tx_hash: str
    ts: int                  # unix seconds, UTC
    asset_key: str           # see AssetInfo.key
    amount: Decimal          # signed: + received, - sent / paid
    kind: str                # "in" | "out" | "fee"
    counterparty: str = ""
    failed: bool = False
    from_address: str = ""   # sender of this leg, when the source tells
    to_address: str = ""     # recipient of this leg
    balance_after: Optional[Decimal] = None   # absolute balance after this tx, if the chain says it
    info: str = ""           # method name, contract type...


@dataclass
class AssetInfo:
    key: str                 # "<network>:native" | "<network>:<contract or mint>" | "binance:<TICKER>"
    network: str
    symbol: str
    name: str = ""
    decimals: Optional[int] = None
    contract: Optional[str] = None
    coingecko_id: Optional[str] = None
    binance_symbol: Optional[str] = None
    fiat: Optional[str] = None
    status: str = "unmapped"  # known | mapped | unmapped | spam | ignored | ghost


@dataclass
class Snapshot:
    taken_at: int
    asset_key: str
    amount: Decimal
    source: str              # exchange_daily | chain_state | ...
    note: str = ""


@dataclass
class StakePosition:
    """Coins staked by a wallet, read live at each sync (listed on the Staking page)."""
    asset_key: str
    staked: Decimal = Decimal(0)
    unbonding: Decimal = Decimal(0)          # on their way back, not earning
    pending_rewards: Decimal = Decimal(0)    # earned, not claimed yet (not income until received)
    protocol: str = ""                       # "Terra staking", "Tron Stake 2.0", "Binance Simple Earn"…
    validator: str = ""                      # validator / pool / product


@dataclass
class SyncResult:
    movements: List[Movement] = field(default_factory=list)
    assets: List[AssetInfo] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    snapshots: List[Snapshot] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
