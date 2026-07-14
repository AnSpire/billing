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


API_DESCRIPTION = """
Биллинг, в котором тариф — это **исполнимая формализация текста договора**, а не
захардкоженная формула.

### Типовой сценарий

1. `POST /tariffs` — текст договора формализуется в черновик версии тарифа.
2. `POST /tariffs/{id}/versions/{v}/validate` — компиляция формул (Catala) и
   резолв ссылок на справочные параметры.
3. `POST /tariffs/{id}/versions/{v}/publish` — публикация; обязателен
   человек-апрувер, автопубликации нет.
4. `POST /accounts/{id}/usage` — приём фактов потребления (идемпотентно).
5. `POST /assessments` — начисление за период; сага сама выпускает квитанцию и
   проводит её по лицевому счёту.

### Что стоит знать заранее

* **Ничего не перезаписывается.** Пересчёт создаёт новую версию начисления и
  корректирующую квитанцию; журнал проводок только дописывается.
* **Справочные параметры битемпоральны** — отдельно «когда значение действует по
  закону» (valid-time) и «когда мы о нём узнали» (tx-time). Поэтому расчёт
  прошлого периода воспроизводится ровно так, как он считался тогда.
* **Ретроактивная коррекция параметра** (`.../corrections`) веером пересчитывает
  все затронутые начисления — это самая тяжёлая операция API.
* **Квитанции и проводки создаёт только сага.** Эндпоинтов «выставить квитанцию»
  или «записать проводку» нет by design.

Доменные ошибки приходят как `{"detail": "..."}`: `404` — нет ресурса, `409` —
конфликт состояния, `422` — вход валиден синтаксически, но нарушает правило
предметной области.
"""

TAGS_METADATA = [
    {
        "name": "tariffs",
        "description": "Версии тарифов: формализация текста договора и жизненный цикл "
        "**draft → validate → publish**. Начислять можно только по опубликованной версии.",
    },
    {
        "name": "reference-parameters",
        "description": "Справочные параметры (ставки НДС, нормы ЖКХ) — битемпоральные, "
        "с обязательной ссылкой на нормативный акт. Здесь же ретроактивная коррекция, "
        "запускающая веерный пересчёт начислений.",
    },
    {
        "name": "consumption",
        "description": "Приём фактов потребления по лицевому счёту. Идемпотентно "
        "по `external_event_id` — источник может безопасно ретраить.",
    },
    {
        "name": "assessments",
        "description": "Начисления: вход в биллинг. Расчёт за период и пересчёт; "
        "каждый пересчёт добавляет новую версию, старые остаются в истории.",
    },
    {
        "name": "invoices",
        "description": "Квитанции — замороженные копии начислений. Только чтение: "
        "выпускает их исключительно сага.",
    },
    {
        "name": "accounts",
        "description": "Лицевые счета: баланс и журнал проводок. Только чтение — "
        "проводки создаёт сага.",
    },
    {"name": "meta", "description": "Служебные эндпоинты."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_settings()
    app.state.dispatcher = build_dispatcher(config.database_url)
    app.state.formalizer = FixtureContractFormalizer(default_fixtures())
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Billing API",
        version="0.0.1",
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )

    app.include_router(reference_parameters.router)
    app.include_router(consumption.router)
    app.include_router(tariffs.router)
    app.include_router(assessments.router)
    app.include_router(invoices.router)
    app.include_router(accounts.router)

    register_exception_handlers(app)

    @app.get("/health", tags=["meta"], summary="Проверка живости процесса")
    def health() -> dict[str, str]:
        """Отвечает `{"status": "ok"}`, если процесс поднят.

        Только liveness: доступность БД не проверяется, поэтому `ok` здесь не
        гарантирует, что расчётные эндпоинты отработают.
        """
        return {"status": "ok"}

    return app


app = create_app()
