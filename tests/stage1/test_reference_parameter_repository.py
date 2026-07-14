"""ReferenceParameter поверх реальной БД — DoD фазы 1 (PLAN.md):

- нет пересечений valid-time среди актуальных версий (пропуски допустимы);
- `Correct` не удаляет старую версию, закрывает tx_to и создаёт новую
  (сценарий UC-7: 0.20 -> 0.10);
- as-of-tx запрос возвращает историческое убеждение;
- провенанс обязателен.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.domain.reference_parameter import (
    MissingProvenanceError,
    OverlappingValidTimeError,
    ParameterValue,
    Provenance,
    ReferenceParameterNotFoundError,
    TemporalValidity,
)
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _provenance(ref: str = "98-FZ vat_rate") -> Provenance:
    return Provenance(regulation_ref=ref, document_id="doc-1", effective_date=_dt(2024, 1, 1).date())


def test_register_value_is_resolvable(db_connection) -> None:
    repo = PostgresReferenceParameterRepository(db_connection)

    repo.register_value(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    resolved = repo.resolve("vat_rate", "RU", valid_on=_dt(2025, 1, 1), as_of_tx=_dt(2025, 1, 1))

    assert resolved is not None
    assert resolved.value.as_scalar() == Decimal("0.20")


def test_no_overlap_among_actual_versions_is_enforced_by_the_database(db_connection) -> None:
    """Регистрируем без предварительного чтения текущего состояния — overlap
    здесь ловит ровно exclusion constraint, а не код приложения."""
    repo = PostgresReferenceParameterRepository(db_connection)
    repo.register_value(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    with pytest.raises(OverlappingValidTimeError):
        with db_connection.transaction():
            repo.register_value(
                "vat_rate",
                "RU",
                ParameterValue.scalar(Decimal("0.18")),
                TemporalValidity(valid_from=_dt(2024, 6, 1)),
                _provenance(),
                now=_dt(2024, 6, 1),
            )

    # Соединение осталось рабочим (savepoint откатил только конфликтующую вставку).
    resolved = repo.resolve("vat_rate", "RU", valid_on=_dt(2025, 1, 1), as_of_tx=_dt(2025, 1, 1))
    assert resolved.value.as_scalar() == Decimal("0.20")


def test_gaps_between_actual_versions_are_allowed(db_connection) -> None:
    repo = PostgresReferenceParameterRepository(db_connection)
    repo.register_value(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1), valid_to=_dt(2024, 6, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    # Не пересекается с первой версией — пропуск в валидности допустим.
    version = repo.register_value(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal("0.22")),
        TemporalValidity(valid_from=_dt(2024, 9, 1)),
        _provenance(),
        now=_dt(2024, 9, 1),
    )

    assert version.is_actual


def test_correct_closes_old_belief_and_as_of_tx_query_returns_history(db_connection) -> None:
    """Точный сценарий UC-7 из PLAN.md: старая 0.20 valid=[2024-01-01,∞)
    остаётся историческим фактом, новая 0.10 valid=[2026-06-01,∞); то, что
    система считала 5 июля (до коррекции) — 0.20, сегодня — 0.10."""
    repo = PostgresReferenceParameterRepository(db_connection)
    old_version = repo.register_value(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        _provenance(),
        now=_dt(2024, 1, 1),
    )

    correction_moment = _dt(2026, 7, 10)
    new_version, _event = repo.correct(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal("0.10")),
        TemporalValidity(valid_from=_dt(2026, 6, 1)),
        _provenance("98-FZ amendment"),
        now=correction_moment,
    )

    valid_on = _dt(2026, 6, 15)  # входит и в старый (unbounded), и в новый диапазон

    believed_before_correction = repo.resolve(
        "vat_rate", "RU", valid_on=valid_on, as_of_tx=_dt(2026, 7, 5)
    )
    believed_today = repo.resolve("vat_rate", "RU", valid_on=valid_on, as_of_tx=_dt(2026, 7, 11))

    assert believed_before_correction is not None
    assert believed_before_correction.version_id == old_version.version_id
    assert believed_before_correction.value.as_scalar() == Decimal("0.20")

    assert believed_today is not None
    assert believed_today.version_id == new_version.version_id
    assert believed_today.value.as_scalar() == Decimal("0.10")

    # Старая версия физически не удалена — она историческая (tx_to закрыт).
    old_as_of_registration = repo.resolve(
        "vat_rate", "RU", valid_on=_dt(2024, 3, 1), as_of_tx=_dt(2024, 3, 1)
    )
    assert old_as_of_registration is not None
    assert old_as_of_registration.version_id == old_version.version_id

    # Обе оси независимы, и клетка "свежий as_of × старая valid_on" — не дыра.
    # Коррекция действует с 2026-06-01 и про март 2024 ничего не говорила,
    # поэтому СЕГОДНЯ система обязана по-прежнему отвечать 0.20 на вопрос
    # "какая ставка действовала в марте 2024?". Именно этот вопрос задаёт
    # пересчёт старого периода; без переутверждения обрезка тут был бы None,
    # и пересчёт падал бы с UnresolvedReferenceParameterError.
    believed_today_about_2024 = repo.resolve(
        "vat_rate", "RU", valid_on=_dt(2024, 3, 1), as_of_tx=_dt(2026, 7, 11)
    )
    assert believed_today_about_2024 is not None
    assert believed_today_about_2024.value.as_scalar() == Decimal("0.20")
    assert believed_today_about_2024.validity.valid_to == _dt(2026, 6, 1)


def test_repeal_truncates_valid_to_via_repository(db_connection) -> None:
    repo = PostgresReferenceParameterRepository(db_connection)
    repo.register_value(
        "social_norm",
        "RU",
        ParameterValue.scalar(Decimal("6.0")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        _provenance("social norm decree"),
        now=_dt(2024, 1, 1),
    )

    repo.repeal(
        "social_norm", "RU", _dt(2026, 1, 1), _provenance("repeal decree"), now=_dt(2026, 1, 1)
    )

    still_valid_before_repeal = repo.resolve(
        "social_norm", "RU", valid_on=_dt(2025, 1, 1), as_of_tx=_dt(2026, 1, 1)
    )
    no_longer_valid_after_repeal = repo.resolve(
        "social_norm", "RU", valid_on=_dt(2026, 6, 1), as_of_tx=_dt(2026, 1, 1)
    )

    assert still_valid_before_repeal is not None
    assert no_longer_valid_after_repeal is None


def test_repeal_without_an_actual_version_raises(db_connection) -> None:
    repo = PostgresReferenceParameterRepository(db_connection)

    with pytest.raises(ReferenceParameterNotFoundError):
        repo.repeal(
            "unknown_key", "RU", _dt(2026, 1, 1), _provenance(), now=_dt(2026, 1, 1)
        )


def test_missing_provenance_rejects_the_command_before_it_reaches_the_database(
    db_connection,
) -> None:
    repo = PostgresReferenceParameterRepository(db_connection)

    with pytest.raises(MissingProvenanceError):
        repo.register_value(
            "vat_rate",
            "RU",
            ParameterValue.scalar(Decimal("0.20")),
            TemporalValidity(valid_from=_dt(2024, 1, 1)),
            Provenance(regulation_ref="", document_id="", effective_date=_dt(2024, 1, 1).date()),
            now=_dt(2024, 1, 1),
        )
