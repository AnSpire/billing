"""Pydantic-схемы ответов + конвертеры из доменных объектов.

Доменные VO наружу напрямую не отдаём — у них своя семантика и они не знают про
JSON. Здесь тонкие DTO и функции ``*_out``. Инварианты представления
(PRESENTATION.md §4): деньги/количества — ``Decimal`` (Pydantic сериализует их
строкой, без потери копеек), период — строка ``"YYYY-MM"``, идентификаторы —
UUID-строкой.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from billing.domain.account import LedgerEntry
from billing.domain.billing_assessment import AssessmentDiff, BillingAssessment
from billing.domain.invoice import Invoice
from billing.domain.reference_parameter import ParameterValueVersion
from billing.domain.shared import Money
from billing.domain.tariff_version import TariffVersion
from billing.domain.consumption_stream import UsageEvent


class MoneyOut(BaseModel):
    amount: Decimal
    currency: str


def money_out(m: Money) -> MoneyOut:
    return MoneyOut(amount=m.amount, currency=m.currency)


# --- ReferenceParameter ------------------------------------------------------

class ProvenanceOut(BaseModel):
    regulation_ref: str
    document_id: str
    effective_date: str


class RefParamVersionOut(BaseModel):
    version_id: uuid.UUID
    key: str
    jurisdiction: str
    value: Decimal
    valid_from: datetime
    valid_to: datetime | None
    provenance: ProvenanceOut


def ref_param_version_out(v: ParameterValueVersion) -> RefParamVersionOut:
    return RefParamVersionOut(
        version_id=v.version_id,
        key=v.key,
        jurisdiction=v.jurisdiction,
        value=v.value.as_scalar(),
        valid_from=v.validity.valid_from,
        valid_to=v.validity.valid_to,
        provenance=ProvenanceOut(
            regulation_ref=v.provenance.regulation_ref,
            document_id=v.provenance.document_id,
            effective_date=v.provenance.effective_date.isoformat(),
        ),
    )


# --- Consumption -------------------------------------------------------------

class UsageEventOut(BaseModel):
    event_id: uuid.UUID
    account_id: str
    metric: str
    quantity: Decimal
    external_event_id: str
    recorded_at: datetime


def usage_event_out(e: UsageEvent) -> UsageEventOut:
    return UsageEventOut(
        event_id=e.event_id,
        account_id=e.account_id,
        metric=e.metric,
        quantity=e.quantity.value,
        external_event_id=e.external_event_id.value,
        recorded_at=e.recorded_at,
    )


# --- TariffVersion -----------------------------------------------------------

class ScopeInputOut(BaseModel):
    arg_name: str
    arg_type: str
    binding_kind: str
    binding_payload: dict


class TariffVersionOut(BaseModel):
    tariff_id: str
    version: int
    status: str
    scope_name: str
    formula_kind: str
    inputs: list[ScopeInputOut]
    coefficients: dict
    valid_from: datetime
    valid_to: datetime | None
    approved_by: str | None
    published_at: datetime | None


def tariff_version_out(t: TariffVersion) -> TariffVersionOut:
    return TariffVersionOut(
        tariff_id=t.tariff_id,
        version=t.version,
        status=t.status.value,
        scope_name=t.scope_manifest.scope_name,
        formula_kind=t.formula_form.kind,
        inputs=[
            ScopeInputOut(
                arg_name=i.arg_name,
                arg_type=i.arg_type,
                binding_kind=i.binding.kind,
                binding_payload=dict(i.binding.payload),
            )
            for i in t.scope_manifest.inputs
        ],
        coefficients=dict(t.coefficients.payload),
        valid_from=t.temporal_validity.valid_from,
        valid_to=t.temporal_validity.valid_to,
        approved_by=t.approved_by,
        published_at=t.published_at,
    )


# --- BillingAssessment -------------------------------------------------------

class ChargeLineOut(BaseModel):
    line_id: uuid.UUID
    rule_label: str
    amount: MoneyOut


class AssessmentOut(BaseModel):
    account_id: str
    period: str
    version: int
    status: str
    charge_lines: list[ChargeLineOut]
    total: MoneyOut


def assessment_out(a: BillingAssessment) -> AssessmentOut:
    return AssessmentOut(
        account_id=a.account_id,
        period=str(a.period),
        version=a.version,
        status=a.status.value,
        charge_lines=[
            ChargeLineOut(line_id=cl.line_id, rule_label=cl.rule_label, amount=money_out(cl.amount))
            for cl in a.charge_lines
        ],
        total=money_out(a.total),
    )


class ChargeLineDiffOut(BaseModel):
    rule_label: str
    before: MoneyOut | None
    after: MoneyOut | None
    changed: bool


class AssessmentDiffOut(BaseModel):
    account_id: str
    period: str
    line_diffs: list[ChargeLineDiffOut]
    total_before: MoneyOut
    total_after: MoneyOut
    changed_parameter_keys: list[str]


def assessment_diff_out(d: AssessmentDiff) -> AssessmentDiffOut:
    return AssessmentDiffOut(
        account_id=d.account_id,
        period=d.period,
        line_diffs=[
            ChargeLineDiffOut(
                rule_label=ld.rule_label,
                before=money_out(ld.before) if ld.before is not None else None,
                after=money_out(ld.after) if ld.after is not None else None,
                changed=ld.changed,
            )
            for ld in d.line_diffs
        ],
        total_before=money_out(d.total_before),
        total_after=money_out(d.total_after),
        changed_parameter_keys=list(d.changed_parameter_keys),
    )


# --- Invoice -----------------------------------------------------------------

class InvoiceLineOut(BaseModel):
    line_id: uuid.UUID
    rule_label: str
    amount: MoneyOut


class InvoiceOut(BaseModel):
    invoice_id: uuid.UUID
    account_id: str
    period: str
    assessment_version: int
    lines: list[InvoiceLineOut]
    total: MoneyOut
    corrects_invoice_id: uuid.UUID | None
    issued_at: datetime


def invoice_out(inv: Invoice) -> InvoiceOut:
    return InvoiceOut(
        invoice_id=inv.invoice_id,
        account_id=inv.account_id,
        period=str(inv.period),
        assessment_version=inv.assessment_version,
        lines=[
            InvoiceLineOut(line_id=ln.line_id, rule_label=ln.rule_label, amount=money_out(ln.amount))
            for ln in inv.lines
        ],
        total=money_out(inv.total),
        corrects_invoice_id=(
            inv.correction_link.original_invoice_id if inv.correction_link is not None else None
        ),
        issued_at=inv.issued_at,
    )


# --- Account -----------------------------------------------------------------

class LedgerEntryOut(BaseModel):
    entry_id: uuid.UUID
    direction: str
    entry_type: str
    amount: MoneyOut
    period: str
    invoice_id: uuid.UUID | None
    corrects_invoice_id: uuid.UUID | None
    recorded_at: datetime


def ledger_entry_out(e: LedgerEntry) -> LedgerEntryOut:
    return LedgerEntryOut(
        entry_id=e.entry_id,
        direction=e.direction.value,
        entry_type=e.entry_type.value,
        amount=money_out(e.amount),
        period=str(e.period),
        invoice_id=e.invoice_id,
        corrects_invoice_id=(
            e.correction_link.original_invoice_id if e.correction_link is not None else None
        ),
        recorded_at=e.recorded_at,
    )
