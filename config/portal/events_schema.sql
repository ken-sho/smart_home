-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «События» (оповещалка)
--  PostgreSQL 18 · схема evt (одна схема на вкладку)
-- ════════════════════════════════════════════════════════════
--
--  Единая модель: типы событий УБРАНЫ. Всё — в одном событии.
--  Два режима повторения:
--    recur='yearly'  — раз в год (день рождения, годовщина):
--        day/month (+year опц.); lead_days — за сколько дней начать
--        информировать; lead_daily — слать раз в сутки «прогрев»
--        до дня события; lead_time — во сколько прогрев (по умолч. 12:00);
--        в день события: at_time — со скольки + серия (burst_*).
--    recur='daily'   — по дням недели (weekdays; пусто = каждый день):
--        at_time — во сколько + серия (burst_*).
--  «Серия» (burst): сколько раз прислать оповещение в ТГ и через сколько
--    минут — burst_count × burst_interval_min (напр. 5 раз каждые 90 мин).
--  evt.templates — пресеты конфигурации: настроил оповещение (напр. для
--    дней рождения), сохранил в шаблон, дальше создаёшь события из шаблона,
--    меняя лишь имя/дату.
--
--  acked_key — строковый ключ последнего «снятого» наступления:
--    yearly → год наступления ('2026'); daily → 'YYYY-MM-DD' этой даты.
--    На следующее наступление оповещение возвращается (логика в приложении).
--
--  ⚠️  Реальная отправка в Telegram — отдельная фаза (планировщик).
--      Здесь только модель/форма; сервер пока хранит настройки.
--
--  Идемпотентно: накатывается при каждом старте бэкенда.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS evt;

-- ── Одноразовая деструктивная миграция со старой type-модели ───
-- Срабатывает ТОЛЬКО пока жива старая структура (evt.events.type_id).
-- После миграции колонки type_id нет → повторные старты это пропускают
-- и НЕ трогают новые данные. Старые данные ценности не имели (по ТЗ).
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'evt' AND table_name = 'events' AND column_name = 'type_id'
    ) THEN
        DROP TABLE IF EXISTS evt.events CASCADE;
        DROP TABLE IF EXISTS evt.types  CASCADE;
    END IF;
END $$;

-- ── Зачистка прежнего модуля «Дни рождения» (bday) ────────────
-- События когда-то заменили модуль bday, но его схему не дропали —
-- на проде она могла остаться мёртвым грузом. В коде/SCHEMA_FILES
-- bday больше нигде нет → удаляем. Идемпотентно (IF EXISTS = no-op).
DROP SCHEMA IF EXISTS bday CASCADE;

