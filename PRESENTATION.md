# PRESENTATION.md — руководство по REST API поверх биллингового ядра (FastAPI)

Ядро сервиса готово и покрыто тестами (99 шт., фазы 0–8), но пользоваться им
снаружи нельзя: нет транспортного слоя. Это руководство описывает, как надеть на
существующий **application-слой** тонкий REST-фасад на FastAPI, ничего не меняя в
домене.

> **Главный принцип.** API — это **триггер и адаптер**, а не место для бизнес-логики.
> Роутер разбирает HTTP, собирает репозитории на транзакции запроса, вызывает
> готовую application-функцию, маппит доменные исключения на HTTP-коды. Ни одной
> формулы, ни одного расчёта в роутере быть не должно (CLAUDE.md: домен не знает
> про БД и тем более про HTTP).

---

## 1. Что уже есть и на что мы ложимся

Публичная поверхность application-слоя (то, что API будет вызывать):

| Функция | Файл | Делает |
|---|---|---|
| `calculate_assessment(...)` | `application/billing_calculation.py` | считает начисление, возвращает `(BillingAssessment, AssessmentCalculated)` |
| `recalculate_assessment(...)` | `application/billing_calculation.py` | пересчёт задним числом, `RecalculateResult` (с diff) |
| `validate_tariff_version(...)` | `application/tariff_validation.py` | компиляция Catala + резолв биндингов, `(TariffVersion, TariffValidated)` |
| `register_billing_saga(dispatcher)` | `application/billing_saga.py` | подписывает `Assessment→Invoice→Account` |
| `register_mass_recalculation_saga(dispatcher, factory)` | `application/mass_recalculation.py` | веерный пересчёт при коррекции параметра |
| `EventDispatcher(connection_factory)` | `application/dispatcher.py` | внутрипроцессная «сага»: `dispatch(event)` |

Плюс доменные команды на агрегатах (`TariffVersion.draft_from_text`,
`ReferenceParameter.register_value/correct/repeal`, `ConsumptionStream.record_usage`,
`Invoice.issue`) и порт-заглушка формализатора `ContractFormalizer.formalize`.

Три вещи, которые определяют всю форму API (следуют из кода, не выдуманы):

1. **Транзакциями владеет вызывающий, не application-функция.** Функции принимают
   уже построенные репозитории (обёрнутые вокруг `psycopg.Connection`) и `now`.
   Значит, транзакцию открывает и коммитит именно роутер/зависимость FastAPI.
2. **`now` инжектируется снаружи.** Ни одна функция не зовёт `datetime.now()` внутри
   (кроме саги — там время берётся в обработчике). API подставляет
   `datetime.now(timezone.utc)` в точке входа — это же даёт тестируемость.
3. **Сага запускается извне через `dispatch(event)`.** После записи, породившей
   событие (`AssessmentCalculated` и т.п.), кто-то должен вызвать `dispatch`. В
   тестах это делает тест; **в проде это делает API**. Это и есть тот самый
   «недостающий триггер» — см. §9 про ограничения.

---

## 2. Слои и поток запроса

```
HTTP-запрос
   │
   ▼
FastAPI router  ──►  Pydantic-схема (валидация входа, парсинг period/Decimal)
   │
   ▼
Depends: соединение БД (транзакция запроса)  ──►  сборка Postgres*Repository
   │
   ▼
application-функция  (calculate_assessment / validate_tariff_version / …)
   │  возвращает (агрегат, событие)
   ▼
commit транзакции запроса
   │
   ▼
dispatcher.dispatch(event)   ──►  сага в СВОИХ транзакциях (Invoice, Account)
   │
   ▼
Pydantic-схема ответа  ◄── читаем финальное состояние (invoice/ledger)
```

Ключевой момент, повторяющий тесты фазы 6 буквально: **сначала коммитим расчёт,
только потом диспатчим**. Обработчики саги получают собственные соединения и не
увидят незакоммиченный `BillingAssessment` (см. docstring `EventDispatcher`).

---

## 3. Каркас приложения

Зависимость: добавить `fastapi` и `uvicorn` в `pyproject.toml`
(`dependencies`/`dev`). Пакет предлагается положить в
`src/billing/interface/http/`.

