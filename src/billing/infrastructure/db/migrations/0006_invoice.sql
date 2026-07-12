-- Invoice (billing_aggregates.md §4): выставленная квитанция никогда не
-- мутирует. В коде это гарантирует то, что InvoiceRepository (порт в
-- domain/invoice.py) не предоставляет никакого метода обновления — здесь
-- дополнительный constraint/триггер не нужен ровно по той же причине, по
-- которой он не нужен был бы для таблицы без UPDATE-кода вообще: нечему
-- защищаться от, кроме дисциплины доступа только через типизированный порт.
--
-- UNIQUE (account_id, period, assessment_version) добавлен в фазе 6: сага
-- проверяет find_by_assessment_version перед Issue (идемпотентность
-- повторной доставки события), а этот индекс — тот же приём, что partial
-- unique index у billing_assessment: не даёт двум конкурентным доставкам
-- одного события выставить две квитанции на одну и ту же версию расчёта.
CREATE TABLE invoice (
    invoice_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    period_year INTEGER NOT NULL,
    period_month INTEGER NOT NULL,
    assessment_version INTEGER NOT NULL,
    lines JSONB NOT NULL,
    total JSONB NOT NULL,
    correction_of_invoice_id UUID REFERENCES invoice (invoice_id),
    issued_at TIMESTAMPTZ NOT NULL,

    UNIQUE (account_id, period_year, period_month, assessment_version)
);

CREATE INDEX invoice_account_period_idx ON invoice (account_id, period_year, period_month);