-- ── События ───────────────────────────────────────────────────
-- JS: {
--   id, name, recur, color, acked_key,
--   weekdays|null, monthdays|null, at_time|null,  // daily / monthly / день-события yearly
--   burst_count, burst_interval_min,              // серия (все ветки)
--   day|null, month|null, year|null,              // yearly
--   lead_days, lead_daily, lead_time              // yearly: прогрев
-- }
--  Три режима повторения:
--    yearly  — раз в год (day/month);
--    daily   — по дням недели (weekdays; пусто = каждый день);
--    monthly — раз в месяц по числам (monthdays 1–31; день > длины месяца
--              срабатывает в последний день месяца). Поля как у daily:
--              at_time + серия burst.
CREATE TABLE IF NOT EXISTS evt.events (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text        NOT NULL,
    recur              text        NOT NULL DEFAULT 'yearly' CHECK (recur IN ('yearly','daily','monthly')),
    color              text        NOT NULL DEFAULT 'blue',
    -- daily: дни недели (0=Пн…6=Вс); пусто/NULL = каждый день
    weekdays           smallint[],
    -- monthly: числа месяца (1…31); день > длины месяца → последний день
    monthdays          smallint[],
    -- время: daily — когда слать; yearly — со скольки в день события
    at_time            text,
    -- серия оповещений в ТГ: сколько раз × интервал в минутах
    burst_count        smallint    NOT NULL DEFAULT 1 CHECK (burst_count BETWEEN 1 AND 50),
    burst_interval_min smallint    NOT NULL DEFAULT 0 CHECK (burst_interval_min BETWEEN 0 AND 1440),
    -- yearly: дата (year опционален)
    day                smallint    CHECK (day   IS NULL OR day   BETWEEN 1 AND 31),
    month              smallint    CHECK (month IS NULL OR month BETWEEN 1 AND 12),
    year               smallint    CHECK (year  IS NULL OR year  BETWEEN 1900 AND 2100),
    -- yearly: предварительное информирование («прогрев»)
    lead_days          smallint    NOT NULL DEFAULT 0 CHECK (lead_days BETWEEN 0 AND 365),
    lead_daily         boolean     NOT NULL DEFAULT false,
    lead_time          text        NOT NULL DEFAULT '12:00',
    acked_key          text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    -- день не должен превышать число дней в месяце (29.02 разрешаем)
    CONSTRAINT events_valid_day CHECK (
        month IS NULL OR day IS NULL OR
        day <= CASE month
            WHEN 2 THEN 29
            WHEN 4 THEN 30 WHEN 6 THEN 30 WHEN 9 THEN 30 WHEN 11 THEN 30
            ELSE 31
        END
    ),
    -- набор полей под режим повторения
    CONSTRAINT events_recur_fields CHECK (
        (recur = 'yearly' AND day IS NOT NULL AND month IS NOT NULL) OR
        (recur = 'daily') OR
        (recur = 'monthly')
    ),
    -- форматы времени ЧЧ:ММ
    CONSTRAINT events_at_time_fmt CHECK (
        at_time IS NULL OR at_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
    ),
    CONSTRAINT events_lead_time_fmt CHECK (
        lead_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
    )
);

CREATE INDEX IF NOT EXISTS idx_events_md ON evt.events (month, day);

-- ── Миграция: режим 'monthly' + колонка monthdays ───────────
-- Идемпотентно подтягивает уже существующие таблицы (CREATE TABLE
-- IF NOT EXISTS не добавит новую колонку/не обновит CHECK на живой таблице).
ALTER TABLE evt.events    ADD COLUMN IF NOT EXISTS monthdays smallint[];
ALTER TABLE evt.templates ADD COLUMN IF NOT EXISTS monthdays smallint[];

-- recur: старый column-CHECK разрешал только yearly/daily — расширяем.
ALTER TABLE evt.events    DROP CONSTRAINT IF EXISTS events_recur_check;
ALTER TABLE evt.templates DROP CONSTRAINT IF EXISTS templates_recur_check;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'events_recur_ck') THEN
        ALTER TABLE evt.events ADD CONSTRAINT events_recur_ck
            CHECK (recur IN ('yearly','daily','monthly'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'templates_recur_ck') THEN
        ALTER TABLE evt.templates ADD CONSTRAINT templates_recur_ck
            CHECK (recur IN ('yearly','daily','monthly'));
    END IF;
END $$;

-- recur_fields: разрешаем monthly (без day/month).
ALTER TABLE evt.events DROP CONSTRAINT IF EXISTS events_recur_fields;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'events_recur_fields') THEN
        ALTER TABLE evt.events ADD CONSTRAINT events_recur_fields CHECK (
            (recur = 'yearly' AND day IS NOT NULL AND month IS NOT NULL) OR
            (recur = 'daily') OR
            (recur = 'monthly')
        );
    END IF;
END $$;