### 3.1. Соединение как зависимость (транзакция на запрос)

```python
# src/billing/interface/http/deps.py
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import Depends
from psycopg import Connection

from billing.infrastructure.db.connection import new_connection

# DATABASE_URL берём из окружения (.env уже есть в проекте)
def get_settings() -> Settings: ...

def db_connection(settings=Depends(get_settings)) -> Iterator[Connection]:
    """Одна транзакция на запрос: commit при успехе, rollback при исключении.
    Ровно то, что делают тесты через `with new_connection(...)`."""
    with new_connection(settings.database_url) as conn:
        yield conn        # psycopg-контекст сам коммитит на выходе / откатывает на ошибке

def now() -> datetime:
    return datetime.now(timezone.utc)
```

### 3.2. Диспетчер саги — синглтон приложения

Диспетчеру нужен **фабричный** `connection_factory` (свежее соединение на каждый
шаг), а не соединение запроса. Собираем один раз на старте:

```python
# src/billing/interface/http/saga.py
from billing.application.dispatcher import EventDispatcher
from billing.application.billing_saga import register_billing_saga
from billing.application.mass_recalculation import register_mass_recalculation_saga
from billing.infrastructure.db.connection import new_connection

def build_dispatcher(database_url: str) -> EventDispatcher:
    factory = lambda: new_connection(database_url)
    dispatcher = EventDispatcher(factory)
    register_billing_saga(dispatcher)
    register_mass_recalculation_saga(dispatcher, factory)
    return dispatcher
```

```python
# src/billing/interface/http/app.py
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.dispatcher = build_dispatcher(settings.database_url)
    yield

def create_app() -> FastAPI:
    app = FastAPI(title="Billing API", version="0.0.1", lifespan=lifespan)
    app.include_router(reference_parameters.router)
    app.include_router(consumption.router)
    app.include_router(tariffs.router)
    app.include_router(assessments.router)
    app.include_router(invoices.router)
    app.include_router(accounts.router)
    register_exception_handlers(app)     # §8
    return app
```

> **Про async.** `psycopg` здесь синхронный, а обработчики саги — тоже синхронные и
> ходят в БД. Поэтому эндпоинты делаем **`def`, а не `async def`** — FastAPI сам
> уведёт их в пул потоков и не заблокирует event loop. Не притворяемся
> асинхронными поверх синхронного драйвера.

---

## 4. Схемы (Pydantic) и общие типы

Доменные VO не отдаём наружу напрямую — у них своя семантика. Вводим тонкие
схемы и пару конвертеров.

```python
# Общие представления
class MoneyOut(BaseModel):
    amount: Decimal          # Pydantic сериализует Decimal как строку -> без потери копеек
    currency: str = "RUB"

class PeriodStr(str):
    """'YYYY-MM' -> BillingPeriod.parse(...) на входе, str(period) на выходе."""

# Пример конвертера
def to_money_out(m: Money) -> MoneyOut:
    return MoneyOut(amount=m.amount, currency=m.currency)
```

Правила, которые нельзя нарушать в схемах:

- **Деньги и количества — `Decimal`, сериализуются строкой.** Никогда не `float`:
  калькулятор округляет `ROUND_HALF_UP` до копеек, `float` это сломает.
- **Период — строка `"2026-06"`**, парсится через `BillingPeriod.parse` (уже
  существует, используется сагой). Отдаём через `str(period)`.
- **`version_id`, `invoice_id` — UUID**, отдаём строкой.

---

## 5. Карта эндпоинтов

Сгруппировано по агрегату. `→ saga` помечает эндпоинты, которые после записи
запускают `dispatch` и потому имеют побочные эффекты в других агрегатах.

### 5.1. Справочные параметры (ReferenceParameter)

| Метод | Путь | Тело / параметры | Отдаёт |
|---|---|---|---|
| `POST` | `/reference-parameters` | key, jurisdiction, value, valid_from, valid_to?, provenance | `version_id` |
| `POST` | `/reference-parameters/{key}/{jurisdiction}/corrections` **→ saga** | новое value, validity, provenance | `version_id` + сводка веерного пересчёта |
| `POST` | `/reference-parameters/{key}/{jurisdiction}/repeal` | repeal_from, provenance | `version_id` |
| `GET` | `/reference-parameters/{key}/{jurisdiction}` | `?valid_on=&as_of=` | резолвнутая версия (value, validity, provenance) |

