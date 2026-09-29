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

Советы (snapshot) — формулы по дневным свечам UTC. Правила «по закрытию»
смотрят только на закрытые дни, живая цена — лишь для пометок «ждём закрытия».
  Продажа (торговая часть = qty − ядро, от min_position_usd):
    закрытие < стоп                → выход
    продано ≥ ⅔                    → последняя треть по трейлинг-стопу
                                     (макс. закрытие с начала плана − trail_atr·ATR)
    закрытие ≥ T2                  → фиксировать до ⅔
    закрытие ≥ T1                  → фиксировать ⅓, стоп в безубыток
    иначе                          → держать
  Покупка (свободные USDT от min_usdt), по каждой монете:
    S — ближайшая поддержка ниже цены, R1/R2 — сопротивления выше,
    стоп = S − 0.5·ATR, цель = (R1 + 2·R2)/3 (выход третями),
    R:R = (цель − цена)/(цена − стоп); нужно ≥ rr_min, а если реальная
    доходность Earn монеты (ставка − инфляция) ниже USDT — ≥ rr_min_low_earn;
    цена ≤ S + near_atr·ATR; RSI ≤ rsi_hot; цена > MA50 или RSI < rsi_buy.
    Сумма = min(риск risk_pct% капитала / дистанция до стопа, max_part свободных).
