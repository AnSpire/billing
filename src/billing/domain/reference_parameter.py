"""ReferenceParameter — billing_aggregates.md §2.

Внешний референсный контекст расчёта (налоги, нормы ЖКХ, законодательные
коэффициенты). Идентичность — ``(key, jurisdiction)``. Самый чистый носитель
битемпоральности в системе: `ParameterValueVersion` занимает прямоугольник в
двумерном времени (valid-time × transaction-time).

Важно: инвариант "нет пересечений valid-time среди актуальных версий"
здесь **не проверяется в памяти агрегата** — его физически охраняет
exclusion constraint в БД (см. миграцию ``0001_reference_parameter.sql`` и
CLAUDE.md §7: "constraint не соврёт под конкурентной записью, приложение
может"). Методы агрегата ниже — чистые функции без побочных эффектов: по
текущему состоянию (то, что уже актуально) и новым данным команды они
вычисляют, что нужно записать. Чтение текущего состояния и запись — забота
репозитория (infrastructure/db/reference_parameter_repository.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from billing.domain.events import DomainEvent


class ReferenceParameterError(Exception):
    """Базовая ошибка домена ReferenceParameter."""


class MissingProvenanceError(ReferenceParameterError):
    """Provenance обязателен — без него команда не проходит (billing_aggregates.md §2)."""


class OverlappingValidTimeError(ReferenceParameterError):
    """Новая/скорректированная версия пересекается по valid-time с уже актуальной.

    В норме репозиторий сам находит и закрывает пересекающиеся актуальные
    версии перед вставкой — если это исключение всё же дошло сюда, его
    выбросил exclusion constraint в БД, то есть сработала защита от гонки
    конкурентной записи, а не ошибка расчёта в этом процессе.
    """


class InvalidRepealDateError(ReferenceParameterError):
    """Дата отмены не может быть раньше начала действия отменяемой версии."""


class ReferenceParameterNotFoundError(ReferenceParameterError):
    """Нет актуальной версии, которую можно было бы отменить/скорректировать."""


@dataclass(frozen=True)
class ParameterValue:
    """Полиморфное значение: скаляр сейчас; пороговая таблица, прогрессивная
    шкала — позже, тем же VO с другим ``kind``. Тип стабилен в пределах
    параметра (billing_aggregates.md §2)."""

    kind: str
    payload: Mapping[str, Any]

    @staticmethod
    def scalar(amount: Decimal) -> ParameterValue:
        return ParameterValue(kind="scalar", payload={"amount": str(amount)})

    def as_scalar(self) -> Decimal:
        if self.kind != "scalar":
            raise TypeError(f"value is not scalar (kind={self.kind!r})")
        return Decimal(self.payload["amount"])


@dataclass(frozen=True)
class TemporalValidity:
    """valid-time: когда норма действует по закону. ``valid_to=None`` — «до
    отмены» (общий VO, переиспользуется — billing_aggregates.md, «Общие VO»)."""

    valid_from: datetime
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be strictly after valid_from")


@dataclass(frozen=True)
class Provenance:
    """Ссылка на нормативный акт + id текста в document store + дата
    вступления. Без него обрывается объяснимость (billing_aggregates.md §2)."""

    regulation_ref: str
    document_id: str
    effective_date: date

    def __post_init__(self) -> None:
        if not self.regulation_ref or not self.document_id:
            raise MissingProvenanceError(
                "provenance requires a non-empty regulation_ref and document_id"
            )


@dataclass(frozen=True)
class ParameterValueVersion:
    """Занимает прямоугольник (valid_range × tx_range) в двумерном времени.
    На неё пиннится CalcContext (по ``version_id``)."""

    version_id: uuid.UUID
    key: str
    jurisdiction: str
    value: ParameterValue
    validity: TemporalValidity
    tx_from: datetime
    tx_to: datetime | None
    provenance: Provenance

    @property
    def is_actual(self) -> bool:
        return self.tx_to is None


@dataclass(frozen=True, kw_only=True)
class ReferenceParameterRegistered(DomainEvent):
    key: str
    jurisdiction: str
    version_id: uuid.UUID


@dataclass(frozen=True, kw_only=True)
class ReferenceParameterCorrected(DomainEvent):
    key: str
    jurisdiction: str
    version_id: uuid.UUID
    superseded_version_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, kw_only=True)
class ReferenceParameterRepealed(DomainEvent):
    key: str
    jurisdiction: str
    version_id: uuid.UUID
    superseded_version_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class ReferenceParameter:
    """Идентичность — ``(key, jurisdiction)``. См. docstring модуля: агрегат
    не хранит историю версий, его методы — чистые функции команда+состояние
    -> что записать."""

    key: str
    jurisdiction: str

    def register_value(
        self,
        value: ParameterValue,
        validity: TemporalValidity,
        provenance: Provenance,
        *,
        now: datetime,
    ) -> tuple[ParameterValueVersion, ReferenceParameterRegistered]:
        version = ParameterValueVersion(
            version_id=uuid.uuid4(),
            key=self.key,
            jurisdiction=self.jurisdiction,
            value=value,
            validity=validity,
            tx_from=now,
            tx_to=None,
            provenance=provenance,
        )
        event = ReferenceParameterRegistered(
            key=self.key, jurisdiction=self.jurisdiction, version_id=version.version_id
        )
        return version, event

    def correct(
        self,
        value: ParameterValue,
        validity: TemporalValidity,
        provenance: Provenance,
        *,
        now: datetime,
        superseded: Sequence[ParameterValueVersion],
    ) -> tuple[ParameterValueVersion, ReferenceParameterCorrected]:
        """Ретроактивная коррекция убеждения (UC-7).

        ``superseded`` — актуальные версии, чей valid-time пересекается с
        новым; их находит репозиторий ДО вызова этого метода (см. docstring
        модуля). Их ``tx_to`` закрывается моментом ``now`` — старые строки не
        удаляются и не мутируют своё ``valid_range``, они остаются
        историческим фактом ("что тогда считали правдой").
        """
        version = ParameterValueVersion(
            version_id=uuid.uuid4(),
            key=self.key,
            jurisdiction=self.jurisdiction,
            value=value,
            validity=validity,
            tx_from=now,
            tx_to=None,
            provenance=provenance,
        )
        event = ReferenceParameterCorrected(
            key=self.key,
            jurisdiction=self.jurisdiction,
            version_id=version.version_id,
            superseded_version_ids=tuple(v.version_id for v in superseded),
        )
        return version, event

    def repeal(
        self,
        repeal_from: datetime,
        provenance: Provenance,
        *,
        now: datetime,
        target: ParameterValueVersion,
    ) -> tuple[ParameterValueVersion, ReferenceParameterRepealed]:
        """Отмена нормы с даты — «удаление» через INSERT, обрезающий
        ``valid_to`` (billing_aggregates.md §2). Значение не меняется, меняется
        только конец периода действия; ``target`` — актуальная версия, которую
        репозиторий нашёл по покрытию ``repeal_from``.
        """
        if repeal_from <= target.validity.valid_from:
            raise InvalidRepealDateError(
                "repeal_from must be strictly after the target version's valid_from"
            )
        truncated_validity = TemporalValidity(
            valid_from=target.validity.valid_from, valid_to=repeal_from
        )
        version = ParameterValueVersion(
            version_id=uuid.uuid4(),
            key=self.key,
            jurisdiction=self.jurisdiction,
            value=target.value,
            validity=truncated_validity,
            tx_from=now,
            tx_to=None,
            provenance=provenance,
        )
        event = ReferenceParameterRepealed(
            key=self.key,
            jurisdiction=self.jurisdiction,
            version_id=version.version_id,
            superseded_version_ids=(target.version_id,),
        )
        return version, event
