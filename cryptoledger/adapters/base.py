"""The adapter contract. To connect a new blockchain or exchange API, subclass
`Adapter`, implement `sync` and `current_balances`, and decorate it with
`@register`. See docs/ADDING_AN_ADAPTER.md."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from ..models import AdapterError, AssetInfo, Movement, Snapshot, StakePosition, SyncResult  # noqa: F401

Logger = Callable[[str], None]


class Adapter:
    id = "base"
    kind = "chain"                 # chain | exchange | manual
    needs_address = True
    has_history = True             # lists every transaction -> balances can be reconstructed
    credentials: List[str] = []    # per-wallet secrets (e.g. exchange API key/secret)

    def __init__(self, ctx, network):
        self.ctx = ctx
        self.network = network
        self.staking_found: List[StakePosition] = []   # filled by current_balances where it reads them

    # --- required ---------------------------------------------------------------
    def validate_address(self, address: str) -> str:
        """Return the normalized address or raise ValueError."""
        raise NotImplementedError

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        """Fetch new movements since `state` (the dict you returned last time)."""
        raise NotImplementedError

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> "tuple[Dict[str, Decimal], List[AssetInfo]]":
        """Live balances: {asset_key: amount}, plus info for any new asset."""
        raise NotImplementedError

    # --- optional ---------------------------------------------------------------
    def balance_at(self, wallet: Dict[str, Any], ts: int, asset_keys: List[str],
                   log: Logger) -> Optional[Dict[str, Decimal]]:
        """Balances read from chain state at time `ts` (archive node). None = unsupported."""
        return None

    def staking_positions(self, wallet: Dict[str, Any], log: Logger) -> List[StakePosition]:
        """Staking positions, read live; called right after current_balances (same instance), so
        an adapter can return what it already read there. Default: none."""
        return list(self.staking_found)

    # --- helpers ----------------------------------------------------------------
    def secret(self, name: str) -> Optional[str]:
        return self.ctx.secrets.get(name)

    def wallet_secret(self, wallet: Dict[str, Any], name: str) -> Optional[str]:
        return self.ctx.secrets.get(f"wallet:{wallet['id']}:{name}")
