-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «Календарь»
--  PostgreSQL · схема cal (одна схема на вкладку)
-- ════════════════════════════════════════════════════════════
--
--  Календарь — ВИТРИНА (только просмотр). Сам ничего не шлёт и своих
--  событий не создаёт — собирает в одном месте уже запланированное:
--    • события вкладки «События» (evt) — фронт берёт из их state;
--    • разовые Todo (once, remind_at)   — фронт берёт из их state;
--    • внешние календари (Google; позже Outlook) — ЭТА схема.
--
--  Зачем БД для внешних календарей: храним подключение (OAuth) и КЭШ
--  развёрнутых экземпляров событий, чтобы сетка месяца рисовалась
--  мгновенно и работала при недоступности внешнего API. Синк —
--  инкрементальный (Google nextSyncToken). Доступ ТОЛЬКО ЧТЕНИЕ —
--  создание/правка событий идёт во вкладке «События», не здесь.
--
--  ⚠️  Токены (access/refresh) — СЕКРЕТЫ. Эндпоинты подключения/синка —
--      только из доверенной сети (см. app_schema.sql / middleware),
--      наружу через Funnel выставлять нельзя (кроме самого OAuth-callback,
--      защищённого параметром state). По возможности шифровать на уровне БД.
--
--  Идемпотентно: накатывается при каждом старте бэкенда.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS cal;

-- ── Подключённые аккаунты (OAuth) ─────────────────────────────
-- JS (без токенов наружу!): { id, provider, email, status, last_error,
--   last_sync_at, created_at }
CREATE TABLE IF NOT EXISTS cal.accounts (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider      text        NOT NULL DEFAULT 'google' CHECK (provider IN ('google','outlook')),
    email         text,
    -- OAuth — секреты, наружу никогда не отдаём
    access_token  text,
    refresh_token text,
    token_expiry  timestamptz,
    scopes        text,
    status        text        NOT NULL DEFAULT 'active' CHECK (status IN ('active','error','revoked')),
    last_error    text,
    last_sync_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- одно подключение на (провайдер, аккаунт)
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_provider_email_uniq'
    ) THEN
        ALTER TABLE cal.accounts
            ADD CONSTRAINT accounts_provider_email_uniq UNIQUE (provider, email);
    END IF;
END $$;

-- ── Календари аккаунта ────────────────────────────────────────
-- У одного аккаунта их несколько (личный/рабочий/праздники…).
-- enabled — показывать ли в портале (галка на вкладке «Календарь»).
-- sync_token — состояние инкрементального синка этого календаря.
-- JS: { id, account_id, external_id, name, color, enabled, last_sync_at }
CREATE TABLE IF NOT EXISTS cal.calendars (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   uuid        NOT NULL REFERENCES cal.accounts(id) ON DELETE CASCADE,
    external_id  text        NOT NULL,
    name         text,
    color        text,                              -- hex провайдера или маппинг на палитру портала
    enabled      boolean     NOT NULL DEFAULT true,
    sync_token   text,                              -- Google nextSyncToken / Outlook deltaLink
    last_sync_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'calendars_account_external_uniq'
    ) THEN
        ALTER TABLE cal.calendars
            ADD CONSTRAINT calendars_account_external_uniq UNIQUE (account_id, external_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cal_calendars_account ON cal.calendars (account_id);

-- ── Кэш событий внешних календарей (read-only зеркало) ─────────
-- Храним УЖЕ РАЗВЁРНУТЫЕ экземпляры (Google singleEvents=true /
-- Outlook calendarView) — не разворачиваем RRULE сами.
-- event_date — дата для раскладки в сетке (в локальной TZ);
-- starts_at/ends_at — точное время (NULL у all_day).
-- JS: { id, calendar_id, title, location, all_day, event_date,
--       starts_at, ends_at, status, html_link }
CREATE TABLE IF NOT EXISTS cal.events (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    calendar_id   uuid        NOT NULL REFERENCES cal.calendars(id) ON DELETE CASCADE,
    external_id   text        NOT NULL,             -- id экземпляра у провайдера
    title         text,
    location      text,
    all_day       boolean     NOT NULL DEFAULT false,
    event_date    date        NOT NULL,             -- день для сетки (локальная TZ)
    starts_at     timestamptz,
    ends_at       timestamptz,
    status        text        NOT NULL DEFAULT 'confirmed'
                              CHECK (status IN ('confirmed','tentative','cancelled')),
    html_link     text,                             -- ссылка обратно в Google
    provider_updated_at timestamptz,
    synced_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT events_time_ok CHECK (all_day OR starts_at IS NOT NULL)
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cal_events_calendar_external_uniq'
    ) THEN
        ALTER TABLE cal.events
            ADD CONSTRAINT cal_events_calendar_external_uniq UNIQUE (calendar_id, external_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cal_events_cal_date ON cal.events (calendar_id, event_date);
CREATE INDEX IF NOT EXISTS idx_cal_events_date     ON cal.events (event_date);

-- ── updated_at автоматика ─────────────────────────────────────
CREATE OR REPLACE FUNCTION cal.trg_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER accounts_touch
    BEFORE UPDATE ON cal.accounts
    FOR EACH ROW EXECUTE FUNCTION cal.trg_touch();

CREATE OR REPLACE TRIGGER calendars_touch
    BEFORE UPDATE ON cal.calendars
    FOR EACH ROW EXECUTE FUNCTION cal.trg_touch();

-- ════════════════════════════════════════════════════════════
--  Права: всё принадлежит пользователю portal
-- ════════════════════════════════════════════════════════════
ALTER SCHEMA   cal                  OWNER TO portal;
ALTER TABLE    cal.accounts         OWNER TO portal;
ALTER TABLE    cal.calendars        OWNER TO portal;
ALTER TABLE    cal.events           OWNER TO portal;
ALTER FUNCTION cal.trg_touch()      OWNER TO portal;

-- ════════════════════════════════════════════════════════════
--  Заметки по интеграции (поток синка Google — read-only)
-- ════════════════════════════════════════════════════════════
-- 1. OAuth: redirect_uri = {PORTAL_PUBLIC_URL}/api/cal/google/callback,
--    scope calendar.readonly. Refresh-токен → cal.accounts.
-- 2. Список календарей: GET calendarList → upsert в cal.calendars.
-- 3. Синк событий по каждому enabled-календарю:
--    GET events?singleEvents=true&timeMin=…&syncToken=… → upsert/удаление
--    в cal.events; nextSyncToken → cal.calendars.sync_token.
--    410 Gone (токен протух) → полный ресинк окна (timeMin/timeMax).
-- 4. Частота: по таймеру (как планировщик событий) и/или вручную кнопкой.
-- 5. Удаление аккаунта → каскад сносит календари и кэш событий.
-- 6. Календарь НИКОГДА не пишет во внешний календарь и не шлёт оповещения —
--    только читает и показывает. Алерты остаются за вкладкой «События».
