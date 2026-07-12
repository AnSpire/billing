"""Реализация порта ``TariffVersionRepository`` (domain) поверх psycopg3.

Сериализация вложенных VO (``ScopeManifest``, ``FormulaForm`` и т.д.) в JSON и
обратно — забота этого модуля, не домена (та же граница, что в
``reference_parameter_repository.py``): домен не знает, что его сохраняют как
JSONB.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from billing.domain.shared import TemporalValidity
from billing.domain.tariff_version import (
    Binding,
    Coefficients,
    FormulaForm,
    ScopeInput,
    ScopeManifest,
    ScopeOutput,
    SourceText,
    TariffVersion,
    TariffVersionImmutableError,
    TariffVersionRepository,
    TariffVersionStatus,
)

_SELECT_COLUMNS = """
    tariff_id, version, status, source_text, scope_manifest, formula_form,
    coefficients, valid_from, valid_to, created_at, published_at, approved_by
"""


class PostgresTariffVersionRepository(TariffVersionRepository):
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def save(self, version: TariffVersion) -> None:
        row = self._conn.execute(
            f"""
            INSERT INTO tariff_version (
                tariff_id, version, status, source_text, scope_manifest, formula_form,
                coefficients, valid_from, valid_to, created_at, published_at, approved_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tariff_id, version) DO UPDATE SET
                status = EXCLUDED.status,
                published_at = EXCLUDED.published_at,
                approved_by = EXCLUDED.approved_by
            WHERE tariff_version.status <> 'published'
            RETURNING {_SELECT_COLUMNS}
            """,
            (
                version.tariff_id,
                version.version,
                version.status.value,
                Jsonb(_source_text_to_json(version.source_text)),
                Jsonb(_scope_manifest_to_json(version.scope_manifest)),
                Jsonb({"kind": version.formula_form.kind, "body": dict(version.formula_form.body)}),
                Jsonb(dict(version.coefficients.payload)),
                version.temporal_validity.valid_from,
                version.temporal_validity.valid_to,
                version.created_at,
                version.published_at,
                version.approved_by,
            ),
        ).fetchone()
        if row is None:
            raise TariffVersionImmutableError(
                f"({version.tariff_id!r}, {version.version!r}) is already published "
                "and cannot be modified"
            )

    def get(self, tariff_id: str, version: int) -> TariffVersion | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM tariff_version WHERE tariff_id = %s AND version = %s",
            (tariff_id, version),
        ).fetchone()
        return self._row_to_version(row) if row else None

    @staticmethod
    def _row_to_version(row: tuple) -> TariffVersion:
        (
            tariff_id,
            version,
            status,
            source_text,
            scope_manifest,
            formula_form,
            coefficients,
            valid_from,
            valid_to,
            created_at,
            published_at,
            approved_by,
        ) = row
        return TariffVersion(
            tariff_id=tariff_id,
            version=version,
            status=TariffVersionStatus(status),
            source_text=_source_text_from_json(source_text),
            scope_manifest=_scope_manifest_from_json(scope_manifest),
            formula_form=FormulaForm(kind=formula_form["kind"], body=formula_form["body"]),
            coefficients=Coefficients(payload=coefficients),
            temporal_validity=TemporalValidity(valid_from=valid_from, valid_to=valid_to),
            created_at=created_at,
            published_at=published_at,
            approved_by=approved_by,
        )


def _source_text_to_json(source_text: SourceText) -> dict[str, Any]:
    return {
        "text": source_text.text,
        "formalizer_model_version": source_text.formalizer_model_version,
    }


def _source_text_from_json(data: dict[str, Any]) -> SourceText:
    return SourceText(text=data["text"], formalizer_model_version=data["formalizer_model_version"])


def _binding_to_json(binding: Binding) -> dict[str, Any]:
    return {"kind": binding.kind, "payload": dict(binding.payload)}


def _binding_from_json(data: dict[str, Any]) -> Binding:
    return Binding(kind=data["kind"], payload=data["payload"])


def _scope_manifest_to_json(manifest: ScopeManifest) -> dict[str, Any]:
    return {
        "scope_name": manifest.scope_name,
        "inputs": [
            {
                "arg_name": i.arg_name,
                "arg_type": i.arg_type,
                "binding": _binding_to_json(i.binding),
            }
            for i in manifest.inputs
        ],
        "outputs": [
            {"arg_name": o.arg_name, "produces": o.produces} for o in manifest.outputs
        ],
    }


def _scope_manifest_from_json(data: dict[str, Any]) -> ScopeManifest:
    return ScopeManifest(
        scope_name=data["scope_name"],
        inputs=tuple(
            ScopeInput(
                arg_name=i["arg_name"],
                arg_type=i["arg_type"],
                binding=_binding_from_json(i["binding"]),
            )
            for i in data["inputs"]
        ),
        outputs=tuple(
            ScopeOutput(arg_name=o["arg_name"], produces=o["produces"]) for o in data["outputs"]
        ),
    )
