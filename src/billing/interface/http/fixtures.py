"""Каталог фикстур для ``FixtureContractFormalizer`` — заглушка вместо
AI-агента-формализатора (PLAN.md, «Мок-агента = порт»). Ключ словаря — это
``contract_doc``, который приходит в ``POST /tariffs``; значение — готовый
``FormalizationResult`` (какую формулу Catala применить и от каких параметров
она зависит).

Здесь один демонстрационный договор — «comfort» (юрисдикция RU): база 300
кВт·ч по 3.20, превышение с надбавкой 15%, НДС отдельной ставкой (те же числа,
что в golden-тестах UC-4). Настоящий агент заменит этот словарь, не трогая
HTTP-слой: он реализует тот же порт ``ContractFormalizer.formalize``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from billing.domain.shared import TemporalValidity
from billing.domain.tariff_version import (
    Binding,
    Coefficients,
    FormalizationResult,
    FormulaForm,
    ScopeInput,
    ScopeManifest,
    ScopeOutput,
    SourceText,
)
from billing.infrastructure.formula_engine.fixtures import load_source

# Юрисдикция «зашита» в договор: формализатор детерминирован по одному
# строковому ключу, поэтому все параметры (включая jurisdiction ref_param'а)
# фиксируются здесь, а не приходят из запроса.
COMFORT_JURISDICTION = "RU"
COMFORT_CONTRACT = "comfort-v1"


def comfort_fixture(jurisdiction: str = COMFORT_JURISDICTION) -> FormalizationResult:
    """``FormalizationResult`` для договора «comfort» с заданной юрисдикцией.

    Юрисдикция вынесена в аргумент, чтобы её можно было варьировать (например
    контрактные тесты дают каждому прогону уникальную юрисдикцию для изоляции
    в общей БД). В проде каталог ``default_fixtures`` фиксирует RU."""
    return FormalizationResult(
        source_text=SourceText(
            text="база 300 кВт·ч по 3.20, превышение с надбавкой 15%, НДС отдельной ставкой",
            formalizer_model_version="mock-v1",
        ),
        scope_manifest=ScopeManifest(
            scope_name="Comfort",
            inputs=(
                ScopeInput(
                    arg_name="consumption",
                    arg_type="Decimal",
                    binding=Binding.metric("electricity_kwh"),
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
            # Движок читает ChargeLine именно из объявленных выходов скоупа —
            # без них расчёт вернул бы ноль строк.
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
        temporal_validity=TemporalValidity(valid_from=datetime(2026, 7, 1, tzinfo=timezone.utc)),
    )


def default_fixtures() -> Mapping[str, FormalizationResult]:
    return {COMFORT_CONTRACT: comfort_fixture(COMFORT_JURISDICTION)}
