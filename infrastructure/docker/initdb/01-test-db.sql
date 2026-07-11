-- Отдельная БД для pytest, чтобы тестовые прогоны не пересекались с dev-данными.
CREATE DATABASE billing_test OWNER billing;
