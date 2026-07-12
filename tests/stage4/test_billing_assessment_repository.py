"""BillingAssessment поверх реальной БД — DoD фазы 4:

- Recalculate атомарно помечает старую версию superseded и создаёт новую;
- не больше одной активной версии на (account_id, period), даже под гонкой.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.domain.billing_assessment import (
    AssessmentNotFoundError,
    AssessmentStatus,
    ArtifactRef,
    CalcContext,
    ChargeLine,
    DuplicateActiveAssessmentError,
    Money,
    ResolvedParameterRef,
)
from billing.domain.shared import BillingPeriod, Quantity
from billing.infrastructure.db.billing_assessment_repository import (
    PostgresBillingAssessmentRepository,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _calc_context(quantity: Decimal = Decimal(340)) -> CalcContext:
    return CalcContext(
        artifact_ref=ArtifactRef(
            tariff_id="comfort", version=1, artifact_hash="hash-1", toolchain_version="stub-v1"
        ),
        resolved_parameters=(
            ResolvedParameterRef(key="vat_rate", jurisdiction="RU", version_id=uuid.uuid4()),
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


def test_calculate_persists_v1_as_active(db_connection) -> None:
    repo = PostgresBillingAssessmentRepository(db_connection)
    period = BillingPeriod(2026, 6)

    assessment, event = repo.calculate("acc-4471", period, _lines(), _calc_context(), now=_dt(2026, 7, 1))

    assert assessment.version == 1
    assert event.version == 1
    fetched = repo.get_active("acc-4471", period)
    assert fetched is not None
    assert fetched.status is AssessmentStatus.ACTIVE


def test_calculate_twice_for_the_same_period_is_rejected(db_connection) -> None:
    repo = PostgresBillingAssessmentRepository(db_connection)
    period = BillingPeriod(2026, 6)
    repo.calculate("acc-4471", period, _lines(), _calc_context(), now=_dt(2026, 7, 1))

    with pytest.raises(DuplicateActiveAssessmentError):
        with db_connection.transaction():
            repo.calculate("acc-4471", period, _lines(), _calc_context(), now=_dt(2026, 7, 1))


def test_recalculate_atomically_supersedes_and_creates_new_version(db_connection) -> None:
    repo = PostgresBillingAssessmentRepository(db_connection)
    period = BillingPeriod(2026, 6)
    repo.calculate("acc-4471", period, _lines(), _calc_context(), now=_dt(2026, 7, 1))

    result = repo.recalculate(
        "acc-4471",
        period,
        _lines(base="992.00", overage="0.00", vat="198.40"),
        _calc_context(quantity=Decimal(310)),
        now=_dt(2026, 7, 10),
    )

    assert result.superseded.version == 1
    assert result.new_version.version == 2
    assert result.diff.total_before != result.diff.total_after

    active = repo.get_active("acc-4471", period)
    assert active is not None
    assert active.version == 2

    v1 = repo.get_version("acc-4471", period, 1)
    assert v1 is not None
    assert v1.status is AssessmentStatus.SUPERSEDED
    # v1 остаётся историческим фактом с прежними ChargeLine — не переписан.
    assert {line.rule_label: line.amount for line in v1.charge_lines}["base"] == Money(Decimal("960.00"))


def test_recalculate_without_an_active_version_raises(db_connection) -> None:
    repo = PostgresBillingAssessmentRepository(db_connection)

    with pytest.raises(AssessmentNotFoundError):
        repo.recalculate("acc-unknown", BillingPeriod(2026, 6), _lines(), _calc_context(), now=_dt(2026, 7, 10))


def test_only_one_active_version_per_period_even_under_a_race(db_connection) -> None:
    """Частичный уникальный индекс — вторая попытка "создать активную версию
    поверх активной" (минуя нормальный recalculate) должна быть отклонена
    БД, а не тихо задвоить активные строки."""
    repo = PostgresBillingAssessmentRepository(db_connection)
    period = BillingPeriod(2026, 6)
    repo.calculate("acc-4471", period, _lines(), _calc_context(), now=_dt(2026, 7, 1))

    # Симулируем гонку: второй "calculate" пытается вставить активную версию
    # с другим номером напрямую через repository._insert (минуя проверку
    # get_active, как это было бы при двух параллельных транзакциях).
    from dataclasses import replace

    from billing.domain.billing_assessment import BillingAssessment

    racing_version, _event = BillingAssessment.calculate(
        "acc-4471", period, _lines(), _calc_context(), now=_dt(2026, 7, 1)
    )
    racing_version = replace(racing_version, version=2)

    with pytest.raises(DuplicateActiveAssessmentError):
        with db_connection.transaction():
            repo._insert(racing_version)  # noqa: SLF001 — намеренно бьём мимо recalculate()

    active_versions = [
        v for v in (repo.get_version("acc-4471", period, 1), repo.get_version("acc-4471", period, 2))
        if v is not None and v.status is AssessmentStatus.ACTIVE
    ]
    assert len(active_versions) == 1
