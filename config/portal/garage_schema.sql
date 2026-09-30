-- ════════════════════════════════════════════════════════════
--  Личный портал — модуль «Гараж»
--  PostgreSQL 18 · схема garage (одна схема на вкладку)
-- ════════════════════════════════════════════════════════════
--
--  Модель: ТС → обслуживание (master-detail).
--    vehicles  — карточка ТС: название, тип, год выпуска, VIN,
--                произвольные метки (labels jsonb), флаг архива.
--    services  — записи обслуживания внутри ТС: название, сумма,
--                дата, пробег. Удаляются каскадом вместе с ТС.
--
--  Архив: удалить ТС можно ТОЛЬКО когда archived = true (логика в API).
--
--  Идемпотентно: накатывается при каждом старте бэкенда.
-- ════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS garage;

-- ── Транспортные средства ─────────────────────────────────────
-- JS: state.vehicles[i] = { id, name, type, year, vin, labels, archived }
--   type   = 'car' | 'moto' | 'quad' | 'other'
--   labels = [{ name, value }] — произвольные метки (давление, бензин…)
CREATE TABLE IF NOT EXISTS garage.vehicles (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text        NOT NULL,
    type        text        NOT NULL DEFAULT 'car'
                            CHECK (type IN ('car', 'moto', 'quad', 'other')),
    year        smallint    CHECK (year BETWEEN 1900 AND 2200),  -- год выпуска, может быть NULL
    vin         text,                                            -- VIN, может быть NULL
    labels      jsonb       NOT NULL DEFAULT '[]'::jsonb,        -- [{name, value}]
    archived    boolean     NOT NULL DEFAULT false,
    position    int         NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- список ТС: сначала активные, затем архив; в порядке отображения
CREATE INDEX IF NOT EXISTS idx_vehicles_archived ON garage.vehicles (archived, position);

-- ── Записи обслуживания ───────────────────────────────────────
-- JS: state.services[i] = { id, vehicle_id, name, cost, date, mileage, items }
CREATE TABLE IF NOT EXISTS garage.services (
    id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id  uuid          NOT NULL REFERENCES garage.vehicles(id) ON DELETE CASCADE,
    name        text          NOT NULL DEFAULT '',    -- рукописное название работ
    cost        numeric(12,2) CHECK (cost >= 0),      -- сумма, может быть NULL
    date        date          NOT NULL DEFAULT current_date,
    mileage     int           CHECK (mileage >= 0),   -- пробег, может быть NULL
    position    int           NOT NULL DEFAULT 0,
    created_at  timestamptz   NOT NULL DEFAULT now(),
    updated_at  timestamptz   NOT NULL DEFAULT now()
);

-- история обслуживания ТС, новые сверху (сортировка по дате)
CREATE INDEX IF NOT EXISTS idx_services_vehicle ON garage.services (vehicle_id, date DESC);

-- подпункты записи: [{name, cost, kind}]; kind = 'work' | 'part' | null (без типа);
-- cost записи = сумма подпунктов (считает API).
-- Колонка добавляется один раз; старые записи получают один подпункт
-- с собственным названием и суммой (повторно не выполняется).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'garage' AND table_name = 'services' AND column_name = 'items'
    ) THEN
        ALTER TABLE garage.services ADD COLUMN items jsonb NOT NULL DEFAULT '[]'::jsonb;
        UPDATE garage.services
           SET items = jsonb_build_array(jsonb_build_object('name', name, 'cost', cost));
    END IF;
END;
$$;

-- ── Документы записи: фото/сканы чеков и заказ-нарядов ─────────
-- Хранятся прямо в БД (bytea), чтобы попадать в ночной pg_dumpall → Яндекс Диск
-- вместе с записями. Фото сжимает клиент (~2400px JPEG), PDF — как есть.
-- bootstrap отдаёт только метаданные; содержимое — GET /api/garage/files/{id}.
CREATE TABLE IF NOT EXISTS garage.service_files (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id  uuid        NOT NULL REFERENCES garage.services(id) ON DELETE CASCADE,
    filename    text        NOT NULL DEFAULT '',
    mime        text        NOT NULL,
    size        int         NOT NULL,
    data        bytea       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_service_files_service ON garage.service_files (service_id, created_at);

-- ── updated_at автоматика ─────────────────────────────────────
CREATE OR REPLACE FUNCTION garage.trg_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER vehicles_touch
    BEFORE UPDATE ON garage.vehicles
    FOR EACH ROW EXECUTE FUNCTION garage.trg_touch();

CREATE OR REPLACE TRIGGER services_touch
    BEFORE UPDATE ON garage.services
    FOR EACH ROW EXECUTE FUNCTION garage.trg_touch();

-- ════════════════════════════════════════════════════════════
--  Права: всё принадлежит пользователю portal
-- ════════════════════════════════════════════════════════════
ALTER SCHEMA   garage                 OWNER TO portal;
ALTER TABLE    garage.vehicles        OWNER TO portal;
ALTER TABLE    garage.services        OWNER TO portal;
ALTER TABLE    garage.service_files   OWNER TO portal;
ALTER FUNCTION garage.trg_touch()     OWNER TO portal;

-- ════════════════════════════════════════════════════════════
--  Заметки по интеграции
-- ════════════════════════════════════════════════════════════
-- 1. labels — массив объектов {name, value}; API отдаёт/принимает как есть
--    (на коннекте зарегистрирован jsonb-кодек → Python list ↔ JSON).
-- 2. year — число (smallint); vin — строка; оба опциональны.
-- 3. cost/mileage в записях обслуживания опциональны (NULL).
-- 4. Удаление ТС разрешено в API только при archived=true; иначе 409.
--    services удаляются каскадом (ON DELETE CASCADE).
