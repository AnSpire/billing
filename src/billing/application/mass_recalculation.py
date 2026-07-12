"""Веерный пересчёт — PLAN.md, фаза 8; use_case.md UC-7.

Продолжение той же идеи, что и ``application/billing_saga.py`` (обработчик
события на ``EventDispatcher``), но с другой формой: не «один аккаунт — один
шаг», а «одно событие ``ReferenceParameterCorrected`` — N независимых шагов»
(billing_aggregates.md: «Инстансов миллионы, они независимы, поэтому массовый
пересчёт параллелится»). Отсюда ключевое структурное отличие от
``billing_saga``: обработчик здесь сам открывает СВОЁ соединение на каждый
затронутый аккаунт (через переданный ``connection_factory``), а не
довольствуется одним ``conn``, которое даёт ему ``EventDispatcher.dispatch``
— иначе сбой на аккаунте №17 из 40 000 откатил бы (или держал в одной
транзакции) всё остальное, что прямо противоречит DoD фазы 8.

``conn``, который передаёт диспетчер, используется только для ОДНОГО
read-only запроса — найти список затронутых `BillingAssessment`. Дальше
для каждого — своя транзакция, свой independent commit/rollback.

**Обработка ошибок веера** (use_case.md UC-7) — два разных смысла, оба ведут
в dead-letter, но с разным ``retryable``:

- ``ConflictError`` (Catala: перекрывающиеся ``applies_when`` для НОВОГО
  значения параметра) — детерминированная ошибка формализации, ретраить
  бессмысленно, нужен разбор человеком → ``retryable=False``;
- любая другая ошибка (например ``SagaError`` — ссылка на
  ``TariffVersion``, которой почему-то нет) — трактуется как инфраструктурный
  сбой, кандидат на ретрай → ``retryable=True``. Сам ретрай (checkpoint/resume)
  — открытый вопрос №2 use_case.md, эта фаза его не решает, только
  фиксирует различие.

Ни то, ни другое не прерывает веер: падение одного аккаунта не мешает
обработать остальных (цикл ниже намеренно не поднимает исключение наружу).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from psycopg import Connection

from billing.application.billing_calculation import recalculate_assessment
from billing.application.dispatcher import ConnectionFactory, EventDispatcher, EventHandler
from billing.domain.billing_assessment import ConflictError
from billing.domain.events import DomainEvent
from billing.domain.reference_parameter import ReferenceParameterCorrected
from billing.domain.tariff_version import TariffVersion
from billing.infrastructure.db.billing_assessment_repository import (
    PostgresBillingAssessmentRepository,
)
from billing.infrastructure.db.consumption_stream_repository import (
    PostgresConsumptionStreamRepository,
)
from billing.infrastructure.db.dead_letter_store import record_dead_letter
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)
from billing.infrastructure.db.tariff_artifact_repository import (
    PostgresTariffArtifactRepository,
)
from billing.infrastructure.db.tariff_version_repository import (
    PostgresTariffVersionRepository,
)
from billing.infrastructure.formula_engine.catala_formula_engine import CatalaFormulaEngine
from billing.infrastructure.formula_engine.stub_formula_engine import StubFormulaEngine


class MissingTariffVersionError(Exception):
    """``TariffVersion``, на который ссылается ``ArtifactRef`` уже
    посчитанного ``BillingAssessment``, не найден. Как и ``SagaError`` в
    billing_saga.py — сигнал рассинхронизации, не ожидаемый бизнес-случай,
    поэтому в вере трактуется как инфраструктурный, а не доменный сбой."""


def _formula_engine_for(tariff: TariffVersion, conn: Connection):
    """Тот же выбор, что описан в docstring ``CatalaFormulaEngine`` — по
    ``formula_form.kind`` тарифа, а не по конфигурации. Единственное место в
    проекте, которому реально нужно выбирать движок за произвольный
    ``TariffVersion`` (остальной код либо весь на stub, либо весь на catala,
    см. ``infrastructure/formula_engine/catala_formula_engine.py:6-8``)."""
    if tariff.formula_form.kind == "catala":
        return CatalaFormulaEngine(PostgresTariffArtifactRepository(conn))
    return StubFormulaEngine(PostgresTariffVersionRepository(conn))


def make_handle_reference_parameter_corrected(
    connection_factory: ConnectionFactory,
) -> EventHandler:
    def handle_reference_parameter_corrected(
        event: ReferenceParameterCorrected, conn: Connection
    ) -> Sequence[DomainEvent]:
        affected = PostgresBillingAssessmentRepository(conn).find_active_by_ref_param_and_period_range(
            event.key, event.jurisdiction, event.valid_from, event.valid_to
        )

        follow_up: list[DomainEvent] = []
        for assessment in affected:
            now = datetime.now(timezone.utc)
            try:
                with connection_factory() as item_conn:
                    tariff = PostgresTariffVersionRepository(item_conn).get(
                        assessment.calc_context.artifact_ref.tariff_id,
                        assessment.calc_context.artifact_ref.version,
                    )
                    if tariff is None:
                        raise MissingTariffVersionError(
                            f"no TariffVersion ({assessment.calc_context.artifact_ref.tariff_id!r}, "
                            f"{assessment.calc_context.artifact_ref.version!r}) referenced by "
                            f"({assessment.account_id!r}, {assessment.period})"
                        )
                    result = recalculate_assessment(
                        assessment.account_id,
                        assessment.period,
                        tariff,
                        PostgresReferenceParameterRepository(item_conn),
                        PostgresConsumptionStreamRepository(item_conn),
                        _formula_engine_for(tariff, item_conn),
                        PostgresBillingAssessmentRepository(item_conn),
                        metric=assessment.calc_context.total_quantity.metric,
                        now=now,
                        artifacts=PostgresTariffArtifactRepository(item_conn),
                    )
                follow_up.append(result.event)
            except ConflictError as exc:
                with connection_factory() as dl_conn:
                    record_dead_letter(
                        dl_conn,
                        account_id=assessment.account_id,
                        period=str(assessment.period),
                        key=event.key,
                        jurisdiction=event.jurisdiction,
                        reason="conflict",
                        retryable=False,
                        detail=str(exc),
                        now=now,
                    )
            except Exception as exc:  # noqa: BLE001 — намеренно: см. docstring модуля
                with connection_factory() as dl_conn:
                    record_dead_letter(
                        dl_conn,
                        account_id=assessment.account_id,
                        period=str(assessment.period),
                        key=event.key,
                        jurisdiction=event.jurisdiction,
                        reason="infrastructure",
                        retryable=True,
                        detail=str(exc),
                        now=now,
                    )
        return follow_up

    return handle_reference_parameter_corrected


def register_mass_recalculation_saga(
    dispatcher: EventDispatcher, connection_factory: ConnectionFactory
) -> None:
    dispatcher.subscribe(
        ReferenceParameterCorrected,
        make_handle_reference_parameter_corrected(connection_factory),
    )
