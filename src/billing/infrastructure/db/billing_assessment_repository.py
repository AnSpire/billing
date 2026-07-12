"""Реализация порта ``BillingAssessmentRepository`` (domain) поверх psycopg3.

"Атомарно" в DoD фазы 4 ("Recalculate атомарно помечает старую версию
superseded и создаёт новую") обеспечивается тем, что UPDATE и INSERT ниже
выполняются на одном ``Connection`` в транзакции вызывающего кода (тот же
паттерн, что ``PostgresReferenceParameterRepository.correct``), а конкурентная
гонка двух Recalculate — partial unique index'ом
``billing_assessment_one_active_per_period`` (см. миграцию
``0005_billing_assessment.sql``): вторая транзакция получит
``UniqueViolation``, а не тихо создаст вторую активную версию.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg.errors
from psycopg import Connection
from psycopg.types.json import Jsonb

from billing.domain.billing_assessment import (
    ArtifactRef,
    AssessmentCalculated,
    AssessmentNotFoundError,
    AssessmentStatus,
    BillingAssessment,
    BillingAssessmentRepository,
    CalcContext,
    ChargeLine,
    DuplicateActiveAssessmentError,
    Money,
    RecalculateResult,
    ResolvedParameterRef,
)
from billing.domain.shared import BillingPeriod, Quantity

_SELECT_COLUMNS = """
    account_id, period_year, period_month, version, status, charge_lines, calc_context, created_at
"""


class PostgresBillingAssessmentRepository(BillingAssessmentRepository):
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def calculate(
        self,
        account_id: str,
        period: BillingPeriod,
        charge_lines: Sequence[ChargeLine],
        calc_context: CalcContext,
        *,
        now: datetime,
    ) -> tuple[BillingAssessment, AssessmentCalculated]:
        assessment, event = BillingAssessment.calculate(
            account_id, period, charge_lines, calc_context, now=now
        )
        self._insert(assessment)
        return assessment, event

    def recalculate(
        self,
        account_id: str,
        period: BillingPeriod,
        charge_lines: Sequence[ChargeLine],
        calc_context: CalcContext,
        *,
        now: datetime,
    ) -> RecalculateResult:
        previous = self.get_active(account_id, period)
        if previous is None:
            raise AssessmentNotFoundError(
                f"no active BillingAssessment for ({account_id!r}, {period})"
            )
        superseded, new_version, event = previous.recalculate(charge_lines, calc_context, now=now)
        self._mark_superseded(superseded)
        self._insert(new_version)
        return RecalculateResult(superseded=superseded, new_version=new_version, event=event)

    def get_active(self, account_id: str, period: BillingPeriod) -> BillingAssessment | None:
        row = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS} FROM billing_assessment
            WHERE account_id = %s AND period_year = %s AND period_month = %s AND status = 'active'
            """,
            (account_id, period.year, period.month),
        ).fetchone()
        return self._row_to_assessment(row) if row else None

    def get_version(
        self, account_id: str, period: BillingPeriod, version: int
    ) -> BillingAssessment | None:
        row = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS} FROM billing_assessment
            WHERE account_id = %s AND period_year = %s AND period_month = %s AND version = %s
            """,
            (account_id, period.year, period.month, version),
        ).fetchone()
        return self._row_to_assessment(row) if row else None

    def _insert(self, assessment: BillingAssessment) -> None:
        try:
            self._conn.execute(
                """
                INSERT INTO billing_assessment (
                    account_id, period_year, period_month, version, status,
                    charge_lines, calc_context, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    assessment.account_id,
                    assessment.period.year,
                    assessment.period.month,
                    assessment.version,
                    assessment.status.value,
                    Jsonb([_charge_line_to_json(line) for line in assessment.charge_lines]),
                    Jsonb(_calc_context_to_json(assessment.calc_context)),
                    assessment.created_at,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateActiveAssessmentError(
                f"({assessment.account_id!r}, {assessment.period}) already has an active "
                f"version, or version {assessment.version} already exists"
            ) from exc

    def _mark_superseded(self, assessment: BillingAssessment) -> None:
        self._conn.execute(
            """
            UPDATE billing_assessment SET status = 'superseded'
            WHERE account_id = %s AND period_year = %s AND period_month = %s
              AND version = %s AND status = 'active'
            """,
            (assessment.account_id, assessment.period.year, assessment.period.month, assessment.version),
        )

    @staticmethod
    def _row_to_assessment(row: tuple) -> BillingAssessment:
        (
            account_id,
            period_year,
            period_month,
            version,
            status,
            charge_lines,
            calc_context,
            created_at,
        ) = row
        return BillingAssessment(
            account_id=account_id,
            period=BillingPeriod(year=period_year, month=period_month),
            version=version,
            status=AssessmentStatus(status),
            charge_lines=tuple(_charge_line_from_json(line) for line in charge_lines),
            calc_context=_calc_context_from_json(calc_context),
            created_at=created_at,
        )


def _charge_line_to_json(line: ChargeLine) -> dict[str, Any]:
    return {
        "line_id": str(line.line_id),
        "rule_label": line.rule_label,
        "amount": {"amount": str(line.amount.amount), "currency": line.amount.currency},
    }


def _charge_line_from_json(data: dict[str, Any]) -> ChargeLine:
    return ChargeLine(
        line_id=uuid.UUID(data["line_id"]),
        rule_label=data["rule_label"],
        amount=Money(amount=Decimal(data["amount"]["amount"]), currency=data["amount"]["currency"]),
    )


def _calc_context_to_json(context: CalcContext) -> dict[str, Any]:
    return {
        "artifact_ref": {
            "tariff_id": context.artifact_ref.tariff_id,
            "version": context.artifact_ref.version,
            "artifact_hash": context.artifact_ref.artifact_hash,
            "toolchain_version": context.artifact_ref.toolchain_version,
        },
        "resolved_parameters": [
            {"key": p.key, "jurisdiction": p.jurisdiction, "version_id": str(p.version_id)}
            for p in context.resolved_parameters
        ],
        "consumption_event_ids": [str(event_id) for event_id in context.consumption_event_ids],
        "total_quantity": {
            "value": str(context.total_quantity.value),
            "metric": context.total_quantity.metric,
        },
    }


def _calc_context_from_json(data: dict[str, Any]) -> CalcContext:
    artifact_ref_data = data["artifact_ref"]
    return CalcContext(
        artifact_ref=ArtifactRef(
            tariff_id=artifact_ref_data["tariff_id"],
            version=artifact_ref_data["version"],
            artifact_hash=artifact_ref_data["artifact_hash"],
            toolchain_version=artifact_ref_data["toolchain_version"],
        ),
        resolved_parameters=tuple(
            ResolvedParameterRef(
                key=p["key"], jurisdiction=p["jurisdiction"], version_id=uuid.UUID(p["version_id"])
            )
            for p in data["resolved_parameters"]
        ),
        consumption_event_ids=tuple(
            uuid.UUID(event_id) for event_id in data["consumption_event_ids"]
        ),
        total_quantity=Quantity(
            value=Decimal(data["total_quantity"]["value"]), metric=data["total_quantity"]["metric"]
        ),
    )