`corrections` — самый «тяжёлый»: порождает `ReferenceParameterCorrected`, диспетчер
запускает `register_mass_recalculation_saga` → пересчитываются только счета,
реально читавшие этот параметр; сбои уходят в dead-letter. В ответе полезно
вернуть, сколько счетов пересчитано и сколько ушло в dead-letter (query по
`dead_letter`).

### 5.2. Потребление (ConsumptionStream)

| Метод | Путь | Тело | Отдаёт |
|---|---|---|---|
| `POST` | `/accounts/{account_id}/usage` | metric, quantity, external_event_id, meta? | `event_id`, `is_duplicate` |
| `GET` | `/accounts/{account_id}/usage` | `?metric=&period=` | список `UsageEvent` |

**Идемпотентность встроена в домен**: повторный `external_event_id` — no-op,
возвращаем `is_duplicate=true` и `200 OK` (не `409`). Клиент может слать смело.

### 5.3. Тарифы (TariffVersion) — тут живёт мок-формализатор

| Метод | Путь | Тело | Отдаёт |
|---|---|---|---|
| `POST` | `/tariffs` | `contract_doc` (ключ фикстуры), tariff_id, version | draft: статус, scope_manifest, coefficients |
| `POST` | `/tariffs/{tariff_id}/versions/{version}/validate` **→ compile** | — | статус `validated` или ошибка компиляции |
| `POST` | `/tariffs/{tariff_id}/versions/{version}/publish` | `approved_by` | статус `published`, `published_at` |
| `GET` | `/tariffs/{tariff_id}/versions/{version}` | — | вся версия |

Поток `POST /tariffs`:

```python
formalizer: ContractFormalizer = FixtureContractFormalizer(FIXTURES)
result = formalizer.formalize(body.contract_doc)      # мок вместо AI-агента
draft, event = TariffVersion.draft_from_text(body.tariff_id, body.version, result, now=now())
PostgresTariffVersionRepository(conn).save(draft)
```

`publish` **обязан** требовать непустой `approved_by` — домен сам кинет
`PublishRequiresApprovalError`, если его нет (CLAUDE.md §4: автопубликация
запрещена, человек подтверждает AI-формализацию). API не должен этот шаг обходить.

### 5.4. Начисления (BillingAssessment) — вход в биллинг

| Метод | Путь | Тело | Отдаёт |
|---|---|---|---|
| `POST` | `/assessments` **→ saga** | account_id, period, tariff_ref, metric | assessment v1 + выпущенный invoice |
| `POST` | `/assessments/{account_id}/{period}/recalculate` **→ saga** | tariff_ref, metric | assessment v2 + diff + корректирующий invoice |
| `GET` | `/assessments/{account_id}/{period}` | — | активная версия (charge_lines, total, calc_context) |
| `GET` | `/assessments/{account_id}/{period}/diff` | `?v1=&v2=` | `AssessmentDiff` |

⚠️ **`tariff_ref` в теле — вынужденно.** В домене нет агрегата, хранящего связку
`account → активный тариф` (осознанное решение, см. docstring
`billing_calculation.py`). Поэтому API обязан принять `(tariff_id, version)`
явно и сам загрузить `TariffVersion` из репозитория перед вызовом
`calculate_assessment`. Это — кандидат №1 на будущую доработку (см. §9).

### 5.5. Квитанции (Invoice) и лицевой счёт (Account) — только чтение

| Метод | Путь | Отдаёт |
|---|---|---|
| `GET` | `/invoices/{invoice_id}` | invoice (lines, total, correction_link) |
| `GET` | `/accounts/{account_id}/invoices` | список квитанций |
| `GET` | `/accounts/{account_id}/balance` | `{ balance, projected_balance }` |
| `GET` | `/accounts/{account_id}/ledger` | append-only записи леджера |

