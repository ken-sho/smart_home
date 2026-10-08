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
--  Этап 2: советы.
--    plans   — план по торговой части монеты: T1, T2, стоп. Сколько
--              третей уже продано, считается по продажам с created_at.
--    levels  — уровни поддержки/сопротивления (для алертов и советов).
--    params  — переопределения порогов движка (дефолты — в crypto.py).
--    signals — лог смены советов (одна строка на смену статуса).
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

-- стратегия монеты (настройки карточки):
--   strategy      swing — свинг у поддержки; hold — без сигналов на покупку, продажи по плану;
--                 dca — накопление: напоминание купить dca_amount каждого dca_day-го
--   check_hours   как часто проверять вход (1/4/6/12/24 ч, от 12:00 МСК); продажи — по закрытию дня
--   cooldown_days пауза между повторами «Покупать X» в Telegram (0 — без паузы)
ALTER TABLE crypto.assets ADD COLUMN IF NOT EXISTS strategy      text          NOT NULL DEFAULT 'swing';
ALTER TABLE crypto.assets ADD COLUMN IF NOT EXISTS check_hours   smallint      NOT NULL DEFAULT 24;
ALTER TABLE crypto.assets ADD COLUMN IF NOT EXISTS cooldown_days smallint      NOT NULL DEFAULT 3;
ALTER TABLE crypto.assets ADD COLUMN IF NOT EXISTS dca_amount    numeric(14,2) NOT NULL DEFAULT 0;
ALTER TABLE crypto.assets ADD COLUMN IF NOT EXISTS dca_day       smallint      NOT NULL DEFAULT 25;
--   quiet_from/quiet_to — тихие часы по МСК (0–23): сообщения по монете в это время
--   откладываются до конца окна (NULL — без тишины). Окно может переходить через полночь.
ALTER TABLE crypto.assets ADD COLUMN IF NOT EXISTS quiet_from    smallint;
ALTER TABLE crypto.assets ADD COLUMN IF NOT EXISTS quiet_to      smallint;
--   move_alert_pct — алерт на резкое движение: цена отошла от максимума/минимума
--   последних 24 ч на столько % (0 — выключено). Информационный, не совет.
ALTER TABLE crypto.assets ADD COLUMN IF NOT EXISTS move_alert_pct numeric(5,2) NOT NULL DEFAULT 0;

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

-- вывод в фиат → приход в Д/К (finance.entries): сколько пришло рублей,
-- рыночный курс USDT/RUB на момент вывода, ссылка на строку Д/К
ALTER TABLE crypto.cash ADD COLUMN IF NOT EXISTS rub          numeric(14,2);
ALTER TABLE crypto.cash ADD COLUMN IF NOT EXISTS rate         numeric(12,4);
ALTER TABLE crypto.cash ADD COLUMN IF NOT EXISTS fin_entry_id uuid
    REFERENCES finance.entries(id) ON DELETE SET NULL;

-- ── План по монете ────────────────────────────────────────────
--   created_at — старт плана: продажи после него считаются третями
CREATE TABLE IF NOT EXISTS crypto.plans (
    symbol      text          PRIMARY KEY REFERENCES crypto.assets(symbol) ON DELETE CASCADE,
    t1          numeric(20,8) CHECK (t1 > 0),
    t2          numeric(20,8) CHECK (t2 > 0),
    stop        numeric(20,8) CHECK (stop > 0),
    created_at  timestamptz   NOT NULL DEFAULT now(),
    updated_at  timestamptz   NOT NULL DEFAULT now()
);

INSERT INTO crypto.plans (symbol, t1, t2, stop)
SELECT 'TRX', 0.35, 0.3773, 0.322
WHERE NOT EXISTS (SELECT 1 FROM crypto.plans WHERE symbol = 'TRX');

-- ── Уровни ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crypto.levels (
    symbol  text          NOT NULL REFERENCES crypto.assets(symbol) ON DELETE CASCADE,
    price   numeric(20,8) NOT NULL CHECK (price > 0),
    PRIMARY KEY (symbol, price)
);

-- стартовая сетка (конец сентября 2026) — только если по монете уровней ещё нет
INSERT INTO crypto.levels (symbol, price)
SELECT 'GRAM', p FROM unnest(ARRAY[2.00, 1.80, 1.5854, 1.4084]::numeric[]) AS p
WHERE NOT EXISTS (SELECT 1 FROM crypto.levels WHERE symbol = 'GRAM');

INSERT INTO crypto.levels (symbol, price)
SELECT 'TRX', p FROM unnest(ARRAY[0.3773, 0.35, 0.3359, 0.322, 0.30846]::numeric[]) AS p
WHERE NOT EXISTS (SELECT 1 FROM crypto.levels WHERE symbol = 'TRX');

-- ── Параметры движка (переопределения дефолтов из crypto.py) ──
CREATE TABLE IF NOT EXISTS crypto.params (
    key     text     PRIMARY KEY,
    value   numeric  NOT NULL
);

-- ── Лог советов ───────────────────────────────────────────────
--   symbol: GRAM / TRX — продажа по позиции, USDT — покупка на свободные
--   key — что считается «сменой совета» (статус [+ монета покупки])
CREATE TABLE IF NOT EXISTS crypto.signals (
    id       bigserial    PRIMARY KEY,
    ts       timestamptz  NOT NULL DEFAULT now(),
    symbol   text         NOT NULL,
    key      text         NOT NULL,
    status   text         NOT NULL,
    title    text         NOT NULL,
    reasons  jsonb        NOT NULL DEFAULT '[]'::jsonb,
    sent     boolean      NOT NULL DEFAULT false     -- отправлено в Telegram (этап 3)
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol ON crypto.signals (symbol, ts DESC);

-- служебные данные сигнала (зона входа у «Покупать» — чтобы понять, что её пробили вниз)
ALTER TABLE crypto.signals ADD COLUMN IF NOT EXISTS data jsonb;

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
ALTER TABLE    crypto.plans           OWNER TO portal;
ALTER TABLE    crypto.levels          OWNER TO portal;
ALTER TABLE    crypto.params          OWNER TO portal;
ALTER TABLE    crypto.signals         OWNER TO portal;   -- serial-последовательность переходит вместе с таблицей
ALTER FUNCTION crypto.trg_touch()     OWNER TO portal;
