"""Сборка FastAPI-приложения.

Диспетчер саги и формализатор строятся один раз на старте (lifespan) и живут в
``app.state`` — эндпоинты берут их через зависимости ``get_dispatcher`` /
``get_formalizer``. Эндпоинты объявлены обычными ``def`` (не ``async``): драйвер
БД (psycopg) и обработчики саги синхронные, FastAPI уводит такие эндпоинты в
пул потоков и не блокирует event loop (PRESENTATION.md §3).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from billing.infrastructure.formalization.fixture_contract_formalizer import (
    FixtureContractFormalizer,
)
from billing.interface.http.errors import register_exception_handlers
from billing.interface.http.fixtures import default_fixtures
from billing.interface.http.routers import (
    accounts,
    assessments,
    consumption,
    invoices,
    reference_parameters,
    tariffs,
)
from billing.interface.http.saga import build_dispatcher
from billing.interface.http.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_settings()
    app.state.dispatcher = build_dispatcher(config.database_url)
    app.state.formalizer = FixtureContractFormalizer(default_fixtures())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Billing API", version="0.0.1", lifespan=lifespan)

    app.include_router(reference_parameters.router)
    app.include_router(consumption.router)
    app.include_router(tariffs.router)
    app.include_router(assessments.router)
    app.include_router(invoices.router)
    app.include_router(accounts.router)

    register_exception_handlers(app)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
