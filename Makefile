.PHONY: db-up db-down migrate test serve

# PYTHONPATH=src, а не editable-install: uv на macOS создаёт .pth со скрытым
# флагом, а Python 3.14 такие .pth пропускает — PYTHONPATH обходит это надёжно.
RUN = PYTHONPATH=src uv run

db-up:
	docker compose up -d

db-down:
	docker compose down

migrate:
	$(RUN) python -m billing.infrastructure.db.migrate

test:
	$(RUN) pytest

# BILLING_DATABASE_URL берётся из окружения (по умолчанию — локальный Postgres
# из docker-compose).
serve:
	$(RUN) uvicorn billing.interface.http.app:app --reload