"""
import json
import asyncio
import urllib.request
from datetime import datetime, timedelta, timezone

CG_CHART = ("https://api.coingecko.com/api/v3/coins/{cid}/market_chart"
            "?vs_currency=usd&days={days}")
REFRESH_MIN_SEC = 60        # чаще не дёргаем CoinGecko по кнопке

# пороги движка; переопределяются строками в crypto.params
DEFAULT_PARAMS = {
    "min_usdt": 50,             # меньше — совета на покупку нет
    "min_position_usd": 10,     # торговая часть дешевле — советов на продажу нет
    "risk_pct": 2,              # риск на сделку, % капитала (USDT + торговые части)
    "max_part": 0.3333,         # доля свободных USDT на один вход
    "rr_min": 2,
    "rr_min_low_earn": 2.5,     # для монет, чей Earn (минус инфляция) хуже USDT
    "near_atr": 1,              # «у поддержки» = не выше S + near_atr·ATR
    "trail_atr": 2,
    "rsi_hot": 70,
    "rsi_buy": 40,
}
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


# ── индикаторы ────────────────────────────────────────────────
def _candles(points) -> list[dict]:
    """Дневные свечи UTC из точек (ts, price) в хронологическом порядке."""
    out = []
    for ts, p in points:
        d = ts.astimezone(timezone.utc).date()
        if out and out[-1]["day"] == d:
            c = out[-1]
            c["high"] = max(c["high"], p)
            c["low"] = min(c["low"], p)
            c["close"] = p
        else:
            out.append({"day": d, "open": p, "high": p, "low": p, "close": p})
    return out


def _rsi(closes, n=14):
    if len(closes) <= n:
        return None
    up = dn = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        u, w = max(d, 0.0), max(-d, 0.0)
        if i == 1:
            up, dn = u, w
        else:
            up += (u - up) / n
            dn += (w - dn) / n
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)


def _sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def indicators(points, price) -> dict | None:
    today = datetime.now(timezone.utc).date()
    closed = [c for c in _candles(points) if c["day"] < today]
    if len(closed) < 2 or price is None:
        return None
    C = [c["close"] for c in closed]
    trs = [max(closed[i]["high"] - closed[i]["low"],
               abs(closed[i]["high"] - C[i - 1]),
               abs(closed[i]["low"] - C[i - 1])) for i in range(1, len(closed))]
    atr = sum(trs[-14:]) / min(len(trs), 14)
    rets = [C[i] / C[i - 1] - 1 for i in range(max(1, len(C) - 30), len(C))]
    mean = sum(rets) / len(rets)
    vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 * 365 ** 0.5 * 100
    sundays = [c for c in closed if c["day"].weekday() == 6]
    chg = lambda k: (price / C[-k] - 1) * 100 if len(C) >= k else None
    return {
        "close": C[-1],                       # последнее дневное закрытие
        "close_day": closed[-1]["day"].isoformat(),
        "prev_close": C[-2],
        "week_close": sundays[-1]["close"] if sundays else None,
        "rsi": _rsi(C + [price]),             # живой RSI (с текущей ценой)
        "ma20": _sma(C, 20),
        "ma50": _sma(C, 50),
        "atr": atr,
        "vol30": vol,
        "chg1": chg(1), "chg7": chg(7), "chg30": chg(30),
        "hi30": max(c["high"] for c in closed[-30:]),
        "lo30": min(c["low"] for c in closed[-30:]),
    }


async def load_params(c) -> dict:
    p = dict(DEFAULT_PARAMS)
    for r in await c.fetch("SELECT key, value FROM crypto.params"):
        p[r["key"]] = float(r["value"])
    return p


def _pct(a, b):
    return (a / b - 1) * 100


def _px(v):
    return f"{v:.4f}"


def _usd(v):
    return f"${v:,.2f}".replace(",", " ")


# ── продажа по позиции ────────────────────────────────────────
def sell_advice(coin, ind, plan, levels, prm, sold_since, max_close_since) -> dict:
    price = coin["price"]
    trade = max(coin["qty"] - coin["core_qty"], 0.0)
    reasons = []
    if not ind:
        return {"status": "none", "title": "Мало данных для расчёта", "reasons": []}
    # ядро: только слом тренда по недельному закрытию
    if coin["core_qty"] * price >= prm["min_position_usd"] and plan and plan["stop"] \
            and ind["week_close"] and ind["week_close"] < plan["stop"]:
        reasons.append(f"⚠ Ядро: недельное закрытие {_px(ind['week_close'])} ниже стопа {_px(plan['stop'])} — пересмотреть")
    if trade * price < prm["min_position_usd"]:
        return {"status": "none", "title": "Торговой позиции нет", "reasons": reasons}
    if not plan or not (plan["t1"] or plan["t2"] or plan["stop"]):
        return {"status": "noplan", "title": "Нет плана — задайте T1 / T2 / стоп",
                "reasons": reasons + ["Нажмите на карточку → план"]}

    t1, t2, stop = plan["t1"], plan["t2"], plan["stop"]
    cl, atr = ind["close"], ind["atr"]
    base = trade + sold_since
    thirds = min(3, max(0, round(sold_since / base * 3))) if base > 0 else 0
    part = base / 3
    fee = coin["fee_pct"] / 100
    earn_day = lambda q: q * price * coin["earn_apr"] / 100 / 365
    sell_line = lambda q: f"{q:,.2f} {coin['symbol']} ≈ {_usd(q * price * (1 - fee))} после спреда".replace(",", " ")

    if stop and cl < stop:
        status, title = "exit", "Выход: продать торговую часть"
        reasons.insert(0, f"Закрытие {ind['close_day']} {_px(cl)} ниже стопа {_px(stop)}")
        reasons.append("Продать " + sell_line(trade))
    elif thirds >= 2:
        trail = max_close_since - prm["trail_atr"] * atr
        if cl < trail:
            status, title = "exit", "Выход: последняя треть по трейлинг-стопу"
            reasons.insert(0, f"Закрытие {_px(cl)} ниже трейлинг-стопа {_px(trail)}")
            reasons.append("Продать " + sell_line(trade))
        else:
            status, title = "hold", "Держать последнюю треть"
            reasons.insert(0, f"Трейлинг-стоп {_px(trail)} (макс. закрытие {_px(max_close_since)} − {prm['trail_atr']:g}·ATR)")
    elif t2 and cl >= t2:
        q = min(trade, part * (2 - thirds))
        status, title = "take", "Фиксировать ⅔" if thirds == 0 else "Фиксировать ещё ⅓"
        reasons.insert(0, f"Закрытие {_px(cl)} ≥ T2 {_px(t2)}")
        reasons.append("Продать " + sell_line(q))
        reasons.append(f"Стоп поднять на T1 {_px(t1)}" if t1 else "Подтянуть стоп")
    elif t1 and cl >= t1 and thirds < 1:
        q = min(trade, part)
        status, title = "take", "Фиксировать ⅓"
        reasons.insert(0, f"Закрытие {_px(cl)} ≥ T1 {_px(t1)}")
        reasons.append("Продать " + sell_line(q))
        be = coin["entry"] * (1 + fee) if coin["entry"] else None
        if be and (not stop or be > stop):
            reasons.append(f"Стоп в безубыток {_px(be)}")
        reasons.append(f"Вне Earn эта часть теряет ≈ {_usd(earn_day(q))}/день")
    else:
        status, title = "hold", "Держать"
        dist = []
        if t1 and price < t1:
            dist.append(f"до T1 {_pct(t1, price):+.1f}%")
        elif t2 and price < t2:
            dist.append(f"до T2 {_pct(t2, price):+.1f}%")
        if stop:
            dist.append(f"до стопа {_pct(stop, price):+.1f}%")
        if dist:
            s = ", ".join(dist)
            reasons.insert(0, s[0].upper() + s[1:])

    # пометки
    if ind["rsi"] and ind["rsi"] > prm["rsi_hot"]:
        reasons.append(f"RSI {ind['rsi']:.0f} — перекупленность, риск отката")
    if status == "hold" and t1 and thirds < 1 and price >= t1 > cl:
        reasons.append(f"Цена выше T1 внутри дня — ждём закрытия")
    if status != "exit" and stop and price < stop <= cl:
        reasons.append(f"Цена ниже стопа внутри дня — ждём закрытия")
    for lv in levels:
        if (ind["prev_close"] - lv) * (cl - lv) < 0:
            reasons.append(f"Закрытием пробит уровень {_px(lv)} — проверьте сетку алертов")
    if thirds:
        reasons.append(f"По плану продано: {thirds}/3")
    return {"status": status, "title": title, "reasons": reasons}


# ── покупка на свободные USDT ─────────────────────────────────
def buy_candidate(coin, ind, levels, prm, usdt_apr, capital, usdt) -> dict | None:
    if not ind:
        return None
    p, atr = coin["price"], ind["atr"]
    sup = [l for l in levels if l < p]
    res = sorted(l for l in levels if l > p)
    S = max(sup) if sup else ind["lo30"]
    R1 = res[0] if res else ind["hi30"]
    R2 = res[1] if len(res) > 1 else R1
    stop = S - 0.5 * atr
    rr_at = lambda e: ((R1 + 2 * R2) / 3 - e) / (e - stop) if e > stop else None
    rr = rr_at(p)
    real = coin["earn_apr"] - coin["inflation"]
    rr_need = prm["rr_min_low_earn"] if real < usdt_apr else prm["rr_min"]
    zone_hi = S + prm["near_atr"] * atr
    near = p <= zone_hi
    hot = ind["rsi"] is not None and ind["rsi"] > prm["rsi_hot"]
    trend = (ind["ma50"] is not None and p > ind["ma50"]) or (ind["rsi"] is not None and ind["rsi"] < prm["rsi_buy"])
    ok = bool(rr and rr >= rr_need and near and not hot and trend)
    risk_frac = (p - stop) / p if p > stop else None
    amount = min(capital * prm["risk_pct"] / 100 / risk_frac, usdt * prm["max_part"], usdt) if risk_frac else 0
    mark = lambda b: "✓" if b else "✗"
    rr_zone = rr_at(zone_hi)
    rsi_s = f"{ind['rsi']:.0f}" if ind["rsi"] is not None else "—"
    if ind["ma50"] is None:
        trend_s = "MA50 ещё нет"
    elif p > ind["ma50"]:
        trend_s = f"Цена выше MA50 {_px(ind['ma50'])}"
    else:
        trend_s = f"Цена ниже MA50 {_px(ind['ma50'])}" + (f", но RSI < {prm['rsi_buy']:g} (перепроданность)" if trend else "")
    reasons = [
        f"{mark(rr and rr >= rr_need)} R:R {rr:.1f} (нужно ≥ {rr_need:g}): стоп {_px(stop)}, цели {_px(R1)} / {_px(R2)}" if rr
        else f"✗ Цена ниже расчётного стопа {_px(stop)}",
        f"{mark(near)} Зона входа {_px(S)}–{_px(zone_hi)} (поддержка + {prm['near_atr']:g}·ATR)"
        + ("" if near else f", цена на {_pct(p, zone_hi):.1f}% выше" + (f"; R:R в зоне ≈ {rr_zone:.1f}" if rr_zone else "")),
        f"{mark(not hot)} RSI {rsi_s}" + (" — перекупленность" if hot else ""),
        f"{mark(trend)} {trend_s}",
    ]
    if real < usdt_apr:
        reasons.append(f"Earn {coin['earn_apr']:g}% − инфляция {coin['inflation']:g}% = {real:.1f}% < USDT {usdt_apr:g}% — порог R:R выше")
    return {
        "symbol": coin["symbol"], "ok": ok, "rr": rr, "rr_need": rr_need,
        "amount": amount, "zone": [S, zone_hi],
        "plan": {"t1": R1, "t2": R2, "stop": stop},
        "reasons": reasons,
    }


def buy_advice(cands, usdt, prm) -> dict:
    if usdt < prm["min_usdt"]:
        return {"status": "none", "title": f"Свободных USDT меньше {_usd(prm['min_usdt'])}", "reasons": [], "candidates": cands}
    good = sorted([c for c in cands if c["ok"]], key=lambda c: -c["rr"])
    if not good:
        return {"status": "wait", "title": "Ждать", "reasons": [], "candidates": cands}
    b = good[0]
    return {
        "status": "buy", "symbol": b["symbol"],
        "title": f"Покупать {b['symbol']} на {_usd(b['amount'])}",
        "reasons": [f"План: T1 {_px(b['plan']['t1'])}, T2 {_px(b['plan']['t2'])}, стоп {_px(b['plan']['stop'])}"],
        "candidates": cands,
    }


async def snapshot(c) -> dict:
    """Портфель + индикаторы + план/уровни + советы по каждой монете и по USDT."""
    snap = await portfolio(c)
    prm = await load_params(c)
    since = datetime.now(timezone.utc) - timedelta(days=150)
    pts = await c.fetch(
        "SELECT symbol, ts, price FROM crypto.prices WHERE ts > $1 ORDER BY symbol, ts", since
    )
    plans = {r["symbol"]: r for r in await c.fetch("SELECT * FROM crypto.plans")}
    lv_rows = await c.fetch("SELECT symbol, price FROM crypto.levels ORDER BY price DESC")
    usdt_coin = next(x for x in snap["coins"] if x["symbol"] == "USDT")
    capital = snap["usdt"] + sum(
        max(x["qty"] - x["core_qty"], 0) * x["price"]
        for x in snap["coins"] if x["symbol"] != "USDT" and x["price"]
    )
    cands = []
    for coin in snap["coins"]:
        sym = coin["symbol"]
        if sym == "USDT":
            continue
        ind = indicators([(r["ts"], r["price"]) for r in pts if r["symbol"] == sym], coin["price"])
        levels = [float(r["price"]) for r in lv_rows if r["symbol"] == sym]
        pr = plans.get(sym)
        plan = {k: (float(pr[k]) if pr[k] is not None else None) for k in ("t1", "t2", "stop")} if pr else None
        sold_since = max_close = 0.0
        if pr:
            sold_since = float(await c.fetchval(
                "SELECT COALESCE(sum(qty), 0) FROM crypto.trades WHERE symbol = $1 AND side = 'sell' AND created_at >= $2",
                sym, pr["created_at"]))
            if ind:
                day0 = pr["created_at"].astimezone(timezone.utc).date()
                closes = [cd["close"] for cd in _candles([(r["ts"], r["price"]) for r in pts if r["symbol"] == sym])
                          if day0 <= cd["day"] < datetime.now(timezone.utc).date()]
                max_close = max(closes) if closes else ind["close"]
        coin["ind"] = ind
        coin["levels"] = levels
        coin["plan"] = {**plan, "created_at": pr["created_at"].isoformat()} if pr else None
        coin["advice"] = sell_advice(coin, ind, plan, levels, prm, sold_since, max_close) if coin["price"] else None
        cand = buy_candidate(coin, ind, levels, prm, usdt_coin["earn_apr"], capital, snap["usdt"]) if coin["price"] else None
        if cand:
            cands.append(cand)
    usdt_coin["advice"] = buy_advice(cands, snap["usdt"], prm)
    snap["params"] = prm
    return snap


async def log_signals(c, snap) -> int:
    """Пишет в crypto.signals смену совета (по ключу статуса). Возвращает число новых строк."""
    n = 0
    for coin in snap["coins"]:
        adv = coin.get("advice")
        if not adv:
            continue
        key = adv["status"] + (":" + adv["symbol"] if adv.get("symbol") else "")
        last = await c.fetchval(
            "SELECT key FROM crypto.signals WHERE symbol = $1 ORDER BY ts DESC, id DESC LIMIT 1", coin["symbol"]
        )
        if last == key:
            continue
        await c.execute(
            "INSERT INTO crypto.signals (symbol, key, status, title, reasons) VALUES ($1, $2, $3, $4, $5)",
            coin["symbol"], key, adv["status"], adv["title"], adv["reasons"],
        )
        n += 1
    return n


async def refresh_spot(pool) -> bool:
    """Кнопка «Обновить»: последняя точка market_chart?days=1 (5-минутная) по каждой
    монете, не чаще REFRESH_MIN_SEC. simple/price с Core не используем — отдавал 403."""
    async with pool.acquire() as c:
        last = await c.fetchval("SELECT max(ts) FROM crypto.prices")
        if last and (datetime.now(timezone.utc) - last).total_seconds() < REFRESH_MIN_SEC:
            return False
        assets = await c.fetch("SELECT symbol, cg_id FROM crypto.assets WHERE cg_id IS NOT NULL ORDER BY position")
    rows = []
    for i, a in enumerate(assets):
        if i:
            await asyncio.sleep(1)
        data = await asyncio.to_thread(_fetch_chart, a["cg_id"], "1")
        if data.get("prices"):
            t, p = data["prices"][-1]
            vols = data.get("total_volumes") or []
            rows.append((a["symbol"], datetime.fromtimestamp(int(t) / 1000, tz=timezone.utc), float(p),
                         vols[-1][1] if vols else None))
    async with pool.acquire() as c:
        await c.executemany(
            "INSERT INTO crypto.prices (symbol, ts, price, volume) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (symbol, ts) DO UPDATE SET price = EXCLUDED.price, volume = EXCLUDED.volume",
            rows,
        )
        await log_signals(c, await snapshot(c))
    return True


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
    try:
        async with pool.acquire() as c:
            n = await log_signals(c, await snapshot(c))
        if n:
            print(f"[crypto] новых советов: {n}")
    except Exception as e:
        print(f"[crypto] ошибка расчёта советов: {e}")
