# Личный портал

Личный портал — веб-приложение для боковой панели Vivaldi и Telegram Mini App.  
Живёт на Core, порт 7000.

---

## Архитектура

```
Vivaldi sidebar / Telegram Mini App
        │
        ▼
   nginx (HTTPS, порт 8443/8445)
        │
        ▼
   FastAPI :7000  ←→  PostgreSQL 18 (локально)
   /data/portal/
     portal.html      ← весь фронтенд, один файл (~306KB)
     sw.js            ← Service Worker (браузерные уведомления)
     backend/
       main.py        ← API + бот + авторизация + планировщик
       *_schema.sql   ← схемы БД, накатываются при старте
       google_auth_migration.py  ← парсер Google Authenticator export
```

**Фронтенд** — SPA на чистом HTML/CSS/JS без фреймворков. Tabler Icons. Тёмная тема. Ширина ~380px под sidebar.

**Бэкенд** — FastAPI (Python 3.12). Запускается как systemd сервис. При старте автоматически накатывает SQL схемы (идемпотентно).

**Планировщик** — APScheduler внутри FastAPI процесса. Единая джоба `notify_tick` каждую минуту — обработка всех событий, автоочистка, Telegram оповещения.

**БД** — PostgreSQL 18, база `portal`, пользователь `portal`. Каждый модуль — отдельная схема.

---

## Модули

| Вкладка | Иконка | Схема БД | Статус | Описание |
|---------|--------|----------|--------|----------|
| Todo | ✅ | `todo` | В БД | Списки задач трёх типов: покупки, дела, разовые |
| Финансы | 💰 | `finance` | В БД | Кредиты и платежи по месяцам |
| События | 🎂 | `evt` | В БД | Yearly/daily события с Telegram оповещениями |
| Календарь | 📅 | `cal` | Заготовка | Планируется CalDAV (DavMail/Outlook) |
| Гараж | 🚗 | `garage` | В БД | История ТО транспортных средств |
| Заметки | 📝 | `notes` | В БД | Заметки с проектами, тегами, кодом, поиском |
| 2FA коды | 🛡 | `auth2fa` | В БД | TOTP коды (импорт из Google Authenticator) |
| Настройки | ⚙️ | `app` | В БД | Telegram токен, owner ID, Chat ID и др. |

---

## Схемы БД

| Файл | Схема | Основные таблицы |
|------|-------|-----------------|
| `todo_schema.sql` | `todo` | `lists`, `items`, `tags` |
| `notes_schema.sql` | `notes` | `projects`, `tags`, `notes`, `note_tags`, `quick` |
| `finance_schema.sql` | `finance` | `credits`, `entries` |
| `garage_schema.sql` | `garage` | `vehicles`, `services` |
| `events_schema.sql` | `evt` | `events`, `sent_log` |
| `cal_schema.sql` | `cal` | `accounts`, `calendars`, `events` |
| `auth2fa_schema.sql` | `auth2fa` | `accounts` |
| `app_schema.sql` | `app` | `settings`, `sessions` |

**Правила написания новых схем:**
- `CREATE TABLE IF NOT EXISTS` — всегда
- `CREATE INDEX IF NOT EXISTS` — всегда
- `CREATE OR REPLACE FUNCTION/TRIGGER` — для триггеров
- Новые колонки: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
- В конце: `ALTER SCHEMA ... OWNER TO portal` и `ALTER TABLE ... OWNER TO portal`
- Сидинг дефолтных строк: только `INSERT ... WHERE NOT EXISTS`

---

## Типы Todo списков

| Тип | Логика |
|-----|--------|
| `shopping` | Отмеченные строки исчезают через `auto_clear_min` минут. Пустой список тоже удаляется. |
| `tasks` | Строки не исчезают. Список можно отправить в архив вручную. |
| `once` | Разовое оповещение в Telegram в `remind_at`. После отработки → архив. В полночь следующего дня → удаление. |

---

## Планировщик (notify_tick)

Единая APScheduler джоба каждую минуту. Всё в одном месте:

- **Shopping автоочистка** — удаляет отмеченные items по таймеру, пустые списки
- **Once архивирование** — при `reminded=true` → `archived=true`
- **Once удаление** — в полночь удаляет archived once старше текущих суток
- **Events yearly** — прогрев за N дней + день события (burst серия)
- **Events daily** — по weekdays в at_time (burst серия)
- **Events дедупликация** — через `evt.sent_log` с `INSERT ON CONFLICT DO NOTHING`

