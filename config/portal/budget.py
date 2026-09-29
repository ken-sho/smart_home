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
from datetime import datetime
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
        if who == owner_short:
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


def match_category(op: dict, rules: list[dict]) -> int | None:
    """rules — [{category_id, pattern}] в порядке приоритета; первое совпадение."""
    hay = (op.get("counterparty") or "") + " " + (op.get("desc") or "")
    hay_l = hay.lower()
    for r in rules:
        p = r["pattern"]
        if p.startswith("kind:"):
            if op["kind"] == p[5:]:
                return r["category_id"]
        elif p.lower() in hay_l:
            return r["category_id"]
    return None


# ── работа с БД ───────────────────────────────────────────────
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


def _pick(op, rules, fb):
    if op["kind"] in EXCLUDED_KINDS:
        return None
    # зачисления — только в категории доходов; исключение — возврат Ozon (уменьшает трату)
    if op["amount"] > 0 and op["kind"] != "ozon_refund":
        rules = [r for r in rules if r.get("cat_kind") == "income"]
    cid = match_category(op, rules)
    if cid is None:
        cid = fb.get("Поступления") if op["amount"] > 0 else fb.get("Прочее")
    return cid


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
            ok = await c.fetchval(
                "INSERT INTO budget.ops (bank, op_at, doc, amount, description, kind, counterparty, order_no, category_id, statement_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
                "ON CONFLICT (bank, op_at, doc, amount) DO NOTHING RETURNING 1",
                parsed["bank"], o["op_at"], o["doc"], o["amount"], o["desc"], o["kind"], o["counterparty"],
                o["order_no"], _pick(o, rules, fb), sid,
            )
            new += 1 if ok else 0
        dup = len(parsed["ops"]) - new
        await c.execute("UPDATE budget.statements SET ops_new = $2, ops_dup = $3 WHERE id = $1", sid, new, dup)
    return {"statement_id": str(sid), "new": new, "dup": dup}


async def month_summary(c, month: str) -> dict:
    """Траты/доходы месяца по категориям + операции. month = 'YYYY-MM'."""
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), m % 12 + 1, 1)
    cats = await c.fetch("SELECT * FROM budget.categories ORDER BY position, id")
    ops = await c.fetch(
        "SELECT * FROM budget.ops WHERE op_at >= $1 AND op_at < $2 ORDER BY op_at DESC", start, end)
    by_cat: dict = {}
    excluded = Decimal(0)
    for o in ops:
        if o["kind"] in EXCLUDED_KINDS or o["category_id"] is None:
            excluded += o["amount"]
            continue
        by_cat[o["category_id"]] = by_cat.get(o["category_id"], Decimal(0)) + o["amount"]
    categories = [{
        "id": ct["id"], "name": ct["name"], "kind": ct["kind"],
        "total": float(by_cat.get(ct["id"], 0)),
    } for ct in cats]
    expense = -sum((v for cid, v in by_cat.items() if any(ct["id"] == cid and ct["kind"] == "expense" for ct in cats)), Decimal(0))
    income = sum((v for cid, v in by_cat.items() if any(ct["id"] == cid and ct["kind"] == "income" for ct in cats)), Decimal(0))
    return {
        "month": month,
        "income": float(income),
        "expense": float(expense),
        "excluded": float(excluded),
        "categories": categories,
        "ops": [{
            "id": str(o["id"]), "op_at": o["op_at"].isoformat(), "amount": float(o["amount"]),
            "kind": o["kind"], "counterparty": o["counterparty"], "order_no": o["order_no"],
            "category_id": o["category_id"], "description": o["description"],
            "excluded": o["kind"] in EXCLUDED_KINDS,
        } for o in ops],
    }
