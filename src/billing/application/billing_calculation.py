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
from billing.domain.tariff_artifact import TariffArtifactRepository
from billing.domain.tariff_version import TariffVersion


def _artifact_ref_for(
    tariff: TariffVersion, artifacts: TariffArtifactRepository | None
) -> ArtifactRef:
    """Для ``kind == "catala"`` (фаза 7) — пин **реального** артефакта из
    реестра ``tariff_artifact``: то, что действительно скомпилировано и
    провалидировано, а не пересчитанный на лету хеш (billing_aggregates.md
    §3: ``CalcContext`` пиннит версии, а не выводит их заново).

    Для ``kind == "stub"`` (фазы 3–6, без реестра) — прежнее поведение: хеш
    JSON-заглушки формы, ``toolchain_version`` заглушки-калькулятора. Ничего
    не меняется для существующих вызовов, не передающих ``artifacts``."""
    if tariff.formula_form.kind == "catala":
        if artifacts is None:
            raise ValueError(
                "resolving an ArtifactRef for a catala-kind TariffVersion requires "
                "a TariffArtifactRepository"
            )
        artifact = artifacts.get(tariff.tariff_id, tariff.version)
        if artifact is None:
            raise ValueError(
                f"no TariffArtifact for ({tariff.tariff_id!r}, {tariff.version!r}) — "
                "was Validate ever run for this version?"
            )
        return ArtifactRef(
            tariff_id=tariff.tariff_id,
            version=tariff.version,
            artifact_hash=artifact.source_hash,
            toolchain_version=artifact.compiler_version,
        )

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
    artifacts: TariffArtifactRepository | None = None,
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

    for scope_input in tariff.scope_manifest.inputs:
        if scope_input.binding.kind == "coefficient":
            name = scope_input.binding.payload["name"]
            resolved_values[name] = Decimal(str(tariff.coefficients.payload[name]))

    events = consumption.events_for(account_id, metric, period=period)
    total = sum((event.quantity.value for event in events), start=Decimal(0))
    total_quantity = Quantity(value=total, metric=metric)

    artifact_ref = _artifact_ref_for(tariff, artifacts)
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
    artifacts: TariffArtifactRepository | None = None,
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
        artifacts=artifacts,
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
    artifacts: TariffArtifactRepository | None = None,
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
        artifacts=artifacts,
    )
    return assessments.recalculate(account_id, period, charge_lines, calc_context, now=now)
