.PHONY: db-up db-down migrate test

db-up:
	docker compose up -d

db-down:
	docker compose down

migrate:
	uv run python -m billing.infrastructure.db.migrate

test:
	uv run pytest
