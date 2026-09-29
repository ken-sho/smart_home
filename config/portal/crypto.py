"""Модуль «Крипто»: сбор цен GRAM/TRX из CoinGecko в crypto.prices.

Джоба crypto_tick вызывается планировщиком раз в час. Если истории по
монете ещё нет — сначала догружает год дневных точек и 90 дней почасовых
(бесплатный CoinGecko на отрезке 2–90 дней отдаёт почасовую гранулярность).
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
