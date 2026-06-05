-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «Заметки»
--  PostgreSQL 18 · схема notes (одна схема на вкладку)
--  Соответствует структуре данных фронтенда (localStorage → API)
-- ════════════════════════════════════════════════════════════
--
--  Архитектура: каждая вкладка портала = отдельная схема
--  (todo, finance, birthdays, calendar, garage, notes).
--  Это даёт независимые миграции и частичный ввод в эксплуатацию:
--  модуль можно дорабатывать, не затрагивая остальные.
--  Cross-schema внешних ключей между модулями НЕТ.
--  Сквозные вещи (уведомления Telegram, настройки) — в схеме app.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS notes;

-- ── Проекты ───────────────────────────────────────────────────
-- JS: state.projects = [{ id, name, color, position, created_at }]
CREATE TABLE IF NOT EXISTS notes.projects (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text        NOT NULL,
    color       text        NOT NULL DEFAULT '#52b788',
    position    int         NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ── Хэштеги (индивидуальны для каждого проекта) ───────────────
-- JS: state.tags = [{ id, project_id, name, color, position, created_at }]
-- color = ключ палитры: green | blue | yellow | red | purple | gray
CREATE TABLE IF NOT EXISTS notes.tags (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid        NOT NULL REFERENCES notes.projects(id) ON DELETE CASCADE,
    name        text        NOT NULL,
    color       text        NOT NULL DEFAULT 'green',
    position    int         NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tags_unique_name_per_project UNIQUE (project_id, name)
);

CREATE INDEX IF NOT EXISTS idx_tags_project ON notes.tags (project_id, position);

-- ── Заметки (текст / код) ─────────────────────────────────────
-- JS: state.notes = [{ id, project_id, title, type, language, body,
--                      tags:[id], favorite, rich, position,
--                      created_at, updated_at }]
--
--  type='code' → body хранит исходный код (plain), language обязателен.
--  type='text' → body хранит HTML из визуального редактора, rich=true.
CREATE TABLE IF NOT EXISTS notes.notes (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid        NOT NULL REFERENCES notes.projects(id) ON DELETE CASCADE,
    title       text        NOT NULL DEFAULT '',
    type        text        NOT NULL DEFAULT 'text'
                            CHECK (type IN ('text', 'code')),
    language    text,                       -- только для type='code': sql, pgsql, python…
    body        text        NOT NULL DEFAULT '',
    rich        boolean     NOT NULL DEFAULT true,   -- body это HTML (true) или plain-код (false)
    favorite    boolean     NOT NULL DEFAULT false,  -- ★ избранное
    position    int         NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- лента по проекту (хронология) — created_at, т.к. порядок не дёргается при правке
CREATE INDEX IF NOT EXISTS idx_notes_project_created ON notes.notes (project_id, created_at);
-- быстрый фильтр избранного по проекту
CREATE INDEX IF NOT EXISTS idx_notes_project_fav ON notes.notes (project_id, favorite) WHERE favorite;
-- полнотекстовый поиск (заголовок + тело)
CREATE INDEX IF NOT EXISTS idx_notes_search ON notes.notes
    USING gin (to_tsvector('russian', title || ' ' || body));

-- ── Связь заметка ↔ тег (многие-ко-многим) ───────────────────
-- На фронте: notes[i].tags = [tag_id, …]
CREATE TABLE IF NOT EXISTS notes.note_tags (
    note_id  uuid NOT NULL REFERENCES notes.notes(id) ON DELETE CASCADE,
    tag_id   uuid NOT NULL REFERENCES notes.tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (note_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON notes.note_tags (tag_id);

-- ── Кнопки быстрого доступа (секреты для копирования) ─────────
-- JS: state.quick = [{ id, project_id, name, value, position, created_at }]
-- Индивидуальны для каждого проекта. Значение НЕ отображается на
-- фронте — только копируется в буфер по клику (безопасно при
-- демонстрации экрана). Посмотреть/изменить можно через управление.
CREATE TABLE IF NOT EXISTS notes.quick_secrets (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid        NOT NULL REFERENCES notes.projects(id) ON DELETE CASCADE,
    name        text        NOT NULL,
    value       text        NOT NULL DEFAULT '',
    position    int         NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_quick_project ON notes.quick_secrets (project_id, position);

-- ── Авто-обновление updated_at ────────────────────────────────
CREATE OR REPLACE FUNCTION notes.trg_set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER notes_set_updated_at
    BEFORE UPDATE ON notes.notes
    FOR EACH ROW EXECUTE FUNCTION notes.trg_set_updated_at();

-- ════════════════════════════════════════════════════════════
--  Заметки по интеграции
-- ════════════════════════════════════════════════════════════
-- 1. tags на фронте лежат массивом id внутри заметки. API при чтении
--    отдаёт notes с полем tags:[...] (агрегат из note_tags), на запись
--    принимает тот же массив и пересобирает junction-таблицу.
--    Менять структуру фронта не нужно.
-- 2. id на фронте — строки (Date+random). В БД uuid; маппинг делаем
--    при первом импорте из localStorage.
-- 3. position зарезервирован под ручную сортировку (drag&drop) — на
--    фронте пока не используется, поле уже есть.
-- 4. created_at = «дата» заметки в ленте (разделители по дням берут
--    date_trunc('day', created_at)).
