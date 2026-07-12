"""Реестр `tariff_artifact` — billing_aggregates.md, «Реестр артефактов и
порт FormulaEngine»: **не агрегат**, инфраструктурная таблица. Записи
неизменяемы после записи, их немного (одна опубликованная/провалидированная
версия тарифа = одна запись).

Появляется в фазе 7 вместе с реальной компиляцией: до неё (фазы 3–6)
компилировать было нечего — ``FormulaForm`` был JSON-заглушкой, не Catala-
исходником, поэтому реестр компилированных артефактов не имел смысла (см.
PLAN.md, фаза 3, «полный цикл DraftFromText -> Validate -> Publish с
реальной компиляцией — в фазе 7»).

Идентичность строки — ``(tariff_id, version)``, те же координаты, что несёт
``ArtifactRef`` в ``CalcContext`` (billing_assessment.py) — не потому что
это один и тот же тип, а потому что ``ArtifactRef`` как раз и есть ссылка на
конкретную строку этого реестра.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from billing.domain.tariff_version import ScopeManifest


@dataclass(frozen=True)
class TariffArtifact:
    """``compiled_py_path`` — путь к собранному standalone Python-пакету на
    диске, а не ``bytea`` в БД (billing_aggregates.md допускает оба варианта:
    "compiled_py -- bytea | ссылка на объектное хранилище"; локальный диск
    монолита здесь играет роль объектного хранилища — заводить реальный S3/
    MinIO ради одного узла было бы преждевременно, см. CLAUDE.md §1)."""

    tariff_id: str
    version: int
    catala_source: str
    source_hash: str
    compiler_version: str
    runtime_version: str
    scope_name: str
    scope_manifest: ScopeManifest
    compiled_py_path: str
    built_at: datetime


class TariffArtifactRepository(ABC):
    """Порт (см. PLAN.md, «Repository — порт в домене, реализация в
    infrastructure»). Единственная реализация — ``PostgresTariffArtifactRepository``.

    Как и у ``Invoice``: в контракте нет метода обновления — артефакт либо
    существует таким, каким его собрал ``Validate``, либо (при изменении
    источника) это уже другая версия ``(tariff_id, version+1)`` со своей
    строкой реестра."""

    @abstractmethod
    def save(self, artifact: TariffArtifact) -> None: ...

    @abstractmethod
    def get(self, tariff_id: str, version: int) -> TariffArtifact | None: ...