Invoice и Account **не имеют пишущих эндпоинтов**: их записи создаёт только сага
(`Invoice.issue`, `Account.post_charge/post_correction`). Дать снаружи «выставить
квитанцию руками» — значит обойти инвариант «квитанция замораживает копию
начисления». Наружу — только чтение.

---

## 6. Эталонный эндпоинт целиком (Calculate)

```python
# src/billing/interface/http/routers/assessments.py
router = APIRouter(prefix="/assessments", tags=["assessments"])

class CalculateIn(BaseModel):
    account_id: str
    period: str                       # "2026-06"
    tariff_id: str
    tariff_version: int
    metric: str = "electricity_kwh"

@router.post("", status_code=201, response_model=CalculateOut)
def calculate(
    body: CalculateIn,
    request: Request,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(now),
):
    period = BillingPeriod.parse(body.period)

    tariff = PostgresTariffVersionRepository(conn).get(body.tariff_id, body.tariff_version)
    if tariff is None:
        raise HTTPException(404, "tariff version not found")
    if tariff.status is not TariffVersionStatus.PUBLISHED:
        raise HTTPException(409, "tariff version is not published")

    assessment, event = calculate_assessment(
        body.account_id, period, tariff,
        PostgresReferenceParameterRepository(conn),
        PostgresConsumptionStreamRepository(conn),
        CatalaFormulaEngine(PostgresTariffArtifactRepository(conn)),
        PostgresBillingAssessmentRepository(conn),
        metric=body.metric, now=now,
        artifacts=PostgresTariffArtifactRepository(conn),
    )
    # <-- выход из Depends(db_connection) закоммитит транзакцию ПОСЛЕ эндпоинта.
    #     Но саге нужен уже закоммиченный assessment, поэтому коммитим ЯВНО здесь:
    conn.commit()

    request.app.state.dispatcher.dispatch(event)   # Invoice.Issue -> Account.PostCharge

    # читаем результат саги на новом соединении (или через отдельную Depends-сессию)
    with new_connection(get_settings().database_url) as read_conn:
        invoice = PostgresInvoiceRepository(read_conn).find_by_assessment_version(
            body.account_id, period, assessment.version
        )
    return CalculateOut.from_domain(assessment, invoice)
```

Два тонких места, оба взяты из тестов фазы 6:

1. **Явный `conn.commit()` перед `dispatch`.** Иначе обработчик `InvoiceIssued` на
   своём соединении не увидит `BillingAssessment`. Если это неудобно совмещать с
   авто-commit зависимости — сделайте для «пишущих + саговых» эндпоинтов отдельную
   зависимость, которая коммитит внутри и отдаёт управление до `dispatch`.
2. **Результат саги читаем отдельным чтением**, потому что он записан в других
   транзакциях, уже после нашей.

---

## 7. Маппинг: HTTP-семантика для остальных эндпоинтов

- `POST /assessments/{...}/recalculate` — то же, но `recalculate_assessment` →
  `RecalculateResult`; диспатчим `result.event`; в ответ кладём `result.diff`
  (построчный `before/after`, `changed_parameter_keys`) — это готовый UC-10.
- `GET .../diff?v1=&v2=` — читаем две версии через `get_version`, зовём
  `BillingAssessment.diff(v1, v2)` (это query, не команда — можно звать в GET).

---

## 8. Ошибки: доменные исключения → HTTP-коды

Единый обработчик, чтобы роутеры не были засыпаны `try/except`. Маппинг по
смыслу доменных исключений (все они уже определены):

| Доменное исключение | HTTP | Когда |
|---|---|---|
| `UnknownContractError` (формализатор) | `404` | нет такой фикстуры договора |
| `TariffVersionNotFound` / `InvoiceNotFoundError` / `AssessmentNotFoundError` | `404` | нет ресурса |
| `InvalidTariffVersionTransitionError`, `InvalidAssessmentTransitionError` | `409` | операция из неверного статуса |
| `PublishRequiresApprovalError` | `422` | publish без `approved_by` |
| `UnresolvedScopeBindingError`, `UnresolvedReferenceParameterError` | `422` | биндинг/параметр не резолвится |
| `DuplicateActiveAssessmentError`, `DuplicateInvoiceError`, `OverlappingValidTimeError` | `409` | нарушение уникальности/периодов |
| `CatalaCompilationError` | `422` | тариф не компилируется (ошибка формализации) |
| `ConflictError` (конфликт дефолтов Catala) | `409` | пересекающиеся правила |
| `SagaError` / `MissingTariffVersionError` | `500` | рассинхронизация ссылок (не happy-path) |

