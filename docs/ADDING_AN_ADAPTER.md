# Connecting a new API

There are three levels, from no code to a new module. Most needs stop at level 1.

## 1. A provider or a chain that speaks a supported type: no code

Everything happens in **Settings**:

- **API providers → + Add provider**: name, type, base URL, key, limits. **Test** makes one
  cheap call. The key is stored as the secret `<provider id>_api_key` (OS keychain or
  `data/secrets.json`); its limits are the rate-limit entry with the same id.
- **Networks → + Add network**: id, name, the provider that reads it, chain id (EVM), Alchemy
  name (Alchemy providers), native coin (symbol, decimals, CoinGecko id, Binance ticker),
  explorer links. Or change the provider of an existing chain in the table, or hide it.

Supported types (`cryptoledger/providers.py: TYPES`):

| Type | Reads | Examples | Adapter |
|---|---|---|---|
| Etherscan-compatible | any EVM chain: transactions, internal transfers, tokens | Etherscan V2 `https://api.etherscan.io/v2/api` (one key, `chainid` sent), Blockscout `https://<chain>.blockscout.com/api` (no key), Routescan `https://api.routescan.io/v2/network/mainnet/evm/<chainId>/etherscan/api` | `evm` |
| Alchemy-compatible | any EVM chain the provider serves: native and token transfers, tokens held now | Alchemy `https://{network}.g.alchemy.com/v2` (`{network}` = the chain's Alchemy name) | `alchemy` |
| Moralis | any EVM chain Moralis serves | `https://deep-index.moralis.io/api/v2.2` (each network gives its hex chain id) | `moralis` |
| Esplora | Bitcoin | mempool.space, Blockstream, your own node | `bitcoin` |
| Horizon | Stellar | `https://horizon.stellar.org` or any Horizon | `stellar` |

Solana (Helius or any Solana RPC), Tron (TronGrid) and Solar are built in with their own
endpoints. Built-in networks are listed in `config/networks.json`, each with its `provider`.

## 2. A token the app does not know: no code

Assets page → enter the CoinGecko id or the Binance ticker. To ship it for everyone, add it to
`config/tokens.json` (contract in lower case for EVM).

## 3. A new protocol or exchange: one Python class

Needed only when a service speaks a protocol none of the types above understands (e.g. a
Cosmos or Cardano API, a new exchange's account API). Write the adapter below; to make its
providers addable from the UI too, add its type to `TYPES` in `cryptoledger/providers.py`
(label, family, adapter id, default URL) and a branch in `apitest.test_provider` for its Test.

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
