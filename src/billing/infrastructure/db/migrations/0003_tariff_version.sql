-- TariffVersion (billing_aggregates.md §1): "держатель данных" фазы 3, без
-- Catala. Идентичность — (tariff_id, version); одна версия = одна строка.
--
-- Неизменяемость после Publish — не exclusion constraint (как у
-- ReferenceParameter, где инвариант про пересечение диапазонов), а
-- guarded UPDATE: приложение вставляет/обновляет строку через
-- "ON CONFLICT (tariff_id, version) DO UPDATE ... WHERE status <> 'published'"
-- (см. PostgresTariffVersionRepository.save). Если строка уже опубликована,
-- WHERE не пропускает UPDATE, и приложение получает ноль строк в RETURNING —
-- сигнал, что мутация отклонена. Это безопасно и под конкурентной записью:
-- вторая транзакция увидит уже закоммиченный статус 'published' благодаря
-- обычной блокировке строки Postgres, а не полагается на проверку в коде
-- до записи.
CREATE TABLE tariff_version (
    tariff_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'validated', 'published')),

    source_text JSONB NOT NULL,
    scope_manifest JSONB NOT NULL,
    formula_form JSONB NOT NULL,
    coefficients JSONB NOT NULL,

    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,

    PRIMARY KEY (tariff_id, version)
);
