-- Account (billing_aggregates.md §5): журнал проводок, append-only. amount
-- хранит неотрицательную величину — знак задаёт direction (см. docstring
-- domain/account.py про трактовку "двойной записи" в этом проекте).
--
-- invoice_id/correction_of_invoice_id — БЕЗ FK на invoice: Account ссылается
-- на Invoice только по id (CLAUDE.md §7, «ссылки между агрегатами — только
-- по id, никогда прямой ссылкой на объект»), и это касается не только
-- доменного кода, но и схемы — FK здесь означал бы, что LedgerEntry нельзя
-- записать, пока не существует чужой агрегат, что излишне жёстко привязывает
-- границы транзакций друг к другу. confirms_pending_entry_id — исключение:
-- это ссылка ВНУТРИ своего же агрегата (на другую LedgerEntry), обычная
-- целостность в пределах агрегата, не межагрегатная связанность.
CREATE TABLE ledger_entry (
    entry_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    entry_type TEXT NOT NULL CHECK (entry_type IN ('pending', 'posted')),
    amount NUMERIC NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL,
    period_year INTEGER NOT NULL,
    period_month INTEGER NOT NULL,
    invoice_id UUID,
    correction_of_invoice_id UUID,
    confirms_pending_entry_id UUID REFERENCES ledger_entry (entry_id),
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX ledger_entry_account_idx ON ledger_entry (account_id);
