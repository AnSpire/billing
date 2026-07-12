"""Веерный пересчёт — DoD фазы 8 (use_case.md UC-7, «Перерасчёт по закону»):

- коррекция `vat_rate` триггерит `Recalculate` только у аккаунтов, чьи
  тарифы реально читают именно этот `(key, jurisdiction)` — не у всех
  подряд;
- пересчёт берёт новую версию константы, но ту же версию `TariffVersion`;
- конфликт дефолтов Catala на одном аккаунте уходит в dead-letter
  (`retryable=False`) и не останавливает веер для остальных;
- инфраструктурный сбой (пример здесь — рассинхронизация: TariffVersion,
  на который ссылается уже посчитанный BillingAssessment, пропал) тоже
  уходит в dead-letter, но с `retryable=True`, и тоже не останавливает веер.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from billing.application.billing_calculation import calculate_assessment
from billing.application.dispatcher import EventDispatcher
from billing.application.mass_recalculation import register_mass_recalculation_saga
from billing.application.tariff_validation import validate_tariff_version
from billing.domain.consumption_stream import ExternalEventId
from billing.domain.reference_parameter import ParameterValue, Provenance
from billing.domain.shared import BillingPeriod, Quantity, TemporalValidity
from billing.domain.tariff_version import (
    Binding,
    Coefficients,
    FormalizationResult,
    FormulaForm,
    ScopeInput,
    ScopeManifest,
    ScopeOutput,
    SourceText,
    TariffVersion,
)
from billing.infrastructure.db.billing_assessment_repository import (
    PostgresBillingAssessmentRepository,
)
from billing.infrastructure.db.connection import new_connection
from billing.infrastructure.db.consumption_stream_repository import (
    PostgresConsumptionStreamRepository,
)
from billing.infrastructure.db.dead_letter_store import find_dead_letters_for_account
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
from billing.infrastructure.formula_engine.fixtures import load_source


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _provenance(ref: str = "98-FZ") -> Provenance:
    return Provenance(regulation_ref=ref, document_id=_unique("doc"), effective_date=_dt(2024, 1, 1).date())


def _register_vat_rate(test_database_url: str, *, jurisdiction: str, value: str) -> None:
    with new_connection(test_database_url) as conn:
        PostgresReferenceParameterRepository(conn).register_value(
            "vat_rate",
            jurisdiction,
            ParameterValue.scalar(Decimal(value)),
            TemporalValidity(valid_from=_dt(2024, 1, 1)),
            _provenance(),
            now=_dt(2024, 1, 1),
        )


def _comfort_formalization(jurisdiction: str) -> FormalizationResult:
    return FormalizationResult(
        source_text=SourceText(text="comfort tariff", formalizer_model_version="mock-v1"),
        scope_manifest=ScopeManifest(
            scope_name="Comfort",
            inputs=(
                ScopeInput(
                    arg_name="consumption", arg_type="Decimal", binding=Binding.metric("electricity_kwh")
                ),
                ScopeInput(
                    arg_name="base_kwh", arg_type="Decimal", binding=Binding.coefficient("base_kwh")
                ),
                ScopeInput(
                    arg_name="base_rate", arg_type="Money", binding=Binding.coefficient("base_rate")
                ),
                ScopeInput(
                    arg_name="overage_multiplier",
                    arg_type="Decimal",
                    binding=Binding.coefficient("overage_multiplier"),
                ),
                ScopeInput(
                    arg_name="vat_rate", arg_type="Decimal", binding=Binding.ref_param("vat_rate", jurisdiction)
                ),
            ),
            outputs=(
                ScopeOutput(arg_name="base_amount", produces="ChargeLine"),
                ScopeOutput(arg_name="overage_amount", produces="ChargeLine"),
                ScopeOutput(arg_name="vat_amount", produces="ChargeLine"),
            ),
        ),
        formula_form=FormulaForm.catala(load_source("comfort_v1")),
        coefficients=Coefficients(
            payload={"base_kwh": "300", "base_rate": "3.20", "overage_multiplier": "1.15"}
        ),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )


def _mixed_conflict_formalization(jurisdiction: str) -> FormalizationResult:
    return FormalizationResult(
        source_text=SourceText(text="mixed conflict tariff", formalizer_model_version="mock-v1"),
        scope_manifest=ScopeManifest(
            scope_name="MixedConflict",
            inputs=(
                ScopeInput(
                    arg_name="consumption", arg_type="Decimal", binding=Binding.metric("electricity_kwh")
                ),
                ScopeInput(
                    arg_name="vat_rate", arg_type="Decimal", binding=Binding.ref_param("vat_rate", jurisdiction)
                ),
            ),
            outputs=(ScopeOutput(arg_name="amount", produces="ChargeLine"),),
        ),
        formula_form=FormulaForm.catala(load_source("mixed_conflict")),
        coefficients=Coefficients(payload={}),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )


def _publish(test_database_url: str, *, tariff_id: str, formalization: FormalizationResult) -> TariffVersion:
    """В отличие от фазы 7 (где ``TariffVersion`` живёт только в памяти вызова
    ``Calculate``), веерный пересчёт перечитывает тариф из ``TariffVersionRepository``
    по ``(tariff_id, version)`` — поэтому здесь, в отличие от фикстур фазы 7,
    КАЖДЫЙ переход обязательно сохраняется (см. ``tests/stage6``, где сага тоже
    перечитывает тарифы между шагами)."""
    with new_connection(test_database_url) as conn:
        tariff_versions = PostgresTariffVersionRepository(conn)
        reference_parameters = PostgresReferenceParameterRepository(conn)
        artifacts = PostgresTariffArtifactRepository(conn)
        draft, _ = TariffVersion.draft_from_text(tariff_id, 1, formalization, now=_dt(2026, 6, 1))
        tariff_versions.save(draft)
        validated, _ = validate_tariff_version(
            draft, reference_parameters, artifacts=artifacts, now=_dt(2026, 6, 1)
        )
        tariff_versions.save(validated)
        published, _ = validated.publish(approved_by="qa-lead", now=_dt(2026, 6, 2))
        tariff_versions.save(published)
        return published


def _record_consumption(test_database_url: str, *, account_id: str, value: str) -> None:
    with new_connection(test_database_url) as conn:
        PostgresConsumptionStreamRepository(conn).record_usage(
            account_id,
            "electricity_kwh",
            Quantity(Decimal(value), "electricity_kwh"),
            ExternalEventId(_unique("evt")),
            now=_dt(2026, 6, 15),
        )


def _calculate(test_database_url: str, *, account_id: str, period: BillingPeriod, tariff: TariffVersion):
    with new_connection(test_database_url) as conn:
        assessment, _event = calculate_assessment(
            account_id,
            period,
            tariff,
            PostgresReferenceParameterRepository(conn),
            PostgresConsumptionStreamRepository(conn),
            CatalaFormulaEngine(PostgresTariffArtifactRepository(conn)),
            PostgresBillingAssessmentRepository(conn),
            metric="electricity_kwh",
            now=_dt(2026, 7, 1),
            artifacts=PostgresTariffArtifactRepository(conn),
        )
    return assessment


def _correct_vat_rate(test_database_url: str, *, jurisdiction: str, new_value: str):
    with new_connection(test_database_url) as conn:
        _new_version, event = PostgresReferenceParameterRepository(conn).correct(
            "vat_rate",
            jurisdiction,
            ParameterValue.scalar(Decimal(new_value)),
            TemporalValidity(valid_from=_dt(2026, 6, 1)),
            _provenance("98-FZ amendment"),
            now=_dt(2026, 7, 10),
        )
    return event


def _fan_dispatcher(test_database_url: str) -> EventDispatcher:
    factory = lambda: new_connection(test_database_url)  # noqa: E731
    dispatcher = EventDispatcher(factory)
    register_mass_recalculation_saga(dispatcher, factory)
    return dispatcher


def test_correction_recalculates_only_accounts_that_actually_read_it(test_database_url: str) -> None:
    jurisdiction_ru = _unique("RU")
    jurisdiction_kz = _unique("KZ")
    period = BillingPeriod(2026, 6)

    _register_vat_rate(test_database_url, jurisdiction=jurisdiction_ru, value="0.20")
    _register_vat_rate(test_database_url, jurisdiction=jurisdiction_kz, value="0.20")
    tariff_ru = _publish(
        test_database_url,
        tariff_id=_unique("comfort-ru"),
        formalization=_comfort_formalization(jurisdiction_ru),
    )
    tariff_kz = _publish(
        test_database_url,
        tariff_id=_unique("comfort-kz"),
        formalization=_comfort_formalization(jurisdiction_kz),
    )

    account_ru = _unique("acc-ru")
    account_kz = _unique("acc-kz")
    _record_consumption(test_database_url, account_id=account_ru, value="340")
    _record_consumption(test_database_url, account_id=account_kz, value="340")

    before_ru = _calculate(test_database_url, account_id=account_ru, period=period, tariff=tariff_ru)
    _calculate(test_database_url, account_id=account_kz, period=period, tariff=tariff_kz)
    old_version_id = before_ru.calc_context.resolved_parameters[0].version_id

    event = _correct_vat_rate(test_database_url, jurisdiction=jurisdiction_ru, new_value="0.10")
    _fan_dispatcher(test_database_url).dispatch(event)

    with new_connection(test_database_url) as conn:
        recalculated_ru = PostgresBillingAssessmentRepository(conn).get_active(account_ru, period)
        still_v1_kz = PostgresBillingAssessmentRepository(conn).get_active(account_kz, period)

    assert recalculated_ru.version == 2
    assert recalculated_ru.calc_context.artifact_ref.tariff_id == tariff_ru.tariff_id
    assert recalculated_ru.calc_context.artifact_ref.version == tariff_ru.version
    assert recalculated_ru.calc_context.resolved_parameters[0].version_id != old_version_id
    assert recalculated_ru.total != before_ru.total  # vat_rate 0.20 -> 0.10 меняет сумму

    assert still_v1_kz.version == 1  # другая jurisdiction — vat_rate/RU её не касается


def test_conflict_on_one_account_dead_letters_without_halting_the_fan(test_database_url: str) -> None:
    jurisdiction = _unique("RU")
    period = BillingPeriod(2026, 6)

    _register_vat_rate(test_database_url, jurisdiction=jurisdiction, value="0.05")
    conflict_tariff = _publish(
        test_database_url,
        tariff_id=_unique("mixed"),
        formalization=_mixed_conflict_formalization(jurisdiction),
    )
    ok_tariff = _publish(
        test_database_url,
        tariff_id=_unique("comfort"),
        formalization=_comfort_formalization(jurisdiction),
    )

    account_conflict = _unique("acc-conflict")
    account_ok = _unique("acc-ok")
    # consumption=12 -> правило "consumption >= 10" истинно уже сейчас;
    # правило "vat_rate >= 0.15" пока ложно (0.05) -> конфликта нет, амаунт=$100.
    _record_consumption(test_database_url, account_id=account_conflict, value="12")
    _record_consumption(test_database_url, account_id=account_ok, value="340")

    _calculate(test_database_url, account_id=account_conflict, period=period, tariff=conflict_tariff)
    _calculate(test_database_url, account_id=account_ok, period=period, tariff=ok_tariff)

    # Коррекция поднимает vat_rate до 0.15+ -> у account_conflict ОБА правила
    # становятся истинны одновременно -> конфликт дефолтов Catala.
    event = _correct_vat_rate(test_database_url, jurisdiction=jurisdiction, new_value="0.20")
    _fan_dispatcher(test_database_url).dispatch(event)

    with new_connection(test_database_url) as conn:
        still_v1_conflict = PostgresBillingAssessmentRepository(conn).get_active(account_conflict, period)
        recalculated_ok = PostgresBillingAssessmentRepository(conn).get_active(account_ok, period)
        dead_letters = find_dead_letters_for_account(conn, account_conflict)

    assert still_v1_conflict.version == 1  # пересчёт упал -> новой версии нет
    assert len(dead_letters) == 1
    assert dead_letters[0].reason == "conflict"
    assert dead_letters[0].retryable is False
    assert dead_letters[0].key == "vat_rate"
    assert dead_letters[0].jurisdiction == jurisdiction

    assert recalculated_ok.version == 2  # веер не остановился на соседнем аккаунте


def test_infrastructure_failure_dead_letters_as_retryable_without_halting_the_fan(
    test_database_url: str,
) -> None:
    jurisdiction = _unique("RU")
    period = BillingPeriod(2026, 6)

    _register_vat_rate(test_database_url, jurisdiction=jurisdiction, value="0.20")
    broken_tariff = _publish(
        test_database_url,
        tariff_id=_unique("comfort-broken"),
        formalization=_comfort_formalization(jurisdiction),
    )
    fine_tariff = _publish(
        test_database_url,
        tariff_id=_unique("comfort-fine"),
        formalization=_comfort_formalization(jurisdiction),
    )

    account_broken = _unique("acc-broken")
    account_fine = _unique("acc-fine")
    _record_consumption(test_database_url, account_id=account_broken, value="340")
    _record_consumption(test_database_url, account_id=account_fine, value="340")

    _calculate(test_database_url, account_id=account_broken, period=period, tariff=broken_tariff)
    _calculate(test_database_url, account_id=account_fine, period=period, tariff=fine_tariff)

    # Имитация рассинхронизации: TariffVersion, на который ссылается уже
    # посчитанный BillingAssessment, пропадает (например ошибка в соседнем
    # процессе) — это НЕ доменный конфликт Catala, а нарушение ссылочной
    # целостности, ровно то, что SagaError/MissingTariffVersionError и
    # призваны сигнализировать (billing_saga.py, docstring SagaError).
    with new_connection(test_database_url) as conn:
        conn.execute(
            "DELETE FROM tariff_version WHERE tariff_id = %s AND version = %s",
            (broken_tariff.tariff_id, broken_tariff.version),
        )

    event = _correct_vat_rate(test_database_url, jurisdiction=jurisdiction, new_value="0.10")
    _fan_dispatcher(test_database_url).dispatch(event)

    with new_connection(test_database_url) as conn:
        still_v1_broken = PostgresBillingAssessmentRepository(conn).get_active(account_broken, period)
        recalculated_fine = PostgresBillingAssessmentRepository(conn).get_active(account_fine, period)
        dead_letters = find_dead_letters_for_account(conn, account_broken)

    assert still_v1_broken.version == 1
    assert len(dead_letters) == 1
    assert dead_letters[0].reason == "infrastructure"
    assert dead_letters[0].retryable is True

    assert recalculated_fine.version == 2  # веер не остановился на соседнем аккаунте
