"""Application Service: ``TariffVersion.Validate`` требует прочитать реестр
``ReferenceParameter``, а это чужой агрегат — и, с фазы 7, ещё и реально
скомпилировать ``CatalaSource``, если формула не заглушка.

Это не сага — саги (фаза 6) координируют ЗАПИСЬ через несколько транзакций
по цепочке событий. Здесь же нет ни цепочки, ни отложенной согласованности:
синхронные шаги перед командой над ОДНИМ агрегатом (``TariffVersion``).
CLAUDE.md, чек-лист §8, пункт 2 — "не сливаю ли я несколько агрегатов в одну
транзакцию" — здесь не нарушается: пишем только в ``TariffVersion`` (и в
реестр ``tariff_artifact`` — не агрегат, а инфраструктурная таблица, см.
domain/tariff_artifact.py), ``ReferenceParameter`` только читаем.

Компиляция — ПЕРЕД резолвингом параметров: тариф, который даже не
типизируется, не стоит того, чтобы проверять его RefParam-биндинги
(billing_aggregates.md §1: «падать она должна там, где это ошибка
формализации, а не в момент публикации» — именно здесь, на Validate).
"""

from __future__ import annotations

from datetime import datetime

from billing.domain.reference_parameter import ReferenceParameterRepository
from billing.domain.tariff_artifact import TariffArtifact, TariffArtifactRepository
from billing.domain.tariff_version import Binding, TariffValidated, TariffVersion
from billing.infrastructure.formula_engine.catala_toolchain import compile_source


def validate_tariff_version(
    tariff: TariffVersion,
    reference_parameters: ReferenceParameterRepository,
    *,
    artifacts: TariffArtifactRepository | None = None,
    now: datetime,
) -> tuple[TariffVersion, TariffValidated]:
    """Резолвит каждый ``RefParam``-биндинг из ``ScopeManifest`` на момент
    начала действия версии (``temporal_validity.valid_from``) — это и есть
    вопрос, который в реальности задаёт ``Validate``: "будут ли входы этой
    версии резолвиться, когда она начнёт действовать?" — а не абстрактное
    "существует ли когда-нибудь такой ключ".

    ``artifacts`` обязателен только для ``formula_form.kind == "catala"``
    (фаза 7); заглушки фаз 3–6 (``kind == "stub"``) компиляцию не проходят —
    старые вызовы этой функции не меняются."""
    if tariff.formula_form.kind == "catala":
        if artifacts is None:
            raise ValueError("validating a catala-kind TariffVersion requires an artifacts repository")
        source = tariff.formula_form.catala_source
        compiled = compile_source(source)  # CatalaCompilationError всплывает как есть
        artifacts.save(
            TariffArtifact(
                tariff_id=tariff.tariff_id,
                version=tariff.version,
                catala_source=source,
                source_hash=compiled.source_hash,
                compiler_version=compiled.compiler_version,
                runtime_version=compiled.runtime_version,
                scope_name=tariff.scope_manifest.scope_name,
                scope_manifest=tariff.scope_manifest,
                compiled_py_path=str(compiled.package_dir),
                built_at=now,
            )
        )

    unresolved: list[Binding] = []
    for binding in tariff.scope_manifest.ref_param_bindings():
        key, jurisdiction = binding.ref_param_key
        resolved = reference_parameters.resolve(
            key,
            jurisdiction,
            valid_on=tariff.temporal_validity.valid_from,
            as_of_tx=now,
        )
        if resolved is None:
            unresolved.append(binding)
    return tariff.validate(unresolved_ref_param_bindings=unresolved, now=now)
