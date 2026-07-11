-- ConsumptionStream (billing_aggregates.md §6): тонкий агрегат, append-only.
-- Единственный реальный инвариант — идемпотентность приёма по внешнему id
-- (FR-14). В отличие от ReferenceParameter здесь не нужен GiST/exclusion —
-- обычный UNIQUE достаточно, потому что инвариант одномерный (совпадение
-- ключа), а не пересечение диапазонов.
CREATE TABLE usage_event (
    event_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    quantity_value NUMERIC NOT NULL,
    external_event_id TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT usage_event_external_event_id_unique
        UNIQUE (account_id, metric, external_event_id)
);

CREATE INDEX usage_event_stream_lookup ON usage_event (account_id, metric);
