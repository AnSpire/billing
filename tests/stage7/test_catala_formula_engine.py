"""CatalaFormulaEngine поверх скомпилированного артефакта — DoD фазы 7:

- расчёт UC-4 идёт через FormulaEngine.execute на скомпилированном артефакте
  и совпадает с эталонными golden-парами (числа из use_case.md UC-4/UC-10:
  960.00 / 147.20 / 221.44, итого 1328.64);
- artifact_hash + toolchain_version фиксируются в CalcContext.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from billing.application.billing_calculation import calculate_assessment
from billing.application.tariff_validation import validate_tariff_version
from billing.domain.billing_assessment import Money
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
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)
from billing.infrastructure.db.tariff_artifact_repository import (
    PostgresTariffArtifactRepository,
)
from billing.infrastructure.formula_engine import catala_toolchain as toolchain
from billing.infrastructure.formula_engine.catala_formula_engine import CatalaFormulaEngine
from billing.infrastructure.formula_engine.fixtures import load_source


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


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
                    arg_name="vat_rate",
                    arg_type="Decimal",
                    binding=Binding.ref_param("vat_rate", jurisdiction),
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


def _published_comfort_v1(test_database_url: str, *, jurisdiction: str, tariff_id: str) -> TariffVersion:
    with new_connection(test_database_url) as conn:
        PostgresReferenceParameterRepository(conn).register_value(
            "vat_rate",
            jurisdiction,
            ParameterValue.scalar(Decimal("0.20")),
            TemporalValidity(valid_from=_dt(2024, 1, 1)),
            Provenance(regulation_ref="98-FZ", document_id="doc-1", effective_date=_dt(2024, 1, 1).date()),
            now=_dt(2024, 1, 1),
        )
    with new_connection(test_database_url) as conn:
        reference_parameters = PostgresReferenceParameterRepository(conn)
        artifacts = PostgresTariffArtifactRepository(conn)
        draft, _ = TariffVersion.draft_from_text(
            tariff_id, 1, _comfort_formalization(jurisdiction), now=_dt(2026, 6, 1)
        )
        validated, _ = validate_tariff_version(
            draft, reference_parameters, artifacts=artifacts, now=_dt(2026, 6, 1)
        )
        published, _ = validated.publish(approved_by="qa-lead", now=_dt(2026, 6, 2))
        return published


def test_calculate_via_real_catala_matches_uc4_golden_numbers(test_database_url: str) -> None:
    jurisdiction = _unique("RU")
    tariff_id = _unique("comfort")
    account_id = _unique("acc")
    period = BillingPeriod(2026, 6)
    tariff = _published_comfort_v1(test_database_url, jurisdiction=jurisdiction, tariff_id=tariff_id)

    with new_connection(test_database_url) as conn:
        PostgresConsumptionStreamRepository(conn).record_usage(
            account_id,
            "electricity_kwh",
            Quantity(Decimal(340), "electricity_kwh"),
            ExternalEventId(_unique("evt")),
            now=_dt(2026, 6, 15),
        )

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

    by_label = {line.rule_label: line.amount for line in assessment.charge_lines}
    assert by_label["base_amount"] == Money(Decimal("960.00"))
    assert by_label["overage_amount"] == Money(Decimal("147.20"))
    assert by_label["vat_amount"] == Money(Decimal("221.44"))
    assert assessment.total == Money(Decimal("1328.64"))

    # artifact_hash + toolchain_version зафиксированы в CalcContext и это
    # именно sha256 РЕАЛЬНОГО catala-исходника (не хеш JSON-заглушки).
    expected_hash = toolchain.source_hash(load_source("comfort_v1"))
    assert assessment.calc_context.artifact_ref.artifact_hash == expected_hash
    assert assessment.calc_context.artifact_ref.toolchain_version == toolchain.compiler_version()
    assert assessment.calc_context.artifact_ref.tariff_id == tariff_id
    assert assessment.calc_context.artifact_ref.version == 1
