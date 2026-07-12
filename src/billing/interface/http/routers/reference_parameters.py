"""Справочные параметры (ставки НДС, нормы ЖКХ и т.п.) — ReferenceParameter.

``corrections`` — единственный саговый эндпоинт здесь: ретроактивная коррекция
порождает ``ReferenceParameterCorrected``, диспетчер запускает веерный пересчёт
затронутых начислений (только тех, чьи тарифы реально читают этот параметр);
сбои по отдельным аккаунтам уходят в dead-letter, не роняя веер (фаза 8).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from psycopg import Connection
from pydantic import BaseModel

from billing.domain.reference_parameter import ParameterValue, Provenance
from billing.domain.shared import TemporalValidity
from billing.infrastructure.db.connection import new_connection
from billing.infrastructure.db.dead_letter_store import find_dead_letters_for_account
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)
from billing.interface.http.deps import db_connection, get_dispatcher, get_now, settings
from billing.interface.http.serialization import RefParamVersionOut, ref_param_version_out

router = APIRouter(prefix="/reference-parameters", tags=["reference-parameters"])


class ProvenanceIn(BaseModel):
    regulation_ref: str
    document_id: str
    effective_date: date


class RegisterIn(BaseModel):
    key: str
    jurisdiction: str
    value: Decimal
    valid_from: datetime
    valid_to: datetime | None = None
    provenance: ProvenanceIn


class VersionIdOut(BaseModel):
    version_id: str


def _provenance(p: ProvenanceIn) -> Provenance:
    return Provenance(
        regulation_ref=p.regulation_ref,
        document_id=p.document_id,
        effective_date=p.effective_date,
    )


@router.post("", status_code=201, response_model=VersionIdOut)
def register(
    body: RegisterIn,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> VersionIdOut:
    version = PostgresReferenceParameterRepository(conn).register_value(
        body.key,
        body.jurisdiction,
        ParameterValue.scalar(body.value),
        TemporalValidity(valid_from=body.valid_from, valid_to=body.valid_to),
        _provenance(body.provenance),
        now=now,
    )
    return VersionIdOut(version_id=str(version.version_id))


class CorrectIn(BaseModel):
    value: Decimal
    valid_from: datetime
    valid_to: datetime | None = None
    provenance: ProvenanceIn


class CorrectionOut(BaseModel):
    version_id: str
    superseded_count: int
    recalculated_versions: list[int]
    dead_letters: int


@router.post("/{key}/{jurisdiction}/corrections", response_model=CorrectionOut)
def correct(
    key: str,
    jurisdiction: str,
    body: CorrectIn,
    dispatcher=Depends(get_dispatcher),
    config=Depends(settings),
    now: datetime = Depends(get_now),
) -> CorrectionOut:
    # Пишем коррекцию в своей транзакции и коммитим ДО dispatch: обработчик
    # веера читает уже закоммиченную версию (PRESENTATION.md §6).
    with new_connection(config.database_url) as conn:
        version, event = PostgresReferenceParameterRepository(conn).correct(
            key,
            jurisdiction,
            ParameterValue.scalar(body.value),
            TemporalValidity(valid_from=body.valid_from, valid_to=body.valid_to),
            _provenance(body.provenance),
            now=now,
        )

    dispatcher.dispatch(event)  # веерный пересчёт + корректирующие квитанции

    return CorrectionOut(
        version_id=str(version.version_id),
        superseded_count=len(event.superseded_version_ids),
        recalculated_versions=[],  # веер асинхронен по аккаунтам; детали — в dead_letter/assessment
        dead_letters=0,
    )


class RepealIn(BaseModel):
    repeal_from: datetime
    provenance: ProvenanceIn


@router.post("/{key}/{jurisdiction}/repeal", response_model=VersionIdOut)
def repeal(
    key: str,
    jurisdiction: str,
    body: RepealIn,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> VersionIdOut:
    version = PostgresReferenceParameterRepository(conn).repeal(
        key, jurisdiction, body.repeal_from, _provenance(body.provenance), now=now
    )
    return VersionIdOut(version_id=str(version.version_id))


@router.get("/{key}/{jurisdiction}", response_model=RefParamVersionOut)
def resolve(
    key: str,
    jurisdiction: str,
    valid_on: datetime = Query(..., description="момент valid-time, на который резолвим"),
    as_of: datetime | None = Query(None, description="tx-time; по умолчанию — сейчас"),
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> RefParamVersionOut:
    from fastapi import HTTPException

    resolved = PostgresReferenceParameterRepository(conn).resolve(
        key, jurisdiction, valid_on=valid_on, as_of_tx=as_of or now
    )
    if resolved is None:
        raise HTTPException(404, f"{key}/{jurisdiction} does not resolve at valid_on={valid_on}")
    return ref_param_version_out(resolved)
