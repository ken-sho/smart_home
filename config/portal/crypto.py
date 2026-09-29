"""Модуль «Крипто»: сбор цен GRAM/TRX из CoinGecko и расчёт портфеля.

Джоба crypto_tick вызывается планировщиком раз в час. Если истории по
монете ещё нет — сначала догружает год дневных точек и 90 дней почасовых
(бесплатный CoinGecko на отрезке 2–90 дней отдаёт почасовую гранулярность).

Портфель (position_from_trades) — метод средней стоимости:
  buy       qty += q, cost += total
  earn      qty += q, earn_qty += q          (бесплатные монеты, cost не растёт)
  sell      доля f = q/qty списывает f от cost и earn_qty; realized += total − f·cost
  writeoff  как sell с total = 0
  entry = cost / (qty − earn_qty) — средняя цена ПОКУПКИ, начисления Earn её не портят;
  P&L   = qty·price − cost — уже включает доход от Earn.
"""
import json
import asyncio
import urllib.request
from datetime import datetime, timezone

CG_CHART = ("https://api.coingecko.com/api/v3/coins/{cid}/market_chart"
            "?vs_currency=usd&days={days}")
BACKFILL_MIN_POINTS = 100   # меньше точек — считаем, что истории нет
CG_PAUSE_SEC = 3            # пауза между запросами (лимиты бесплатного API)


def _fetch_chart(cid: str, days: str) -> dict:
    """Синхронный запрос к CoinGecko (urllib, без доп. зависимостей)."""
    url = CG_CHART.format(cid=cid, days=days)
    req = urllib.request.Request(url, headers={"User-Agent": "portal-crypto"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def _store(pool, symbol: str, data: dict) -> int:
    vols = {int(t): v for t, v in data.get("total_volumes", [])}
    rows = [
        (symbol, datetime.fromtimestamp(int(t) / 1000, tz=timezone.utc), float(p), vols.get(int(t)))
        for t, p in data.get("prices", []) if p is not None
    ]
    async with pool.acquire() as c:
        await c.executemany(
            "INSERT INTO crypto.prices (symbol, ts, price, volume) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (symbol, ts) DO UPDATE SET price = EXCLUDED.price, volume = EXCLUDED.volume",
            rows,
        )
    return len(rows)


def position_from_trades(trades) -> dict:
    """trades — в хронологическом порядке, поля side/qty/total (числа)."""
    qty = cost = earn_qty = realized = 0.0
    for t in trades:
        q, total = float(t["qty"]), float(t["total"])
        if t["side"] == "buy":
            qty += q
            cost += total
        elif t["side"] == "earn":
            qty += q
            earn_qty += q
        elif qty > 0:   # sell / writeoff
            f = min(q / qty, 1.0)
            realized += (total if t["side"] == "sell" else 0.0) - f * cost
            cost -= f * cost
            earn_qty -= f * earn_qty
            qty = max(qty - q, 0.0)
    bought = qty - earn_qty
    return {
        "qty": qty,
        "cost": cost,
        "earn_qty": earn_qty,
        "realized": realized,
        "entry": cost / bought if bought > 1e-9 else None,
    }


async def portfolio(c) -> dict:
    """Снимок портфеля: монеты с позицией и ценой + баланс USDT."""
    assets = await c.fetch("SELECT * FROM crypto.assets ORDER BY position")
    trades = await c.fetch(
        "SELECT symbol, side, qty, total FROM crypto.trades ORDER BY date, created_at"
    )
    prices = await c.fetch(
        """WITH last AS (
               SELECT DISTINCT ON (symbol) symbol, ts, price
                 FROM crypto.prices ORDER BY symbol, ts DESC)
           SELECT l.*,
                  (SELECT p.price FROM crypto.prices p
                    WHERE p.symbol = l.symbol AND p.ts <= l.ts - interval '24 hours'
                    ORDER BY p.ts DESC LIMIT 1) AS price_24h
             FROM last l"""
    )
    usdt = float(await c.fetchval("SELECT COALESCE(sum(amount), 0) FROM crypto.cash"))
    last = {r["symbol"]: r for r in prices}
    coins = []
    for a in assets:
        sym = a["symbol"]
        if sym == "USDT":
            price, price_24h, ts = 1.0, None, None
            pos = {"qty": usdt, "cost": usdt, "earn_qty": 0.0, "realized": 0.0, "entry": None}
        else:
            p = last.get(sym)
            price = float(p["price"]) if p else None
            price_24h = float(p["price_24h"]) if p and p["price_24h"] is not None else None
            ts = p["ts"].isoformat() if p else None
            pos = position_from_trades([t for t in trades if t["symbol"] == sym])
        value = pos["qty"] * price if price is not None else None
        coins.append({
            "symbol": sym,
            "name": a["name"],
            "earn_apr": float(a["earn_apr"]),
            "inflation": float(a["inflation"]),
            "core_qty": float(a["core_qty"]),
            "fee_pct": float(a["fee_pct"]),
            "price": price,
            "price_ts": ts,
            "change_24h": (price / price_24h - 1) * 100 if price and price_24h else None,
            "value": value,
            "pnl": value - pos["cost"] if value is not None and sym != "USDT" else None,
            **pos,
        })
    return {"coins": coins, "usdt": usdt}


async def crypto_tick(pool):
    """Раз в час: свежие почасовые точки; при пустой истории — бэкофилл."""
    async with pool.acquire() as c:
        assets = await c.fetch(
            "SELECT symbol, cg_id FROM crypto.assets WHERE cg_id IS NOT NULL ORDER BY position"
        )
    for a in assets:
        sym = a["symbol"]
        try:
            async with pool.acquire() as c:
                n = await c.fetchval("SELECT count(*) FROM crypto.prices WHERE symbol = $1", sym)
            for days in (["365", "90"] if n < BACKFILL_MIN_POINTS else ["2"]):
                data = await asyncio.to_thread(_fetch_chart, a["cg_id"], days)
                cnt = await _store(pool, sym, data)
                print(f"[crypto] {sym} days={days}: {cnt} точек")
                await asyncio.sleep(CG_PAUSE_SEC)
        except Exception as e:
            print(f"[crypto] {sym}: ошибка сбора: {e}")
