"""Чистые тесты агрегата TariffVersion и мок-формализатора — без БД.

Резолвинг против ReferenceParameter здесь НЕ проверяется (это работа
application-слоя, см. ``test_tariff_validation.py``) — только форма команд и
переходы жизненного цикла.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from billing.domain.shared import TemporalValidity
from billing.domain.tariff_version import (
    Binding,
    Coefficients,
    ContractFormalizer,
    FormalizationResult,
    FormulaForm,
    InvalidTariffVersionTransitionError,
    ScopeInput,
    ScopeManifest,
    SourceText,
    TariffDrafted,
    TariffValidated,
    TariffVersion,
    TariffVersionPublished,
    TariffVersionRepository,
    TariffVersionStatus,
    UnresolvedScopeBindingError,
)
from billing.infrastructure.formalization.fixture_contract_formalizer import (
    FixtureContractFormalizer,
    UnknownContractError,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _formalization(vat_rate_reads: bool = True) -> FormalizationResult:
    inputs = (
        ScopeInput(
            arg_name="consumption",
            arg_type="Quantity",
            binding=Binding.metric("electricity_kwh"),
        ),
    )
    if vat_rate_reads:
        inputs = inputs + (
            ScopeInput(
                arg_name="vat_rate", arg_type="Decimal", binding=Binding.ref_param("vat_rate", "RU")
            ),
        )
    return FormalizationResult(
        source_text=SourceText(text="300 kWh base, 15% overage surcharge", formalizer_model_version="mock-v1"),
        scope_manifest=ScopeManifest(scope_name="comfort_v1", inputs=inputs),
        formula_form=FormulaForm.stub(
            {"base_kwh": "300", "base_rate": "3.20", "overage_multiplier": "1.15"}
        ),
        coefficients=Coefficients(payload={"base_rate": "3.20", "overage_multiplier": "1.15"}),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )


def test_draft_from_text_produces_draft_and_event() -> None:
    draft, event = TariffVersion.draft_from_text(
        "comfort", 1, _formalization(), now=_dt(2026, 6, 1)
    )

    assert draft.status is TariffVersionStatus.DRAFT
    assert draft.published_at is None
    assert isinstance(event, TariffDrafted)
    assert event.tariff_id == "comfort"
    assert event.version == 1


def test_validate_transitions_draft_to_validated_when_all_bindings_resolve() -> None:
    draft, _ = TariffVersion.draft_from_text("comfort", 1, _formalization(), now=_dt(2026, 6, 1))

    validated, event = draft.validate(unresolved_ref_param_bindings=[], now=_dt(2026, 6, 2))

    assert validated.status is TariffVersionStatus.VALIDATED
    assert isinstance(event, TariffValidated)


def test_validate_rejects_when_bindings_are_unresolved() -> None:
    draft, _ = TariffVersion.draft_from_text("comfort", 1, _formalization(), now=_dt(2026, 6, 1))
    missing = Binding.ref_param("vat_rate", "RU")

    with pytest.raises(UnresolvedScopeBindingError):
        draft.validate(unresolved_ref_param_bindings=[missing], now=_dt(2026, 6, 2))


def test_validate_cannot_run_twice() -> None:
    draft, _ = TariffVersion.draft_from_text("comfort", 1, _formalization(), now=_dt(2026, 6, 1))
    validated, _ = draft.validate(unresolved_ref_param_bindings=[], now=_dt(2026, 6, 2))

    with pytest.raises(InvalidTariffVersionTransitionError):
        validated.validate(unresolved_ref_param_bindings=[], now=_dt(2026, 6, 3))


def test_publish_requires_validated_status() -> None:
    draft, _ = TariffVersion.draft_from_text("comfort", 1, _formalization(), now=_dt(2026, 6, 1))

    with pytest.raises(InvalidTariffVersionTransitionError):
        draft.publish(approved_by="qa-lead", now=_dt(2026, 6, 2))


def test_publish_transitions_validated_to_published() -> None:
    draft, _ = TariffVersion.draft_from_text("comfort", 1, _formalization(), now=_dt(2026, 6, 1))
    validated, _ = draft.validate(unresolved_ref_param_bindings=[], now=_dt(2026, 6, 2))

    published, event = validated.publish(approved_by="qa-lead", now=_dt(2026, 6, 3))

    assert published.status is TariffVersionStatus.PUBLISHED
    assert published.published_at == _dt(2026, 6, 3)
    assert published.approved_by == "qa-lead"
    assert isinstance(event, TariffVersionPublished)


def test_repository_port_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        TariffVersionRepository()


def test_contract_formalizer_port_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        ContractFormalizer()


def test_fixture_contract_formalizer_is_deterministic() -> None:
    result = _formalization()
    formalizer = FixtureContractFormalizer({"comfort-v1-contract": result})

    first = formalizer.formalize("comfort-v1-contract")
    second = formalizer.formalize("comfort-v1-contract")

    assert first == second == result


def test_fixture_contract_formalizer_rejects_unknown_contract() -> None:
    formalizer = FixtureContractFormalizer({})

    with pytest.raises(UnknownContractError):
        formalizer.formalize("never seen this contract before")
