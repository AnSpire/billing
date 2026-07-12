"""BillingAssessment — billing_aggregates.md §3. Сердце домена: расчёт для
одного аккаунта за один период. PLAN.md, фаза 4: арифметику считает простая
заглушка-калькулятор (``FormulaEngine``-порт с реализацией
``StubFormulaEngine``), не Catala — Catala подключится в фазе 7 БЕЗ смены
этого порта (billing_aggregates.md: «Агрегат BillingAssessment не знает, что
за портом стоит Catala. Если движок когда-нибудь заменят — домен не
шелохнётся»).

Как и другие агрегаты: сам он не резолвит параметры, не читает
``TariffVersion``/``ConsumptionStream`` и не ходит в БД — это работа
application-слоя (``application/billing_calculation.py``), см. также
billing_aggregates.md, «Резолвинг референсных параметров», «Кто выполняет
резолвинг: не агрегат».

Намеренное упрощение: нет отдельного поля "rule id" в ``ArtifactRef`` (в
billing_aggregates.md ``CalcContext`` пиннит "версии применённых правил
(TariffId, version, rule id)") — потому что в фазе 3 не строилась сущность
``TariffRule``, пиннить пока нечего кроме самой версии тарифа. Появится
вместе с ``TariffRule`` в фазе 7.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum

from billing.domain.events import DomainEvent
from billing.domain.shared import BillingPeriod, Quantity


class BillingAssessmentError(Exception):
    """Базовая ошибка домена BillingAssessment."""


class InvalidAssessmentTransitionError(BillingAssessmentError):
    """Recalculate вызван не на активной версии."""


class DuplicateActiveAssessmentError(BillingAssessmentError):
    """Уже есть активная версия для (account_id, period) — Calculate не
    первый, либо две параллельные попытки Recalculate столкнулись."""


class AssessmentNotFoundError(BillingAssessmentError):
    """Recalculate вызван, а активной версии для этого (account_id, period) нет."""


class UnresolvedReferenceParameterError(BillingAssessmentError):
    """RefParam-биндинг тарифа не резолвится на valid_on периода — расчёт
    невозможен (application-слой поднимает это до вызова агрегата)."""


class ArtifactNotFoundError(BillingAssessmentError):
    """FormulaEngine не смог загрузить форму по artifact_ref (TariffVersion
    с такими (tariff_id, version) не существует) — по построению не должно
    происходить, т.к. artifact_ref всегда строится из уже резолвленной
    TariffVersion, но явная ошибка лучше немого KeyError."""


class AssessmentStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Money:
    """Сумма + валюта (billing_aggregates.md, «Общие VO»). Правило округления
    сейчас не отдельное поле VO, а фиксированная политика калькулятора
    (ROUND_HALF_UP до копеек, см. infrastructure/formula_engine) — в DoD
    фазы 4 не встретилось сценария с разными правилами округления для разных
    Money, вводить настраиваемое поле было бы преждевременно."""

    amount: Decimal
    currency: str = "RUB"

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError(
                f"cannot add Money in different currencies: {self.currency!r} vs {other.currency!r}"
            )
        return Money(self.amount + other.amount, self.currency)


@dataclass(frozen=True)
class ArtifactRef:
    """Координаты формулы, применённой в расчёте — то же, что пиннит
    ``CalcContext`` (billing_aggregates.md §1/§3). ``artifact_hash`` сейчас —
    хеш JSON-заглушки ``FormulaForm`` (не Catala-исходника, его ещё нет);
    ``toolchain_version`` — версия самого́ ``StubFormulaEngine``. Форма
    неизменна с фазы 7: там ``artifact_hash`` станет ``sha256(CatalaSource)``,
    ``toolchain_version`` — версией компилятора+рантайма."""

    tariff_id: str
    version: int
    artifact_hash: str
    toolchain_version: str


@dataclass(frozen=True)
class ResolvedParameterRef:
    """Ссылка на версию, а не значение — billing_aggregates.md §3: "версии
    референсных констант... не значения, а ссылки на версии"."""

    key: str
    jurisdiction: str
    version_id: uuid.UUID


@dataclass(frozen=True)
class CalcInput:
    """Вход в ``FormulaEngine.execute``: уже резолвленные значения параметров
    + свёрнутое потребление. Собирается application-слоем."""

    resolved_parameters: Mapping[str, Decimal]
    total_quantity: Quantity


