-- BillingAssessment (billing_aggregates.md §3): нить версий (account_id,
-- period), внутри — версии со статусом active/superseded. Инвариант "не
-- больше одной активной версии на период" (DoD фазы 4: Recalculate атомарно
-- помечает старую superseded и создаёт новую) — тем же приёмом, что у
-- ReferenceParameter (constraint, а не проверка в коде): частичный уникальный
-- индекс "по статусу active" не даёт вставить вторую активную строку, даже
-- если два Recalculate столкнутся конкурентно.
-- PK — естественный составной ключ (account_id, period, version), без
-- суррогатного id: в домене у BillingAssessment такого поля нет, агрегат
-- адресуется именно этой тройкой координат.
CREATE TABLE billing_assessment (
    account_id TEXT NOT NULL,
    period_year INTEGER NOT NULL,
    period_month INTEGER NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),

    charge_lines JSONB NOT NULL,
    calc_context JSONB NOT NULL,

    created_at TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (account_id, period_year, period_month, version)
);

CREATE UNIQUE INDEX billing_assessment_one_active_per_period
    ON billing_assessment (account_id, period_year, period_month)
    WHERE status = 'active';
