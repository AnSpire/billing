"""Чистые тесты агрегата BillingAssessment — без БД.

Резолвинг параметров, свёртка потребления и вызов FormulaEngine здесь НЕ
проверяются (это работа application-слоя, см.
``test_billing_calculation.py``) — только форма команд, переходы и diff.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.domain.billing_assessment import (
    ArtifactRef,
    AssessmentCalculated,
    AssessmentRecalculated,
    AssessmentStatus,
    BillingAssessment,
    CalcContext,
    ChargeLine,
    InvalidAssessmentTransitionError,
    Money,
    ResolvedParameterRef,
)
from billing.domain.shared import BillingPeriod, Quantity


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _artifact_ref(version: int = 1) -> ArtifactRef:
    return ArtifactRef(
        tariff_id="comfort", version=version, artifact_hash="hash-1", toolchain_version="stub-v1"
    )


def _calc_context(*, vat_version_id: uuid.UUID | None = None, quantity: Decimal = Decimal(340)) -> CalcContext:
    return CalcContext(
        artifact_ref=_artifact_ref(),
        resolved_parameters=(
            ResolvedParameterRef(
                key="vat_rate", jurisdiction="RU", version_id=vat_version_id or uuid.uuid4()
            ),
        ),
        consumption_event_ids=(uuid.uuid4(),),
        total_quantity=Quantity(value=quantity, metric="electricity_kwh"),
    )


def _lines(base: str = "960.00", overage: str = "147.20", vat: str = "221.44") -> tuple[ChargeLine, ...]:
    return (
        ChargeLine(line_id=uuid.uuid4(), rule_label="base", amount=Money(Decimal(base))),
        ChargeLine(line_id=uuid.uuid4(), rule_label="overage", amount=Money(Decimal(overage))),
        ChargeLine(line_id=uuid.uuid4(), rule_label="vat", amount=Money(Decimal(vat))),
    )


def test_calculate_produces_active_v1_and_event() -> None:
    assessment, event = BillingAssessment.calculate(
        "acc-4471", BillingPeriod(2026, 6), _lines(), _calc_context(), now=_dt(2026, 7, 1)
    )

    assert assessment.version == 1
    assert assessment.status is AssessmentStatus.ACTIVE
    assert assessment.total == Money(Decimal("1328.64"))
    assert isinstance(event, AssessmentCalculated)
    assert event.version == 1


def test_recalculate_supersedes_old_and_creates_new_version() -> None:
    v1, _ = BillingAssessment.calculate(
        "acc-4471", BillingPeriod(2026, 6), _lines(), _calc_context(), now=_dt(2026, 7, 1)
    )

    superseded, v2, event = v1.recalculate(
        _lines(overage="0.00", base="992.00", vat="198.40"),
        _calc_context(quantity=Decimal(310)),
        now=_dt(2026, 7, 10),
    )

    assert superseded.status is AssessmentStatus.SUPERSEDED
    assert v2.status is AssessmentStatus.ACTIVE
    assert v2.version == 2
    assert isinstance(event, AssessmentRecalculated)
    assert event.version == 2
    # Прежняя версия не мутирует — v1 остаётся, каким было (иммутабельность объекта).
    assert v1.status is AssessmentStatus.ACTIVE


def test_recalculate_rejects_a_non_active_version() -> None:
    v1, _ = BillingAssessment.calculate(
        "acc-4471", BillingPeriod(2026, 6), _lines(), _calc_context(), now=_dt(2026, 7, 1)
    )
    superseded, _v2, _event = v1.recalculate(_lines(), _calc_context(), now=_dt(2026, 7, 10))

    with pytest.raises(InvalidAssessmentTransitionError):
        superseded.recalculate(_lines(), _calc_context(), now=_dt(2026, 7, 11))


def test_diff_matches_uc10_example() -> None:
    """billing_aggregates.md / use_case.md UC-10: vat_rate 0.20 → 0.10,
    base/overage не изменились, vat_amount и total изменились."""
    vat_v1 = uuid.uuid4()
    vat_v2 = uuid.uuid4()
    v1, _ = BillingAssessment.calculate(
        "acc-4471",
        BillingPeriod(2026, 6),
        _lines(vat="221.44"),
        _calc_context(vat_version_id=vat_v1),
        now=_dt(2026, 7, 1),
    )
    _superseded, v2, _event = v1.recalculate(
        _lines(vat="110.72"), _calc_context(vat_version_id=vat_v2), now=_dt(2026, 7, 10)
    )

    result = BillingAssessment.diff(v1, v2)

    by_label = {d.rule_label: d for d in result.line_diffs}
    assert by_label["base"].changed is False
    assert by_label["overage"].changed is False
    assert by_label["vat"].before == Money(Decimal("221.44"))
    assert by_label["vat"].after == Money(Decimal("110.72"))
    assert by_label["vat"].changed is True
    assert result.total_before == Money(Decimal("1328.64"))
    # use_case.md UC-10 пишет total=1218.56, но 1107.20 + 110.72 = 1217.92 —
    # арифметическая неточность в документе; используем корректную сумму.
    assert result.total_after == Money(Decimal("1217.92"))
    assert result.changed_parameter_keys == ("vat_rate",)


def test_diff_requires_the_same_thread() -> None:
    v1, _ = BillingAssessment.calculate(
        "acc-4471", BillingPeriod(2026, 6), _lines(), _calc_context(), now=_dt(2026, 7, 1)
    )
    other, _ = BillingAssessment.calculate(
        "acc-9999", BillingPeriod(2026, 6), _lines(), _calc_context(), now=_dt(2026, 7, 1)
    )

    with pytest.raises(ValueError):
        BillingAssessment.diff(v1, other)


def test_diff_reports_a_line_that_only_exists_in_one_version() -> None:
    v1, _ = BillingAssessment.calculate(
        "acc-4471",
        BillingPeriod(2026, 6),
        (ChargeLine(line_id=uuid.uuid4(), rule_label="base", amount=Money(Decimal("960.00"))),),
        _calc_context(quantity=Decimal(250)),
        now=_dt(2026, 7, 1),
    )
    _superseded, v2, _event = v1.recalculate(_lines(), _calc_context(quantity=Decimal(340)), now=_dt(2026, 7, 10))

    result = BillingAssessment.diff(v1, v2)

    by_label = {d.rule_label: d for d in result.line_diffs}
    assert by_label["overage"].before is None
    assert by_label["overage"].after == Money(Decimal("147.20"))
    assert by_label["overage"].changed is True
