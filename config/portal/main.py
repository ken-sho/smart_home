"""
Личный портал — бэкенд (FastAPI).
Пока подключён ТОЛЬКО модуль «Задачи» (схема todo).
Этот же процесс отдаёт portal.html — значит фронт и API на одном
origin, CORS не нужен, fetch('/api/...') работает напрямую.

Запуск:
    uvicorn main:app --host 0.0.0.0 --port 7000

Переменные окружения (см. .env.example):
    DATABASE_URL  — строка подключения к PostgreSQL
    PORTAL_HTML   — путь к portal.html (по умолчанию ../portal.html)
"""

import os
import re
import uuid
import json
import hmac
import hashlib
import secrets
import asyncio
import ipaddress
import calendar
from urllib.parse import unquote
from datetime import date as Date
from pathlib import Path
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATABASE_URL = os.getenv("DATABASE_URL")
# Предпочитаем раздельные переменные (удобно для systemd Environment=,
# где спецсимволы в пароле ломают разбор единой строки DSN).
DB_HOST = os.getenv("DB_HOST")
if DB_HOST and not DATABASE_URL:
    DB_KWARGS = dict(
        host=DB_HOST,
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "portal"),
        user=os.getenv("DB_USER", "portal"),
        password=os.getenv("DB_PASS", ""),
    )
else:
    DB_KWARGS = None
    DATABASE_URL = DATABASE_URL or "postgresql://postgres:postgres@localhost:5432/portal"
BASE_DIR = Path(__file__).resolve().parent
PORTAL_HTML = Path(os.getenv("PORTAL_HTML", BASE_DIR.parent / "portal.html"))
# Публичный адрес портала (для кнопки Mini App и URL вебхука). Можно
# переопределить через настройку portal_public_url в app.settings.
PORTAL_PUBLIC_URL = os.getenv("PORTAL_PUBLIC_URL", "https://core.tail751bc9.ts.net").rstrip("/")
SCHEMA_FILES = [
    BASE_DIR / "app_schema.sql",
    BASE_DIR / "events_schema.sql",
    BASE_DIR / "notes_schema.sql",
    BASE_DIR / "finance_schema.sql",
    BASE_DIR / "garage_schema.sql",
]

pool: asyncpg.Pool | None = None


async def _setup_conn(conn):
    # отдаём/принимаем jsonb как нативные Python-объекты (garage.vehicles.labels)
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await (
        asyncpg.create_pool(**DB_KWARGS, min_size=1, max_size=5, init=_setup_conn)
        if DB_KWARGS else
        asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, init=_setup_conn)
    )
    # идемпотентно создаём/обновляем схемы подключённых модулей при старте
    async with pool.acquire() as c:
        for sql in SCHEMA_FILES:
            if sql.exists():
                await c.execute(sql.read_text(encoding="utf-8"))
    # регистрируем вебхук бота, если задан токен (не валим старт при ошибке)
    try:
        if await get_setting("telegram_bot_token"):
            res = await register_webhook()
            if not res.get("ok"):
                print("[webhook] не зарегистрирован:", res.get("error"))
    except Exception as e:
        print("[webhook] ошибка регистрации:", e)
    yield
    await pool.close()


app = FastAPI(title="Portal API", lifespan=lifespan)

# на случай разработки, когда portal.html открыт как файл (origin file://)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════
#  АВТОРИЗАЦИЯ (Telegram Mini App + доверенная сеть Tailscale)
#  Портал можно безопасно открыть наружу: публичные запросы должны
#  нести подписанный Telegram initData, запросы из tailnet проходят
#  без проверки. Подробности — в комментариях ниже.
# ══════════════════════════════════════════════════════════════

# CGNAT-диапазон Tailscale (100.64.0.0/10). За nginx настоящий адрес клиента
# лежит в X-Real-IP (nginx ставит его из $remote_addr и затирает любой
# присланный клиентом). Доверяем X-Real-IP ТОЛЬКО когда запрос пришёл от
# самого прокси (приватный/loopback peer) — иначе публичный клиент, достучавшись
# до бэкенда напрямую, мог бы подделать X-Real-IP: 100.x и обойти проверку.
TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")


def is_tailscale_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in TAILSCALE_RANGE
    except ValueError:
        return False


def _is_proxy_peer(ip: str) -> bool:
    """True, если сокет-peer — наш доверенный прокси: приватная сеть Docker
       (172.16/12, 10/8, 192.168/16) или loopback. Только от такого peer
       имеет смысл читать X-Real-IP."""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback
    except ValueError:
        return False


def client_real_ip(request: Request) -> str:
    """Реальный IP клиента: за nginx — из X-Real-IP, при прямом доступе — peer."""
    peer = request.client.host if request.client else ""
    if peer and _is_proxy_peer(peer):
        return request.headers.get("X-Real-IP") or peer
    return peer


def verify_telegram_init_data(init_data: str, bot_token: str) -> bool:
    """Проверка подписи Telegram WebApp initData (HMAC-SHA256).
       Значения приходят URL-кодированными — обязательно декодируем перед
       сборкой data_check_string, иначе подпись поля `user` (JSON) не сойдётся."""
    if not init_data:
        return False
    parsed: dict[str, str] = {}
    for chunk in init_data.split("&"):
        k, _, v = chunk.partition("=")
        parsed[k] = unquote(v)
    hash_val = parsed.pop("hash", None)
    if not hash_val:
        return False
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, hash_val)


async def get_setting(key: str) -> str | None:
    """Достаёт одно значение из app.settings (например, telegram_bot_token)."""
    async with pool.acquire() as c:
        return await c.fetchval("SELECT value FROM app.settings WHERE key=$1", key)


def get_telegram_user_id(init_data: str) -> str | None:
    """Достаёт id пользователя из подписанного initData (поле user — JSON)."""
    for chunk in init_data.split("&"):
        k, _, v = chunk.partition("=")
        if k == "user":
            try:
                return str(json.loads(unquote(v)).get("id"))
            except (ValueError, AttributeError):
                return None
    return None


# ── Telegram Bot API (отправка сообщений, вебхук) ──────────────
def _bot_api(token: str, method: str, payload: dict) -> dict:
    """Синхронный вызов Bot API (urllib, без доп. зависимостей).
       Бросает RuntimeError с понятным текстом при ошибке."""
    import urllib.request
    import urllib.error

    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8"))
            raise RuntimeError(data.get("description") or f"HTTP {e.code}")
        except (ValueError, AttributeError):
            raise RuntimeError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Сеть: {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "Telegram вернул ok=false")
    return data


