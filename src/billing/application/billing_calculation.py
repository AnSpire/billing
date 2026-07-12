"""Application Service: ``BillingAssessment.Calculate``/``Recalculate``.

Реализует шаги 2–7 из billing_aggregates.md, «Резолвинг референсных
параметров» → «Порядок в прикладном слое (вызывается сагой)». Шаг 1
("Резолв активной (TariffId, version) для аккаунта на период") сюда не
входит: в принятой модели нет агрегата, который хранит связку
account→tariff (ни `billing_aggregates.md`, ни `use_case.md` его не
описывают — Account появится только в фазе 5, и даже он не заявлен как
владелец этой связи). Поэтому ``tariff`` здесь — явный параметр вызывающего
кода, а не результат внутреннего резолвинга; если/когда такая связка
понадобится, она заводится отдельным явным решением, а не тихо
достраивается здесь.

Это не сага (нет цепочки записей через события, нет отложенной
согласованности) — синхронная координация чтения трёх чужих для
``BillingAssessment`` источников (``TariffVersion`` уже передан, значения
``ReferenceParameter`` и снапшот ``ConsumptionStream`` читаются здесь) перед
одной командой над одним агрегатом. Пишем только в ``BillingAssessment``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from billing.domain.billing_assessment import (
    ArtifactRef,
    AssessmentCalculated,
    BillingAssessment,
    BillingAssessmentRepository,
    CalcContext,
    CalcInput,
    ChargeLine,
    FormulaEngine,
    RecalculateResult,
    ResolvedParameterRef,
    UnresolvedReferenceParameterError,
)
from billing.domain.consumption_stream import ConsumptionStreamRepository
from billing.domain.reference_parameter import ReferenceParameterRepository
from billing.domain.shared import BillingPeriod, Quantity
from billing.domain.tariff_version import TariffVersion


def _artifact_ref_for(tariff: TariffVersion) -> ArtifactRef:
    """``artifact_hash`` = sha256 заглушки формы (не Catala-исходника — его
    ещё нет, см. docstring ``ArtifactRef``); ``toolchain_version`` — версия
    самого́ ``StubFormulaEngine``, а не компилятора Catala."""
    body_json = json.dumps(
        {"kind": tariff.formula_form.kind, "body": tariff.formula_form.body}, sort_keys=True
    )
    artifact_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
    return ArtifactRef(
        tariff_id=tariff.tariff_id,
        version=tariff.version,
        artifact_hash=artifact_hash,
        toolchain_version="stub-formula-engine-v1",
    )


def _build_charge_lines_and_context(
    tariff: TariffVersion,
    reference_parameters: ReferenceParameterRepository,
    consumption: ConsumptionStreamRepository,
    formula_engine: FormulaEngine,
    *,
    account_id: str,
    period: BillingPeriod,
    metric: str,
    now: datetime,
) -> tuple[tuple[ChargeLine, ...], CalcContext]:
    resolved_refs: list[ResolvedParameterRef] = []
    resolved_values: dict[str, object] = {}
    for binding in tariff.scope_manifest.ref_param_bindings():
        key, jurisdiction = binding.ref_param_key
        resolved = reference_parameters.resolve(
            key, jurisdiction, valid_on=period.valid_on, as_of_tx=now
        )
        if resolved is None:
            raise UnresolvedReferenceParameterError(
                f"{key}/{jurisdiction} does not resolve for {period} (valid_on={period.valid_on})"
            )
        resolved_refs.append(
            ResolvedParameterRef(key=key, jurisdiction=jurisdiction, version_id=resolved.version_id)
        )
        resolved_values[key] = resolved.value.as_scalar()

    events = consumption.events_for(account_id, metric, period=period)
    total = sum((event.quantity.value for event in events), start=Decimal(0))
    total_quantity = Quantity(value=total, metric=metric)

    artifact_ref = _artifact_ref_for(tariff)
    calc_input = CalcInput(resolved_parameters=resolved_values, total_quantity=total_quantity)
    charge_lines, _steps = formula_engine.execute(artifact_ref, calc_input)
    # steps намеренно отбрасывается здесь — "не материализуется при
    # Calculate/Recalculate" (use_case.md, UC-9).

    calc_context = CalcContext(
        artifact_ref=artifact_ref,
        resolved_parameters=tuple(resolved_refs),
        consumption_event_ids=tuple(event.event_id for event in events),
        total_quantity=total_quantity,
    )
    return charge_lines, calc_context


def calculate_assessment(
    account_id: str,
    period: BillingPeriod,
    tariff: TariffVersion,
    reference_parameters: ReferenceParameterRepository,
    consumption: ConsumptionStreamRepository,
    formula_engine: FormulaEngine,
    assessments: BillingAssessmentRepository,
    *,
    metric: str,
    now: datetime,
) -> tuple[BillingAssessment, AssessmentCalculated]:
    charge_lines, calc_context = _build_charge_lines_and_context(
        tariff,
        reference_parameters,
        consumption,
        formula_engine,
        account_id=account_id,
        period=period,
        metric=metric,
        now=now,
    )
    return assessments.calculate(account_id, period, charge_lines, calc_context, now=now)


def recalculate_assessment(
    account_id: str,
    period: BillingPeriod,
    tariff: TariffVersion,
    reference_parameters: ReferenceParameterRepository,
    consumption: ConsumptionStreamRepository,
    formula_engine: FormulaEngine,
    assessments: BillingAssessmentRepository,
    *,
    metric: str,
    now: datetime,
) -> RecalculateResult:
    charge_lines, calc_context = _build_charge_lines_and_context(
        tariff,
        reference_parameters,
        consumption,
        formula_engine,
        account_id=account_id,
        period=period,
        metric=metric,
        now=now,
    )
    return assessments.recalculate(account_id, period, charge_lines, calc_context, now=now)
