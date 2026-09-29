"""Модуль «Траты»: разбор справки о движении средств Ozon Банка (PDF) и
раскладка операций по видам и категориям.

parse_ozon_pdf(path) → шапка справки + операции. Проверяет себя по итогам
справки: сумма зачислений/списаний и «вход + зачисления − списания = исход»
должны сойтись до копейки, иначе ValueError (частичный импорт хуже никакого).

Вид операции (kind) определяется по «Назначению платежа»:
  card         оплата картой (counterparty = торговая точка)
  ozon         покупка на Платформе Ozon (counterparty = «Ozon», order = № заказа)
  ozon_refund  возврат Ozon (+)
  sbp_person   СБП физлицу / от физлица (counterparty = «Имя Отчество Ф.»)
  sbp_org      СБП организации / ИП, переводы в пользу юрлиц по реквизитам
  payment      «Платеж в пользу …» (связь и т.п.)
  salary       зарплата / аванс
  self         перевод между своими счетами (собственные средства, СБП самому себе)
  credit       погашение кредита — учитывается в модуле «Кредиты»
  other        всё остальное
self и credit в бюджет не входят (excluded): это перемещение денег, а не трата.
"""
import re
from datetime import datetime, timedelta
from decimal import Decimal

EXCLUDED_KINDS = ("self", "credit")

_AMOUNT = re.compile(r"([+-])\s*([\d\s]+[.,]\d{2})\s*₽")
_MONEY = lambda s: Decimal(re.sub(r"\s", "", s).replace(",", "."))


def _cell(s) -> str:
    """Склейка многострочной ячейки: перенос после дефиса — без пробела."""
    if s is None:
        return ""
    lines = [ln.strip() for ln in str(s).split("\n")]
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif out.endswith("-"):
            out += ln
        else:
            out += " " + ln
    return out.strip()


def _signed(s) -> Decimal | None:
    m = _AMOUNT.search(s or "")
    if not m:
        return None
    v = _MONEY(m.group(2))
    return -v if m.group(1) == "-" else v


def _short_name(full: str) -> str:
    """«Фамилия Имя Отчество» → «Имя Отчество Ф.» — так СБП пишет получателя."""
    parts = full.split()
    if len(parts) >= 3:
        return f"{parts[1]} {parts[2]} {parts[0][0]}."
    return full


def classify(desc: str, amount: Decimal, owner_short: str) -> dict:
    """Вид операции, контрагент и № заказа Ozon по тексту назначения платежа."""
    d = desc
    m = re.search(r"Оплата товаров по карте \d{4} сумма [\d.]+ в (.+?) дата \d{4}-", d)
    if m:
        merchant = re.sub(r"\s+RU$", "", m.group(1)).strip()
        return {"kind": "card", "counterparty": merchant, "order_no": None}
    m = re.search(r"Возврат оплаты .*Платформе Ozon, заказ\s*№\s*([\d-]+)", d)
    if m:
        return {"kind": "ozon_refund", "counterparty": "Ozon", "order_no": m.group(1)}
    m = re.search(r"Платформе Ozon, заказ\s*№\s*([\d-]+)", d)
    if m:
        return {"kind": "ozon", "counterparty": "Ozon", "order_no": m.group(1)}
    if "Перевод собственных средств" in d:
        return {"kind": "self", "counterparty": "Свои счета", "order_no": None}
    if re.search(r"Погашени[ея] кредита", d):
        return {"kind": "credit", "counterparty": "Кредит", "order_no": None}
    if re.search(r"заработной плат", d, re.I):
        return {"kind": "salary", "counterparty": "Зарплата", "order_no": None}
    m = re.search(r"(?:Получатель|Отправитель):\s*(.+?)\.\s*Без НДС", d)
    if m and "через СБП" in d:
        who = m.group(1).strip()
        # регулярка съедает точку после инициала («… П. Без НДС») — сравниваем без неё
        if who.rstrip(".") == owner_short.rstrip("."):
            return {"kind": "self", "counterparty": "Свои счета", "order_no": None}
        org = re.search(r'\b(ООО|АО|ПАО|НКО|ИП)\b|предприниматель', who, re.I)
        return {"kind": "sbp_org" if org else "sbp_person", "counterparty": who, "order_no": None}
    m = re.search(r"Платеж в пользу (.+?),", d)
    if m:
        return {"kind": "payment", "counterparty": m.group(1).strip(), "order_no": None}
    m = re.search(r"Перевод [0-9a-f-]{20,}\.\s*\d{10,12}\s+(.+?)\.\s*Без НДС", d)
    if m:
        who = re.sub(r"ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ", "ИП", m.group(1)).strip()
        return {"kind": "sbp_org", "counterparty": who, "order_no": None}
    if d.startswith("Перевод клиенту Банка"):
        return {"kind": "sbp_person", "counterparty": "Клиент Ozon Банка", "order_no": None}
    return {"kind": "other", "counterparty": d[:60], "order_no": None}


