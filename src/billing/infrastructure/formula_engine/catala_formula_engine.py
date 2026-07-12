"""``CatalaFormulaEngine`` — реализация порта ``FormulaEngine`` (domain)
поверх скомпилированного Catala-артефакта (PLAN.md, фаза 7).

Не заменяет ``StubFormulaEngine`` — оба остаются валидными реализациями
одного порта (billing_aggregates.md: «если движок когда-нибудь заменят —
домен не шелохнётся»). Выбор между ними — за тем, чей ``TariffVersion``
считается: ``formula_form.kind == "catala"`` → этот движок, ``"stub"`` →
заглушка (см. ``application/billing_calculation.py``).

Как настоящий движок и должен: "загрузка модуля по artifact_ref, кеширование
по (tariff_id, version) в процессе воркера, конверсия типов ParameterValue ->
типы рантайма Catala и обратно результат -> Money, отлов ошибки конфликта
дефолтов" (billing_aggregates.md, «Реестр артефактов и порт FormulaEngine»).
"""

from __future__ import annotations

import importlib
import re
import sys
import uuid
from decimal import Decimal

from billing.domain.billing_assessment import (
    ArtifactNotFoundError,
    ArtifactRef,
    CalcInput,
    ChargeLine,
    ConflictError,
    FormulaEngine,
    Money,
)
from billing.domain.tariff_artifact import TariffArtifactRepository
from billing.domain.tariff_version import ScopeInput
from billing.infrastructure.formula_engine.catala_toolchain import (
    CompiledArtifact,
    compile_source,
)


class CatalaRuntimeError(Exception):
    """Скомпилированный скоуп упал в рантайме не конфликтом дефолтов, а
    чем-то ещё (например ``NoValue`` — ни одно правило не подошло). Отлично
    от ``ConflictError`` намеренно: разный смысл для вызывающего кода."""


def _scope_function_name(module_name: str) -> str:
    """Catala транспилирует ``NoMeterSOI`` (CamelCase, с сериями заглавных
    подряд) в ``no_meter_s_o_i`` — подчёркивание перед КАЖДОЙ заглавной, а не
    только перед началом нового слова (проверено на реальном выводе
    catala_research/case3, не по памяти — правило неочевидное)."""
    return re.sub(r"(?<!^)([A-Z])", r"_\1", module_name).lower()


_module_cache: dict[str, object] = {}


def _load_module(compiled: CompiledArtifact):
    cache_key = compiled.source_hash
    if cache_key in _module_cache:
        return _module_cache[cache_key]

    package_root = str(compiled.package_dir)
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    module = importlib.import_module(f"{compiled.package_name}.{compiled.module_name}")
    _module_cache[cache_key] = module
    return module


def _to_catala_value(runtime, value: Decimal, arg_type: str):
    if arg_type == "Decimal":
        return runtime.Decimal(str(value))
    if arg_type == "Money":
        return runtime.Money(str(value))
    if arg_type == "Integer":
        return runtime.Integer(int(value))
    raise ValueError(f"unsupported ScopeInput.arg_type for Catala conversion: {arg_type!r}")


def _from_catala_money(money_value) -> Decimal:
    """``BECARE.md``: Money — целое число копеек внутри; ``int(money)`` даёт
    копейки. Точное преобразование в рубли, без ухода через float."""
    return Decimal(int(money_value)) / Decimal(100)


def _resolve_input_value(scope_input: ScopeInput, calc_input: CalcInput) -> Decimal:
    binding = scope_input.binding
    if binding.kind == "ref_param":
        key, _jurisdiction = binding.ref_param_key
        return calc_input.resolved_parameters[key]
    if binding.kind == "metric":
        return calc_input.total_quantity.value
    if binding.kind == "coefficient":
        name = binding.payload["name"]
        return calc_input.resolved_parameters[name]
    raise ValueError(
        f"CatalaFormulaEngine does not know how to resolve a {binding.kind!r} binding "
        f"for scope input {scope_input.arg_name!r}"
    )


class CatalaFormulaEngine(FormulaEngine):
    def __init__(self, artifacts: TariffArtifactRepository) -> None:
        self._artifacts = artifacts

    def execute(
        self, artifact_ref: ArtifactRef, calc_input: CalcInput
    ) -> tuple[tuple[ChargeLine, ...], tuple[str, ...]]:
        artifact = self._artifacts.get(artifact_ref.tariff_id, artifact_ref.version)
        if artifact is None:
            raise ArtifactNotFoundError(
                f"no TariffArtifact for ({artifact_ref.tariff_id!r}, {artifact_ref.version!r})"
            )

        # Идемпотентно: если артефакт с этим hash уже собран на диске (тот
        # же source -> тот же hash), compile_source просто находит его.
        compiled = compile_source(artifact.catala_source)
        module = _load_module(compiled)

        package_root = str(compiled.package_dir)
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
        runtime = importlib.import_module("catala_runtime")

        input_type = getattr(module, f"{compiled.module_name}In")
        kwargs = {
            f"{scope_input.arg_name}_in": _to_catala_value(
                runtime, _resolve_input_value(scope_input, calc_input), scope_input.arg_type
            )
            for scope_input in artifact.scope_manifest.inputs
        }

        scope_fn = getattr(module, _scope_function_name(compiled.module_name))
        try:
            result = scope_fn(input_type(**kwargs))
        except runtime.Conflict as exc:
            raise ConflictError(
                f"overlapping applies_when in {artifact.tariff_id!r} v{artifact.version}: {exc}"
            ) from exc
        except runtime.CatalaError as exc:
            raise CatalaRuntimeError(
                f"Catala runtime error evaluating {artifact.tariff_id!r} v{artifact.version}: {exc}"
            ) from exc

        lines: list[ChargeLine] = []
        steps: list[str] = []
        for scope_output in artifact.scope_manifest.outputs:
            raw = getattr(result, scope_output.arg_name)
            amount = _from_catala_money(raw)
            lines.append(
                ChargeLine(line_id=uuid.uuid4(), rule_label=scope_output.arg_name, amount=Money(amount))
            )
            steps.append(f"{scope_output.arg_name}={amount}")

        return tuple(lines), tuple(steps)
