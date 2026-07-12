"""DoD фазы 7: перекрывающиеся ``applies_when`` дают ошибку конфликта
дефолтов (не молчаливый приоритет).

``conflicting_discount.catala_en`` (infrastructure/formula_engine/fixtures)
намеренно объявляет два правила скидки, чьи условия одновременно истинны при
``consumption >= 10`` — Catala не выбирает "первое" или "более специфичное"
правило сама, а поднимает ``Conflict`` в рантайме; ``CatalaFormulaEngine``
транслирует это в доменную ``ConflictError``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.domain.billing_assessment import ArtifactRef, CalcInput, ConflictError
from billing.domain.shared import Quantity
from billing.domain.tariff_artifact import TariffArtifact
from billing.domain.tariff_version import Binding, ScopeInput, ScopeManifest, ScopeOutput
from billing.infrastructure.db.connection import new_connection
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


_MANIFEST = ScopeManifest(
    scope_name="ConflictingDiscount",
    inputs=(
        ScopeInput(arg_name="consumption", arg_type="Decimal", binding=Binding.metric("x")),
    ),
    outputs=(ScopeOutput(arg_name="discount", produces="ChargeLine"),),
)


def _register_artifact(test_database_url: str, *, tariff_id: str) -> None:
    source = load_source("conflicting_discount")
    compiled = toolchain.compile_source(source)
    with new_connection(test_database_url) as conn:
        PostgresTariffArtifactRepository(conn).save(
            TariffArtifact(
                tariff_id=tariff_id,
                version=1,
                catala_source=source,
                source_hash=compiled.source_hash,
                compiler_version=compiled.compiler_version,
                runtime_version=compiled.runtime_version,
                scope_name="ConflictingDiscount",
                scope_manifest=_MANIFEST,
                compiled_py_path=str(compiled.package_dir),
                built_at=_dt(2026, 6, 1),
            )
        )


def test_overlapping_applies_when_raises_conflict_not_silent_priority(test_database_url: str) -> None:
    tariff_id = _unique("conflict-tariff")
    _register_artifact(test_database_url, tariff_id=tariff_id)

    with new_connection(test_database_url) as conn:
        engine = CatalaFormulaEngine(PostgresTariffArtifactRepository(conn))
        artifact_ref = ArtifactRef(
            tariff_id=tariff_id, version=1, artifact_hash="irrelevant", toolchain_version="irrelevant"
        )
        # consumption=12 -> ОБА правила (>=10 и >=5) истинны одновременно.
        calc_input = CalcInput(
            resolved_parameters={}, total_quantity=Quantity(value=Decimal(12), metric="x")
        )

        with pytest.raises(ConflictError):
            engine.execute(artifact_ref, calc_input)


def test_non_overlapping_input_resolves_without_conflict(test_database_url: str) -> None:
    """Контрольный случай: consumption=7 попадает только под второе правило
    (>=5, но <10) — конфликта нет, движок отрабатывает нормально."""
    tariff_id = _unique("conflict-tariff")
    _register_artifact(test_database_url, tariff_id=tariff_id)

    with new_connection(test_database_url) as conn:
        engine = CatalaFormulaEngine(PostgresTariffArtifactRepository(conn))
        artifact_ref = ArtifactRef(
            tariff_id=tariff_id, version=1, artifact_hash="irrelevant", toolchain_version="irrelevant"
        )
        calc_input = CalcInput(
            resolved_parameters={}, total_quantity=Quantity(value=Decimal(7), metric="x")
        )

        lines, _steps = engine.execute(artifact_ref, calc_input)

    assert lines[0].amount.amount == Decimal("50")