async def send_message(token: str, chat_id, text: str, reply_markup: dict | None = None,
                       thread_id: str | None = None) -> dict:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            raise RuntimeError("Thread ID должен быть числом")
    return await asyncio.to_thread(_bot_api, token, "sendMessage", payload)


async def set_webhook(token: str, url: str, secret: str | None = None) -> dict:
    payload = {"url": url, "allowed_updates": ["message"]}
    if secret:
        payload["secret_token"] = secret
    return await asyncio.to_thread(_bot_api, token, "setWebhook", payload)


async def portal_public_base() -> str:
    """Базовый публичный URL портала (настройка переопределяет константу)."""
    return ((await get_setting("portal_public_url")) or PORTAL_PUBLIC_URL).rstrip("/")


async def ensure_webhook_secret() -> str:
    """Секрет для проверки X-Telegram-Bot-Api-Secret-Token. Генерим один раз."""
    sec = await get_setting("telegram_webhook_secret")
    if not sec:
        sec = secrets.token_hex(16)
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO app.settings (key, value) VALUES ('telegram_webhook_secret', $1) "
                "ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=now()",
                sec,
            )
    return sec


async def register_webhook() -> dict:
    """Регистрирует вебхук бота на публичном адресе портала."""
    token = await get_setting("telegram_bot_token")
    if not token:
        return {"ok": False, "error": "Не задан токен бота"}
    base = await portal_public_base()
    hook_url = f"{base}/api/bot/webhook"
    try:
        secret = await ensure_webhook_secret()
        await set_webhook(token, hook_url, secret)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "url": hook_url}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Авторизуем только API. Сам SPA (/), иконка и т.п. — открыты,
    # без них не показать баннер «откройте через Telegram».
    # CORS-preflight (OPTIONS) пропускаем — у него нет заголовка авторизации.
    if not path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)

    # Вебхук бота — публичный: сюда стучится сам Telegram. Подлинность
    # проверяется отдельно по секрету X-Telegram-Bot-Api-Secret-Token.
    if path == "/api/bot/webhook":
        return await call_next(request)

    # 1) Доверенная сеть Tailscale — без авторизации.
    #    За nginx реальный IP берём из X-Real-IP (см. client_real_ip).
    real_ip = client_real_ip(request)
    if is_tailscale_ip(real_ip):
        request.state.auth_source = "tailscale"
        return await call_next(request)

    # 2) Публичный доступ — только с валидным Telegram initData
    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    bot_token = await get_setting("telegram_bot_token")
    if not bot_token:
        return JSONResponse({"detail": "Bot token not configured"}, status_code=503)
    if not verify_telegram_init_data(init_data, bot_token):
        return JSONResponse({"detail": "Invalid Telegram auth"}, status_code=401)
    # Подпись валидна → запрос точно из Telegram. Теперь проверяем, что открыл
    # именно владелец портала. Если telegram_owner_id не задан — пускаем любого
    # (режим первичной настройки), иначе сверяем user.id.
    owner_id = await get_setting("telegram_owner_id")
    if owner_id:
        user_id = get_telegram_user_id(init_data)
        if not user_id or user_id != owner_id.strip():
            return JSONResponse({"detail": "Access denied"}, status_code=403)
    request.state.auth_source = "telegram"
    return await call_next(request)


@app.get("/api/auth/check")
async def auth_check(request: Request):
    """Лёгкий пинг для фронта: дошли сюда → авторизованы. Источник —
       tailscale (доверенная сеть) или telegram (подписанный initData)."""
    return {"ok": True, "source": getattr(request.state, "auth_source", "unknown")}


# ── Бот: вебхук и переподключение ─────────────────────────────
@app.post("/api/bot/webhook")
async def bot_webhook(request: Request):
    """Принимает апдейты от Telegram. Публичный (см. middleware), но защищён
       секретом setWebhook. Отвечает на /portal и /start кнопкой Mini App."""
    expected = await get_setting("telegram_webhook_secret")
    if expected:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if got != expected:
            return JSONResponse({"ok": False}, status_code=403)

    try:
        data = await request.json()
    except Exception:
        return {"ok": True}
    message = data.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id or not text:
        return {"ok": True}

    # /portal@botname → /portal
    cmd = text.split()[0].split("@")[0].lower()
    if cmd in ("/portal", "/start"):
        token = await get_setting("telegram_bot_token")
        if token:
            base = await portal_public_base()
            greeting = "Добро пожаловать в портал 👋" if cmd == "/start" else "Откройте портал 👇"
            try:
                await send_message(token, chat_id, greeting, reply_markup={
                    "inline_keyboard": [[{
                        "text": "Открыть портал",
                        "web_app": {"url": f"{base}/portal/"},
                    }]],
                })
            except Exception as e:
                print("[webhook] sendMessage:", e)
    return {"ok": True}


@app.post("/api/bot/webhook/register")
async def bot_webhook_register():
    """Переподключить вебхук (например, после смены токена). Только изнутри
       (за авторизацией middleware)."""
    return await register_webhook()


# ── модели ────────────────────────────────────────────────────
class TaskIn(BaseModel):
    text: str
    deadline: Date | None = None
    tag: str | None = None


class TaskPatch(BaseModel):
    text: str | None = None
    done: bool | None = None
    deadline: Date | None = None
    tag: str | None = None


def to_task(r: asyncpg.Record) -> dict:
    return {
        "id": str(r["id"]),
        "text": r["text"],
        "done": r["done"],
        "deadline": r["deadline"].isoformat() if r["deadline"] else None,
        "tag": r["tag"],
        "created_at": r["created_at"].isoformat(),
    }


# ── Todo API ──────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    async with pool.acquire() as c:
        await c.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/todo/tasks")
async def list_tasks():
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM todo.tasks ORDER BY created_at")
    return [to_task(r) for r in rows]


@app.post("/api/todo/tasks", status_code=201)
async def create_task(t: TaskIn):
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "INSERT INTO todo.tasks (text, deadline, tag) "
            "VALUES ($1, $2, $3) RETURNING *",
            t.text, t.deadline, t.tag,
        )
    return to_task(r)