def parse_ozon_pdf(path) -> dict:
    import pdfplumber   # тяжёлый импорт — только при разборе

    ops, text_all = [], []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text_all.append(page.extract_text() or "")
            for table in page.extract_tables():
                for row in table:
                    if not row or not row[0]:
                        continue
                    ts = _cell(row[0])
                    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}", ts):
                        continue   # шапка таблицы
                    amount = _signed(_cell(row[3]))
                    if amount is None:
                        raise ValueError(f"Не разобрана сумма в строке {ts}")
                    ops.append({
                        "op_at": datetime.strptime(ts, "%d.%m.%Y %H:%M:%S"),
                        "doc": re.sub(r"\s", "", _cell(row[1])),
                        "desc": _cell(row[2]),
                        "amount": amount,
                    })
    text = "\n".join(text_all)
    if "ОЗОН Банк" not in text and "Ozon" not in text:
        raise ValueError("Это не справка Ozon Банка")

    def grab(rx, conv=str):
        m = re.search(rx, text)
        if not m:
            raise ValueError(f"В справке не найдено: {rx}")
        return conv(m.group(1))

    owner = grab(r"Владелец:\s*(.+)").strip()
    p_from, p_to = re.search(r"Период выписки:\s*(\d{2}\.\d{2}\.\d{4})\s*[–—-]\s*(\d{2}\.\d{2}\.\d{4})", text).groups()
    opening = grab(r"Входящий остаток:\s*([\d\s]+[.,]\d{2})", _MONEY)
    total_in = grab(r"Итого зачислений за период:\s*([\d\s]+[.,]\d{2})", _MONEY)
    total_out = grab(r"Итого списаний за период:\s*([\d\s]+[.,]\d{2})", _MONEY)
    closing = grab(r"Исходящий остаток:\s*([\d\s]+[.,]\d{2})", _MONEY)

    s_in = sum((o["amount"] for o in ops if o["amount"] > 0), Decimal(0))
    s_out = -sum((o["amount"] for o in ops if o["amount"] < 0), Decimal(0))
    if s_in != total_in or s_out != total_out or opening + total_in - total_out != closing:
        raise ValueError(
            f"Справка не сходится: зачисления {s_in} vs {total_in}, списания {s_out} vs {total_out}, "
            f"остатки {opening} + {total_in} − {total_out} ≠ {closing}"
        )

    short = _short_name(owner)
    for o in ops:
        o.update(classify(o["desc"], o["amount"], short))
    return {
        "bank": "ozon",
        "owner": owner,
        "period_from": datetime.strptime(p_from, "%d.%m.%Y").date(),
        "period_to": datetime.strptime(p_to, "%d.%m.%Y").date(),
        "opening": opening, "closing": closing, "total_in": total_in, "total_out": total_out,
        "ops": ops,
    }


# ── категории по умолчанию и правила (подстрока в контрагенте/назначении) ──
#   kind:<вид> — правило по виду операции
DEFAULT_CATEGORIES = [
    # (название, тип, [правила])
    ("Продукты", "expense", ["PYATEROCHKA", "MAGNIT", "LENTA", "OOOSKONTO", "VV_", "VKUSVILL", "KHLEBNYJ",
                             "FRUKTY", "KRASNOE&BELOE", "SOLOVI", "ИКС 5 ДИДЖИТАЛ", "VODOMAT", "АЛЬФА-М"]),
    ("Ozon", "expense", ["kind:ozon", "kind:ozon_refund"]),
    ("Дом и ремонт", "expense", ["LEMANA", "LERUA", "LEROY", "ЛЕ МОНЛИД", "FIXPRICE", "ONTARIO"]),
    ("Здоровье", "expense", ["APTEKA", "KINDERMED", "Zdorovyy", "Panteleimo"]),
    ("Кафе и доставка", "expense", ["KAFE", "SUSHI", "ПиццаФабрика", "SLADKAYA"]),
    ("Связь и подписки", "expense", ["kind:payment", "T-Mobile", "Яндекс Плюс", "ПСКОВЛАЙН", "LITMARKET", "АЕЗА"]),
    ("Авто", "expense", ["GAZPROM", "GPN"]),
    ("Одежда и спорт", "expense", ["SPORTMASTER", "СПОРТМАСТЕР"]),
    ("Госуслуги и налоги", "expense", ["EPGU"]),
    ("Развлечения", "expense", ["ПЕРСПЕКТИВА"]),
    ("Переводы людям", "expense", ["kind:sbp_person"]),
    ("Прочее", "expense", []),
    ("Зарплата", "income", ["kind:salary"]),
    ("Поступления", "income", []),
]


