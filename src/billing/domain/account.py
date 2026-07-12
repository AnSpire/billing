"""Account — billing_aggregates.md §5. Лицевой счёт: журнал проводок
(append-only), баланс — производная, не хранимое поле. Идентичность —
``AccountId``.

**Трактовка «двойной записи»** (явно не расписана в billing_aggregates.md —
решение принято в обсуждении с пользователем при реализации фазы 5): не
классическая бухгалтерия со счетами-корреспондентами (плана счетов в модели
нет — ни в одном UC он не встречается), а знак направления на каждой
проводке. ``LedgerEntry.direction`` (DEBIT увеличивает то, что должен
аккаунт; CREDIT — уменьшает) всегда хранится явно, а не подразумевается
знаком числа — баланс это свёртка с учётом знака, а не просто сумма
"amount"-полей без семантики. Это легче полного плана счетов, но не даёт
проводке существовать без явно объявленной стороны.

Как и другие тонкие агрегаты (``ReferenceParameter``, ``ConsumptionStream``):
не хранит журнал в памяти, методы — чистые функции. ``balance``/
``projected_balance`` — тоже чистые функции над уже прочитанным списком
проводок (репозиторий их читает, домен считает) — ровно то, что означает
"баланс не хранимое поле".
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from billing.domain.events import DomainEvent
from billing.domain.shared import BillingPeriod, CorrectionLink, Money


class AccountError(Exception):
    """Базовая ошибка домена Account."""


class InvalidLedgerEntryStateError(AccountError):
    """ConfirmPending вызван не на pending-проводке или не того аккаунта."""


class EntryDirection(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class EntryType(str, Enum):
    PENDING = "pending"
    POSTED = "posted"


@dataclass(frozen=True)
class LedgerEntry:
    """Проводка. Неизменяема после записи (billing_aggregates.md §5).
    ``amount`` — всегда неотрицательная величина; знак задаёт ``direction``."""

    entry_id: uuid.UUID
    account_id: str
    direction: EntryDirection
    entry_type: EntryType
    amount: Money
    period: BillingPeriod
    invoice_id: uuid.UUID | None
    correction_link: CorrectionLink | None
    confirms_pending_entry_id: uuid.UUID | None
    recorded_at: datetime

    def __post_init__(self) -> None:
        if self.amount.amount < 0:
            raise ValueError("LedgerEntry.amount must be a non-negative magnitude")

    @property
    def signed_amount(self) -> Decimal:
        return self.amount.amount if self.direction is EntryDirection.DEBIT else -self.amount.amount


@dataclass(frozen=True, kw_only=True)
class PendingReserved(DomainEvent):
    account_id: str
    entry_id: uuid.UUID
    amount: Money


@dataclass(frozen=True, kw_only=True)
class EntryPosted(DomainEvent):
    account_id: str
    entry_id: uuid.UUID
    amount: Money


@dataclass(frozen=True, kw_only=True)
class CorrectionPosted(DomainEvent):
    account_id: str
    entry_id: uuid.UUID
    amount: Money
    original_invoice_id: uuid.UUID


@dataclass(frozen=True)
class Account:
    account_id: str

    def reserve_pending(
        self, amount: Money, period: BillingPeriod, *, now: datetime
    ) -> tuple[LedgerEntry, PendingReserved]:
        entry = LedgerEntry(
            entry_id=uuid.uuid4(),
            account_id=self.account_id,
            direction=EntryDirection.DEBIT,
            entry_type=EntryType.PENDING,
            amount=amount,
            period=period,
            invoice_id=None,
            correction_link=None,
            confirms_pending_entry_id=None,
            recorded_at=now,
        )
        event = PendingReserved(account_id=self.account_id, entry_id=entry.entry_id, amount=amount)
        return entry, event

    def confirm_pending(
        self, pending: LedgerEntry, *, now: datetime
    ) -> tuple[LedgerEntry, EntryPosted]:
        """«On-timer фиксация»: пересчёта нет, сумма берётся из самой
        pending-проводки — это НЕ правка (pending остаётся в журнале как
        есть), а новая posted-проводка со ссылкой ``confirms_pending_entry_id``
        (billing_aggregates.md §5, «двойная запись» + «проводки неизменяемы»
        не позволяют мутировать pending на месте)."""
        if pending.account_id != self.account_id:
            raise InvalidLedgerEntryStateError(
                f"pending entry belongs to account {pending.account_id!r}, not {self.account_id!r}"
            )
        if pending.entry_type != EntryType.PENDING:
            raise InvalidLedgerEntryStateError(
                f"entry {pending.entry_id} is not pending (status={pending.entry_type})"
            )
        entry = LedgerEntry(
            entry_id=uuid.uuid4(),
            account_id=self.account_id,
            direction=pending.direction,
            entry_type=EntryType.POSTED,
            amount=pending.amount,
            period=pending.period,
            invoice_id=None,
            correction_link=None,
            confirms_pending_entry_id=pending.entry_id,
            recorded_at=now,
        )
        event = EntryPosted(account_id=self.account_id, entry_id=entry.entry_id, amount=entry.amount)
        return entry, event

    def post_charge(
        self, invoice_id: uuid.UUID, amount: Money, period: BillingPeriod, *, now: datetime
    ) -> tuple[LedgerEntry, EntryPosted]:
        entry = LedgerEntry(
            entry_id=uuid.uuid4(),
            account_id=self.account_id,
            direction=EntryDirection.DEBIT,
            entry_type=EntryType.POSTED,
            amount=amount,
            period=period,
            invoice_id=invoice_id,
            correction_link=None,
            confirms_pending_entry_id=None,
            recorded_at=now,
        )
        event = EntryPosted(account_id=self.account_id, entry_id=entry.entry_id, amount=amount)
        return entry, event

    def post_correction(
        self,
        invoice_id: uuid.UUID,
        original_invoice_id: uuid.UUID,
        delta: Decimal,
        period: BillingPeriod,
        *,
        now: datetime,
    ) -> tuple[LedgerEntry, CorrectionPosted]:
        """``delta`` — со знаком (billing_aggregates.md/UC-6: ``delta=-116₽``
        уменьшает начисление). Отрицательный delta → CREDIT, положительный →
        DEBIT; ``amount`` на проводке всегда хранит модуль."""
        direction = EntryDirection.CREDIT if delta < 0 else EntryDirection.DEBIT
        amount = Money(abs(delta))
        entry = LedgerEntry(
            entry_id=uuid.uuid4(),
            account_id=self.account_id,
            direction=direction,
            entry_type=EntryType.POSTED,
            amount=amount,
            period=period,
            invoice_id=invoice_id,
            correction_link=CorrectionLink(original_invoice_id=original_invoice_id),
            confirms_pending_entry_id=None,
            recorded_at=now,
        )
        event = CorrectionPosted(
            account_id=self.account_id,
            entry_id=entry.entry_id,
            amount=amount,
            original_invoice_id=original_invoice_id,
        )
        return entry, event

    @staticmethod
    def balance(entries: Sequence[LedgerEntry]) -> Money:
        """Подтверждённый баланс — только posted-проводки (UC-5: pending «не
        входит в подтверждённый баланс»)."""
        posted = [e for e in entries if e.entry_type is EntryType.POSTED]
        return _sum_signed(posted)

    @staticmethod
    def projected_balance(entries: Sequence[LedgerEntry]) -> Money:
        """Прогнозный баланс — posted плюс ещё не «закрытые» posted-проводкой
        pending за тот же период. Как только за период появляется posted
        (через PostCharge или ConfirmPending — не важно, каким путём), любая
        pending-проводка за тот же период перестаёт учитываться: это и есть
        защита от задвоения из DoD фазы 5, не привязанная к конкретному
        механизму подтверждения."""
        posted = [e for e in entries if e.entry_type is EntryType.POSTED]
        posted_periods = {e.period for e in posted}
        pending = [
            e for e in entries if e.entry_type is EntryType.PENDING and e.period not in posted_periods
        ]
        return _sum_signed(posted + pending)


def _sum_signed(entries: Sequence[LedgerEntry]) -> Money:
    if not entries:
        return Money(Decimal(0))
    currency = entries[0].amount.currency
    total = sum((e.signed_amount for e in entries), start=Decimal(0))
    return Money(total, currency)


class AccountRepository(ABC):
    """Порт (см. PLAN.md, «Repository — порт в домене, реализация в
    infrastructure»). Единственная реализация — ``PostgresAccountRepository``.

    Как и у ``Invoice``: в контракте нет ни одного метода обновления —
    ``LedgerEntry`` неизменяема на уровне самого набора операций, которые
    можно с ней сделать через порт."""

    @abstractmethod
    def reserve_pending(
        self, account_id: str, amount: Money, period: BillingPeriod, *, now: datetime
    ) -> tuple[LedgerEntry, PendingReserved]: ...

    @abstractmethod
    def confirm_pending(
        self, pending_entry_id: uuid.UUID, *, now: datetime
    ) -> tuple[LedgerEntry, EntryPosted]: ...

    @abstractmethod
    def post_charge(
        self, account_id: str, invoice_id: uuid.UUID, amount: Money, period: BillingPeriod, *, now: datetime
    ) -> tuple[LedgerEntry, EntryPosted]: ...

    @abstractmethod
    def post_correction(
        self,
        account_id: str,
        invoice_id: uuid.UUID,
        original_invoice_id: uuid.UUID,
        delta: Decimal,
        period: BillingPeriod,
        *,
        now: datetime,
    ) -> tuple[LedgerEntry, CorrectionPosted]: ...

    @abstractmethod
    def entries_for(self, account_id: str) -> list[LedgerEntry]: ...

    @abstractmethod
    def find_by_invoice(self, invoice_id: uuid.UUID) -> LedgerEntry | None:
        """Нужен с фазы 6: обработчик ``InvoiceIssued``/
        ``CorrectingInvoiceIssued`` проверяет этим запросом, не проведён ли
        уже платёж/корректировка по этому ``invoice_id`` — идемпотентность
        повторной доставки, тем же приёмом, что
        ``InvoiceRepository.find_by_assessment_version``."""
        ...

    @abstractmethod
    def balance(self, account_id: str) -> Money: ...

    @abstractmethod
    def projected_balance(self, account_id: str) -> Money: ...
