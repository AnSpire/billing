"""TariffVersion поверх реальной БД — DoD фазы 3 (PLAN.md):

- коэффициенты заморожены на Publish — опубликованную версию нельзя мутировать;
- изменение коэффициента = новая версия (tariff_id, version+1) с новым
  TemporalValidity, а не правка старой.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

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
    TariffVersionImmutableError,
    TariffVersionStatus,
)
from billing.infrastructure.db.tariff_version_repository import (
    PostgresTariffVersionRepository,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _formalization(base_rate: str = "3.20") -> FormalizationResult:
    return FormalizationResult(
        source_text=SourceText(text="300 kWh base tariff", formalizer_model_version="mock-v1"),
        scope_manifest=ScopeManifest(
            scope_name="comfort_v1",
            inputs=(
                ScopeInput(
                    arg_name="vat_rate",
                    arg_type="Decimal",
                    binding=Binding.ref_param("vat_rate", "RU"),
                ),
            ),
        ),
        formula_form=FormulaForm.stub({"base_kwh": "300", "base_rate": base_rate}),
        coefficients=Coefficients(payload={"base_rate": base_rate}),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )


def _published_version(repo: PostgresTariffVersionRepository, *, version: int = 1, base_rate: str = "3.20") -> TariffVersion:
    draft, _ = TariffVersion.draft_from_text(
        "comfort", version, _formalization(base_rate), now=_dt(2026, 6, 1)
    )
    repo.save(draft)
    validated, _ = draft.validate(unresolved_ref_param_bindings=[], now=_dt(2026, 6, 2))
    repo.save(validated)
    published, _ = validated.publish(now=_dt(2026, 6, 3))
    repo.save(published)
    return published


def test_save_and_get_round_trip(db_connection) -> None:
    repo = PostgresTariffVersionRepository(db_connection)
    draft, _ = TariffVersion.draft_from_text("comfort", 1, _formalization(), now=_dt(2026, 6, 1))

    repo.save(draft)
    fetched = repo.get("comfort", 1)

    assert fetched is not None
    assert fetched.status is TariffVersionStatus.DRAFT
    assert fetched.coefficients.payload["base_rate"] == "3.20"
    assert fetched.scope_manifest.ref_param_bindings()[0].ref_param_key == ("vat_rate", "RU")


def test_lifecycle_transitions_persist_in_place(db_connection) -> None:
    repo = PostgresTariffVersionRepository(db_connection)
    published = _published_version(repo)

    fetched = repo.get("comfort", 1)

    assert fetched is not None
    assert fetched.status is TariffVersionStatus.PUBLISHED
    assert fetched.published_at == published.published_at


def test_published_version_cannot_be_mutated(db_connection) -> None:
    repo = PostgresTariffVersionRepository(db_connection)
    published = _published_version(repo)

    tampered = replace(published, coefficients=Coefficients(payload={"base_rate": "999.00"}))

    with pytest.raises(TariffVersionImmutableError):
        repo.save(tampered)

    # В БД коэффициенты остались прежними — попытка не прошла тихо.
    fetched = repo.get("comfort", 1)
    assert fetched is not None
    assert fetched.coefficients.payload["base_rate"] == "3.20"


def test_changing_a_coefficient_requires_a_new_version(db_connection) -> None:
    repo = PostgresTariffVersionRepository(db_connection)
    v1 = _published_version(repo, version=1, base_rate="3.20")

    v2_draft, _ = TariffVersion.draft_from_text(
        "comfort", 2, _formalization(base_rate="3.50"), now=_dt(2026, 7, 5)
    )
    repo.save(v2_draft)

    fetched_v1 = repo.get("comfort", 1)
    fetched_v2 = repo.get("comfort", 2)

    assert fetched_v1 is not None and fetched_v2 is not None
    # Старая версия не тронута — другая строка, другой (tariff_id, version).
    assert fetched_v1.coefficients.payload["base_rate"] == "3.20"
    assert fetched_v1.status is TariffVersionStatus.PUBLISHED
    assert fetched_v1.published_at == v1.published_at
    # Новая версия — черновик с новым коэффициентом и своим TemporalValidity.
    assert fetched_v2.coefficients.payload["base_rate"] == "3.50"
    assert fetched_v2.status is TariffVersionStatus.DRAFT
    assert fetched_v2.temporal_validity.valid_from == _dt(2026, 7, 1)
