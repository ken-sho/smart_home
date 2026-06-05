# Личный портал

Личный портал — веб-приложение для боковой панели Vivaldi и Telegram Mini App.  
Живёт на Core, порт 7000.

---

## Архитектура

```
Vivaldi sidebar / Telegram Mini App
        │
        ▼
   nginx (HTTPS)
        │
        ▼
   FastAPI :7000  ←→  PostgreSQL 18 (локально)
   /data/portal/
     portal.html      ← весь фронтенд, один файл (~190KB)
     backend/
       main.py        ← API + бот + авторизация
       *_schema.sql   ← схемы БД, накатываются при старте
```

**Фронтенд** — SPA на чистом HTML/CSS/JS без фреймворков. Tabler Icons. Тёмная тема. Ширина ~380px под sidebar.

**Бэкенд** — FastAPI (Python 3.12). Запускается как systemd сервис. При старте автоматически накатывает SQL схемы (идемпотентно).

**БД** — PostgreSQL 18, база `portal`, пользователь `portal`. Каждый модуль — отдельная схема.

---

## Модули

| Вкладка | Схема БД | Статус | Описание |
|---------|----------|--------|----------|
| ✅ Todo | `todo` | В БД | Задачи с тегами, дедлайнами, фильтрацией |
| 💰 Финансы | `finance` | В БД | Кредиты и платежи по месяцам |
| 📝 Заметки | `notes` | В БД | Заметки с проектами, тегами, поиском, кодом |
| 🚗 Гараж | `garage` | В БД | История ТО транспортных средств |
| 🎂 Дни рождения | — | localStorage | Планируется перенос в БД |
| 📅 Календарь | — | Заглушка | Планируется DavMail (CalDAV) |
| ⚙️ Настройки | `app` | В БД | Telegram токен, owner ID, Chat ID |

---

## Авторизация

Портал защищён двухуровневой системой — без авторизации данные недоступны.

### Уровень 1 — Tailscale (доверенная сеть)

Запросы из подсети `100.64.0.0/10` пропускаются без проверки. Это доступ с домашнего компьютера, ноутбука — всего что в Tailscale сети.

### Уровень 2 — Telegram Mini App

Запросы снаружи должны содержать заголовок `X-Telegram-Init-Data` с подписанными данными от Telegram. Бэкенд проверяет:

1. Подпись HMAC-SHA256 через Bot Token — что запрос действительно от Telegram
2. `user_id` из initData — что это именно владелец (сравнивается с `telegram_owner_id` из БД)

Если проверка не пройдена — `401 Unauthorized`.

### Защита от подделки IP

Реальный IP читается из заголовка `X-Real-IP` (который ставит nginx из `$remote_addr`). Заголовок доверяется только если сокет-peer — доверенный прокси (Docker/loopback). Прямой публичный запрос с поддельным `X-Real-IP: 100.64.0.1` не пройдёт.

---

## Telegram бот (@ken_sho_portal_bot)

Бот — точка входа с телефона.

| Команда | Ответ |
|---------|-------|
| `/start` | Приветствие + кнопка Mini App |
| `/portal` | Кнопка Mini App |

Mini App ссылка: `t.me/ken_sho_portal_bot/portal`

Webhook регистрируется автоматически при старте сервиса. Адрес: `https://core.tail751bc9.ts.net/api/bot/webhook`.

Принудительная перерегистрация:
```bash
curl -X POST http://100.69.214.120:7000/api/bot/webhook/register
```

