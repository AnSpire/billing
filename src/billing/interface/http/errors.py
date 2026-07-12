"""Единый маппинг доменных исключений на HTTP-коды (PRESENTATION.md §8).

Роутеры не оборачивают вызовы в ``try/except`` — они дают доменным исключениям
всплыть, а этот модуль превращает их в аккуратный ``{"detail": ...}`` с нужным
статусом. Регистрируем один обработчик на каждый базовый класс агрегата (и на
отдельные standalone-исключения); статус подбираем по конкретному типу, обходя
MRO, с дефолтом ``400`` для «прочей доменной ошибки».
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from billing.application.billing_saga import SagaError
from billing.application.mass_recalculation import MissingTariffVersionError
from billing.domain.account import AccountError, InvalidLedgerEntryStateError
from billing.domain.billing_assessment import (
    ArtifactNotFoundError,
    AssessmentNotFoundError,
    BillingAssessmentError,
    ConflictError,
    DuplicateActiveAssessmentError,
    InvalidAssessmentTransitionError,
    UnresolvedReferenceParameterError,
)
from billing.domain.consumption_stream import ConsumptionStreamError, MetricMismatchError
from billing.domain.invoice import (
    DuplicateInvoiceError,
    InvalidCorrectionError,
    InvoiceError,
    InvoiceNotFoundError,
)
from billing.domain.reference_parameter import (
    InvalidRepealDateError,
    MissingProvenanceError,
    OverlappingValidTimeError,
    ReferenceParameterError,
    ReferenceParameterNotFoundError,
)
from billing.domain.tariff_version import (
    InvalidTariffVersionTransitionError,
    PublishRequiresApprovalError,
    TariffVersionError,
    TariffVersionImmutableError,
    UnresolvedScopeBindingError,
)
from billing.infrastructure.formalization.fixture_contract_formalizer import UnknownContractError
from billing.infrastructure.formula_engine.catala_toolchain import CatalaCompilationError

# Конкретный тип -> HTTP-статус. Порядок значения не имеет: ищем точное
# совпадение, затем идём вверх по MRO.
_STATUS: dict[type[Exception], int] = {
    # 404 — ресурса/записи нет
    UnknownContractError: 404,
    InvoiceNotFoundError: 404,
    AssessmentNotFoundError: 404,
    ReferenceParameterNotFoundError: 404,
    # 409 — конфликт состояния/уникальности
    InvalidTariffVersionTransitionError: 409,
    InvalidAssessmentTransitionError: 409,
    TariffVersionImmutableError: 409,
    DuplicateActiveAssessmentError: 409,
    DuplicateInvoiceError: 409,
    OverlappingValidTimeError: 409,
    InvalidCorrectionError: 409,
    InvalidLedgerEntryStateError: 409,
    ConflictError: 409,
    # 422 — вход синтаксически валиден, но семантически не проходит
    PublishRequiresApprovalError: 422,
    UnresolvedScopeBindingError: 422,
    UnresolvedReferenceParameterError: 422,
    MetricMismatchError: 422,
    InvalidRepealDateError: 422,
    MissingProvenanceError: 422,
    CatalaCompilationError: 422,
    # 500 — рассинхронизация/инвариант, которого не должно быть на happy-path
    ArtifactNotFoundError: 500,
    SagaError: 500,
    MissingTariffVersionError: 500,
}

# Базовые классы, на которые вешаем обработчик (покрывают подтипы через MRO).
_BASES: tuple[type[Exception], ...] = (
    BillingAssessmentError,
    TariffVersionError,
    ReferenceParameterError,
    ConsumptionStreamError,
    InvoiceError,
    AccountError,
    # standalone-исключения (не наследуют доменные базы)
    ConflictError,
    CatalaCompilationError,
    UnknownContractError,
    SagaError,
    MissingTariffVersionError,
)


def _status_for(exc: Exception) -> int:
    for cls in type(exc).__mro__:
        if cls in _STATUS:
            return _STATUS[cls]
    return 400  # прочая доменная ошибка — клиентская по умолчанию


def _handle(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=_status_for(exc), content={"detail": str(exc)})


def register_exception_handlers(app: FastAPI) -> None:
    for base in _BASES:
        app.add_exception_handler(base, _handle)