_ALLOWED = ("text", "done", "deadline", "tag")


@app.patch("/api/todo/tasks/{task_id}")
async def update_task(task_id: uuid.UUID, t: TaskPatch):
    fields = {k: v for k, v in t.model_dump(exclude_unset=True).items() if k in _ALLOWED}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    cols = list(fields.keys())
    set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    values = [fields[c] for c in cols]
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE todo.tasks SET {set_clause} WHERE id = $1 RETURNING *",
            task_id, *values,
        )
    if not r:
        raise HTTPException(404, "Задача не найдена")
    return to_task(r)


@app.delete("/api/todo/tasks/{task_id}", status_code=204)
async def delete_task(task_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM todo.tasks WHERE id = $1", task_id)
    if res.endswith("0"):
        raise HTTPException(404, "Задача не найдена")


@app.post("/api/todo/tasks/clear-done", status_code=204)
async def clear_done():
    async with pool.acquire() as c:
        await c.execute("DELETE FROM todo.tasks WHERE done")


# ══════════════════════════════════════════════════════════════
#  SETTINGS API (схема app: settings — key-value)
#  Сквозные настройки портала. Содержит секреты (токен бота) →
#  ВНИМАНИЕ (ops): эндпоинт только для внутренней сети, НЕ выставлять
#  наружу через Funnel.
# ══════════════════════════════════════════════════════════════
class SettingIn(BaseModel):
    key: str
    value: str | None = None


@app.get("/api/settings")
async def get_settings():
    """Все настройки одним плоским объектом { key: value, ... }."""
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT key, value FROM app.settings")
    return {r["key"]: r["value"] for r in rows}


@app.post("/api/settings")
async def save_setting(s: SettingIn):
    """UPSERT одной настройки. Возвращает { key, value }."""
    key = s.key.strip()
    if not key:
        raise HTTPException(400, "Пустой ключ")
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "INSERT INTO app.settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now() "
            "RETURNING key, value",
            key, s.value,
        )
    return {"key": r["key"], "value": r["value"]}


def _send_telegram(token: str, chat_id: str, text: str, thread_id: str = "") -> None:
    """Синхронная отправка через Telegram Bot API (urllib, без доп. зависимостей).
       thread_id (необязателен) → message_thread_id для топиков супергрупп.
       Бросает исключение с понятным текстом при ошибке."""
    import json as _json
    import urllib.request
    import urllib.error

    payload = {"chat_id": chat_id, "text": text}
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            raise RuntimeError("Thread ID должен быть числом")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Telegram отдаёт описание ошибки в теле ответа
        try:
            data = _json.loads(e.read().decode("utf-8"))
            raise RuntimeError(data.get("description") or f"HTTP {e.code}")
        except (ValueError, AttributeError):
            raise RuntimeError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Сеть: {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "Telegram вернул ok=false")