@dataclass(frozen=True)
class CalcContext:
    """Снапшот контекста расчёта — несущая конструкция воспроизводимости и
    объяснимости (billing_aggregates.md §3). ``steps`` сюда намеренно не
    входит: "не материализуется при Calculate/Recalculate" (use_case.md,
    UC-9, «Трейс регенерируется лениво, а не хранится») — регенерация лениво
    по запросу (UC-9) в этой фазе не строится, это её собственная, ещё не
    запланированная работа."""

    artifact_ref: ArtifactRef
    resolved_parameters: tuple[ResolvedParameterRef, ...]
    consumption_event_ids: tuple[uuid.UUID, ...]
    total_quantity: Quantity


@dataclass(frozen=True)
class ChargeLine:
    """Начисление — какое правило сработало и сумма (billing_aggregates.md
    §3). ``rule_label`` заменяет "rule id" из полной модели — id самой
    сущности ``TariffRule`` пока нет (см. docstring модуля)."""

    line_id: uuid.UUID
    rule_label: str
    amount: Money


@dataclass(frozen=True, kw_only=True)
class AssessmentCalculated(DomainEvent):
    account_id: str
    period: str
    version: int


@dataclass(frozen=True)
class ChargeLineDiff:
    rule_label: str
    before: Money | None
    after: Money | None

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class AssessmentDiff:
    """Построчная разница между двумя версиями (billing_aggregates.md,
    «Что осознанно НЕ агрегат» — diff это query, не состояние; UC-10)."""

    account_id: str
    period: str
    line_diffs: tuple[ChargeLineDiff, ...]
    total_before: Money
    total_after: Money
    changed_parameter_keys: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class AssessmentRecalculated(DomainEvent):
    account_id: str
    period: str
    version: int
    diff: AssessmentDiff


@dataclass(frozen=True)
class BillingAssessment:
    """Идентичность нити — ``(account_id, period)``; версии внутри, каждый
    пересчёт вытесняет (supersedes) прежнюю (billing_aggregates.md §3)."""

    account_id: str
    period: BillingPeriod
    version: int
    status: AssessmentStatus
    charge_lines: tuple[ChargeLine, ...]
    calc_context: CalcContext
    created_at: datetime

    @property
    def total(self) -> Money:
        lines = self.charge_lines
        if not lines:
            raise ValueError("cannot compute total of an assessment with no charge lines")
        result = lines[0].amount
        for line in lines[1:]:
            result = result + line.amount
        return result

    @staticmethod
    def calculate(
        account_id: str,
        period: BillingPeriod,
        charge_lines: Sequence[ChargeLine],
        calc_context: CalcContext,
        *,
        now: datetime,
    ) -> tuple[BillingAssessment, AssessmentCalculated]:
        assessment = BillingAssessment(
            account_id=account_id,
            period=period,
            version=1,
            status=AssessmentStatus.ACTIVE,
            charge_lines=tuple(charge_lines),
            calc_context=calc_context,
            created_at=now,
        )
        event = AssessmentCalculated(account_id=account_id, period=str(period), version=1)
        return assessment, event

    def recalculate(
        self,
        charge_lines: Sequence[ChargeLine],
        calc_context: CalcContext,
        *,
        now: datetime,
    ) -> tuple[BillingAssessment, BillingAssessment, AssessmentRecalculated]:
        """Возвращает (прежняя-версия-как-superseded, новая-версия, событие).
        Сам не удаляет и не мутирует прежнюю версию — только помечает статус
        в возвращённой копии; персистентность (реальный UPDATE) — забота
        репозитория."""
        if self.status != AssessmentStatus.ACTIVE:
            raise InvalidAssessmentTransitionError(
                f"cannot recalculate a version in status {self.status}"
            )
        superseded = replace(self, status=AssessmentStatus.SUPERSEDED)
        new_version = BillingAssessment(
            account_id=self.account_id,
            period=self.period,
            version=self.version + 1,
            status=AssessmentStatus.ACTIVE,
            charge_lines=tuple(charge_lines),
            calc_context=calc_context,
            created_at=now,
        )
        assessment_diff = BillingAssessment.diff(self, new_version)
        event = AssessmentRecalculated(
            account_id=self.account_id,
            period=str(self.period),
            version=new_version.version,
            diff=assessment_diff,
        )
        return superseded, new_version, event

    @staticmethod
    def diff(v1: BillingAssessment, v2: BillingAssessment) -> AssessmentDiff:
        """Query, не команда (billing_aggregates.md, «Что осознанно НЕ
        агрегат»). Строки сопоставляются по ``rule_label`` — объединение
        меток из обеих версий, отсутствующая с одной стороны сторона = None
        (правило могло появиться/исчезнуть, например превышение при
        отсутствии перерасхода)."""
        if v1.account_id != v2.account_id or v1.period != v2.period:
            raise ValueError("diff requires two versions of the same (account_id, period) thread")

        before_by_label = {line.rule_label: line.amount for line in v1.charge_lines}
        after_by_label = {line.rule_label: line.amount for line in v2.charge_lines}
        labels = sorted(set(before_by_label) | set(after_by_label))
        line_diffs = tuple(
            ChargeLineDiff(
                rule_label=label,
                before=before_by_label.get(label),
                after=after_by_label.get(label),
            )
            for label in labels
        )

        before_keys = {p.key: p.version_id for p in v1.calc_context.resolved_parameters}
        after_keys = {p.key: p.version_id for p in v2.calc_context.resolved_parameters}
        changed_parameter_keys = tuple(
            sorted(
                key
                for key in set(before_keys) & set(after_keys)
                if before_keys[key] != after_keys[key]
            )
        )

        return AssessmentDiff(
            account_id=v1.account_id,
            period=str(v1.period),
            line_diffs=line_diffs,
            total_before=v1.total,
            total_after=v2.total,
            changed_parameter_keys=changed_parameter_keys,
        )


