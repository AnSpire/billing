"""Реальные ``.catala_en`` исходники — фикстуры для ``FixtureContractFormalizer``
(фаза 7). Само построение ``FormalizationResult`` (какой ``ScopeManifest``,
какие ``Coefficients``) — забота вызывающего кода/тестов, здесь только
загрузка текста источника с диска."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def load_source(name: str) -> str:
    """``name`` без расширения, например ``"comfort_v1"``."""
    return (_DIR / f"{name}.catala_en").read_text()
