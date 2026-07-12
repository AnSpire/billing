"""Реализация порта ``TariffArtifactRepository`` (domain) поверх psycopg3.

Сериализация ``ScopeManifest`` переиспользует те же приватные функции, что
``tariff_version_repository.py`` — тот же VO, та же форма JSON.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from billing.domain.tariff_artifact import TariffArtifact, TariffArtifactRepository
from billing.infrastructure.db.tariff_version_repository import (
    _scope_manifest_from_json,
    _scope_manifest_to_json,
)

_SELECT_COLUMNS = """
    tariff_id, version, catala_source, source_hash, compiler_version,
    runtime_version, scope_name, scope_manifest, compiled_py_path, built_at
"""


class PostgresTariffArtifactRepository(TariffArtifactRepository):
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def save(self, artifact: TariffArtifact) -> None:
        self._conn.execute(
            f"""
            INSERT INTO tariff_artifact (
                tariff_id, version, catala_source, source_hash, compiler_version,
                runtime_version, scope_name, scope_manifest, compiled_py_path, built_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tariff_id, version) DO NOTHING
            """,
            (
                artifact.tariff_id,
                artifact.version,
                artifact.catala_source,
                artifact.source_hash,
                artifact.compiler_version,
                artifact.runtime_version,
                artifact.scope_name,
                Jsonb(_scope_manifest_to_json(artifact.scope_manifest)),
                artifact.compiled_py_path,
                artifact.built_at,
            ),
        )

    def get(self, tariff_id: str, version: int) -> TariffArtifact | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM tariff_artifact WHERE tariff_id = %s AND version = %s",
            (tariff_id, version),
        ).fetchone()
        return self._row_to_artifact(row) if row else None

    @staticmethod
    def _row_to_artifact(row: tuple[Any, ...]) -> TariffArtifact:
        (
            tariff_id,
            version,
            catala_source,
            source_hash,
            compiler_version,
            runtime_version,
            scope_name,
            scope_manifest,
            compiled_py_path,
            built_at,
        ) = row
        return TariffArtifact(
            tariff_id=tariff_id,
            version=version,
            catala_source=catala_source,
            source_hash=source_hash,
            compiler_version=compiler_version,
            runtime_version=runtime_version,
            scope_name=scope_name,
            scope_manifest=_scope_manifest_from_json(scope_manifest),
            compiled_py_path=compiled_py_path,
            built_at=built_at,
        )
