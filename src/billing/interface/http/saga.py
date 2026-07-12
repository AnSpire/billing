"""Сборка внутрипроцессной саги для приложения. Диспетчеру нужна **фабрика**
соединений (свежее соединение на каждый шаг цепочки/веера), а не соединение
запроса — поэтому строим его один раз на старте приложения (lifespan), а не в
зависимости запроса."""

from __future__ import annotations

from billing.application.billing_saga import register_billing_saga
from billing.application.dispatcher import EventDispatcher
from billing.application.mass_recalculation import register_mass_recalculation_saga
from billing.infrastructure.db.connection import new_connection


def build_dispatcher(database_url: str) -> EventDispatcher:
    def factory():
        return new_connection(database_url)

    dispatcher = EventDispatcher(factory)
    register_billing_saga(dispatcher)  # Assessment -> Invoice -> Account
    register_mass_recalculation_saga(dispatcher, factory)  # веерный пересчёт
    return dispatcher
