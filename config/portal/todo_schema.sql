-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «Задачи» (Todo)
--  PostgreSQL 18 · схема todo
--  Идемпотентно: можно выполнять при каждом старте бэкенда.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS todo;

-- ── Задачи ────────────────────────────────────────────────────
-- JS: state.tasks = [{ id, text, done, deadline, tag, created_at }]
CREATE TABLE IF NOT EXISTS todo.tasks (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    text          text        NOT NULL,
    done          boolean     NOT NULL DEFAULT false,
    deadline      date,                       -- дедлайн (может быть NULL)
    tag           text,                       -- одиночный тег (может быть NULL)
    position      int         NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz                 -- когда отметили выполненной
);

-- активные/просроченные сортируются по дедлайну
CREATE INDEX IF NOT EXISTS idx_tasks_done     ON todo.tasks (done, deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_tag      ON todo.tasks (tag) WHERE tag IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON todo.tasks (deadline) WHERE deadline IS NOT NULL;

-- ── updated_at + completed_at автоматика ──────────────────────
CREATE OR REPLACE FUNCTION todo.trg_task_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    -- проставляем/снимаем дату выполнения при смене флага done
    IF NEW.done AND NOT OLD.done THEN
        NEW.completed_at = now();
    ELSIF NOT NEW.done AND OLD.done THEN
        NEW.completed_at = NULL;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tasks_touch ON todo.tasks;
CREATE TRIGGER tasks_touch
    BEFORE UPDATE ON todo.tasks
    FOR EACH ROW EXECUTE FUNCTION todo.trg_task_touch();

-- ════════════════════════════════════════════════════════════
--  Заметки по интеграции
-- ════════════════════════════════════════════════════════════
-- 1. Фронт хранит deadline строкой 'YYYY-MM-DD' — кладётся в date as-is.
-- 2. tag сейчас одиночный (как в текущем UI). Если позже захотим
--    несколько тегов — выносим в todo.task_tags по образцу notes.
-- 3. completed_at пригодится для будущей статистики/уведомлений,
--    UI его пока не использует.
