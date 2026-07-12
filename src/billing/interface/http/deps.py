"""FastAPI-зависимости: соединение-на-запрос, ``now`` и общий доступ к
диспетчеру саги / формализатору.

Ключевое решение (PRESENTATION.md §2): **транзакцией владеет вызывающий, а не
application-функция**. ``db_connection`` открывает одну транзакцию на запрос и
коммитит её на выходе (или откатывает при исключении) — ровно как
``with new_connection(...)`` в тестах. ``now`` инжектируется снаружи, чтобы его
можно было переопределить в тестах через ``app.dependency_overrides``.

Саговые эндпоинты (``assessments``, ``corrections``) этой зависимостью для
записи НЕ пользуются: им нужно закоммитить запись ДО ``dispatch`` (обработчик
саги работает на своём соединении и не увидит незакоммиченное) — они управляют
соединением явно через ``new_connection``. См. PRESENTATION.md §6.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import Depends, Request
from psycopg import Connection

from billing.application.dispatcher import EventDispatcher
from billing.domain.tariff_version import ContractFormalizer
from billing.infrastructure.db.connection import new_connection
from billing.interface.http.settings import Settings, get_settings


def settings() -> Settings:
    return get_settings()


def get_now() -> datetime:
    return datetime.now(timezone.utc)


def db_connection(config: Settings = Depends(settings)) -> Iterator[Connection]:
    """Транзакция на запрос: commit при успехе, rollback при исключении.

    Годится для read-only и «одноагрегатных» пишущих эндпоинтов, которым не
    нужно, чтобы результат увидел кто-то на другом соединении в пределах того
    же запроса. Саговые эндпоинты — см. docstring модуля."""
    with new_connection(config.database_url) as conn:
        yield conn


def get_dispatcher(request: Request) -> EventDispatcher:
    return request.app.state.dispatcher


def get_formalizer(request: Request) -> ContractFormalizer:
    return request.app.state.formalizer
