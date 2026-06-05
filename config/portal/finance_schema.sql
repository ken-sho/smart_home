-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «Финансы», режим «Кредиты»
--  PostgreSQL 18 · схема finance (одна схема на вкладку)
-- ════════════════════════════════════════════════════════════
--
--  Модель (плоская, как в Google-таблице пользователя):
--    строка = кредит { банк(name), сумма(amount), день(due_day), оплачено(paid) }
--    привязан к месяцу 'YYYY-MM'. День месяца — константа, при копировании
--    в новый месяц переносится как есть, paid сбрасывается.
--
--  finance.months — маркер «месяц инициализирован»: нужен, чтобы при
--  наступлении нового месяца один раз скопировать список с прошлого
--  и больше не перекопировать (даже если пользователь очистит список).
--
--  Идемпотентно: накатывается при каждом старте бэкенда.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS finance;

-- ── Маркер инициализированных месяцев ─────────────────────────
CREATE TABLE IF NOT EXISTS finance.months (
    month       char(7)     PRIMARY KEY,              -- 'YYYY-MM'
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT months_format CHECK (month ~ '^[0-9]{4}-[0-9]{2}$')
);

-- ── Кредиты (строки по месяцам) ───────────────────────────────
-- JS: state.finance.credits[i] = { id, month, name, amount, due_day, paid }
CREATE TABLE IF NOT EXISTS finance.credits (
    id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    month       char(7)       NOT NULL,               -- 'YYYY-MM'
    name        text          NOT NULL,               -- банк / кому
    amount      numeric(14,2) NOT NULL DEFAULT 0 CHECK (amount >= 0),
    due_day     smallint      CHECK (due_day BETWEEN 1 AND 31),  -- день платежа, может быть NULL
    paid        boolean       NOT NULL DEFAULT false,
    paid_at     timestamptz,                          -- когда отметили оплаченным
    position    int           NOT NULL DEFAULT 0,     -- порядок в месяце
    created_at  timestamptz   NOT NULL DEFAULT now(),
    updated_at  timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT credits_month_format CHECK (month ~ '^[0-9]{4}-[0-9]{2}$')
);

-- список кредитов месяца в порядке отображения
CREATE INDEX IF NOT EXISTS idx_credits_month  ON finance.credits (month, position);
-- быстрый подсчёт неоплаченного / просрочки по всем месяцам (бейдж)
CREATE INDEX IF NOT EXISTS idx_credits_unpaid ON finance.credits (month) WHERE NOT paid;

-- ── updated_at + paid_at автоматика ───────────────────────────
-- Работает и на INSERT (строка сразу оплачена), и на UPDATE (тоггл).
CREATE OR REPLACE FUNCTION finance.trg_credit_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    IF TG_OP = 'INSERT' THEN
        IF NEW.paid THEN
            NEW.paid_at = now();
        END IF;
    ELSE
        IF NEW.paid AND NOT OLD.paid THEN
            NEW.paid_at = now();
        ELSIF NOT NEW.paid AND OLD.paid THEN
            NEW.paid_at = NULL;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER credits_touch
    BEFORE INSERT OR UPDATE ON finance.credits
    FOR EACH ROW EXECUTE FUNCTION finance.trg_credit_touch();

-- ── Режим «Д/К» (дебет/кредит): приход/расход по месяцам ──────
-- JS: state.finance.entries[i] = { id, month, kind, name, amount, due_day }
--   kind = 'in' (приход) | 'out' (расход). Ничего не копируется между
--   месяцами. Оплаченные кредиты подтягиваются в расход на лету (во view),
--   здесь НЕ дублируются.
CREATE TABLE IF NOT EXISTS finance.entries (
    id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    month       char(7)       NOT NULL,               -- 'YYYY-MM'
    kind        text          NOT NULL CHECK (kind IN ('in', 'out')),
    name        text          NOT NULL DEFAULT '',    -- краткое название
    amount      numeric(14,2) NOT NULL DEFAULT 0 CHECK (amount >= 0),
    due_day     smallint      CHECK (due_day BETWEEN 1 AND 31),  -- число, может быть NULL
    position    int           NOT NULL DEFAULT 0,
    created_at  timestamptz   NOT NULL DEFAULT now(),
    updated_at  timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT entries_month_format CHECK (month ~ '^[0-9]{4}-[0-9]{2}$')
);

-- список приходов/расходов месяца в порядке отображения
CREATE INDEX IF NOT EXISTS idx_entries_month ON finance.entries (month, due_day, position);

CREATE OR REPLACE FUNCTION finance.trg_entry_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER entries_touch
    BEFORE UPDATE ON finance.entries
    FOR EACH ROW EXECUTE FUNCTION finance.trg_entry_touch();

-- ════════════════════════════════════════════════════════════
--  Права: всё принадлежит пользователю portal
-- ════════════════════════════════════════════════════════════
ALTER SCHEMA   finance                        OWNER TO portal;
ALTER TABLE    finance.months                 OWNER TO portal;
ALTER TABLE    finance.credits                OWNER TO portal;
ALTER TABLE    finance.entries                OWNER TO portal;
ALTER FUNCTION finance.trg_credit_touch()     OWNER TO portal;
ALTER FUNCTION finance.trg_entry_touch()      OWNER TO portal;

-- ════════════════════════════════════════════════════════════
--  Заметки по интеграции
-- ════════════════════════════════════════════════════════════
-- 1. month — 'YYYY-MM' из выбранного года+месяца на фронте, as-is.
-- 2. amount — число (parseFloat) → numeric(14,2); API отдаёт float.
-- 3. due_day — день месяца (1..31) или NULL. Просрочку считает бэкенд:
--    кредит просрочен, если NOT paid и эффективная дата (year-month-day,
--    с поправкой на короткие месяцы) < сегодня. Для NULL due_day берём
--    последний день месяца → прошлый месяц без даты тоже считается просроченным.
-- 4. Копирование при новом месяце делает бэкенд при первом обращении к
--    текущему месяцу: если его нет в finance.months — создаёт маркер и
--    копирует строки из самого свежего прошлого месяца (paid=false).
