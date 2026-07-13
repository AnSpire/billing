#!/bin/sh
# Миграции накатываются на старте: apply_migrations идемпотентен (ведёт
# таблицу applied), а в lifespan приложения их вызова нет — без этого шага
# сервис поднимется на пустой схеме и упадёт на первом же запросе.
set -e

echo "applying migrations..."
python -m billing.infrastructure.db.migrate

exec "$@"
