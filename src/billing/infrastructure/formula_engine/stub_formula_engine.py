"""Реализация порта ``FormulaEngine`` (domain) — заглушка-калькулятор вместо
Catala (PLAN.md, фаза 4: «арифметику пока считает простой Python-калькулятор»;
Catala подключается в фазе 7 без изменения сигнатуры порта).

Интерпретирует ровно одну форму тарифа: база + превышение + НДС (та же форма,
что использована в фикстурах фазы 3 и в UC-4/UC-10 use_case.md — числа здесь
специально воспроизводят пример из документации как sanity-проверка модели).
Настоящий Catala в фазе 7 не будет ограничен этой единственной формой — это
ограничение именно заглушки, а не порта.

Как настоящий движок "загружает модуль по artifact_ref", а не получает
исходник от вызывающего кода напрямую (billing_aggregates.md, «Реестр
артефактов и порт FormulaEngine»), так и заглушка загружает ``TariffVersion``
через ``TariffVersionRepository`` по ``(tariff_id, version)`` из
``artifact_ref`` — а не принимает ``FormulaForm`` параметром ``execute``.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from billing.domain.billing_assessment import (
    ArtifactNotFoundError,
    ArtifactRef,
    CalcInput,
    ChargeLine,
    FormulaEngine,
    Money,
)
from billing.domain.tariff_version import TariffVersionRepository

_CENT = Decimal("0.01")


def _round(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


class StubFormulaEngine(FormulaEngine):
    def __init__(self, tariff_versions: TariffVersionRepository) -> None:
        self._tariff_versions = tariff_versions

    def execute(
        self, artifact_ref: ArtifactRef, calc_input: CalcInput
    ) -> tuple[tuple[ChargeLine, ...], tuple[str, ...]]:
        tariff = self._tariff_versions.get(artifact_ref.tariff_id, artifact_ref.version)
        if tariff is None:
            raise ArtifactNotFoundError(
                f"no TariffVersion for artifact_ref ({artifact_ref.tariff_id!r}, "
                f"{artifact_ref.version!r})"
            )
        body = tariff.formula_form.body
        base_kwh = Decimal(body["base_kwh"])
        base_rate = Decimal(body["base_rate"])
        overage_multiplier = Decimal(body["overage_multiplier"])
        vat_rate = calc_input.resolved_parameters["vat_rate"]

        quantity = calc_input.total_quantity.value
        base_quantity = min(quantity, base_kwh)
        overage_quantity = max(quantity - base_kwh, Decimal(0))

        base_amount = _round(base_quantity * base_rate)
        overage_amount = _round(overage_quantity * base_rate * overage_multiplier)
        subtotal = base_amount + overage_amount
        vat_amount = _round(subtotal * vat_rate)

        lines = (
            ChargeLine(line_id=uuid.uuid4(), rule_label="base", amount=Money(base_amount)),
            ChargeLine(line_id=uuid.uuid4(), rule_label="overage", amount=Money(overage_amount)),
            ChargeLine(line_id=uuid.uuid4(), rule_label="vat", amount=Money(vat_amount)),
        )
        steps = (
            f"quantity={quantity}",
            f"base={base_quantity}×{base_rate}={base_amount}",
            f"overage={overage_quantity}×{base_rate}×{overage_multiplier}={overage_amount}",
            f"subtotal={subtotal}",
            f"vat={subtotal}×{vat_rate}={vat_amount}",
        )
        return lines, steps