def match_category(op: dict, rules: list[dict]) -> tuple[int | None, str | None]:
    """rules — [{category_id, pattern}] в порядке приоритета; первое совпадение.
    Возвращает (category_id, источник): 'kind' — правило по виду операции, 'rule' — по тексту."""
    hay = (op.get("counterparty") or "") + " " + (op.get("desc") or "")
    hay_l = hay.lower()
    for r in rules:
        p = r["pattern"]
        if p.startswith("kind:"):
            if op["kind"] == p[5:]:
                return r["category_id"], "kind"
        elif p.lower() in hay_l:
            return r["category_id"], "rule"
    return None, None


# ── работа с БД ───────────────────────────────────────────────
USER_RULE_PRIORITY = 10     # правила, заданные руками, — раньше правил по умолчанию (100) и по виду (900)


async def ensure_seed(c):
    """Категории и правила по умолчанию — только если категорий ещё нет."""
    if await c.fetchval("SELECT count(*) FROM budget.categories"):
        return
    for pos, (name, kind, patterns) in enumerate(DEFAULT_CATEGORIES):
        cid = await c.fetchval(
            "INSERT INTO budget.categories (name, kind, position) VALUES ($1, $2, $3) RETURNING id",
            name, kind, pos,
        )
        for p in patterns:
            # правила по виду операции — в конце: сначала конкретные магазины/получатели
            await c.execute(
                "INSERT INTO budget.rules (category_id, pattern, priority) VALUES ($1, $2, $3) "
                "ON CONFLICT (pattern) DO NOTHING",
                cid, p, 900 if p.startswith("kind:") else 100,
            )


async def _rules_and_fallback(c):
    rules = [dict(r) for r in await c.fetch(
        "SELECT r.category_id, r.pattern, ct.kind AS cat_kind FROM budget.rules r "
        "JOIN budget.categories ct ON ct.id = r.category_id ORDER BY r.priority, r.id")]
    fb = {r["name"]: r["id"] for r in await c.fetch(
        "SELECT id, name FROM budget.categories WHERE name IN ('Прочее', 'Поступления')")}
    return rules, fb


def _pick(op, rules, fb) -> tuple[int | None, str]:
    if op["kind"] in EXCLUDED_KINDS:
        return None, "excluded"
    # зачисления — только в категории доходов; исключение — возврат Ozon (уменьшает трату)
    if op["amount"] > 0 and op["kind"] != "ozon_refund":
        rules = [r for r in rules if r.get("cat_kind") == "income"]
    cid, src = match_category(op, rules)
    if cid is None:
        return (fb.get("Поступления") if op["amount"] > 0 else fb.get("Прочее")), "fallback"
    return cid, src


async def recategorize(c) -> int:
    """Прогоняет правила по всем операциям, кроме размеченных вручную."""
    rules, fb = await _rules_and_fallback(c)
    rows = await c.fetch(
        "SELECT id, kind, amount, counterparty, description, category_id, category_source "
        "FROM budget.ops WHERE NOT category_manual")
    changed = 0
    for r in rows:
        cid, src = _pick({"kind": r["kind"], "amount": r["amount"], "counterparty": r["counterparty"],
                          "desc": r["description"]}, rules, fb)
        if cid != r["category_id"] or src != r["category_source"]:
            await c.execute("UPDATE budget.ops SET category_id = $2, category_source = $3 WHERE id = $1",
                            r["id"], cid, src)
            changed += 1
    return changed