@app.post("/api/settings/telegram/test")
async def telegram_test():
    """Берёт токен и chat_id из БД и шлёт тестовое сообщение.
       Только для внутренней сети (см. app_schema.sql). Никогда не выставлять
       наружу через Funnel — использует секретный токен бота."""
    import asyncio

    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT key, value FROM app.settings "
            "WHERE key IN ('telegram_bot_token', 'telegram_chat_id', 'telegram_thread_id')"
        )
    cfg = {r["key"]: (r["value"] or "").strip() for r in rows}
    token = cfg.get("telegram_bot_token", "")
    chat_id = cfg.get("telegram_chat_id", "")
    thread_id = cfg.get("telegram_thread_id", "")
    if not token:
        return {"ok": False, "error": "Не задан токен бота"}
    if not chat_id:
        return {"ok": False, "error": "Не задан Chat ID"}
    try:
        await asyncio.to_thread(_send_telegram, token, chat_id, "✅ Портал работает", thread_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
#  NOTES API (схема notes: projects · tags · notes · note_tags)
# ══════════════════════════════════════════════════════════════
class ProjectIn(BaseModel):
    name: str
    color: str | None = "#52b788"


class ProjectPatch(BaseModel):
    name: str | None = None
    color: str | None = None
    position: int | None = None


class TagIn(BaseModel):
    project_id: uuid.UUID
    name: str
    color: str | None = "green"


class TagPatch(BaseModel):
    name: str | None = None
    color: str | None = None
    position: int | None = None


class NoteIn(BaseModel):
    project_id: uuid.UUID
    title: str = ""
    type: str = "text"
    language: str | None = None
    body: str = ""
    rich: bool = True
    favorite: bool = False
    tags: list[uuid.UUID] = []


class NotePatch(BaseModel):
    title: str | None = None
    type: str | None = None
    language: str | None = None
    body: str | None = None
    rich: bool | None = None
    favorite: bool | None = None
    tags: list[uuid.UUID] | None = None


class QuickIn(BaseModel):
    project_id: uuid.UUID
    name: str
    value: str = ""


class QuickPatch(BaseModel):
    name: str | None = None
    value: str | None = None
    position: int | None = None


def to_project(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "color": r["color"],
        "position": r["position"],
        "created_at": r["created_at"].isoformat(),
    }


def to_tag(r) -> dict:
    return {
        "id": str(r["id"]),
        "project_id": str(r["project_id"]),
        "name": r["name"],
        "color": r["color"],
        "position": r["position"],
        "created_at": r["created_at"].isoformat(),
    }


def to_quick(r) -> dict:
    return {
        "id": str(r["id"]),
        "project_id": str(r["project_id"]),
        "name": r["name"],
        "value": r["value"],
        "position": r["position"],
        "created_at": r["created_at"].isoformat(),
    }


def to_note(r) -> dict:
    return {
        "id": str(r["id"]),
        "project_id": str(r["project_id"]),
        "title": r["title"],
        "type": r["type"],
        "language": r["language"],
        "body": r["body"],
        "rich": r["rich"],
        "favorite": r["favorite"],
        "position": r["position"],
        "tags": [str(x) for x in (r["tags"] or [])],
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    }


_NOTE_SELECT = """
    SELECT n.*,
           COALESCE(array_agg(nt.tag_id) FILTER (WHERE nt.tag_id IS NOT NULL), '{}') AS tags
    FROM notes.notes n
    LEFT JOIN notes.note_tags nt ON nt.note_id = n.id
"""


async def fetch_note(c, note_id) -> dict | None:
    r = await c.fetchrow(_NOTE_SELECT + " WHERE n.id = $1 GROUP BY n.id", note_id)
    return to_note(r) if r else None


async def sync_note_tags(c, note_id, tag_ids):
    await c.execute("DELETE FROM notes.note_tags WHERE note_id = $1", note_id)
    if tag_ids:
        await c.executemany(
            "INSERT INTO notes.note_tags (note_id, tag_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            [(note_id, t) for t in tag_ids],
        )


# ── загрузка всего модуля разом ───────────────────────────────
@app.get("/api/notes/bootstrap")
async def notes_bootstrap():
    async with pool.acquire() as c:
        # гарантируем хотя бы один проект, чтобы фронту было куда писать
        if await c.fetchval("SELECT count(*) FROM notes.projects") == 0:
            await c.execute(
                "INSERT INTO notes.projects (name, position) VALUES ('Общее', 0)"
            )
        projects = await c.fetch(
            "SELECT * FROM notes.projects ORDER BY position, created_at"
        )
        tags = await c.fetch("SELECT * FROM notes.tags ORDER BY position, created_at")
        notes = await c.fetch(_NOTE_SELECT + " GROUP BY n.id ORDER BY n.created_at")
        quick = await c.fetch(
            "SELECT * FROM notes.quick_secrets ORDER BY position, created_at"
        )
    return {
        "projects": [to_project(p) for p in projects],
        "tags": [to_tag(t) for t in tags],
        "notes": [to_note(n) for n in notes],
        "quick": [to_quick(q) for q in quick],
    }


# ── проекты ───────────────────────────────────────────────────
@app.post("/api/notes/projects", status_code=201)
async def create_project(p: ProjectIn):
    async with pool.acquire() as c:
        pos = await c.fetchval("SELECT COALESCE(max(position)+1, 0) FROM notes.projects")
        r = await c.fetchrow(
            "INSERT INTO notes.projects (name, color, position) "
            "VALUES ($1, $2, $3) RETURNING *",
            p.name, p.color or "#52b788", pos,
        )
    return to_project(r)


@app.patch("/api/notes/projects/{project_id}")
async def update_project(project_id: uuid.UUID, p: ProjectPatch):
    fields = {k: v for k, v in p.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    cols = list(fields.keys())
    set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE notes.projects SET {set_clause} WHERE id = $1 RETURNING *",
            project_id, *[fields[c] for c in cols],
        )
    if not r:
        raise HTTPException(404, "Проект не найден")
    return to_project(r)


@app.delete("/api/notes/projects/{project_id}", status_code=204)
async def delete_project(project_id: uuid.UUID):
    # notes и tags удалятся каскадом (ON DELETE CASCADE)
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM notes.projects WHERE id = $1", project_id)
    if res.endswith("0"):
        raise HTTPException(404, "Проект не найден")


# ── теги ──────────────────────────────────────────────────────
@app.post("/api/notes/tags", status_code=201)
async def create_tag(t: TagIn):
    async with pool.acquire() as c:
        pos = await c.fetchval(
            "SELECT COALESCE(max(position)+1, 0) FROM notes.tags WHERE project_id = $1",
            t.project_id,
        )
        try:
            r = await c.fetchrow(
                "INSERT INTO notes.tags (project_id, name, color, position) "
                "VALUES ($1, $2, $3, $4) RETURNING *",
                t.project_id, t.name, t.color or "green", pos,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, "Тег с таким именем уже есть в проекте")
    return to_tag(r)


@app.patch("/api/notes/tags/{tag_id}")
async def update_tag(tag_id: uuid.UUID, t: TagPatch):
    fields = {k: v for k, v in t.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    cols = list(fields.keys())
    set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE notes.tags SET {set_clause} WHERE id = $1 RETURNING *",
            tag_id, *[fields[c] for c in cols],
        )
    if not r:
        raise HTTPException(404, "Тег не найден")
    return to_tag(r)


@app.delete("/api/notes/tags/{tag_id}", status_code=204)
async def delete_tag(tag_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM notes.tags WHERE id = $1", tag_id)
    if res.endswith("0"):
        raise HTTPException(404, "Тег не найден")


# ── быстрый доступ (секреты) ──────────────────────────────────
@app.post("/api/notes/quick", status_code=201)
async def create_quick(q: QuickIn):
    async with pool.acquire() as c:
        pos = await c.fetchval(
            "SELECT COALESCE(max(position)+1, 0) FROM notes.quick_secrets WHERE project_id = $1",
            q.project_id,
        )
        r = await c.fetchrow(
            "INSERT INTO notes.quick_secrets (project_id, name, value, position) "
            "VALUES ($1, $2, $3, $4) RETURNING *",
            q.project_id, q.name, q.value or "", pos,
        )
    return to_quick(r)


@app.patch("/api/notes/quick/{quick_id}")
async def update_quick(quick_id: uuid.UUID, q: QuickPatch):
    fields = {k: v for k, v in q.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    cols = list(fields.keys())
    set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE notes.quick_secrets SET {set_clause} WHERE id = $1 RETURNING *",
            quick_id, *[fields[c] for c in cols],
        )
    if not r:
        raise HTTPException(404, "Кнопка не найдена")
    return to_quick(r)


@app.delete("/api/notes/quick/{quick_id}", status_code=204)
async def delete_quick(quick_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM notes.quick_secrets WHERE id = $1", quick_id)
    if res.endswith("0"):
        raise HTTPException(404, "Кнопка не найдена")


# ── заметки ───────────────────────────────────────────────────
@app.post("/api/notes/notes", status_code=201)
async def create_note(n: NoteIn):
    async with pool.acquire() as c:
        async with c.transaction():
            pos = await c.fetchval(
                "SELECT COALESCE(max(position)+1, 0) FROM notes.notes WHERE project_id = $1",
                n.project_id,
            )
            row = await c.fetchrow(
                "INSERT INTO notes.notes "
                "(project_id, title, type, language, body, rich, favorite, position) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                n.project_id, n.title, n.type, n.language, n.body, n.rich,
                n.favorite, pos,
            )
            await sync_note_tags(c, row["id"], n.tags)
            note = await fetch_note(c, row["id"])
    return note


@app.patch("/api/notes/notes/{note_id}")
async def update_note(note_id: uuid.UUID, n: NotePatch):
    data = n.model_dump(exclude_unset=True)
    tags = data.pop("tags", None)
    async with pool.acquire() as c:
        async with c.transaction():
            if data:
                cols = list(data.keys())
                set_clause = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
                r = await c.fetchrow(
                    f"UPDATE notes.notes SET {set_clause} WHERE id = $1 RETURNING id",
                    note_id, *[data[c] for c in cols],
                )
                if not r:
                    raise HTTPException(404, "Заметка не найдена")
            elif tags is None:
                raise HTTPException(400, "Нет полей для обновления")
            if tags is not None:
                await sync_note_tags(c, note_id, tags)
            note = await fetch_note(c, note_id)
    if not note:
        raise HTTPException(404, "Заметка не найдена")
    return note


@app.delete("/api/notes/notes/{note_id}", status_code=204)
async def delete_note(note_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM notes.notes WHERE id = $1", note_id)
    if res.endswith("0"):
        raise HTTPException(404, "Заметка не найдена")


# ══════════════════════════════════════════════════════════════
#  FINANCE API — режим «Кредиты» (схема finance: credits)
#  Плоская модель: строка = { month, name, amount, due_day, paid }.
#  month = 'YYYY-MM'. Бэкенд считает просрочку и копирует список
#  при наступлении нового месяца.
# ══════════════════════════════════════════════════════════════
class CreditIn(BaseModel):
    month: str
    name: str
    amount: float = 0
    due_day: int | None = None
    paid: bool = False


class CreditPatch(BaseModel):
    name: str | None = None
    amount: float | None = None
    due_day: int | None = None
    paid: bool | None = None
    position: int | None = None


def _is_overdue(month: str, due_day, paid: bool, today: Date) -> bool:
    """Кредит просрочен, если не оплачен и эффективная дата платежа < сегодня.
       Для NULL due_day берём последний день месяца (прошлый месяц без даты
       тоже считается просроченным; текущий месяц без даты — ещё нет)."""
    if paid:
        return False
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    day = min(due_day, last) if due_day else last
    return Date(y, m, day) < today


def to_credit(r, today: Date) -> dict:
    return {
        "id": str(r["id"]),
        "month": r["month"],
        "name": r["name"],
        "amount": float(r["amount"]),
        "due_day": r["due_day"],
        "paid": r["paid"],
        "position": r["position"],
        "overdue": _is_overdue(r["month"], r["due_day"], r["paid"], today),
        "created_at": r["created_at"].isoformat(),
    }


async def _overdue_count(c, today: Date) -> int:
    """Сколько просроченных неоплаченных кредитов по ВСЕМ месяцам (для бейджа)."""
    rows = await c.fetch("SELECT month, due_day FROM finance.credits WHERE NOT paid")
    return sum(1 for r in rows if _is_overdue(r["month"], r["due_day"], False, today))


async def _ensure_month(c, month: str):
    """При первом обращении к месяцу создаём маркер и копируем строки из
       самого свежего прошлого месяца (paid сбрасываем). Повторно не копируем."""
    if await c.fetchval("SELECT 1 FROM finance.months WHERE month = $1", month):
        return
    async with c.transaction():
        await c.execute(
            "INSERT INTO finance.months (month) VALUES ($1) ON CONFLICT DO NOTHING",
            month,
        )
        prev = await c.fetchval(
            "SELECT month FROM finance.credits WHERE month < $1 "
            "ORDER BY month DESC LIMIT 1",
            month,
        )
        if prev:
            await c.execute(
                "INSERT INTO finance.credits "
                "(month, name, amount, due_day, paid, position) "
                "SELECT $1, name, amount, due_day, false, position "
                "FROM finance.credits WHERE month = $2",
                month, prev,
            )


async def _month_payload(c, month: str, today: Date) -> dict:
    credits = await c.fetch(
        "SELECT * FROM finance.credits WHERE month = $1 "
        "ORDER BY due_day ASC NULLS LAST, created_at",   # сортировка по числу (дате)
        month,
    )
    return {
        "month": month,
        "credits": [to_credit(r, today) for r in credits],
        "overdue": await _overdue_count(c, today),
    }


# ── bootstrap: текущий месяц + список месяцев + бейдж ──────────
@app.get("/api/finance/bootstrap")
async def finance_bootstrap():
    today = Date.today()
    cur = today.strftime("%Y-%m")
    async with pool.acquire() as c:
        await _ensure_month(c, cur)           # копирование при наступлении нового месяца
        months = [r["month"] for r in await c.fetch(
            "SELECT DISTINCT month FROM finance.credits ORDER BY month"
        )]
        payload = await _month_payload(c, cur, today)
    payload.update({
        "today": today.isoformat(),
        "current_month": cur,
        "months": months,
    })
    return payload


# ── произвольный месяц (прошлое; будущее запрещено) ───────────
@app.get("/api/finance/month/{month}")
async def finance_month(month: str):
    today = Date.today()
    cur = today.strftime("%Y-%m")
    if month > cur:
        raise HTTPException(400, "Будущие месяцы недоступны")
    async with pool.acquire() as c:
        if month == cur:
            await _ensure_month(c, month)
        payload = await _month_payload(c, month, today)
    return payload


# ── кредиты CRUD ──────────────────────────────────────────────
@app.post("/api/finance/credits", status_code=201)
async def create_credit(cr: CreditIn):
    today = Date.today()
    async with pool.acquire() as c:
        pos = await c.fetchval(
            "SELECT COALESCE(max(position)+1, 0) FROM finance.credits WHERE month = $1",
            cr.month,
        )
        r = await c.fetchrow(
            "INSERT INTO finance.credits "
            "(month, name, amount, due_day, paid, position) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
            cr.month, cr.name, cr.amount, cr.due_day, cr.paid, pos,
        )
    return to_credit(r, today)


@app.patch("/api/finance/credits/{credit_id}")
async def update_credit(credit_id: uuid.UUID, cr: CreditPatch):
    today = Date.today()
    fields = {k: v for k, v in cr.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    cols = list(fields.keys())
    set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(cols))
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE finance.credits SET {set_clause} WHERE id = $1 RETURNING *",
            credit_id, *[fields[col] for col in cols],
        )
    if not r:
        raise HTTPException(404, "Кредит не найден")
    return to_credit(r, today)


@app.delete("/api/finance/credits/{credit_id}", status_code=204)
async def delete_credit(credit_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM finance.credits WHERE id = $1", credit_id)
    if res.endswith("0"):
        raise HTTPException(404, "Кредит не найден")


# ══════════════════════════════════════════════════════════════
#  FINANCE — режим «Д/К» (приход/расход, схема finance.entries)
#  Месяц-ориентированный, БЕЗ копирования между месяцами.
#  Оплаченные кредиты месяца подтягиваются в расход на лету.
# ══════════════════════════════════════════════════════════════
class EntryIn(BaseModel):
    month: str
    kind: str            # 'in' | 'out'
    name: str = ""
    amount: float = 0
    due_day: int | None = None


class EntryPatch(BaseModel):
    kind: str | None = None
    name: str | None = None
    amount: float | None = None
    due_day: int | None = None
    position: int | None = None


def to_entry(r) -> dict:
    return {
        "id": str(r["id"]),
        "month": r["month"],
        "kind": r["kind"],
        "name": r["name"],
        "amount": float(r["amount"]),
        "due_day": r["due_day"],
        "position": r["position"],
        "created_at": r["created_at"].isoformat(),
    }


def _check_kind(kind):
    if kind is not None and kind not in ("in", "out"):
        raise HTTPException(400, "kind должен быть 'in' или 'out'")


# ── загрузка месяца Д/К: события + сумма оплаченных кредитов ───
@app.get("/api/finance/dk/{month}")
async def finance_dk(month: str):
    cur = Date.today().strftime("%Y-%m")
    if month > cur:
        raise HTTPException(400, "Будущие месяцы недоступны")
    async with pool.acquire() as c:
        entries = await c.fetch(
            "SELECT * FROM finance.entries WHERE month = $1 "
            "ORDER BY due_day ASC NULLS LAST, created_at",
            month,
        )
        credits_paid = await c.fetchval(
            "SELECT COALESCE(sum(amount), 0) FROM finance.credits "
            "WHERE month = $1 AND paid",
            month,
        )
    return {
        "month": month,
        "entries": [to_entry(r) for r in entries],
        "credits_paid": float(credits_paid),
    }


@app.post("/api/finance/entries", status_code=201)
async def create_entry(e: EntryIn):
    _check_kind(e.kind)
    async with pool.acquire() as c:
        pos = await c.fetchval(
            "SELECT COALESCE(max(position)+1, 0) FROM finance.entries WHERE month = $1",
            e.month,
        )
        r = await c.fetchrow(
            "INSERT INTO finance.entries "
            "(month, kind, name, amount, due_day, position) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
            e.month, e.kind, e.name, e.amount, e.due_day, pos,
        )
    return to_entry(r)


@app.patch("/api/finance/entries/{entry_id}")
async def update_entry(entry_id: uuid.UUID, e: EntryPatch):
    _check_kind(e.kind)
    fields = {k: v for k, v in e.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    cols = list(fields.keys())
    set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(cols))
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE finance.entries SET {set_clause} WHERE id = $1 RETURNING *",
            entry_id, *[fields[col] for col in cols],
        )
    if not r:
        raise HTTPException(404, "Событие не найдено")
    return to_entry(r)


@app.delete("/api/finance/entries/{entry_id}", status_code=204)
async def delete_entry(entry_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM finance.entries WHERE id = $1", entry_id)
    if res.endswith("0"):
        raise HTTPException(404, "Событие не найдено")


# ══════════════════════════════════════════════════════════════
#  GARAGE API (схема garage: vehicles · services)
#  Master-detail: ТС → записи обслуживания. БД-обязательный модуль.
# ══════════════════════════════════════════════════════════════
class VehicleIn(BaseModel):
    name: str
    type: str = "car"
    year: int | None = None
    vin: str | None = None
    labels: list = []


class VehiclePatch(BaseModel):
    name: str | None = None
    type: str | None = None
    year: int | None = None
    vin: str | None = None
    labels: list | None = None
    archived: bool | None = None
    position: int | None = None


class ServiceIn(BaseModel):
    vehicle_id: uuid.UUID
    name: str = ""
    cost: float | None = None
    date: Date | None = None   # Date = datetime.date (алиас), чтобы имя поля date не затеняло тип
    mileage: int | None = None


class ServicePatch(BaseModel):
    name: str | None = None
    cost: float | None = None
    date: Date | None = None
    mileage: int | None = None
    position: int | None = None


def _check_vtype(t):
    if t is not None and t not in ("car", "moto", "quad", "other"):
        raise HTTPException(400, "Неизвестный тип ТС")


def to_vehicle(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "type": r["type"],
        "year": r["year"],
        "vin": r["vin"],
        "labels": r["labels"] or [],
        "archived": r["archived"],
        "position": r["position"],
        "created_at": r["created_at"].isoformat(),
    }


def to_service(r) -> dict:
    return {
        "id": str(r["id"]),
        "vehicle_id": str(r["vehicle_id"]),
        "name": r["name"],
        "cost": float(r["cost"]) if r["cost"] is not None else None,
        "date": r["date"].isoformat() if r["date"] else None,
        "mileage": r["mileage"],
        "position": r["position"],
        "created_at": r["created_at"].isoformat(),
    }


# ── загрузка всего модуля разом ───────────────────────────────
@app.get("/api/garage/bootstrap")
async def garage_bootstrap():
    async with pool.acquire() as c:
        vehicles = await c.fetch(
            "SELECT * FROM garage.vehicles ORDER BY position, created_at"
        )
        services = await c.fetch(
            "SELECT * FROM garage.services ORDER BY date DESC, created_at DESC"
        )
    return {
        "vehicles": [to_vehicle(v) for v in vehicles],
        "services": [to_service(s) for s in services],
    }


# ── ТС ────────────────────────────────────────────────────────
@app.post("/api/garage/vehicles", status_code=201)
async def create_vehicle(v: VehicleIn):
    _check_vtype(v.type)
    async with pool.acquire() as c:
        pos = await c.fetchval(
            "SELECT COALESCE(max(position)+1, 0) FROM garage.vehicles"
        )
        r = await c.fetchrow(
            "INSERT INTO garage.vehicles (name, type, year, vin, labels, position) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            v.name, v.type, v.year, v.vin, v.labels, pos,
        )
    return to_vehicle(r)


@app.patch("/api/garage/vehicles/{vehicle_id}")
async def update_vehicle(vehicle_id: uuid.UUID, v: VehiclePatch):
    _check_vtype(v.type)
    fields = {k: val for k, val in v.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    cols = list(fields.keys())
    set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(cols))
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE garage.vehicles SET {set_clause} WHERE id = $1 RETURNING *",
            vehicle_id, *[fields[col] for col in cols],
        )
    if not r:
        raise HTTPException(404, "ТС не найдено")
    return to_vehicle(r)


@app.delete("/api/garage/vehicles/{vehicle_id}", status_code=204)
async def delete_vehicle(vehicle_id: uuid.UUID):
    # удалять можно только архивные ТС (services — каскадом)
    async with pool.acquire() as c:
        archived = await c.fetchval(
            "SELECT archived FROM garage.vehicles WHERE id = $1", vehicle_id
        )
        if archived is None:
            raise HTTPException(404, "ТС не найдено")
        if not archived:
            raise HTTPException(409, "Сначала отправьте ТС в архив")
        await c.execute("DELETE FROM garage.vehicles WHERE id = $1", vehicle_id)


# ── записи обслуживания ───────────────────────────────────────
@app.post("/api/garage/services", status_code=201)
async def create_service(s: ServiceIn):
    async with pool.acquire() as c:
        exists = await c.fetchval(
            "SELECT 1 FROM garage.vehicles WHERE id = $1", s.vehicle_id
        )
        if not exists:
            raise HTTPException(404, "ТС не найдено")
        pos = await c.fetchval(
            "SELECT COALESCE(max(position)+1, 0) FROM garage.services WHERE vehicle_id = $1",
            s.vehicle_id,
        )
        r = await c.fetchrow(
            "INSERT INTO garage.services (vehicle_id, name, cost, date, mileage, position) "
            "VALUES ($1, $2, $3, COALESCE($4, current_date), $5, $6) RETURNING *",
            s.vehicle_id, s.name, s.cost, s.date, s.mileage, pos,
        )
    return to_service(r)


@app.patch("/api/garage/services/{service_id}")
async def update_service(service_id: uuid.UUID, s: ServicePatch):
    fields = {k: val for k, val in s.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    cols = list(fields.keys())
    set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(cols))
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE garage.services SET {set_clause} WHERE id = $1 RETURNING *",
            service_id, *[fields[col] for col in cols],
        )
    if not r:
        raise HTTPException(404, "Запись не найдена")
    return to_service(r)


@app.delete("/api/garage/services/{service_id}", status_code=204)
async def delete_service(service_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM garage.services WHERE id = $1", service_id)
    if res.endswith("0"):
        raise HTTPException(404, "Запись не найдена")


# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
#  EVENTS API (схема evt: types · events)
#  Оповещалка: типы (срок оповещения) → события. Год необязателен.
#  Дата валидируется и здесь, и CHECK-ом в БД (31.02 невозможен).
# ══════════════════════════════════════════════════════════════
_DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]   # 29.02 допускаем


def _check_event_dm(day, month):
    if month is None or day is None:
        return
    if not (1 <= month <= 12):
        raise HTTPException(400, "Месяц должен быть 1–12")
    if not (1 <= day <= _DAYS_IN_MONTH[month - 1]):
        raise HTTPException(400, f"Некорректный день для месяца {month:02d}")


_EVT_COLORS = {"blue", "green", "yellow", "red", "pink"}
_BURST_MAX = 10                                    # потолок повторов в серии
_TIME_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def _norm_color(c):
    return c if c in _EVT_COLORS else "blue"


def _norm_time(t):
    """'HH:MM' или None (пусто). Неверный формат → 400."""
    if not t:
        return None
    t = t.strip()
    if not re.match(_TIME_RE, t):
        raise HTTPException(400, "Время должно быть в формате ЧЧ:ММ")
    return t


def _norm_weekdays(weekdays):
    """Чистим: 0–6, уникальные, сортировка. Пусто/None → None."""
    if not weekdays:
        return None
    clean = sorted({d for d in weekdays if isinstance(d, int) and 0 <= d <= 6})
    return clean or None


def _clamp(v, lo, hi, default):
    if v is None:
        return default
    return max(lo, min(hi, v))


class EventIn(BaseModel):
    name: str
    recur: str = "yearly"
    color: str = "blue"
    weekdays: list[int] | None = None
    at_time: str | None = None
    burst_count: int = 1
    burst_interval_min: int = 0
    day: int | None = None
    month: int | None = None
    year: int | None = None
    lead_days: int = 0
    lead_daily: bool = False
    lead_time: str = "12:00"


class EventPatch(BaseModel):
    name: str | None = None
    recur: str | None = None
    color: str | None = None
    weekdays: list[int] | None = None
    at_time: str | None = None
    burst_count: int | None = None
    burst_interval_min: int | None = None
    day: int | None = None
    month: int | None = None
    year: int | None = None
    lead_days: int | None = None
    lead_daily: bool | None = None
    lead_time: str | None = None
    acked_key: str | None = None


class TemplateIn(BaseModel):
    title: str
    recur: str = "yearly"
    color: str = "blue"
    weekdays: list[int] | None = None
    at_time: str | None = None
    burst_count: int = 1
    burst_interval_min: int = 0
    lead_days: int = 0
    lead_daily: bool = False
    lead_time: str = "12:00"


def to_event(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "recur": r["recur"],
        "color": r["color"],
        "weekdays": list(r["weekdays"]) if r["weekdays"] is not None else None,
        "at_time": r["at_time"],
        "burst_count": r["burst_count"],
        "burst_interval_min": r["burst_interval_min"],
        "day": r["day"],
        "month": r["month"],
        "year": r["year"],
        "lead_days": r["lead_days"],
        "lead_daily": r["lead_daily"],
        "lead_time": r["lead_time"],
        "acked_key": r["acked_key"],
    }


def to_template(r) -> dict:
    return {
        "id": str(r["id"]),
        "title": r["title"],
        "recur": r["recur"],
        "color": r["color"],
        "weekdays": list(r["weekdays"]) if r["weekdays"] is not None else None,
        "at_time": r["at_time"],
        "burst_count": r["burst_count"],
        "burst_interval_min": r["burst_interval_min"],
        "lead_days": r["lead_days"],
        "lead_daily": r["lead_daily"],
        "lead_time": r["lead_time"],
        "position": r["position"],
    }


# ── загрузка всего модуля разом ───────────────────────────────
@app.get("/api/events/bootstrap")
async def events_bootstrap():
    async with pool.acquire() as c:
        events = await c.fetch("SELECT * FROM evt.events ORDER BY month, day, name")
        templates = await c.fetch("SELECT * FROM evt.templates ORDER BY position, created_at")
    return {
        "events": [to_event(e) for e in events],
        "templates": [to_template(t) for t in templates],
    }


# ── типы событий ──────────────────────────────────────────────
def _tpl_fields(recur, color, weekdays, at_time, burst_count,
                burst_interval_min, lead_days, lead_daily, lead_time):
    """Нормализует общие поля конфигурации под выбранный режим."""
    if recur not in ("yearly", "daily"):
        raise HTTPException(400, "Неизвестная повторяемость")
    out = {
        "recur": recur,
        "color": _norm_color(color),
        "at_time": _norm_time(at_time),
        "burst_count": _clamp(burst_count, 1, _BURST_MAX, 1),
        "burst_interval_min": _clamp(burst_interval_min, 0, 1440, 0),
    }
    if recur == "daily":
        out["weekdays"] = _norm_weekdays(weekdays)
        out["lead_days"] = 0
        out["lead_daily"] = False
        out["lead_time"] = "12:00"
    else:  # yearly
        out["weekdays"] = None
        out["lead_days"] = _clamp(lead_days, 0, 365, 0)
        out["lead_daily"] = bool(lead_daily)
        out["lead_time"] = _norm_time(lead_time) or "12:00"
    return out


@app.post("/api/event-templates", status_code=201)
async def create_template(t: TemplateIn):
    title = t.title.strip()
    if not title:
        raise HTTPException(400, "Пустое название шаблона")
    f = _tpl_fields(t.recur, t.color, t.weekdays, t.at_time, t.burst_count,
                    t.burst_interval_min, t.lead_days, t.lead_daily, t.lead_time)
    async with pool.acquire() as c:
        pos = await c.fetchval("SELECT COALESCE(max(position)+1, 0) FROM evt.templates")
        r = await c.fetchrow(
            "INSERT INTO evt.templates "
            "(title, recur, color, weekdays, at_time, burst_count, burst_interval_min, "
            " lead_days, lead_daily, lead_time, position) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *",
            title, f["recur"], f["color"], f["weekdays"], f["at_time"], f["burst_count"],
            f["burst_interval_min"], f["lead_days"], f["lead_daily"], f["lead_time"], pos,
        )
    return to_template(r)


@app.delete("/api/event-templates/{template_id}", status_code=204)
async def delete_template(template_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM evt.templates WHERE id = $1", template_id)
    if res.endswith("0"):
        raise HTTPException(404, "Шаблон не найден")


# ── события ───────────────────────────────────────────────────
@app.post("/api/events", status_code=201)
async def create_event(e: EventIn):
    name = e.name.strip()
    if not name:
        raise HTTPException(400, "Пустое имя/примечание")
    f = _tpl_fields(e.recur, e.color, e.weekdays, e.at_time, e.burst_count,
                    e.burst_interval_min, e.lead_days, e.lead_daily, e.lead_time)
    if e.recur == "yearly":
        if e.day is None or e.month is None:
            raise HTTPException(400, "Для «раз в год» нужна дата")
        _check_event_dm(e.day, e.month)
        day, month = e.day, e.month
        year = e.year if (e.year and 1900 <= e.year <= 2100) else None
    else:
        day = month = year = None
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "INSERT INTO evt.events "
            "(name, recur, color, weekdays, at_time, burst_count, burst_interval_min, "
            " day, month, year, lead_days, lead_daily, lead_time) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *",
            name, f["recur"], f["color"], f["weekdays"], f["at_time"], f["burst_count"],
            f["burst_interval_min"], day, month, year, f["lead_days"], f["lead_daily"], f["lead_time"],
        )
    return to_event(r)


@app.patch("/api/events/{event_id}")
async def update_event_row(event_id: uuid.UUID, e: EventPatch):
    fields = {k: v for k, v in e.model_dump(exclude_unset=True).items()}
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    if "recur" in fields and fields["recur"] not in ("yearly", "daily"):
        raise HTTPException(400, "Неизвестная повторяемость")
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise HTTPException(400, "Пустое имя/примечание")
    if "color" in fields:
        fields["color"] = _norm_color(fields["color"])
    if "at_time" in fields:
        fields["at_time"] = _norm_time(fields["at_time"])
    if "lead_time" in fields:
        fields["lead_time"] = _norm_time(fields["lead_time"]) or "12:00"
    if "weekdays" in fields:
        fields["weekdays"] = _norm_weekdays(fields["weekdays"])
    if "burst_count" in fields:
        fields["burst_count"] = _clamp(fields["burst_count"], 1, _BURST_MAX, 1)
    if "burst_interval_min" in fields:
        fields["burst_interval_min"] = _clamp(fields["burst_interval_min"], 0, 1440, 0)
    if "lead_days" in fields:
        fields["lead_days"] = _clamp(fields["lead_days"], 0, 365, 0)
    if "day" in fields or "month" in fields:
        async with pool.acquire() as c:
            cur = await c.fetchrow("SELECT day, month FROM evt.events WHERE id = $1", event_id)
        if not cur:
            raise HTTPException(404, "Событие не найдено")
        _check_event_dm(fields.get("day", cur["day"]), fields.get("month", cur["month"]))
    cols = list(fields.keys())
    set_clause = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(cols))
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE evt.events SET {set_clause} WHERE id = $1 RETURNING *",
            event_id, *[fields[col] for col in cols],
        )
    if not r:
        raise HTTPException(404, "Событие не найдено")
    return to_event(r)


@app.delete("/api/events/{event_id}", status_code=204)
async def delete_event_row(event_id: uuid.UUID):
    async with pool.acquire() as c:
        res = await c.execute("DELETE FROM evt.events WHERE id = $1", event_id)
    if res.endswith("0"):
        raise HTTPException(404, "Событие не найдено")


# ── отдаём SPA ────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(PORTAL_HTML)
