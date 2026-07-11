"""Application Service: ``TariffVersion.Validate`` требует прочитать реестр
``ReferenceParameter``, а это чужой агрегат.

Это не сага — саги (фаза 6) координируют ЗАПИСЬ через несколько транзакций
по цепочке событий. Здесь же нет ни цепочки, ни отложенной согласованности:
один синхронный запрос на чтение перед команду над ОДНИМ агрегатом
(``TariffVersion``). CLAUDE.md, чек-лист §8, пункт 2 — "не сливаю ли я
несколько агрегатов в одну транзакцию" — здесь не нарушается: пишем только в
``TariffVersion``, ``ReferenceParameter`` только читаем.
"""

from __future__ import annotations

from datetime import datetime

from billing.domain.reference_parameter import ReferenceParameterRepository
from billing.domain.tariff_version import Binding, TariffValidated, TariffVersion


def validate_tariff_version(
    tariff: TariffVersion,
    reference_parameters: ReferenceParameterRepository,
    *,
    now: datetime,
) -> tuple[TariffVersion, TariffValidated]:
    """Резолвит каждый ``RefParam``-биндинг из ``ScopeManifest`` на момент
    начала действия версии (``temporal_validity.valid_from``) — это и есть
    вопрос, который в реальности задаёт ``Validate``: "будут ли входы этой
    версии резолвиться, когда она начнёт действовать?" — а не абстрактное
    "существует ли когда-нибудь такой ключ"."""
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