async def import_statement(c, parsed: dict, file_path: str | None) -> dict:
    """Пишет справку и её операции; повторы (bank, op_at, doc, amount) пропускает."""
    rules, fb = await _rules_and_fallback(c)
    async with c.transaction():
        sid = await c.fetchval(
            "INSERT INTO budget.statements (bank, period_from, period_to, opening, closing, total_in, total_out, file_path) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
            parsed["bank"], parsed["period_from"], parsed["period_to"], parsed["opening"], parsed["closing"],
            parsed["total_in"], parsed["total_out"], file_path,
        )
        new = 0
        for o in parsed["ops"]:
            cid, src = _pick(o, rules, fb)
            ok = await c.fetchval(
                "INSERT INTO budget.ops (bank, op_at, doc, amount, description, kind, counterparty, order_no, "
                "category_id, category_source, statement_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
                "ON CONFLICT (bank, op_at, doc, amount) DO NOTHING RETURNING 1",
                parsed["bank"], o["op_at"], o["doc"], o["amount"], o["desc"], o["kind"], o["counterparty"],
                o["order_no"], cid, src, sid,
            )
            new += 1 if ok else 0
        dup = len(parsed["ops"]) - new
        await c.execute("UPDATE budget.statements SET ops_new = $2, ops_dup = $3 WHERE id = $1", sid, new, dup)
        # разбор мог улучшиться с прошлых загрузок — переопределяем вид у уже сохранённых операций
        fixed = await reclassify(c, parsed["bank"], _short_name(parsed["owner"]))
    if fixed:
        await recategorize(c)
    return {"statement_id": str(sid), "new": new, "dup": dup, "reclassified": fixed}


async def reclassify(c, bank: str, owner_short: str) -> int:
    """Заново определяет вид/контрагента/№ заказа по назначению платежа."""
    rows = await c.fetch(
        "SELECT id, description, amount, kind, counterparty, order_no FROM budget.ops WHERE bank = $1", bank)
    n = 0
    for r in rows:
        k = classify(r["description"], r["amount"], owner_short)
        if (k["kind"], k["counterparty"], k["order_no"]) != (r["kind"], r["counterparty"], r["order_no"]):
            await c.execute("UPDATE budget.ops SET kind = $2, counterparty = $3, order_no = $4 WHERE id = $1",
                            r["id"], k["kind"], k["counterparty"], k["order_no"])
            n += 1
    return n


def _month_bounds(month: str):
    y, m = int(month[:4]), int(month[5:7])
    return datetime(y, m, 1), datetime(y + (m == 12), m % 12 + 1, 1)


async def month_summary(c, month: str) -> dict:
    """Траты/доходы месяца по категориям + операции. month = 'YYYY-MM'.
    Разбитая операция идёт в категории своих частей (со знаком операции)."""
    start, end = _month_bounds(month)
    cats = await c.fetch("SELECT * FROM budget.categories ORDER BY position, id")
    kind_of = {ct["id"]: ct["kind"] for ct in cats}
    ops = await c.fetch(
        "SELECT * FROM budget.ops WHERE op_at >= $1 AND op_at < $2 ORDER BY op_at DESC", start, end)
    split_rows = await c.fetch(
        "SELECT s.op_id, s.category_id, s.amount FROM budget.splits s "
        "JOIN budget.ops o ON o.id = s.op_id WHERE o.op_at >= $1 AND o.op_at < $2 ORDER BY s.id", start, end)
    splits: dict = {}
    for s in split_rows:
        splits.setdefault(s["op_id"], []).append(s)
    by_cat: dict = {}
    excluded = Decimal(0)
    for o in ops:
        if o["kind"] in EXCLUDED_KINDS or o["category_id"] is None:
            excluded += o["amount"]
            continue
        sign = 1 if o["amount"] > 0 else -1
        for cid, amt in ([(s["category_id"], sign * s["amount"]) for s in splits[o["id"]]]
                         if o["id"] in splits else [(o["category_id"], o["amount"])]):
            by_cat[cid] = by_cat.get(cid, Decimal(0)) + amt
    expense = -sum((v for cid, v in by_cat.items() if kind_of.get(cid) == "expense"), Decimal(0))
    income = sum((v for cid, v in by_cat.items() if kind_of.get(cid) == "income"), Decimal(0))
    return {
        "month": month,
        "income": float(income),
        "expense": float(expense),
        "excluded": float(excluded),
        "categories": [{"id": ct["id"], "name": ct["name"], "kind": ct["kind"],
                        "total": float(by_cat.get(ct["id"], 0))} for ct in cats],
        "ops": [{
            "id": str(o["id"]), "op_at": o["op_at"].isoformat(), "amount": float(o["amount"]),
            "kind": o["kind"], "counterparty": o["counterparty"], "order_no": o["order_no"],
            "category_id": o["category_id"], "category_source": o["category_source"],
            "description": o["description"], "note": o["note"],
            "excluded": o["kind"] in EXCLUDED_KINDS,
            "splits": [{"category_id": s["category_id"], "amount": float(s["amount"])} for s in splits.get(o["id"], [])],
        } for o in ops],
    }


