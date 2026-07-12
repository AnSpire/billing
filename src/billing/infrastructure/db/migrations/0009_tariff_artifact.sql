-- Реестр tariff_artifact (billing_aggregates.md, «Реестр артефактов и порт
-- FormulaEngine») — НЕ агрегат, инфраструктурная таблица. Появляется в фазе 7
-- вместе с реальной компиляцией Catala (см. docstring domain/tariff_artifact.py).
--
-- Неизменяема тем же приёмом, что Invoice: в порту нет метода обновления,
-- только save (INSERT) и get. compiled_py_path — путь к собранному
-- standalone-пакету на диске (см. infrastructure/formula_engine/catala_toolchain.py),
-- не bytea — локальный диск монолита играет роль "объектного хранилища" из
-- billing_aggregates.md.
CREATE TABLE tariff_artifact (
    tariff_id TEXT NOT NULL,
    version INTEGER NOT NULL,

    catala_source TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    compiler_version TEXT NOT NULL,
    runtime_version TEXT NOT NULL,

    scope_name TEXT NOT NULL,
    scope_manifest JSONB NOT NULL,
    compiled_py_path TEXT NOT NULL,

    built_at TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (tariff_id, version)
);
