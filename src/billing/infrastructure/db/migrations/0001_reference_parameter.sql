-- ReferenceParameter (billing_aggregates.md §2): самый чистый носитель
-- битемпоральности в системе. Инвариант "нет пересечений valid-time среди
-- актуальных версий" охраняется exclusion constraint'ом, а не кодом
-- приложения (CLAUDE.md §7) — constraint не соврёт под конкурентной записью.
--
-- Общий exclusion constraint по (valid_range, tx_range) — это и есть полная
-- битемпоральная защита: две строки с одним (key, jurisdiction) конфликтуют,
-- только если пересекаются ОБА измерения сразу. Правило DoD фазы 1 "нет
-- пересечений valid-time среди tx_to = ∞ версий" — частный случай этого
-- constraint'а (для набора актуальных строк их tx_range тянутся до
-- бесконечности и поэтому взаимно всегда пересекаются).
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE reference_parameter_version (
    version_id UUID PRIMARY KEY,
    key TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,

    -- ParameterValue — полиморфное значение (скаляр сейчас, пороговая таблица
    -- / прогрессивная шкала позже). JSONB не потому что "модно", а потому что
    -- эта полиморфность — часть принятой модели (billing_aggregates.md §2), и
    -- фиксированные колонки под неё пришлось бы менять при каждом новом kind.
    value JSONB NOT NULL,

    valid_range TSTZRANGE NOT NULL,
    tx_range TSTZRANGE NOT NULL,

    -- Provenance — первоклассный VO, обязателен: без него обрывается
    -- объяснимость (UC-9). Дублируем проверку "не пусто" на уровне БД —
    -- домен уже не даст сконструировать Provenance с пустыми полями, но раз
    -- уж это обязательное условие команды, а не просто тип, пусть его не
    -- обойти и прямой записью в таблицу.
    provenance_regulation_ref TEXT NOT NULL,
    provenance_document_id TEXT NOT NULL,
    provenance_effective_date DATE NOT NULL,

    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT reference_parameter_version_provenance_not_blank
        CHECK (provenance_regulation_ref <> '' AND provenance_document_id <> ''),

    EXCLUDE USING gist (
        key WITH =,
        jurisdiction WITH =,
        valid_range WITH &&,
        tx_range WITH &&
    )
);

CREATE INDEX reference_parameter_version_lookup
    ON reference_parameter_version (key, jurisdiction);
