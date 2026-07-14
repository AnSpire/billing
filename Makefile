.PHONY: db-up db-down migrate test serve toolchain-image deploy

# PYTHONPATH=src, а не editable-install: uv на macOS создаёт .pth со скрытым
# флагом, а Python 3.14 такие .pth пропускает — PYTHONPATH обходит это надёжно.
RUN = PYTHONPATH=src uv run

# Тег базового образа с Catala. Совпадает с дефолтом ARG TOOLCHAIN_IMAGE в
# Dockerfile; версия в теге — версия Catala, чтобы её смена явно требовала
# пересборки базы.
TOOLCHAIN_IMAGE = billing-catala-toolchain:1.2.1
COMPOSE_PROD = docker compose -f docker-compose.prod.yml

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

# --- Деплой -----------------------------------------------------------------
#
# Базовый образ с тулчейном Catala: ~40 минут, но собирается ОДИН раз на машину
# (и один раз на демон — у rootless-docker и `sudo docker` раздельные хранилища
# образов, так что запускать надо ровно так же, как потом деплоишь).
# Пересобирать только при смене версии Catala/OCaml или базового digest'а.
toolchain-image:
	docker build -f Dockerfile.toolchain -t $(TOOLCHAIN_IMAGE) .

# Обычный деплой: пересобирает ТОЛЬКО слои приложения (pip install + прогрев
# Catala-кэша), тулчейн приезжает готовым из базового образа. Миграции
# накатывает docker-entrypoint.sh на старте контейнера.
#
# Если менялся только docker-compose.prod.yml (порты, env) — пересборка не
# нужна вовсе, хватит `$(COMPOSE_PROD) up -d`.
deploy:
	$(COMPOSE_PROD) up -d --build
