"""application/tariff_validation.py — DoD фазы 3: reads из ScopeManifest
резолвятся в реестре ReferenceParameter (или Validate падает)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.application.tariff_validation import validate_tariff_version
from billing.domain.reference_parameter import ParameterValue, Provenance
from billing.domain.shared import TemporalValidity
from billing.domain.tariff_version import (
    Binding,
    Coefficients,
    FormalizationResult,
    FormulaForm,
    ScopeInput,
    ScopeManifest,
    SourceText,
    TariffVersion,
    TariffVersionStatus,
    UnresolvedScopeBindingError,
)
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _draft(*ref_param_keys: tuple[str, str]) -> TariffVersion:
    inputs = tuple(
        ScopeInput(arg_name=f"input_{i}", arg_type="Decimal", binding=Binding.ref_param(key, jurisdiction))
        for i, (key, jurisdiction) in enumerate(ref_param_keys)
    )
    formalization = FormalizationResult(
        source_text=SourceText(text="comfort tariff", formalizer_model_version="mock-v1"),
        scope_manifest=ScopeManifest(scope_name="comfort_v1", inputs=inputs),
        formula_form=FormulaForm.stub({}),
        coefficients=Coefficients(payload={"base_rate": "3.20"}),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )
    draft, _ = TariffVersion.draft_from_text("comfort", 1, formalization, now=_dt(2026, 6, 1))
    return draft


def test_validate_succeeds_when_ref_param_resolves_at_tariff_valid_from(db_connection) -> None:
    ref_params = PostgresReferenceParameterRepository(db_connection)
    ref_params.register_value(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1)),
        Provenance(regulation_ref="98-FZ", document_id="doc-1", effective_date=_dt(2024, 1, 1).date()),
        now=_dt(2024, 1, 1),
    )
    draft = _draft(("vat_rate", "RU"))

    validated, event = validate_tariff_version(draft, ref_params, now=_dt(2026, 6, 2))

    assert validated.status is TariffVersionStatus.VALIDATED
    assert event.tariff_id == "comfort"


def test_validate_fails_when_ref_param_does_not_resolve(db_connection) -> None:
    ref_params = PostgresReferenceParameterRepository(db_connection)
    draft = _draft(("vat_rate", "RU"))  # реестр пуст — ничего не зарегистрировано

    with pytest.raises(UnresolvedScopeBindingError):
        validate_tariff_version(draft, ref_params, now=_dt(2026, 6, 2))


def test_validate_uses_valid_from_of_the_tariff_not_now(db_connection) -> None:
    """Норма зарегистрирована, но перестаёт действовать ДО того, как тариф
    вступает в силу — reads не резолвится именно на нужный момент."""
    ref_params = PostgresReferenceParameterRepository(db_connection)
    ref_params.register_value(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal("0.20")),
        TemporalValidity(valid_from=_dt(2024, 1, 1), valid_to=_dt(2026, 1, 1)),
        Provenance(regulation_ref="98-FZ", document_id="doc-1", effective_date=_dt(2024, 1, 1).date()),
        now=_dt(2024, 1, 1),
    )
    # Тариф вступает в силу 2026-07-01 — norm к этому моменту уже не действует.
    draft = _draft(("vat_rate", "RU"))

    with pytest.raises(UnresolvedScopeBindingError):
        validate_tariff_version(draft, ref_params, now=_dt(2026, 6, 2))


def test_validate_ignores_non_ref_param_bindings(db_connection) -> None:
    ref_params = PostgresReferenceParameterRepository(db_connection)
    inputs = (
        ScopeInput(arg_name="consumption", arg_type="Quantity", binding=Binding.metric("electricity_kwh")),
    )
    formalization = FormalizationResult(
        source_text=SourceText(text="comfort tariff", formalizer_model_version="mock-v1"),
        scope_manifest=ScopeManifest(scope_name="comfort_v1", inputs=inputs),
        formula_form=FormulaForm.stub({}),
        coefficients=Coefficients(payload={"base_rate": "3.20"}),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )
    draft, _ = TariffVersion.draft_from_text("comfort", 1, formalization, now=_dt(2026, 6, 1))

    validated, _ = validate_tariff_version(draft, ref_params, now=_dt(2026, 6, 2))

    assert validated.status is TariffVersionStatus.VALIDATED