async def history(c, months: int = 12) -> dict:
    """Траты по месяцам и категориям за последние N месяцев с данными.
    partial = месяц не покрыт справками целиком (начало/конец периода внутри месяца)."""
    first = await c.fetchval("SELECT min(op_at) FROM budget.ops")
    if not first:
        return {"months": []}
    today = datetime.now()
    cur = (today.year, today.month)
    keys, (y, m) = [], (first.year, first.month)
    while (y, m) <= cur:
        keys.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    keys = keys[-months:]
    periods = await c.fetch("SELECT period_from, period_to FROM budget.statements")
    out = []
    for k in keys:
        s = await month_summary(c, k)
        start, end = _month_bounds(k)
        need_to = min(end.date(), today.date()) - timedelta(days=1)
        covered = any(p["period_from"] <= start.date() and p["period_to"] >= need_to for p in periods)
        out.append({
            "month": k,
            "expense": s["expense"],
            "income": s["income"],
            "by_cat": {str(ct["id"]): -ct["total"] for ct in s["categories"] if ct["kind"] == "expense" and ct["total"]},
            "partial": not covered or k == f"{cur[0]}-{cur[1]:02d}",
        })
    return {"months": out}


async def recurring(c) -> list[dict]:
    """Регулярные платежи: один контрагент в ≥3 разных месяцах с похожей суммой
    (разброс ≤ 20% от средней). Ozon и переводы между своими счетами не в счёт."""
    rows = await c.fetch(
        "SELECT o.counterparty, count(DISTINCT date_trunc('month', o.op_at)) AS months, count(*) AS n, "
        "       avg(-o.amount) AS avg, stddev_pop(-o.amount) AS sd, max(o.op_at) AS last_at, "
        "       min(ct.name) AS category "
        "FROM budget.ops o LEFT JOIN budget.categories ct ON ct.id = o.category_id "
        "WHERE o.amount < 0 AND o.kind NOT IN ('self', 'credit', 'ozon') "
        "GROUP BY o.counterparty "
        "HAVING count(DISTINCT date_trunc('month', o.op_at)) >= 3 "
        "   AND stddev_pop(-o.amount) <= 0.2 * avg(-o.amount) "
        "ORDER BY avg(-o.amount) * count(*) DESC")
    return [{"counterparty": r["counterparty"], "months": r["months"], "n": r["n"],
             "avg": float(r["avg"]), "per_month": float(r["avg"]) * r["n"] / r["months"],
             "last_at": r["last_at"].isoformat(), "category": r["category"]} for r in rows]


async def unsorted(c) -> list[dict]:
    """Очередь «Разобрать»: контрагенты без своего правила — попавшие в Прочее/Поступления
    или в «Переводы людям» только по виду операции. Сгруппировано, все месяцы."""
    rows = await c.fetch(
        "SELECT counterparty, kind, count(*) AS n, sum(amount) AS total, max(op_at) AS last_at, "
        "       min(category_id) AS category_id "
        "FROM budget.ops "
        "WHERE NOT category_manual AND kind NOT IN ('self', 'credit') "
        "  AND (category_source = 'fallback' OR (category_source = 'kind' AND kind = 'sbp_person')) "
        "GROUP BY counterparty, kind ORDER BY sum(abs(amount)) DESC")
    return [{"counterparty": r["counterparty"], "kind": r["kind"], "n": r["n"], "total": float(r["total"]),
             "last_at": r["last_at"].isoformat(), "category_id": r["category_id"]} for r in rows]


async def add_rule(c, pattern: str, category_id: int) -> int:
    """Правило пользователя «контрагент → категория» (перезаписывает прежнее) + пересчёт."""
    await c.execute(
        "INSERT INTO budget.rules (category_id, pattern, priority) VALUES ($1, $2, $3) "
        "ON CONFLICT (pattern) DO UPDATE SET category_id = EXCLUDED.category_id, priority = EXCLUDED.priority",
        category_id, pattern, USER_RULE_PRIORITY,
    )
    return await recategorize(c)
