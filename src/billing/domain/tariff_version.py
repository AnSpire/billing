"""TariffVersion — billing_aggregates.md §1.

Формализованный тариф. Идентичность — ``(tariff_id, version)``: одна версия —
граница консистентности (правила внутри неё должны быть непротиворечивы,
разные версии независимы). В этой фазе (PLAN.md, фаза 3) агрегат — «держатель
данных»: без Catala, без исполнения формулы, без разбора на отдельные
``TariffRule``.

Два намеренных упрощения относительно полной модели из billing_aggregates.md,
стоит проговорить явно:

1. **Нет сущности ``TariffRule``.** Сейчас "форма расчёта" — непрозрачный VO
   ``FormulaForm`` (JSON-заглушка), а не список правил с ``Predicate``/
   ``RuleBinding``. Разбор появится в фазе 4, когда заглушка-калькулятор
   получит первого реального потребителя этой структуры — раньше это была бы
   умозрительная схема без кода, который её использует.
2. **``CatalaSource`` замещён ``FormulaForm``.** PLAN.md, «Форма и числа —
   раздельно»: «CatalaSource, а до Catala — заглушка». Название ``CatalaSource``
   зарезервировано для фазы 7, когда там будет настоящий текст на Catala;
   использовать его сейчас для заглушки было бы враньём в коде.

Как и в ``ReferenceParameter``/``ConsumptionStream``: агрегат ничего не знает
про БД, его методы — чистые функции. Проверка "reads из ScopeManifest
резолвятся в реестре ReferenceParameter" требует чтения **чужого** агрегата —
это работа application-слоя (``application/tariff_validation.py``), не
домена: одна транзакция меняет один агрегат, но читать через границу можно
(CLAUDE.md, чек-лист §8).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from billing.domain.events import DomainEvent
from billing.domain.shared import TemporalValidity


class TariffVersionError(Exception):
    """Базовая ошибка домена TariffVersion."""


class InvalidTariffVersionTransitionError(TariffVersionError):
    """Validate/Publish вызваны не из того статуса, откуда положено по жизненному циклу."""


class UnresolvedScopeBindingError(TariffVersionError):
    """Есть RefParam-биндинг из ScopeManifest, который не резолвится в реестре ReferenceParameter."""


class TariffVersionImmutableError(TariffVersionError):
    """Опубликованная версия неизменяема — попытка перезаписать её отклонена."""


class TariffVersionStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"


@dataclass(frozen=True)
class SourceText:
    """Исходный человеческий текст + версия модели, которая его формализовала
    (провенанс формализации, billing_aggregates.md §1)."""

    text: str
    formalizer_model_version: str

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("text must not be empty")
        if not self.formalizer_model_version:
            raise ValueError("formalizer_model_version must not be empty")


@dataclass(frozen=True)
class Binding:
    """Что читает вход скоупа: ``RefParam(key, jurisdiction) | Metric(name) |
    AccountState(field)`` (billing_aggregates.md §1, ``ScopeManifest``).
    Тот же приём, что у ``ParameterValue`` в ReferenceParameter: ``kind`` +
    JSON-``payload`` вместо трёх отдельных классов под один union."""

    kind: str
    payload: Mapping[str, Any]

    @staticmethod
    def ref_param(key: str, jurisdiction: str) -> Binding:
        return Binding(kind="ref_param", payload={"key": key, "jurisdiction": jurisdiction})

    @staticmethod
    def metric(name: str) -> Binding:
        return Binding(kind="metric", payload={"name": name})

    @staticmethod
    def account_state(field: str) -> Binding:
        return Binding(kind="account_state", payload={"field": field})

    @property
    def ref_param_key(self) -> tuple[str, str]:
        if self.kind != "ref_param":
            raise TypeError(f"binding is not ref_param (kind={self.kind!r})")
        return self.payload["key"], self.payload["jurisdiction"]


@dataclass(frozen=True)
class ScopeInput:
    arg_name: str
    arg_type: str
    binding: Binding


@dataclass(frozen=True)
class ScopeOutput:
    arg_name: str
    produces: str  # "ChargeLine" | "State"


@dataclass(frozen=True)
class ScopeManifest:
    """Явный контракт скоупа — делает ``reads`` проверяемым инвариантом,
    потому что скомпилированный артефакт сам по себе не говорит рантайму,
    какие ``ReferenceParameter`` ему нужны (billing_aggregates.md §1)."""

    scope_name: str
    inputs: tuple[ScopeInput, ...] = ()
    outputs: tuple[ScopeOutput, ...] = ()

    def ref_param_bindings(self) -> tuple[Binding, ...]:
        return tuple(i.binding for i in self.inputs if i.binding.kind == "ref_param")


@dataclass(frozen=True)
class FormulaForm:
    """Заглушка формы расчёта — см. docstring модуля, пункт 2. ``kind``
    стабилен в пределах параметра, как и у ``ParameterValue``."""

    kind: str
    body: Mapping[str, Any]

    @staticmethod
    def stub(body: Mapping[str, Any]) -> FormulaForm:
        return FormulaForm(kind="stub", body=dict(body))


@dataclass(frozen=True)
class Coefficients:
    """Коэффициенты тарифа — VO внутри TariffVersion (PLAN.md, «Три ведра
    чисел»). Неизменяемы; valid-time берётся от ``TemporalValidity`` версии,
    отдельной битемпоральности здесь нет."""

    payload: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class TariffDrafted(DomainEvent):
    tariff_id: str
    version: int


@dataclass(frozen=True, kw_only=True)
class TariffValidated(DomainEvent):
    tariff_id: str
    version: int


@dataclass(frozen=True, kw_only=True)
class TariffVersionPublished(DomainEvent):
    tariff_id: str
    version: int


@dataclass(frozen=True)
class FormalizationResult:
    """Всё, что нужно, чтобы завести черновик ``TariffVersion`` —
    результат ``ContractFormalizer.formalize``."""

    source_text: SourceText
    scope_manifest: ScopeManifest
    formula_form: FormulaForm
    coefficients: Coefficients
    temporal_validity: TemporalValidity


@dataclass(frozen=True)
class TariffVersion:
    """Идентичность — ``(tariff_id, version)``."""

    tariff_id: str
    version: int
    status: TariffVersionStatus
    source_text: SourceText
    scope_manifest: ScopeManifest
    formula_form: FormulaForm
    coefficients: Coefficients
    temporal_validity: TemporalValidity
    created_at: datetime
    published_at: datetime | None = None

    @staticmethod
    def draft_from_text(
        tariff_id: str,
        version: int,
        formalization: FormalizationResult,
        *,
        now: datetime,
    ) -> tuple[TariffVersion, TariffDrafted]:
        if version < 1:
            raise ValueError("version must start at 1")
        draft = TariffVersion(
            tariff_id=tariff_id,
            version=version,
            status=TariffVersionStatus.DRAFT,
            source_text=formalization.source_text,
            scope_manifest=formalization.scope_manifest,
            formula_form=formalization.formula_form,
            coefficients=formalization.coefficients,
            temporal_validity=formalization.temporal_validity,
            created_at=now,
        )
        event = TariffDrafted(tariff_id=tariff_id, version=version)
        return draft, event

    def validate(
        self, *, unresolved_ref_param_bindings: Sequence[Binding], now: datetime
    ) -> tuple[TariffVersion, TariffValidated]:
        """``unresolved_ref_param_bindings`` — то, что НЕ резолвится в реестре
        ReferenceParameter; их находит application-слой ДО вызова этого
        метода (см. docstring модуля) — агрегат сам в БД не ходит."""
        if self.status != TariffVersionStatus.DRAFT:
            raise InvalidTariffVersionTransitionError(
                f"cannot validate a version in status {self.status}"
            )
        if unresolved_ref_param_bindings:
            raise UnresolvedScopeBindingError(
                f"{len(unresolved_ref_param_bindings)} RefParam binding(s) do not resolve "
                "in the ReferenceParameter registry"
            )
        validated = replace(self, status=TariffVersionStatus.VALIDATED)
        event = TariffValidated(tariff_id=self.tariff_id, version=self.version)
        return validated, event

    def publish(self, *, now: datetime) -> tuple[TariffVersion, TariffVersionPublished]:
        if self.status != TariffVersionStatus.VALIDATED:
            raise InvalidTariffVersionTransitionError(
                f"cannot publish a version in status {self.status}"
            )
        published = replace(self, status=TariffVersionStatus.PUBLISHED, published_at=now)
        event = TariffVersionPublished(tariff_id=self.tariff_id, version=self.version)
        return published, event


class ContractFormalizer(ABC):
    """Порт: "AI формализует человеческий текст" (billing_aggregates.md §1,
    команда ``DraftFromText``). Единственная реализация сейчас —
    ``FixtureContractFormalizer`` (infrastructure/formalization) — ручная
    заглушка вместо настоящего AI-агента (PLAN.md, «Мок-агента = порт»)."""

    @abstractmethod
    def formalize(self, contract_doc: str) -> FormalizationResult: ...


class TariffVersionRepository(ABC):
    """Порт (см. PLAN.md, «Repository — порт в домене, реализация в
    infrastructure»). Единственная реализация — ``PostgresTariffVersionRepository``.

    Неизменяемость после ``Publish`` в контракте не описана и не может быть
    описана абстрактно — её обеспечивает конкретная реализация (см. docstring
    класса ``TariffVersionImmutableError`` и миграцию
    ``0003_tariff_version.sql``)."""

    @abstractmethod
    def save(self, version: TariffVersion) -> None: ...

    @abstractmethod
    def get(self, tariff_id: str, version: int) -> TariffVersion | None: ...
