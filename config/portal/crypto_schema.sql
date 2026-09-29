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
ALTER FUNCTION crypto.trg_touch()     OWNER TO portal;
