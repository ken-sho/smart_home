-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «Todo» (списки/задачи/покупки)
--  PostgreSQL · схема todo
-- ════════════════════════════════════════════════════════════
--
--  Концепция: лента списков с разделителями по дате создания.
--  Три типа списков:
--    shopping — список покупок; строки автоочищаются через
--               auto_clear_min минут после отметки (done_at + интервал);
--               когда автоочистка убрала последнюю строку — список удаляется.
--    tasks    — обычный чеклист; можно архивировать (archived=true).
--    once     — одноразовое напоминание в Telegram в remind_at;
--               после отправки reminded=true.
--  Теги — для фильтрации ленты (один тег на список).
--
--  ⚠️  Деструктивная миграция: старая таблица todo.tasks больше не нужна
--      (данных нет) — дропаем. Идемпотентно: накатывается при каждом старте.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS todo;

-- ── Снос старой модели (одной таблицы задач) ──────────────────
-- В коде/SCHEMA_FILES она больше не используется. IF EXISTS = no-op.
DROP TABLE IF EXISTS todo.tasks CASCADE;

-- ── Теги ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS todo.tags (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text        NOT NULL UNIQUE,
    color      text        NOT NULL DEFAULT 'green',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ── Списки ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS todo.lists (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text        NOT NULL,
    type           text        NOT NULL CHECK (type IN ('shopping', 'tasks', 'once')),
    tag_id         uuid        REFERENCES todo.tags(id) ON DELETE SET NULL,
    -- shopping: задержка очистки в минутах после отметки строки
    auto_clear_min smallint    CHECK (auto_clear_min IS NULL OR auto_clear_min > 0),
    -- once: время оповещения
    remind_at      timestamptz,
    reminded       boolean     NOT NULL DEFAULT false,
    -- tasks: архив
    archived       boolean     NOT NULL DEFAULT false,
    archived_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- ── Строки списка ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS todo.items (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    list_id    uuid        NOT NULL REFERENCES todo.lists(id) ON DELETE CASCADE,
    text       text        NOT NULL,
    done       boolean     NOT NULL DEFAULT false,
    done_at    timestamptz,                       -- момент отметки (shopping: старт таймера очистки)
    position   int         NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_items_list ON todo.items (list_id, position);
CREATE INDEX IF NOT EXISTS idx_lists_type ON todo.lists (type, archived, created_at);

-- ── updated_at автоматика для списков ─────────────────────────
CREATE OR REPLACE FUNCTION todo.trg_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER lists_touch
    BEFORE UPDATE ON todo.lists
    FOR EACH ROW EXECUTE FUNCTION todo.trg_touch();

-- ── Права: всё принадлежит пользователю portal ────────────────
ALTER SCHEMA   todo                OWNER TO portal;
ALTER TABLE    todo.tags           OWNER TO portal;
ALTER TABLE    todo.lists          OWNER TO portal;
ALTER TABLE    todo.items          OWNER TO portal;
ALTER FUNCTION todo.trg_touch()    OWNER TO portal;
