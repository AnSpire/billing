"""Реализация порта ``ContractFormalizer`` (domain) — ручная заглушка вместо
настоящего AI-агента-формализатора (PLAN.md, «Мок-агента = порт, а не код
внутри движка»).

Требования к фикстурам (PLAN.md): **детерминированные** — один и тот же
``contract_doc`` должен давать идентичный ``FormalizationResult``. Здесь это
получается тривиально: результат — обычный словарь, без случайности и часов
внутри, поэтому повторный вызов с тем же ключом просто возвращает то же
значение.
"""

from __future__ import annotations

from collections.abc import Mapping

from billing.domain.tariff_version import ContractFormalizer, FormalizationResult


class UnknownContractError(Exception):
    """``contract_doc`` не входит в набор фикстур этого формализатора."""


class FixtureContractFormalizer(ContractFormalizer):
    def __init__(self, fixtures: Mapping[str, FormalizationResult]) -> None:
        self._fixtures = dict(fixtures)

    def formalize(self, contract_doc: str) -> FormalizationResult:
        try:
            return self._fixtures[contract_doc]
        except KeyError:
            raise UnknownContractError(contract_doc) from None
