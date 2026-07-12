"""Тарифы — TariffVersion. Жизненный цикл draft → validate → publish.

``POST /tariffs`` — точка, где работает мок-формализатор: ``contract_doc`` из
тела прогоняется через ``ContractFormalizer.formalize`` (заглушка вместо
AI-агента), результат превращается в черновик. ``validate`` компилирует Catala
и резолвит биндинги; ``publish`` требует непустой ``approved_by`` — домен сам
отклонит автопубликацию (CLAUDE.md §4).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from psycopg import Connection
from pydantic import BaseModel

from billing.application.tariff_validation import validate_tariff_version
from billing.domain.tariff_version import ContractFormalizer, TariffVersion
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)
from billing.infrastructure.db.tariff_artifact_repository import PostgresTariffArtifactRepository
from billing.infrastructure.db.tariff_version_repository import PostgresTariffVersionRepository
from billing.interface.http.deps import db_connection, get_formalizer, get_now
from billing.interface.http.serialization import TariffVersionOut, tariff_version_out

router = APIRouter(prefix="/tariffs", tags=["tariffs"])


class DraftIn(BaseModel):
    tariff_id: str
    version: int = 1
    contract_doc: str


@router.post("", status_code=201, response_model=TariffVersionOut)
def draft(
    body: DraftIn,
    formalizer: ContractFormalizer = Depends(get_formalizer),
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> TariffVersionOut:
    result = formalizer.formalize(body.contract_doc)  # UnknownContractError -> 404
    draft, _event = TariffVersion.draft_from_text(body.tariff_id, body.version, result, now=now)
    PostgresTariffVersionRepository(conn).save(draft)
    return tariff_version_out(draft)


def _load(conn: Connection, tariff_id: str, version: int) -> TariffVersion:
    tariff = PostgresTariffVersionRepository(conn).get(tariff_id, version)
    if tariff is None:
        raise HTTPException(404, f"tariff version ({tariff_id}, {version}) not found")
    return tariff


@router.post("/{tariff_id}/versions/{version}/validate", response_model=TariffVersionOut)
def validate(
    tariff_id: str,
    version: int,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> TariffVersionOut:
    repo = PostgresTariffVersionRepository(conn)
    draft = _load(conn, tariff_id, version)
    validated, _event = validate_tariff_version(
        draft,
        PostgresReferenceParameterRepository(conn),
        artifacts=PostgresTariffArtifactRepository(conn),
        now=now,
    )
    repo.save(validated)
    return tariff_version_out(validated)


class PublishIn(BaseModel):
    approved_by: str


@router.post("/{tariff_id}/versions/{version}/publish", response_model=TariffVersionOut)
def publish(
    tariff_id: str,
    version: int,
    body: PublishIn,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> TariffVersionOut:
    repo = PostgresTariffVersionRepository(conn)
    validated = _load(conn, tariff_id, version)
    published, _event = validated.publish(approved_by=body.approved_by, now=now)
    repo.save(published)
    return tariff_version_out(published)


@router.get("/{tariff_id}/versions/{version}", response_model=TariffVersionOut)
def get(
    tariff_id: str,
    version: int,
    conn: Connection = Depends(db_connection),
) -> TariffVersionOut:
    return tariff_version_out(_load(conn, tariff_id, version))
