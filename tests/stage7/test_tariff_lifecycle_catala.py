"""Полный цикл TariffVersion с реальным Catala — DoD фазы 7:

- Validate падает на ошибке компиляции как на ошибке формализации (а не на
  Publish); Publish активирует уже собранный артефакт;
- Publish невозможен без ручного approve.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.application.tariff_validation import validate_tariff_version
from billing.domain.reference_parameter import ParameterValue, Provenance
from billing.domain.shared import TemporalValidity
from billing.domain.tariff_artifact import TariffArtifactRepository
from billing.domain.tariff_version import (
    Binding,
    Coefficients,
    FormalizationResult,
    FormulaForm,
    PublishRequiresApprovalError,
    ScopeInput,
    ScopeManifest,
    SourceText,
    TariffVersion,
    TariffVersionStatus,
)
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)
from billing.infrastructure.db.tariff_artifact_repository import (
    PostgresTariffArtifactRepository,
)
from billing.infrastructure.formula_engine import catala_toolchain as toolchain
from billing.infrastructure.formula_engine.fixtures import load_source


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _comfort_formalization(jurisdiction: str) -> FormalizationResult:
    return FormalizationResult(
        source_text=SourceText(
            text="база 300 кВт·ч по 3.20, превышение с надбавкой 15%, НДС отдельной ставкой",
            formalizer_model_version="mock-v1",
        ),
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
            outputs=(),
        ),
        formula_form=FormulaForm.catala(load_source("comfort_v1")),
        coefficients=Coefficients(
            payload={"base_kwh": "300", "base_rate": "3.20", "overage_multiplier": "1.15"}
        ),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )


def _broken_formalization() -> FormalizationResult:
    return FormalizationResult(
        source_text=SourceText(text="намеренно ломаем компиляцию", formalizer_model_version="mock-v1"),
        scope_manifest=ScopeManifest(scope_name="BrokenTypecheck"),
        formula_form=FormulaForm.catala(load_source("broken_typecheck")),
        coefficients=Coefficients(payload={}),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )


def test_validate_fails_on_compilation_error_not_on_publish(db_connection) -> None:
    reference_parameters = PostgresReferenceParameterRepository(db_connection)
    artifacts = PostgresTariffArtifactRepository(db_connection)
    tariff_id = _unique("broken")
    draft, _ = TariffVersion.draft_from_text(tariff_id, 1, _broken_formalization(), now=_dt(2026, 6, 1))

    with pytest.raises(toolchain.CatalaCompilationError):
        validate_tariff_version(draft, reference_parameters, artifacts=artifacts, now=_dt(2026, 6, 1))

    # Ничего не провалидировано и не опубликовано — draft остался draft'ом,
    # публиковать нечего (ошибка всплыла на Validate, до Publish).
    assert draft.status is TariffVersionStatus.DRAFT
    assert artifacts.get(tariff_id, 1) is None


def test_publish_requires_manual_approval(db_connection) -> None:
    reference_parameters = PostgresReferenceParameterRepository(db_connection)
    artifacts = PostgresTariffArtifactRepository(db_connection)
    jurisdiction = _unique("RU")
    tariff_id = _unique("comfort")
    reference_parameters.register_value(
        "vat_rate",
        jurisdiction,
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        Provenance(regulation_ref="98-FZ", document_id="doc-1", effective_date=_dt(2024, 1, 1).date()),
        now=_dt(2024, 1, 1),
    )
    draft, _ = TariffVersion.draft_from_text(
        tariff_id, 1, _comfort_formalization(jurisdiction), now=_dt(2026, 6, 1)
    )
    validated, _ = validate_tariff_version(
        draft, reference_parameters, artifacts=artifacts, now=_dt(2026, 6, 1)
    )

    with pytest.raises(PublishRequiresApprovalError):
        validated.publish(approved_by="", now=_dt(2026, 6, 2))

    # Не прошедший approve тариф остаётся validated, не published.
    assert validated.status is TariffVersionStatus.VALIDATED


def test_publish_activates_the_already_compiled_artifact(db_connection) -> None:
    reference_parameters = PostgresReferenceParameterRepository(db_connection)
    artifacts = PostgresTariffArtifactRepository(db_connection)
    jurisdiction = _unique("RU")
    tariff_id = _unique("comfort")
    reference_parameters.register_value(
        "vat_rate",
        jurisdiction,
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        Provenance(regulation_ref="98-FZ", document_id="doc-1", effective_date=_dt(2024, 1, 1).date()),
        now=_dt(2024, 1, 1),
    )
    draft, _ = TariffVersion.draft_from_text(
        tariff_id, 1, _comfort_formalization(jurisdiction), now=_dt(2026, 6, 1)
    )

    validated, _ = validate_tariff_version(
        draft, reference_parameters, artifacts=artifacts, now=_dt(2026, 6, 1)
    )
    artifact_before_publish = artifacts.get(tariff_id, 1)
    published, event = validated.publish(approved_by="qa-lead", now=_dt(2026, 6, 2))

    assert artifact_before_publish is not None  # артефакт уже собран на Validate
    assert published.status is TariffVersionStatus.PUBLISHED
    assert published.approved_by == "qa-lead"
    assert event.tariff_id == tariff_id
    # Publish не пересобирает — тот же артефакт, что был после Validate.
    artifact_after_publish = artifacts.get(tariff_id, 1)
    assert artifact_after_publish.source_hash == artifact_before_publish.source_hash