-- ── Шаблоны (пресеты конфигурации) ────────────────────────────
-- JS: { id, title, recur, color, weekdays|null, at_time|null,
--       burst_count, burst_interval_min, lead_days, lead_daily, lead_time, position }
CREATE TABLE IF NOT EXISTS evt.templates (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    title              text        NOT NULL,
    recur              text        NOT NULL DEFAULT 'yearly' CHECK (recur IN ('yearly','daily','monthly')),
    color              text        NOT NULL DEFAULT 'blue',
    weekdays           smallint[],
    monthdays          smallint[],
    at_time            text,
    burst_count        smallint    NOT NULL DEFAULT 1 CHECK (burst_count BETWEEN 1 AND 50),
    burst_interval_min smallint    NOT NULL DEFAULT 0 CHECK (burst_interval_min BETWEEN 0 AND 1440),
    lead_days          smallint    NOT NULL DEFAULT 0 CHECK (lead_days BETWEEN 0 AND 365),
    lead_daily         boolean     NOT NULL DEFAULT false,
    lead_time          text        NOT NULL DEFAULT '12:00',
    position           int         NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT templates_at_time_fmt CHECK (
        at_time IS NULL OR at_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
    ),
    CONSTRAINT templates_lead_time_fmt CHECK (
        lead_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
    )
);

-- ── Шаблоны по умолчанию (только если их ещё нет) ─────────────
-- Пример из ТЗ: «День рождения» — за 7 дней, ежедневный прогрев в 12:00,
-- в день события один пинг в 09:00. Пользователь меняет/удаляет.
INSERT INTO evt.templates
    (title, recur, color, at_time, burst_count, burst_interval_min, lead_days, lead_daily, lead_time, position)
SELECT * FROM (VALUES
    ('День рождения',     'yearly', 'pink',   '09:00', 1::smallint, 0::smallint,   7::smallint, true,  '12:00', 0),
    ('Годовщина',         'yearly', 'yellow', '12:00', 1::smallint, 0::smallint,   1::smallint, false, '12:00', 1)
) AS v(title, recur, color, at_time, burst_count, burst_interval_min, lead_days, lead_daily, lead_time, position)
WHERE NOT EXISTS (SELECT 1 FROM evt.templates);

-- ── updated_at автоматика ─────────────────────────────────────
CREATE OR REPLACE FUNCTION evt.trg_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER events_touch
    BEFORE UPDATE ON evt.events
    FOR EACH ROW EXECUTE FUNCTION evt.trg_touch();

-- ── Журнал отправленных оповещений (дедуп в рамках серии burst) ─
-- Планировщик бьётся раз в минуту. Чтобы не слать дважды (перезапуск,
-- наложение тиков) — каждая отправка фиксируется по уникальному
-- dedup_key. Примеры ключей:
--   warm:2026-03-15:2026-03-08  — прогрев yearly (день напоминания)
--   day:2026-03-15:0           — слот серии в день события yearly
--   daily:2026-03-15:0         — слот серии для recur=daily
-- При удалении события лог сносится каскадно.
CREATE TABLE IF NOT EXISTS evt.sent_log (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id   uuid        NOT NULL REFERENCES evt.events(id) ON DELETE CASCADE,
    dedup_key  text        NOT NULL,
    sent_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_sent_log_event ON evt.sent_log (event_id);
CREATE INDEX IF NOT EXISTS idx_sent_log_time  ON evt.sent_log (sent_at);

-- ════════════════════════════════════════════════════════════
--  Права: всё принадлежит пользователю portal
-- ════════════════════════════════════════════════════════════
ALTER SCHEMA   evt                OWNER TO portal;
ALTER TABLE    evt.events         OWNER TO portal;
ALTER TABLE    evt.templates      OWNER TO portal;
ALTER TABLE    evt.sent_log       OWNER TO portal;
ALTER FUNCTION evt.trg_touch()    OWNER TO portal;

-- ════════════════════════════════════════════════════════════
--  Заметки по интеграции
-- ════════════════════════════════════════════════════════════
-- 1. lead_* живут на самом событии (типов больше нет).
-- 2. year NULL = год неизвестен; возраст/годовщина в UI не показывается.
-- 3. Дата валидируется CHECK-ом в БД и в API (31.02 невозможно).
-- 4. Шаблон — снимок конфигурации; события на него НЕ ссылаются
--    (удаление шаблона ничего не ломает) — значения копируются в форму.
-- 5. Серия (burst) одна для обеих веток: daily — в at_time; yearly — в день
--    события c at_time. Прогрев (lead_daily) — раз в сутки в lead_time.