Проверка webhook у Telegram:
```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

---

## API эндпоинты

### Служебные
```
GET  /api/health                    → { status: "ok" }
GET  /api/auth/check                → { ok: true, source: "tailscale"|"telegram" }
POST /api/bot/webhook               → обработка апдейтов Telegram
POST /api/bot/webhook/register      → переподключение webhook
```

### Настройки
```
GET  /api/settings                  → { key: value, ... }
POST /api/settings                  { key, value }
POST /api/settings/telegram/test    → отправляет тестовое сообщение
```

### Todo
```
GET    /api/todo/tasks              → список задач
POST   /api/todo/tasks              { text, deadline?, tag? }
PATCH  /api/todo/tasks/{id}         { text?, done?, deadline?, tag? }
DELETE /api/todo/tasks/{id}
POST   /api/todo/tasks/clear-done   → удалить все выполненные
```

### Заметки
```
GET    /api/notes/bootstrap         → { projects, tags, notes }
POST   /api/notes/projects          { name, color? }
PATCH  /api/notes/projects/{id}     { name?, color?, position? }
DELETE /api/notes/projects/{id}
POST   /api/notes/tags              { project_id, name, color? }
PATCH  /api/notes/tags/{id}
DELETE /api/notes/tags/{id}
POST   /api/notes/notes             { project_id, title?, body?, type?, tags? }
PATCH  /api/notes/notes/{id}
DELETE /api/notes/notes/{id}
```

### Финансы
```
GET    /api/finance/bootstrap       → { month, credits, overdue, months }
GET    /api/finance/month/{month}   → данные за конкретный месяц
POST   /api/finance/credits         { month, name, amount, due_day? }
PATCH  /api/finance/credits/{id}    { name?, amount?, due_day?, paid?, position? }
DELETE /api/finance/credits/{id}
POST   /api/finance/entries         { credit_id, amount, note? }
PATCH  /api/finance/entries/{id}
DELETE /api/finance/entries/{id}
```

### Гараж
```
GET    /api/garage/bootstrap        → { vehicles, services }
POST   /api/garage/vehicles         { name, type }
PATCH  /api/garage/vehicles/{id}    { name?, type?, archived? }
DELETE /api/garage/vehicles/{id}
POST   /api/garage/services         { vehicle_id, name?, date?, mileage?, cost? }
PATCH  /api/garage/services/{id}
DELETE /api/garage/services/{id}
```

---

## Схемы БД

Каждый модуль — отдельная схема в базе `portal`. Схемы накатываются автоматически при старте (`lifespan`), идемпотентно (`IF NOT EXISTS`).

| Файл | Схема | Основные таблицы |
|------|-------|-----------------|
| `todo_schema.sql` | `todo` | `tasks` |
| `notes_schema.sql` | `notes` | `projects`, `tags`, `notes`, `note_tags` |
| `finance_schema.sql` | `finance` | `credits`, `entries` |
| `garage_schema.sql` | `garage` | `vehicles`, `services` |
| `app_schema.sql` | `app` | `settings` |

Правила написания новых схем:
- `CREATE TABLE IF NOT EXISTS`
- `CREATE INDEX IF NOT EXISTS`
- `CREATE OR REPLACE FUNCTION/TRIGGER`
- В конце: `ALTER SCHEMA ... OWNER TO portal` и `ALTER TABLE ... OWNER TO portal`

---

## Запуск и управление

### Systemd сервис

```bash
systemctl status portal      # статус
systemctl restart portal     # перезапуск (после обновления main.py)
systemctl stop portal        # остановка
journalctl -u portal -f      # логи в реальном времени
journalctl -u portal -n 50   # последние 50 строк
```

### Конфигурация сервиса (`/etc/systemd/system/portal.service`)

```ini
[Unit]
Description=Personal Portal
After=network.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/data/portal/backend
Environment=DB_HOST=localhost
Environment=DB_PORT=5432
Environment=DB_NAME=portal
Environment=DB_USER=portal
Environment=DB_PASS=<пароль>
Environment=PORTAL_HTML=/data/portal/portal.html
ExecStart=/data/portal/backend/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 7000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> Пароль БД прописан напрямую через `Environment=` (не через `EnvironmentFile`) из-за символа `@` в пароле — systemd некорректно парсит `@` в EnvironmentFile.

### Деплой обновлений

```bash
# Скопировать новый архив на Core
scp potral.zip shadmin@192.168.10.254:/data/portal/

# На Core
systemctl stop portal
cd /data/portal && unzip -o potral.zip
systemctl start portal
journalctl -u portal -n 10 --no-pager
```

### Проверка после деплоя

```bash
curl http://localhost:7000/api/health
curl http://100.69.214.120:7000/api/todo/tasks   # через Tailscale — без авторизации
curl http://localhost:7000/api/todo/tasks          # без Tailscale — 401
```

---

## Доступ к порталу

| Способ | URL | Когда использовать |
|--------|-----|-------------------|
| Vivaldi sidebar | `http://100.69.214.120:7000` | Дома, в Tailscale сети |
| Telegram Mini App | `t.me/ken_sho_portal_bot/portal` | С телефона, вне Tailscale |
| Прямой URL | `https://core.tail751bc9.ts.net/portal/` | Только с Tailscale (иначе 401) |

---

## Первоначальная настройка (после DR)

После восстановления сервера нужно настроить Telegram через UI портала:

1. Открыть портал через Tailscale: `http://100.69.214.120:7000`
2. Перейти в ⚙️ Настройки
3. Ввести Bot Token — токен бота `@ken_sho_portal_bot`
4. Ввести Owner ID — твой Telegram user ID (узнать через `@userinfobot`)
5. Ввести Chat ID — ID личного чата с ботом
6. Нажать «Отправить тест» — должно прийти сообщение
7. Нажать «Переподключить webhook» — бот начнёт принимать команды

---

## Планируется

- Дни рождения → перенос в PostgreSQL + Telegram алерты
- Календарь → синхронизация через DavMail/CalDAV
- Todo → новая версия вкладки
- Telegram бот `/start` → статус системы (активные задачи, платежи)
- Алерты: просроченные задачи, дни рождения, события календаря
