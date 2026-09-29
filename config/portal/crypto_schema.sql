-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «Крипто»
--  PostgreSQL 18 · схема crypto (одна схема на вкладку)
-- ════════════════════════════════════════════════════════════
--
--  Этап 0: справочник монет + история цен.
--    assets  — отслеживаемые монеты: id в CoinGecko, ставка Earn,
--              инфляция предложения, ядро (шт), комиссия+спред.
--    prices  — сырые точки цены/объёма из CoinGecko (почасовые,
--              для старой истории — дневные). Свечи и индикаторы
--              считаются из них в crypto.py.
--
--  Этап 1: портфель.
--    trades  — журнал по монетам: покупка / продажа / начисление Earn
--              (сверка «+») / списание (сверка «−»). Позиция, средний
--              вход и P&L считаются из журнала в crypto.py.
--    cash    — движения USDT (подписанная сумма): стартовый остаток,
--              зарплата, вывод в фиат, сделка, сверка. Строки сделок
--              создаются вместе со сделкой и удаляются каскадом.
--
--  Идемпотентно: накатывается при каждом старте бэкенда.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS crypto;

-- ── Монеты ────────────────────────────────────────────────────
--   cg_id = NULL → цена не собирается (USDT считаем равным $1)
CREATE TABLE IF NOT EXISTS crypto.assets (
    symbol      text          PRIMARY KEY,
    name        text          NOT NULL DEFAULT '',
    cg_id       text,                                  -- id в CoinGecko
    earn_apr    numeric(6,3)  NOT NULL DEFAULT 0,      -- % годовых в Earn
    inflation   numeric(6,3)  NOT NULL DEFAULT 0,      -- % годовых эмиссии
    core_qty    numeric(20,8) NOT NULL DEFAULT 0 CHECK (core_qty >= 0),  -- ядро, шт
    fee_pct     numeric(6,3)  NOT NULL DEFAULT 1,      -- комиссия+спред на сделку, %
    position    int           NOT NULL DEFAULT 0,
    updated_at  timestamptz   NOT NULL DEFAULT now()
);

INSERT INTO crypto.assets (symbol, name, cg_id, earn_apr, inflation, position)
SELECT 'GRAM', 'Gram (бывш. Toncoin)', 'the-open-network', 12.27, 8.3, 1
WHERE NOT EXISTS (SELECT 1 FROM crypto.assets WHERE symbol = 'GRAM');

INSERT INTO crypto.assets (symbol, name, cg_id, earn_apr, inflation, position)
SELECT 'TRX', 'TRON', 'tron', 3.33, 1.5, 2
WHERE NOT EXISTS (SELECT 1 FROM crypto.assets WHERE symbol = 'TRX');

INSERT INTO crypto.assets (symbol, name, cg_id, earn_apr, inflation, fee_pct, position)
SELECT 'USDT', 'Доллары', NULL, 4.65, 0, 0, 3
WHERE NOT EXISTS (SELECT 1 FROM crypto.assets WHERE symbol = 'USDT');

-- ── История цен ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto.prices (
    symbol  text              NOT NULL REFERENCES crypto.assets(symbol) ON DELETE CASCADE,
    ts      timestamptz       NOT NULL,
    price   double precision  NOT NULL,
    volume  double precision,                          -- объём за 24ч, $
    PRIMARY KEY (symbol, ts)
);

-- ── Журнал сделок по монетам ──────────────────────────────────
--   side:  buy / sell       — сделка в Wallet (total = USDT списано/получено,
--                             price = total / qty, спред уже внутри)
--          earn / writeoff  — сверка с кошельком: начисление Earn (+) или
--                             расхождение (−); USDT не двигают, цена 0
--   external = true — покупка вне кассы (куплено давно): USDT не списывается
CREATE TABLE IF NOT EXISTS crypto.trades (
    id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol      text          NOT NULL REFERENCES crypto.assets(symbol),
    side        text          NOT NULL CHECK (side IN ('buy', 'sell', 'earn', 'writeoff')),
    qty         numeric(20,8) NOT NULL CHECK (qty > 0),
    price       numeric(20,8) NOT NULL DEFAULT 0 CHECK (price >= 0),
    total       numeric(14,2) NOT NULL DEFAULT 0 CHECK (total >= 0),
    external    boolean       NOT NULL DEFAULT false,
    date        date          NOT NULL DEFAULT current_date,
    reason      text          NOT NULL DEFAULT '',
    created_at  timestamptz   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON crypto.trades (symbol, date, created_at);

-- ── Касса USDT ────────────────────────────────────────────────
--   amount > 0 — приход, < 0 — расход
CREATE TABLE IF NOT EXISTS crypto.cash (
    id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text          NOT NULL
                              CHECK (kind IN ('deposit', 'salary', 'fiat_out', 'trade', 'reconcile')),
    amount      numeric(14,2) NOT NULL CHECK (amount <> 0),
    trade_id    uuid          REFERENCES crypto.trades(id) ON DELETE CASCADE,
    date        date          NOT NULL DEFAULT current_date,
    note        text          NOT NULL DEFAULT '',
    created_at  timestamptz   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cash_date ON crypto.cash (date, created_at);

-- ── updated_at автоматика ─────────────────────────────────────
CREATE OR REPLACE FUNCTION crypto.trg_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER assets_touch
    BEFORE UPDATE ON crypto.assets
    FOR EACH ROW EXECUTE FUNCTION crypto.trg_touch();

-- ════════════════════════════════════════════════════════════
--  Права: всё принадлежит пользователю portal
-- ════════════════════════════════════════════════════════════
ALTER SCHEMA   crypto                 OWNER TO portal;
ALTER TABLE    crypto.assets          OWNER TO portal;
ALTER TABLE    crypto.prices          OWNER TO portal;
ALTER TABLE    crypto.trades          OWNER TO portal;
ALTER TABLE    crypto.cash            OWNER TO portal;
ALTER FUNCTION crypto.trg_touch()     OWNER TO portal;