---

## Service Worker (sw.js)

Браузерные уведомления о событиях.

**Два поллера работают вместе:**
- Страница (`portal.html`) — основной поллер, надёжный пока открыта панель
- SW (`sw.js`) — best-effort фоновый опрос (браузер усыпляет idle-SW ~через 30 сек)

**Особенности:**
- `tag: 'evt:<id>'` — одно уведомление на событие, повтор заменяет а не дублирует
- `renotify: true` — каждый опрос пере-оповещает (звук) пока не подтвердят
- `requireInteraction: true` — уведомление не исчезает пока не кликнут (Chromium/Vivaldi)
- Клик по уведомлению → POST `/api/events/{id}/ack` + фокус портала
- Текст обезличен (без имени события) для приватности на общем/залоченном экране
- SW сообщает странице `unacked_count` для обновления tab badge без открытия портала

**Деплой sw.js:**
```bash
scp sw.js shadmin@192.168.10.254:/data/portal/sw.js
```

---

## 2FA коды (Google Authenticator)

Импорт из Google Authenticator через QR код экспорта.

**Формат экспорта:** `otpauth-migration://offline?data=...` (protobuf, парсится `google_auth_migration.py` без внешних зависимостей).

**Импорт:**
1. Google Authenticator → ⋮ → Передача аккаунтов → Экспорт
2. Скриншот QR кода
3. В портале → вкладка 🛡 → [+] → Импорт из Google Authenticator → загрузить скриншот
4. Выбрать аккаунты → Импортировать

**Важно:** `secret` никогда не возвращается в API ответах — только вычисленный `code`.

---

## Авторизация

Портал защищён двухуровневой системой.

### Уровень 1 — Tailscale (доверенная сеть)

Запросы из `100.64.0.0/10` пропускаются без проверки.

**Важно:** реальный IP читается из `X-Real-IP` (nginx передаёт `$remote_addr`). Заголовок доверяется только если peer — приватная сеть (Docker/loopback). Прямой запрос с поддельным `X-Real-IP: 100.x` не пройдёт — код читает peer напрямую.

### Уровень 2 — Telegram Mini App

Проверяется:
1. Подпись HMAC-SHA256 через Bot Token
2. `user_id` из initData == `telegram_owner_id` из `app.settings`

Если не пройдено → `401 Unauthorized`.

### Настройки авторизации в БД (`app.settings`)

| Ключ | Описание |
|------|----------|
| `telegram_bot_token` | Токен бота `@ken_sho_portal_bot` |
| `telegram_owner_id` | Твой Telegram user ID |
| `telegram_chat_id` | Chat ID для оповещений |
| `telegram_thread_id` | Thread ID (опционально) |
| `telegram_webhook_secret` | Секрет webhook (генерируется автоматически) |
| `timezone` | Часовой пояс (default `Europe/Moscow`) |
| `birthday_reminder_days` | За сколько дней слать алерт о ДР |

---

## Telegram бот (@ken_sho_portal_bot)

| Команда | Ответ |
|---------|-------|
| `/start` | Приветствие + кнопка открытия портала |
| `/portal` | Кнопка открытия портала |

**Mini App:** `t.me/ken_sho_portal_bot/portal`

**Webhook:** `https://core.tail751bc9.ts.net/api/bot/webhook`  
Регистрируется автоматически при старте если задан токен.

Принудительная перерегистрация:
```bash
curl -X POST http://100.69.214.120:7000/api/bot/webhook/register
```

Проверка у Telegram:
```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

---

## Запуск и управление

```bash
systemctl status portal      # статус
systemctl restart portal     # после обновления файлов
systemctl stop portal        # остановка
journalctl -u portal -f      # логи в реальном времени
journalctl -u portal -n 50   # последние 50 строк
```

### Конфигурация (`/etc/systemd/system/portal.service`)

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
```

> Пароль прописан через `Environment=` (не `EnvironmentFile`) — символ `@` в пароле ломает парсинг EnvironmentFile.

---

## Деплой обновлений

