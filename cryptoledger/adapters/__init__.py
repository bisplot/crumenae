"""Adapter registry. Each module registers its class with @register."""
from __future__ import annotations

from typing import Dict, Type

from .base import Adapter, AdapterError, AssetInfo, Movement, Snapshot, SyncResult  # noqa: F401

ADAPTERS: Dict[str, Type[Adapter]] = {}


def register(cls: Type[Adapter]) -> Type[Adapter]:
    ADAPTERS[cls.id] = cls
    return cls


def get_adapter(ctx, network) -> Adapter:
    try:
        cls = ADAPTERS[network.adapter]
    except KeyError:
        raise AdapterError(f"no adapter '{network.adapter}' for network '{network.id}'")
    return cls(ctx, network)


# Import adapter modules so they register themselves.
from . import evm, moralis, nodereal, solana, solar, tron, bitcoin, binance, manual  # noqa: E402,F401
