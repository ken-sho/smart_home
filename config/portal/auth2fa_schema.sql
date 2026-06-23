-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «2FA» (TOTP коды)
--  PostgreSQL · схема auth2fa (одна схема на вкладку)
--  Аналог Google Authenticator внутри портала.
-- ════════════════════════════════════════════════════════════
--  Всё идемпотентно: файл накатывается при каждом старте.
--  Cross-schema FK к другим модулям НЕТ. Владелец — portal.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS auth2fa;

-- ── Списки (группы) аккаунтов ─────────────────────────────────
-- Ключи группируются по спискам; выбранный список фронт помнит в
-- localStorage (по браузеру). Cross-schema FK нет.
CREATE TABLE IF NOT EXISTS auth2fa.lists (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text        NOT NULL,
    position    int         NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ── Аккаунты (TOTP-секреты) ───────────────────────────────────
-- JS видит только { id, list_id, issuer, account, code, period, expires_in, pinned }.
-- secret НИКОГДА не уходит на фронт после сохранения — код считается на бэке.
CREATE TABLE IF NOT EXISTS auth2fa.accounts (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    issuer      text        NOT NULL,                 -- "Google", "Binance.com"…
    account     text,                                 -- email / username
    secret      text        NOT NULL,                 -- base32 TOTP-секрет
    algorithm   text        NOT NULL DEFAULT 'SHA1',  -- SHA1 / SHA256 / SHA512
    digits      smallint    NOT NULL DEFAULT 6,
    period      smallint    NOT NULL DEFAULT 30,      -- секунд на код
    pinned      boolean     NOT NULL DEFAULT false,
    position    int         NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- list_id добавляем идемпотентно (для уже существующей таблицы CREATE TABLE
-- IF NOT EXISTS колонку не добавит). FK — тоже идемпотентно.
ALTER TABLE auth2fa.accounts ADD COLUMN IF NOT EXISTS list_id uuid;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'accounts_list_fk'
    ) THEN
        ALTER TABLE auth2fa.accounts
            ADD CONSTRAINT accounts_list_fk FOREIGN KEY (list_id)
            REFERENCES auth2fa.lists(id) ON DELETE CASCADE;
    END IF;
END $$;

-- закреплённые сверху, дальше — ручной порядок
CREATE INDEX IF NOT EXISTS idx_2fa_pinned ON auth2fa.accounts (pinned DESC, position);
CREATE INDEX IF NOT EXISTS idx_2fa_list   ON auth2fa.accounts (list_id);

-- ── Сидинг: хотя бы один список + привязка «осиротевших» ключей ─
INSERT INTO auth2fa.lists (name, position)
    SELECT 'Основной', 0
    WHERE NOT EXISTS (SELECT 1 FROM auth2fa.lists);

UPDATE auth2fa.accounts
    SET list_id = (SELECT id FROM auth2fa.lists ORDER BY position, created_at LIMIT 1)
    WHERE list_id IS NULL;

-- ── Владелец всего модуля — portal ────────────────────────────
ALTER SCHEMA auth2fa            OWNER TO portal;
ALTER TABLE  auth2fa.lists      OWNER TO portal;
ALTER TABLE  auth2fa.accounts   OWNER TO portal;
