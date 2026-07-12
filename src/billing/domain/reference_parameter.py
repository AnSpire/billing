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
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from billing.domain.events import DomainEvent
from billing.domain.shared import TemporalValidity

__all__ = [
    "ReferenceParameterError",
    "MissingProvenanceError",
    "OverlappingValidTimeError",
    "InvalidRepealDateError",
    "ReferenceParameterNotFoundError",
    "ParameterValue",
    "TemporalValidity",
    "Provenance",
    "ParameterValueVersion",
    "ReferenceParameterRegistered",
    "ReferenceParameterCorrected",
    "ReferenceParameterRepealed",
    "ReferenceParameter",
    "ReferenceParameterRepository",
]


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
    """``valid_from``/``valid_to`` — valid-time новой версии (PLAN.md, фаза 8:
    веерный пересчёт находит затронутые ``BillingAssessment`` по пересечению
    периода с ЭТИМ диапазоном). Несём его в самом событии, а не заставляем
    обработчика саги отдельно перечитывать версию по ``version_id`` —
    события уже сейчас несут всё, что нужно для следующего шага (см.
    ``AssessmentRecalculated.diff`` — тот же приём)."""

    key: str
    jurisdiction: str
    version_id: uuid.UUID
    superseded_version_ids: tuple[uuid.UUID, ...]
    valid_from: datetime
    valid_to: datetime | None


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
            valid_from=validity.valid_from,
            valid_to=validity.valid_to,
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


class ReferenceParameterRepository(ABC):
    """Порт: контракт хранилища ReferenceParameter, не зависящий от СУБД.

    Repository — паттерн из "владеет уверенно" (CLAUDE.md §3): коллекция
    агрегатов, за которой прячется реальное хранилище. Единственная
    реализация сейчас — ``PostgresReferenceParameterRepository``
    (infrastructure/db) — но контракт объявлен здесь, в домене, а не рядом с
    ней: application-слой и саги (с фазы 6) должны зависеть от этого
    интерфейса, а не от конкретной СУБД. Стек проекта зафиксирован на
    PostgreSQL (CLAUDE.md §7) — порт здесь не "на случай смены базы", а
    чтобы прикладной код и тесты сага-оркестрации не тянули за собой
    psycopg/SQL-детали, которых для координации команд не нужно знать.

    Сам инвариант непересечения valid-time в контракте не описан и не может
    быть описан абстрактно — он физически охраняется exclusion constraint'ом
    конкретной реализации (см. docstring класса выше и миграцию
    ``0001_reference_parameter.sql``). Другая реализация обязана обеспечить
    тот же инвариант своими средствами, а не полагаться на этот порт.
    """

    @abstractmethod
    def register_value(
        self,
        key: str,
        jurisdiction: str,
        value: ParameterValue,
        validity: TemporalValidity,
        provenance: Provenance,
        *,
        now: datetime,
    ) -> ParameterValueVersion: ...

    @abstractmethod
    def correct(
        self,
        key: str,
        jurisdiction: str,
        value: ParameterValue,
        validity: TemporalValidity,
        provenance: Provenance,
        *,
        now: datetime,
    ) -> tuple[ParameterValueVersion, ReferenceParameterCorrected]:
        """Возвращает и версию, и событие — в отличие от ``register_value``/
        ``repeal`` ниже (PLAN.md, фаза 8): ``ReferenceParameterCorrected``
        нужен вызывающему коду, чтобы продиспетчить веерный пересчёт
        (``application/mass_recalculation.py``). ``register_value``/``repeal``
        своего потребителя события пока не завели — не расширяем их сигнатуру
        молча "на будущее" (CLAUDE.md §8, п.1)."""
        ...

    @abstractmethod
    def repeal(
        self,
        key: str,
        jurisdiction: str,
        repeal_from: datetime,
        provenance: Provenance,
        *,
        now: datetime,
    ) -> ParameterValueVersion: ...

    @abstractmethod
    def resolve(
        self, key: str, jurisdiction: str, valid_on: datetime, as_of_tx: datetime
    ) -> ParameterValueVersion | None: ...