```bash
# С ноутбука
scp potral.zip portal.html sw.js shadmin@192.168.10.254:/data/portal/

# На Core
systemctl stop portal
cd /data/portal && unzip -o potral.zip
# При изменении requirements.txt:
source backend/.venv/bin/activate && pip install -r backend/requirements.txt
systemctl start portal
journalctl -u portal -n 10 --no-pager
```

### Проверка после деплоя

```bash
curl http://localhost:7000/api/health
curl http://100.69.214.120:7000/api/todo/bootstrap   # через Tailscale
curl http://localhost:7000/api/todo/bootstrap          # без Tailscale → 401
```

---

## Доступ к порталу

| Способ | URL | Когда |
|--------|-----|-------|
| Vivaldi sidebar (Tailscale) | `http://100.69.214.120:7000` | Дома, порт открыт напрямую |
| Vivaldi sidebar (HTTPS+SW) | `https://core.tail751bc9.ts.net:8445/` | Для Service Worker (требует HTTPS) |
| Telegram Mini App | `t.me/ken_sho_portal_bot/portal` | С телефона вне Tailscale |

> **Service Worker** работает только по HTTPS. Для браузерных уведомлений открывай портал через `https://core.tail751bc9.ts.net:8445/` и разреши уведомления в настройках сайта.

---

## API — полный список эндпоинтов

### Служебные
```
GET  /api/health
GET  /api/auth/check        → { ok, source: "tailscale"|"telegram" }
GET  /sw.js                 → Service Worker файл
POST /api/bot/webhook       → Telegram webhook (без авторизации, защищён secret)
POST /api/bot/webhook/register
```

### Настройки
```
GET  /api/settings
POST /api/settings          { key, value }
POST /api/settings/telegram/test
```

### Todo
```
GET  /api/todo/bootstrap    → { tags, lists, items }
GET  /api/todo/archive      → архивные lists
POST /api/todo/tags / PATCH /{id} / DELETE /{id}
POST /api/todo/lists / PATCH /{id} / DELETE /{id}
POST /api/todo/lists/{id}/send-tg   → отправить список покупок в Telegram
POST /api/todo/items / PATCH /{id} / DELETE /{id}
```

### Заметки
```
GET  /api/notes/bootstrap   → { projects, tags, notes }
POST /api/notes/projects / PATCH /{id} / DELETE /{id}
POST /api/notes/tags / PATCH /{id} / DELETE /{id}
POST /api/notes/notes / PATCH /{id} / DELETE /{id}
POST /api/notes/quick / PATCH /{id} / DELETE /{id}
```

### Финансы
```
GET  /api/finance/bootstrap
GET  /api/finance/month/{month}
GET  /api/finance/dk/{month}
POST /api/finance/credits / PATCH /{id} / DELETE /{id}
POST /api/finance/entries / PATCH /{id} / DELETE /{id}
```

### Гараж
```
GET  /api/garage/bootstrap  → { vehicles, services }
POST /api/garage/vehicles / PATCH /{id} / DELETE /{id}
POST /api/garage/services / PATCH /{id} / DELETE /{id}
```

### События
```
GET  /api/events/bootstrap  → { events, templates }
GET  /api/events/unacked    → неподтверждённые события (для SW)
POST /api/events / PATCH /{id} / DELETE /{id}
POST /api/events/{id}/ack   → подтвердить событие
POST /api/event-templates / DELETE /{id}
```

### 2FA
```
GET  /api/2fa/accounts      → список с текущими кодами (secret не возвращается)
POST /api/2fa/accounts / PATCH /{id} / DELETE /{id}
POST /api/2fa/import/qr     → загрузить скриншот QR → превью аккаунтов
POST /api/2fa/import/confirm → сохранить выбранные аккаунты
```

---

## Первоначальная настройка (после DR)

1. Открыть портал через Tailscale: `http://100.69.214.120:7000`
2. ⚙️ Настройки → Telegram Bot Token → сохранить
3. Ввести Owner ID (узнать через `@userinfobot`)
4. Ввести Chat ID
5. «Отправить тест» → проверить что сообщение пришло
6. «Переподключить webhook» → бот начнёт принимать команды

---

## Планируется

- Календарь → CalDAV синхронизация через DavMail (Outlook)
- Дни рождения → перенос в `evt` схему (сейчас localStorage)
- DR реплика → дамп `portal` БД по расписанию на VPS
- Telegram бот `/start` → статус системы (активные задачи, платежи)
- Алерты → просроченные задачи, ближайшие события календаря