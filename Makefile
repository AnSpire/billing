.PHONY: db-up db-down migrate test serve

db-up:
	docker compose up -d

db-down:
	docker compose down

migrate:
	uv run python -m billing.infrastructure.db.migrate

test:
	uv run pytest

# HTTP API (PRESENTATION.md). PYTHONPATH=src, а не editable-install: uv на
# macOS создаёт .pth со скрытым флагом, а Python 3.14 такие .pth пропускает —
# PYTHONPATH обходит это надёжно. BILLING_DATABASE_URL берётся из окружения
# (по умолчанию — локальный Postgres из docker-compose).
serve:
	PYTHONPATH=src uv run uvicorn billing.interface.http.app:app --reload