```python
def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BillingAssessmentError)
    def _(request, exc):
        return JSONResponse(status_code=STATUS[type(exc)], content={"detail": str(exc)})
    # ... по одному на базовый класс каждого агрегата
```

Тело ответа при ошибке — единый формат `{"detail": "..."}` (совместимо с дефолтом
FastAPI/`HTTPException`).

---

## 9. Чего API сам по себе НЕ закрывает (честные ограничения)

API снимает главный блокер — «сервисом нельзя пользоваться». Но три вещи остаются
за его пределами, и о них надо сказать явно:

1. **Надёжная доставка триггера (Outbox).** `dispatch` вызывается синхронно внутри
   запроса и **не переживает падение процесса между шагами саги** (см. docstring
   `billing_saga.py`, PLAN.md «вне плана»). Если API упал после `commit`
   начисления, но до/во время `dispatch`, квитанция не выпустится сама. Пока
   спасает идемпотентность (повторный вызов доделает), но по-хорошему нужен
   **transactional outbox**: писать событие в ту же транзакцию, отдельный воркер
   диспатчит. До этого — `POST /assessments` семантически «at-least-once»,
   клиент/ретрай обязаны это учитывать.
2. **Планировщик «когда биллить».** API даёт *ручной* запуск `POST /assessments`.
   Автоматического «конец месяца → посчитать все счета» нет и на уровне API не
   появляется — это отдельный компонент (cron/worker), который сам вызывает эти же
   эндпоинты или application-функции.
3. **Связка account → тариф.** Пока `tariff_ref` передаётся в теле (§5.4). Полноценно
   это должен резолвить сервис; введение такого агрегата — отдельное продуктовое
   решение, не задача HTTP-слоя.

Плюс кросс-функциональное, что вешается на транспорт при выходе в прод:
**аутентификация/авторизация** (особенно на `publish` и `corrections`),
конфигурация окружений, structured logging и метрики, rate limiting.

---

## 10. Тестирование API

- **Контрактные тесты** через `fastapi.testclient.TestClient` поверх того же
  `test_database_url`, что и в `tests/` — прогонять сквозной сценарий UC-4
  (`POST /tariffs` → validate → publish → `POST /accounts/{}/usage` →
  `POST /assessments` → `GET /invoices/{}` → `GET /accounts/{}/balance`) и
  сверять с golden-числами UC-4, которые уже зафиксированы в фазах 4/7.
- **Идемпотентность на уровне HTTP**: повторный `POST /accounts/{}/usage` с тем же
  `external_event_id` → `is_duplicate=true`; повторный `dispatch` (эмуляция
  ретрая) не выпускает второй invoice — это уже проверено в фазе 6 на уровне саги,
  на HTTP достаточно одного smoke-теста.
- **Инъекция `now`** через `Depends(now)` позволяет переопределять «сейчас» в
  тестах (`app.dependency_overrides`), как это делают доменные тесты параметром
  `now=`.

---

## 11. Порядок внедрения (предлагаемая последовательность)

1. Каркас: `create_app`, `db_connection`, `now`, обработчики ошибок (§3, §8).
2. Read-only эндпоинты (`GET` assessment/invoice/balance/ledger) — нулевой риск,
   сразу дают наблюдаемость.
3. Тарифный жизненный цикл (`POST /tariffs` + validate + publish) — без саги.
4. `POST /accounts/{}/usage` — приём потребления.
5. `POST /assessments` (+ recalculate) с `dispatch` — первый саговый эндпоинт (§6).
6. `corrections` с веерным пересчётом — самый сложный, последним.
7. Затем (отдельные фазы, не часть API): Outbox, планировщик, auth.

Такой порядок даёт работающий сквозной happy-path UC-4 уже после шага 5.
```