class ConflictError(Exception):
    """Конфликт дефолтов (перекрывающиеся ``applies_when``) — в фазе 4
    заглушка не может его породить (нет условной логики), тип фиксируем
    заранее по сигнатуре ``FormulaEngine`` из billing_aggregates.md, чтобы
    порт не менялся в фазе 7."""


class FormulaEngine(ABC):
    """Порт — billing_aggregates.md, «Реестр артефактов и порт FormulaEngine»:
    ``execute(artifact_ref, CalcInput) -> (ChargeLines, steps) | ConflictError``.
    Единственная реализация сейчас — ``StubFormulaEngine`` (infrastructure);
    в фазе 7 её заменит реализация поверх скомпилированного Catala-модуля —
    без изменения этой сигнатуры (PLAN.md, «Мок-агента = порт»)."""

    @abstractmethod
    def execute(
        self, artifact_ref: ArtifactRef, calc_input: CalcInput
    ) -> tuple[tuple[ChargeLine, ...], tuple[str, ...]]: ...


@dataclass(frozen=True)
class RecalculateResult:
    superseded: BillingAssessment
    new_version: BillingAssessment
    diff: AssessmentDiff


class BillingAssessmentRepository(ABC):
    """Порт (см. PLAN.md, «Repository — порт в домене, реализация в
    infrastructure»). Единственная реализация — ``PostgresBillingAssessmentRepository``.

    Инвариант "не больше одной активной версии на (account_id, period)" в
    контракте не описан — его обеспечивает partial unique index конкретной
    реализации (см. миграцию ``0005_billing_assessment.sql``), тем же
    приёмом, что exclusion constraint у ``ReferenceParameter``."""

    @abstractmethod
    def calculate(
        self,
        account_id: str,
        period: BillingPeriod,
        charge_lines: Sequence[ChargeLine],
        calc_context: CalcContext,
        *,
        now: datetime,
    ) -> tuple[BillingAssessment, AssessmentCalculated]: ...

    @abstractmethod
    def recalculate(
        self,
        account_id: str,
        period: BillingPeriod,
        charge_lines: Sequence[ChargeLine],
        calc_context: CalcContext,
        *,
        now: datetime,
    ) -> RecalculateResult: ...

    @abstractmethod
    def get_active(self, account_id: str, period: BillingPeriod) -> BillingAssessment | None: ...

    @abstractmethod
    def get_version(
        self, account_id: str, period: BillingPeriod, version: int
    ) -> BillingAssessment | None: ...
