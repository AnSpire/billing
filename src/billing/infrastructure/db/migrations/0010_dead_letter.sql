-- Dead-letter лог веерного пересчёта (PLAN.md, фаза 8; use_case.md UC-7,
-- «Обработка ошибок веера»). НЕ агрегат и не порт в домене — операционный
-- журнал сбоев саги для ручного разбора, append-only (см. docstring
-- infrastructure/db/dead_letter_store.py).
--
-- ``reason``/``retryable`` различают детерминированный конфликт дефолтов
-- Catala (retryable=false — ретраить бессмысленно, нужен разбор человеком)
-- от инфраструктурного сбоя (retryable=true — кандидат на ретрай; сам ретрай
-- вне этой фазы, см. use_case.md, открытый вопрос №2).
CREATE TABLE dead_letter (
    id UUID PRIMARY KEY,

    account_id TEXT NOT NULL,
    period TEXT NOT NULL,

    parameter_key TEXT NOT NULL,
    parameter_jurisdiction TEXT NOT NULL,

    reason TEXT NOT NULL,
    retryable BOOLEAN NOT NULL,
    detail TEXT NOT NULL,

    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX dead_letter_account_idx ON dead_letter (account_id);
