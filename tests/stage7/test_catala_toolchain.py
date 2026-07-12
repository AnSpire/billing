"""infrastructure/formula_engine/catala_toolchain.py — обёртка над реальным
тулчейном Catala. Гоняет настоящий ``catala``/``clerk`` через subprocess (не
мокается) — это и есть предмет проверки: что пайплайн typecheck ->
транспиляция -> сборка пакета реально работает.
"""

from __future__ import annotations

import sys

import pytest

from billing.infrastructure.formula_engine import catala_toolchain as toolchain
from billing.infrastructure.formula_engine.fixtures import load_source


def test_compile_source_produces_a_loadable_module() -> None:
    source = load_source("comfort_v1")

    artifact = toolchain.compile_source(source)

    assert artifact.module_name == "Comfort"
    assert artifact.source_hash == toolchain.source_hash(source)
    assert (artifact.package_dir / artifact.package_name / "Comfort.py").exists()
    assert (artifact.package_dir / "catala_runtime.py").exists()


def test_compile_source_is_idempotent_by_content() -> None:
    source = load_source("comfort_v1")

    first = toolchain.compile_source(source)
    second = toolchain.compile_source(source)

    assert first.source_hash == second.source_hash
    assert first.package_dir == second.package_dir


def test_compile_source_raises_on_typecheck_failure() -> None:
    source = load_source("broken_typecheck")

    with pytest.raises(toolchain.CatalaCompilationError) as exc_info:
        toolchain.compile_source(source)

    assert "typecheck" in str(exc_info.value)


def test_compiled_module_is_importable_and_computes_uc4_numbers() -> None:
    """Не полный FormulaEngine (см. test_catala_formula_engine.py) — только
    доказательство, что артефакт сам по себе загружаемый и рабочий Python-модуль."""
    source = load_source("comfort_v1")
    artifact = toolchain.compile_source(source)

    sys.path.insert(0, str(artifact.package_dir))
    import importlib

    catala_runtime = importlib.import_module("catala_runtime")
    module = importlib.import_module(f"{artifact.package_name}.{artifact.module_name}")

    result = module.comfort(
        module.ComfortIn(
            consumption_in=catala_runtime.Decimal("340"),
            base_kwh_in=catala_runtime.Decimal("300"),
            base_rate_in=catala_runtime.Money("3.20"),
            overage_multiplier_in=catala_runtime.Decimal("1.15"),
            vat_rate_in=catala_runtime.Decimal("0.20"),
        )
    )

    assert int(result.base_amount) == 96000  # копейки
    assert int(result.overage_amount) == 14720
    assert int(result.vat_amount) == 22144
