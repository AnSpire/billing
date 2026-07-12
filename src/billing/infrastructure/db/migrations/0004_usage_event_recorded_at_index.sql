-- Нужен с фазы 4: BillingAssessment сворачивает UsageEvent за конкретный
-- период (ConsumptionStreamRepository.events_for(..., period=...)),
-- фильтруя по recorded_at — без индекса это full scan потока на каждый расчёт.
CREATE INDEX usage_event_recorded_at_idx ON usage_event (account_id, metric, recorded_at);
