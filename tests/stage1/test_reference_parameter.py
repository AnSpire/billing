"""Чистые тесты агрегата ReferenceParameter — без БД.

Проверяют форму команд (что метод возвращает правильную версию/событие) и
базовую валидацию VO. Инвариант непересечения valid-time здесь НЕ
проверяется — это забота exclusion constraint'а в БД, см.
``test_reference_parameter_repository.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.domain.reference_parameter import (
    InvalidRepealDateError,
    MissingProvenanceError,
    ParameterValue,
    Provenance,
    ReferenceParameter,
    ReferenceParameterCorrected,
    ReferenceParameterRegistered,
    ReferenceParameterRepealed,
    ReferenceParameterRepository,
    TemporalValidity,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _provenance(ref: str = "vat_rate law") -> Provenance:
    return Provenance(regulation_ref=ref, document_id="doc-1", effective_date=_dt(2024, 1, 1).date())


def test_register_value_produces_actual_version_and_event() -> None:
    param = ReferenceParameter(key="vat_rate", jurisdiction="RU")

    version, event = param.register_value(
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    assert version.is_actual
    assert version.value.as_scalar() == Decimal("0.20")
    assert isinstance(event, ReferenceParameterRegistered)
    assert event.version_id == version.version_id


def test_correct_closes_superseded_and_produces_new_actual_version() -> None:
    param = ReferenceParameter(key="vat_rate", jurisdiction="RU")
    old_version, _ = param.register_value(
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    new_version, remainders, event = param.correct(
        ParameterValue.scalar(Decimal("0.10")),
        TemporalValidity(valid_from=_dt(2026, 6, 1)),
        _provenance("vat_rate law amendment"),
        now=_dt(2026, 7, 10),
        superseded=[old_version],
    )

    assert new_version.is_actual
    assert new_version.value.as_scalar() == Decimal("0.10")
    assert isinstance(event, ReferenceParameterCorrected)
    assert event.superseded_version_ids == (old_version.version_id,)
    # Correct не трогает старую версию — она не мутирует, это работа репозитория.
    assert old_version.is_actual

    # Коррекция накрыла [2026-06-01, ∞) — но старая версия действовала с
    # 2024-01-01, и про 2024-2026 коррекция ничего не говорила. Этот кусок
    # переутверждается как актуальный, иначе в valid-time образуется дыра.
    assert len(remainders) == 1
    head = remainders[0]
    assert head.is_actual
    assert head.value.as_scalar() == Decimal("0.20")  # значение старое
    assert head.validity.valid_from == _dt(2024, 1, 1)
    assert head.validity.valid_to == _dt(2026, 6, 1)  # обрезан началом коррекции


def test_correction_bounded_on_both_sides_reasserts_head_and_tail() -> None:
    """Коррекция «окном» внутри действующего интервала оставляет два обрезка."""
    param = ReferenceParameter(key="vat_rate", jurisdiction="RU")
    old_version, _ = param.register_value(
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1), valid_to=_dt(2027, 1, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    _new_version, remainders, _event = param.correct(
        ParameterValue.scalar(Decimal("0.10")),
        TemporalValidity(valid_from=_dt(2025, 1, 1), valid_to=_dt(2026, 1, 1)),
        _provenance("temporary rate cut"),
        now=_dt(2026, 7, 10),
        superseded=[old_version],
    )

    ranges = sorted((r.validity.valid_from, r.validity.valid_to) for r in remainders)
    assert ranges == [
        (_dt(2024, 1, 1), _dt(2025, 1, 1)),  # голова до коррекции
        (_dt(2026, 1, 1), _dt(2027, 1, 1)),  # хвост после коррекции
    ]
    assert all(r.value.as_scalar() == Decimal("0.20") for r in remainders)


def test_repeal_truncates_valid_to_without_changing_value() -> None:
    param = ReferenceParameter(key="vat_rate", jurisdiction="RU")
    target, _ = param.register_value(
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    truncated, event = param.repeal(
        _dt(2026, 6, 1), _provenance("repeal decree"), now=_dt(2026, 6, 1), target=target
    )

    assert truncated.value == target.value
    assert truncated.validity.valid_from == target.validity.valid_from
    assert truncated.validity.valid_to == _dt(2026, 6, 1)
    assert isinstance(event, ReferenceParameterRepealed)
    assert event.superseded_version_ids == (target.version_id,)


def test_repeal_before_valid_from_is_rejected() -> None:
    param = ReferenceParameter(key="vat_rate", jurisdiction="RU")
    target, _ = param.register_value(
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    with pytest.raises(InvalidRepealDateError):
        param.repeal(_dt(2023, 1, 1), _provenance(), now=_dt(2024, 1, 1), target=target)


def test_provenance_requires_non_blank_fields() -> None:
    with pytest.raises(MissingProvenanceError):
        Provenance(regulation_ref="", document_id="doc-1", effective_date=_dt(2024, 1, 1).date())

    with pytest.raises(MissingProvenanceError):
        Provenance(regulation_ref="vat_rate law", document_id="", effective_date=_dt(2024, 1, 1).date())


def test_temporal_validity_rejects_valid_to_before_valid_from() -> None:
    with pytest.raises(ValueError):
        TemporalValidity(valid_from=_dt(2026, 1, 1), valid_to=_dt(2024, 1, 1))


def test_repository_port_cannot_be_instantiated_directly() -> None:
    """Контракт хранилища — абстрактный порт; работать может только адаптер,
    реализовавший все методы (сейчас — PostgresReferenceParameterRepository)."""
    with pytest.raises(TypeError):
        ReferenceParameterRepository()
