"""Начисления — BillingAssessment. Вход в биллинг и единственные саговые
эндпоинты happy-path.

``POST /assessments`` считает начисление, затем ``dispatch`` прогоняет сагу
Assessment→Invoice→Account (выпуск квитанции + проводка). Порядок из
PRESENTATION.md §6: пишем и **коммитим** начисление в своей транзакции, только
потом диспатчим — обработчик саги работает на своём соединении и незакоммиченное
не увидит; результат саги читаем отдельным соединением.

⚠️ ``tariff_id``/``tariff_version`` приходят в теле: в домене нет агрегата,
хранящего связку account→тариф (осознанное решение, см.
``application/billing_calculation.py``). Это кандидат №1 на доработку.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import Connection
from pydantic import BaseModel

from billing.application.billing_calculation import calculate_assessment, recalculate_assessment
from billing.domain.billing_assessment import BillingAssessment, FormulaEngine
from billing.domain.shared import BillingPeriod
from billing.domain.tariff_version import TariffVersion, TariffVersionStatus
from billing.infrastructure.db.billing_assessment_repository import (
    PostgresBillingAssessmentRepository,
)
from billing.infrastructure.db.connection import new_connection
from billing.infrastructure.db.consumption_stream_repository import (
    PostgresConsumptionStreamRepository,
)
from billing.infrastructure.db.invoice_repository import PostgresInvoiceRepository
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)
from billing.infrastructure.db.tariff_artifact_repository import PostgresTariffArtifactRepository
from billing.infrastructure.db.tariff_version_repository import PostgresTariffVersionRepository
from billing.infrastructure.formula_engine.catala_formula_engine import CatalaFormulaEngine
from billing.infrastructure.formula_engine.stub_formula_engine import StubFormulaEngine
from billing.interface.http.deps import get_dispatcher, get_now, settings
from billing.interface.http.serialization import (
    AssessmentDiffOut,
    AssessmentOut,
    InvoiceOut,
    assessment_diff_out,
    assessment_out,
    invoice_out,
)

router = APIRouter(prefix="/assessments", tags=["assessments"])


def _formula_engine(tariff: TariffVersion, conn: Connection) -> FormulaEngine:
    """Выбор движка по виду формы тарифа — тот же критерий, что в
    ``mass_recalculation._formula_engine_for``."""
    if tariff.formula_form.kind == "catala":
        return CatalaFormulaEngine(PostgresTariffArtifactRepository(conn))
    return StubFormulaEngine(PostgresTariffVersionRepository(conn))


def _load_published_tariff(conn: Connection, tariff_id: str, version: int) -> TariffVersion:
    tariff = PostgresTariffVersionRepository(conn).get(tariff_id, version)
    if tariff is None:
        raise HTTPException(404, f"tariff version ({tariff_id}, {version}) not found")
    if tariff.status is not TariffVersionStatus.PUBLISHED:
        raise HTTPException(409, f"tariff version ({tariff_id}, {version}) is not published")
    return tariff


class CalculateIn(BaseModel):
    account_id: str
    period: str  # "2026-06"
    tariff_id: str
    tariff_version: int
    metric: str = "electricity_kwh"


class CalculateOut(BaseModel):
    assessment: AssessmentOut
    invoice: InvoiceOut | None


@router.post("", status_code=201, response_model=CalculateOut)
def calculate(
    body: CalculateIn,
    dispatcher=Depends(get_dispatcher),
    config=Depends(settings),
    now: datetime = Depends(get_now),
) -> CalculateOut:
    period = BillingPeriod.parse(body.period)

    with new_connection(config.database_url) as conn:
        tariff = _load_published_tariff(conn, body.tariff_id, body.tariff_version)
        assessment, event = calculate_assessment(
            body.account_id,
            period,
            tariff,
            PostgresReferenceParameterRepository(conn),
            PostgresConsumptionStreamRepository(conn),
            _formula_engine(tariff, conn),
            PostgresBillingAssessmentRepository(conn),
            metric=body.metric,
            now=now,
            artifacts=PostgresTariffArtifactRepository(conn),
        )
    # транзакция закоммичена — сага увидит начисление
    dispatcher.dispatch(event)  # Invoice.Issue -> Account.PostCharge

    invoice = _read_invoice(config.database_url, body.account_id, period, assessment.version)
    return CalculateOut(
        assessment=assessment_out(assessment),
        invoice=invoice_out(invoice) if invoice is not None else None,
    )


class RecalculateIn(BaseModel):
    tariff_id: str
    tariff_version: int
    metric: str = "electricity_kwh"


class RecalculateOut(BaseModel):
    assessment: AssessmentOut
    diff: AssessmentDiffOut
    correcting_invoice: InvoiceOut | None


@router.post("/{account_id}/{period}/recalculate", response_model=RecalculateOut)
def recalculate(
    account_id: str,
    period: str,
    body: RecalculateIn,
    dispatcher=Depends(get_dispatcher),
    config=Depends(settings),
    now: datetime = Depends(get_now),
) -> RecalculateOut:
    billing_period = BillingPeriod.parse(period)

    with new_connection(config.database_url) as conn:
        tariff = _load_published_tariff(conn, body.tariff_id, body.tariff_version)
        result = recalculate_assessment(
            account_id,
            billing_period,
            tariff,
            PostgresReferenceParameterRepository(conn),
            PostgresConsumptionStreamRepository(conn),
            _formula_engine(tariff, conn),
            PostgresBillingAssessmentRepository(conn),
            metric=body.metric,
            now=now,
            artifacts=PostgresTariffArtifactRepository(conn),
        )
    dispatcher.dispatch(result.event)  # Invoice.IssueCorrecting -> Account.PostCorrection

    invoice = _read_invoice(config.database_url, account_id, billing_period, result.new_version.version)
    return RecalculateOut(
        assessment=assessment_out(result.new_version),
        diff=assessment_diff_out(result.diff),
        correcting_invoice=invoice_out(invoice) if invoice is not None else None,
    )


@router.get("/{account_id}/{period}", response_model=AssessmentOut)
def get_active(
    account_id: str,
    period: str,
    config=Depends(settings),
) -> AssessmentOut:
    billing_period = BillingPeriod.parse(period)
    with new_connection(config.database_url) as conn:
        assessment = PostgresBillingAssessmentRepository(conn).get_active(account_id, billing_period)
    if assessment is None:
        raise HTTPException(404, f"no active assessment for ({account_id}, {period})")
    return assessment_out(assessment)


@router.get("/{account_id}/{period}/diff", response_model=AssessmentDiffOut)
def diff(
    account_id: str,
    period: str,
    v1: int = Query(...),
    v2: int = Query(...),
    config=Depends(settings),
) -> AssessmentDiffOut:
    billing_period = BillingPeriod.parse(period)
    with new_connection(config.database_url) as conn:
        repo = PostgresBillingAssessmentRepository(conn)
        first = repo.get_version(account_id, billing_period, v1)
        second = repo.get_version(account_id, billing_period, v2)
    if first is None or second is None:
        raise HTTPException(404, f"assessment version {v1} or {v2} not found")
    return assessment_diff_out(BillingAssessment.diff(first, second))


def _read_invoice(database_url: str, account_id: str, period: BillingPeriod, version: int):
    with new_connection(database_url) as conn:
        return PostgresInvoiceRepository(conn).find_by_assessment_version(
            account_id, period, version
        )
