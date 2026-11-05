# Connecting a new API

There are three levels, from no code to a new module.

## 1. An EVM chain with an Etherscan-compatible explorer: no code

Settings → Blockchain endpoints (advanced) → **Add another EVM blockchain**: id, chain id, native symbol, explorer API URL.
Examples of compatible APIs:

| Provider | URL pattern | Notes |
|---|---|---|
| Etherscan V2 | `https://api.etherscan.io/v2/api` | one key, `chainid` sent automatically |
| Blockscout | `https://<chain>.blockscout.com/api` | usually no key |
| Routescan | `https://api.routescan.io/v2/network/mainnet/evm/<chainId>/etherscan/api` | no key, rate limited |

To move a built-in chain to another explorer, edit its Explorer API URL in the same table.

## 1b. Any EVM chain supported by Moralis: config only

Add an entry to `config/networks.json` with `"adapter": "moralis"`, `"moralis_chain": "<hex chain id>"`,
`"explorer_api": "https://deep-index.moralis.io/api/v2.2"` and `"explorer_key": "moralis"` (Ronin is
set up this way). The same Moralis key covers every chain. Note: Moralis no longer has a
free plan; BNB Smart Chain uses the `nodereal` adapter (NodeReal MegaNode, free key).

## 2. A token the app does not know: no code

Assets page → enter the CoinGecko id or the Binance ticker. To ship it for everyone, add it to
`config/tokens.json` (contract in lower case for EVM).

## 3. A new blockchain or exchange: one Python class

Create `cryptoledger/adapters/<name>.py`:

```python
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from .. import assets as assets_mod
from ..util import scaled
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult


@register
class MyChainAdapter(Adapter):
    id = "mychain"               # matches "adapter" in config/networks.json

    def validate_address(self, address: str) -> str:
        address = address.strip()
        if not address.startswith("my"):
            raise ValueError("MyChain addresses start with 'my'")
        return address

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        since = state.get("cursor")                       # whatever you returned last time
        rows = self.ctx.http.get_json(self.network.explorer_api + f"/address/{wallet['address']}/txs",
                                      {"after": since})
        native = assets_mod.native_asset(self.network)
        moves = []
        for tx in rows:
            amount = scaled(tx["value"], native.decimals)   # base units -> coins
            sign = 1 if tx["to"] == wallet["address"] else -1
            moves.append(Movement(uid=f"{tx['hash']}:main", tx_hash=tx["hash"], ts=tx["time"],
                                  asset_key=native.key, amount=sign * amount,
                                  kind="in" if sign > 0 else "out",
                                  counterparty=tx["from"] if sign > 0 else tx["to"]))
            # Fees paid by this wallet: a separate Movement with kind="fee" and a negative amount.
        return SyncResult(movements=moves, assets=[native],
                          state={"cursor": rows[-1]["hash"] if rows else since})

    def current_balances(self, wallet, known_assets, log) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        data = self.ctx.http.get_json(self.network.explorer_api + f"/address/{wallet['address']}")
        native = assets_mod.native_asset(self.network)
        return {native.key: scaled(data["balance"], native.decimals)}, [native]
```

Then:

1. import the module at the bottom of `cryptoledger/adapters/__init__.py`;
2. add the network to `config/networks.json` (`adapter`, `explorer_api`, `native` with symbol,
   decimals, CoinGecko id and Binance ticker for pricing, `tx_url` for links);
3. add a test in `tests/test_adapters.py` with a fake response (see `FakeHttp`).

Rules that keep the ledger correct:

- `uid` must be **stable**: the same transaction always yields the same uid, so re-syncing never
  duplicates anything.
- One `Movement` per asset change. Amounts are signed decimals in coins, not base units.
- Record fees as their own movement (`kind="fee"`), only when this wallet paid them.
- Failed transactions: record the fee only.
- If the API returns the balance after each transaction, set `balance_after`: the year-end
  balance is then the chain's own number.
- If the chain can be read at a past block, implement `balance_at()`: it is used as a cross-check.
- Raise `AdapterError("readable message")` for problems the user can fix (missing key, bad address).
- Add the API's host to `cryptoledger/ratelimit.py` (`SERVICES`) so the user can set its calls per
  second and per day; unknown hosts fall under "Other".
- Exchange or account APIs: set `needs_address = False`, `has_history = False` unless you import
  every trade, and `credentials = ["api_key", "api_secret"]`; read them with `self.wallet_secret()`.
  Store snapshots the API offers (`Snapshot(..., source="exchange_daily")`).
