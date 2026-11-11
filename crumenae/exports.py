"""Standard exports, written to <data>/exports/ and rewritten whenever the data changes:

    wallets.csv                one row per wallet
    balances_YYYY-MM-DD.csv    one row per wallet x asset at the fiscal-year cutoff (23:59:59)
    transactions_YYYY.csv      one row per transaction in the fiscal year

Columns are fixed (see docs/REPORT_CONTRACT.md). Numbers use a dot as decimal separator,
EUR values have 2 decimals, dates are ISO 8601 in the fiscal time zone.
"""
from __future__ import annotations

import csv
import io
from collections import OrderedDict
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import assets as assets_mod
from . import yearend
from .util import D, ZERO, dstr, round_eur

WALLET_COLUMNS = ["wallet", "network", "address", "platform"]
BALANCE_COLUMNS = ["wallet", "network", "asset", "quantity", "price_eur", "value_eur", "price_source"]
TX_COLUMNS = ["datetime", "wallet", "network", "tx_hash", "type", "asset_in", "qty_in", "asset_out", "qty_out",
              "fee_asset", "fee_qty", "value_eur", "from_address", "to_address"]
TX_TYPES = ("buy", "sell", "swap", "transfer_in", "transfer_out", "reward", "airdrop", "fee")


def _csv(header: List[str], rows: List[List[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for r in rows:
        writer.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def _eur(value: Optional[Decimal]) -> Optional[str]:
    """EUR with exactly 2 decimals ("2500.00")."""
    return format(round_eur(value), "f") if value is not None else None


def default_platform(ctx, wallet: Dict[str, Any]) -> str:
    if wallet.get("platform"):
        return wallet["platform"]
    try:
        net = ctx.networks.get(wallet["network"])
    except KeyError:
        return ""
    if net.kind == "exchange":
        return net.name.split(" (")[0]          # "Binance (exchange account)" -> "Binance"
    return "manual" if net.kind == "manual" else "self-custody"


# --- file names ------------------------------------------------------------------------

def balances_name(ctx, year: int) -> str:
    return f"balances_{ctx.calendar.end_date(year).isoformat()}.csv"


def transactions_name(year: int) -> str:
    return f"transactions_{year}.csv"


# --- wallets.csv -------------------------------------------------------------------------

def wallets_csv(ctx, portfolio: Optional[str] = None) -> str:
    rows = [[w["label"], w["network"], w["address"], default_platform(ctx, w)]
            for w in ctx.wallets(portfolio=portfolio)]
    return _csv(WALLET_COLUMNS, rows)


# --- balances_YYYY-12-31.csv -----------------------------------------------------------

def balances_csv(ctx, year: int, portfolio: Optional[str] = None) -> str:
    wallets = {w["id"]: w for w in ctx.wallets(portfolio=portfolio)}
    rows = []
    for r in yearend.rows_for_year(ctx, year):
        w = wallets.get(r["wallet_id"])
        if not w or r["asset_key"] == "*":
            continue
        if r["price_eur"] is not None:
            source = r["price_source"]
        else:
            reason = next((f for f in r["flags"] if "price" in f.lower() or "mapped" in f.lower()), "no price")
            source = "none: " + reason.lstrip("⛔🟡 ").strip()
        rows.append([w["label"], w["network"], r["symbol"] or r["asset_key"], r["amount"], r["price_eur"],
                     _eur(D(r["value_eur"])) if r["value_eur"] is not None else None, source])
    rows.sort(key=lambda x: (str(x[0]).lower(), str(x[2])))
    return _csv(BALANCE_COLUMNS, rows)


# --- transactions_YYYY.csv -------------------------------------------------------------

def _group_key(m: Dict[str, Any]) -> str:
    """Movements of one transaction in one wallet. Without a hash (exchange-internal
    records) the fee row shares the uid of its withdrawal: 'wd:123' and 'wd:123:fee'."""
    if m["tx_hash"]:
        return "h:" + m["tx_hash"].lower()
    uid = m["uid"]
    return "u:" + (uid[:-4] if uid.endswith(":fee") else uid)


class _Leg:
    __slots__ = ("asset_key", "symbol", "qty", "value", "price", "from_address", "to_address",
                 "counterparty", "categories")

    def __init__(self, asset_key: str, symbol: str):
        self.asset_key, self.symbol = asset_key, symbol
        self.qty = ZERO
        self.value: Optional[Decimal] = ZERO
        self.price: Optional[Decimal] = None
        self.from_address = self.to_address = self.counterparty = ""
        self.categories: List[str] = []

    def add(self, m: Dict[str, Any]) -> None:
        amount = D(m["amount"])
        self.qty += amount
        if m["price_eur"] is not None and self.price is None:
            self.price = D(m["price_eur"])
        if m["value_eur"] is None:
            self.value = None
        elif self.value is not None:
            self.value += D(m["value_eur"])
        self.from_address = self.from_address or m.get("from_address") or ""
        self.to_address = self.to_address or m.get("to_address") or ""
        self.counterparty = self.counterparty or m.get("counterparty") or ""
        if m.get("category"):
            self.categories.append(m["category"])

    def eur(self) -> Optional[Decimal]:
        """Value of the net quantity at the moment of the transaction."""
        if self.price is not None:
            return abs(self.qty) * self.price
        return abs(self.value) if self.value is not None else None


def _ends(leg_in: Optional[_Leg], leg_out: Optional[_Leg], fee: Optional[_Leg], own: str) -> Tuple[str, str]:
    """(from_address, to_address) of a row. When the wallet sends something (transfer out,
    swap, buy, sell) it is the sender and the recipient is where the asset went."""
    if leg_out:
        return leg_out.from_address or own, leg_out.to_address or leg_out.counterparty
    if leg_in:
        return leg_in.from_address or leg_in.counterparty, leg_in.to_address or own
    if fee:
        return fee.from_address or own, fee.to_address or fee.counterparty
    return own, ""


def _tx_type(ins: List[_Leg], outs: List[_Leg]) -> str:
    cats = {c for leg in ins + outs for c in leg.categories}
    if ins and outs:
        if "buy" in cats:
            return "buy"
        if "sell" in cats:
            return "sell"
        return "swap"
    if ins:
        in_cats = {c for leg in ins for c in leg.categories}
        for t in ("airdrop", "reward", "buy"):
            if t in in_cats:
                return t
        return "transfer_in"
    if outs:
        if "sell" in {c for leg in outs for c in leg.categories}:
            return "sell"
        return "transfer_out"
    return "fee"


def transaction_rows(ctx, year: int, portfolio: Optional[str] = None) -> List[List[Any]]:
    cal = ctx.calendar
    wallets = {w["id"]: w for w in ctx.wallets(portfolio=portfolio)}
    all_assets = assets_mod.all_assets(ctx)
    movements = ctx.db.query(
        "SELECT * FROM movements WHERE ts BETWEEN ? AND ? ORDER BY ts, id", (cal.start(year), cal.cutoff(year)))
    groups: "OrderedDict[Tuple[int, str], List[Dict[str, Any]]]" = OrderedDict()
    for m in movements:
        if m["wallet_id"] in wallets:
            groups.setdefault((m["wallet_id"], _group_key(m)), []).append(m)

    rows: List[List[Any]] = []
    for (wallet_id, _), ms in groups.items():
        w = wallets[wallet_id]
        own = w["address"] or ""
        legs: "OrderedDict[str, _Leg]" = OrderedDict()
        fees: "OrderedDict[str, _Leg]" = OrderedDict()
        for m in ms:
            a = all_assets.get(m["asset_key"]) or {}
            if assets_mod.is_hidden(a) or m["category"] == "spam":
                continue
            target = fees if m["kind"] == "fee" else legs
            leg = target.get(m["asset_key"]) or _Leg(m["asset_key"], a.get("symbol") or m["asset_key"])
            target[m["asset_key"]] = leg
            leg.add(m)
        # Net each asset within the transaction (a refund of the same asset cancels out).
        ins = [leg for leg in legs.values() if leg.qty > 0]
        outs = [leg for leg in legs.values() if leg.qty < 0]
        fee_legs = [leg for leg in fees.values() if leg.qty != 0]
        if not ins and not outs and not fee_legs:
            continue
        tx_type = _tx_type(ins, outs)
        first = ms[0]
        n = max(len(ins), len(outs), 1)
        for i in range(n):
            leg_in = ins[i] if i < len(ins) else None
            leg_out = outs[i] if i < len(outs) else None
            fee = fee_legs[0] if i == 0 and fee_legs else None
            # value_eur: what was received; if nothing was received, what was sent; a fee-only
            # transaction is valued at its fee.
            main = leg_in or leg_out or fee
            value = main.eur() if main else None
            frm, to = _ends(leg_in, leg_out, fee, own)
            rows.append([
                cal.iso_local(first["ts"]), w["label"], w["network"], first["tx_hash"], tx_type,
                leg_in.symbol if leg_in else None, dstr(leg_in.qty) if leg_in else None,
                leg_out.symbol if leg_out else None, dstr(-leg_out.qty) if leg_out else None,
                fee.symbol if fee else None, dstr(-fee.qty) if fee else None,
                _eur(value), frm, to,
            ])
        for extra in fee_legs[1:]:  # a second fee asset in the same transaction (rare)
            rows.append([cal.iso_local(first["ts"]), w["label"], w["network"], first["tx_hash"], "fee",
                         None, None, None, None, extra.symbol, dstr(-extra.qty), _eur(extra.eur()), own, ""])
    return rows


def transactions_csv(ctx, year: int, portfolio: Optional[str] = None) -> str:
    return _csv(TX_COLUMNS, transaction_rows(ctx, year, portfolio))


# --- writing -----------------------------------------------------------------------------

def exports_dir(ctx) -> Path:
    return ctx.data_dir / "exports"


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def write_wallets(ctx) -> bool:
    return _write_if_changed(exports_dir(ctx) / "wallets.csv", wallets_csv(ctx))


def write_year(ctx, year: int) -> List[str]:
    """Write the balances and transactions files of one completed fiscal year."""
    changed = []
    d = exports_dir(ctx)
    if _write_if_changed(d / balances_name(ctx, year), balances_csv(ctx, year)):
        changed.append(balances_name(ctx, year))
    if _write_if_changed(d / transactions_name(year), transactions_csv(ctx, year)):
        changed.append(transactions_name(year))
    return changed


def write_all(ctx, log=lambda m: None) -> List[str]:
    """wallets.csv plus the files of every completed fiscal year. Unchanged files are not touched."""
    changed = ["wallets.csv"] if write_wallets(ctx) else []
    for year in yearend.fiscal_years(ctx):
        changed += write_year(ctx, year)
    if changed:
        log(f"Exports updated in {exports_dir(ctx)}: {', '.join(changed)}")
    return changed


def build_file(ctx, name: str, portfolio: Optional[str] = None) -> Optional[str]:
    """Content of an export by file name (for downloads), or None if the name is not an export."""
    import re
    if name == "wallets.csv":
        return wallets_csv(ctx, portfolio)
    m = re.fullmatch(r"transactions_(\d{4})\.csv", name)
    if m:
        return transactions_csv(ctx, int(m.group(1)), portfolio)
    m = re.fullmatch(r"balances_(\d{4})-\d{2}-\d{2}\.csv", name)
    if m and name == balances_name(ctx, int(m.group(1))):
        return balances_csv(ctx, int(m.group(1)), portfolio)
    return None
