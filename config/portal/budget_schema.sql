-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «Траты» (Финансы → Траты)
--  PostgreSQL 18 · схема budget
-- ════════════════════════════════════════════════════════════
--
--    statements  — загруженные справки банка (период, итоги, файл на диске).
--    ops         — операции из справок. Дубли при повторной загрузке
--                  отсекает UNIQUE (bank, op_at, doc, amount).
--    categories  — категории трат/доходов; kind = expense | income.
--    rules       — правила раскладки: подстрока в контрагенте/назначении
--                  или «kind:<вид операции>»; первое совпадение по priority.
--
--  op_at — местное время из справки (без часового пояса), месяц операции
--  считается по нему. Переводы между своими счетами и погашение кредита
--  (kind self/credit) хранятся, но в бюджет не входят (budget.py).
--
--  Идемпотентно: накатывается при каждом старте бэкенда.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS budget;

CREATE TABLE IF NOT EXISTS budget.statements (
    id           uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    bank         text          NOT NULL,
    period_from  date          NOT NULL,
    period_to    date          NOT NULL,
    opening      numeric(14,2),
    closing      numeric(14,2),
    total_in     numeric(14,2),
    total_out    numeric(14,2),
    file_path    text,
    ops_new      int           NOT NULL DEFAULT 0,
    ops_dup      int           NOT NULL DEFAULT 0,
    uploaded_at  timestamptz   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS budget.categories (
    id        serial   PRIMARY KEY,
    name      text     NOT NULL UNIQUE,
    kind      text     NOT NULL DEFAULT 'expense' CHECK (kind IN ('expense', 'income')),
    position  int      NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budget.rules (
    id           serial   PRIMARY KEY,
    category_id  int      NOT NULL REFERENCES budget.categories(id) ON DELETE CASCADE,
    pattern      text     NOT NULL,
    priority     int      NOT NULL DEFAULT 100,    -- меньше = раньше; правила пользователя — 10
    UNIQUE (pattern)
);

CREATE TABLE IF NOT EXISTS budget.ops (
    id               uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    bank             text          NOT NULL,
    op_at            timestamp     NOT NULL,
    doc              text          NOT NULL,
    amount           numeric(14,2) NOT NULL,       -- + зачисление, − списание
    description      text          NOT NULL DEFAULT '',
    kind             text          NOT NULL,
    counterparty     text          NOT NULL DEFAULT '',
    order_no         text,
    category_id      int           REFERENCES budget.categories(id) ON DELETE SET NULL,
    category_manual  boolean       NOT NULL DEFAULT false,   -- выбрано руками — правила не трогают
    statement_id     uuid          REFERENCES budget.statements(id) ON DELETE SET NULL,
    created_at       timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (bank, op_at, doc, amount)
);

CREATE INDEX IF NOT EXISTS idx_budget_ops_at ON budget.ops (op_at);

-- этап 2: заметка к операции и откуда взялась категория
--   category_source: manual | rule (магазин/получатель) | kind (по виду операции) | fallback (Прочее/Поступления)
ALTER TABLE budget.ops ADD COLUMN IF NOT EXISTS note            text NOT NULL DEFAULT '';
ALTER TABLE budget.ops ADD COLUMN IF NOT EXISTS category_source text NOT NULL DEFAULT 'rule';

-- разбиение операции по категориям (Лента 18 408 = 15 000 продукты + 3 408 дом).
-- amount — положительная часть суммы операции; сумма частей = |ops.amount|.
CREATE TABLE IF NOT EXISTS budget.splits (
    id           serial         PRIMARY KEY,
    op_id        uuid           NOT NULL REFERENCES budget.ops(id) ON DELETE CASCADE,
    category_id  int            NOT NULL REFERENCES budget.categories(id) ON DELETE CASCADE,
    amount       numeric(14,2)  NOT NULL CHECK (amount > 0)
);

CREATE INDEX IF NOT EXISTS idx_budget_splits_op ON budget.splits (op_id);

ALTER SCHEMA budget             OWNER TO portal;
ALTER TABLE  budget.statements  OWNER TO portal;
ALTER TABLE  budget.categories  OWNER TO portal;
ALTER TABLE  budget.rules       OWNER TO portal;
ALTER TABLE  budget.ops         OWNER TO portal;
ALTER TABLE  budget.splits      OWNER TO portal;
