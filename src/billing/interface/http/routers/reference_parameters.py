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


@router.post(
    "",
    status_code=201,
    response_model=VersionIdOut,
    summary="Зарегистрировать новую версию справочного параметра",
    responses={
        409: {"description": "Интервал valid-time пересекается с уже существующей версией"},
        422: {"description": "Не указана провенанс-ссылка на нормативный документ"},
    },
)
def register(
    body: RegisterIn,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> VersionIdOut:
    """Заводит версию параметра `key` в юрисдикции `jurisdiction`.

    Параметр битемпоральный: `valid_from`/`valid_to` задают **valid-time** (когда
    значение действует по закону), время записи (**tx-time**) проставляется само.
    Открытый интервал — `valid_to: null`.

    `provenance` обязателен: у каждого значения должна быть ссылка на нормативный
    акт (`regulation_ref`), идентификатор документа и дата вступления в силу —
    без этого домен отклонит регистрацию (`422`).

    Пересечение valid-time с уже существующей версией того же ключа — конфликт
    (`409`). Чтобы заменить действующее значение задним числом, используйте не
    этот эндпоинт, а `POST /{key}/{jurisdiction}/corrections`.

    Возвращает `version_id` созданной версии.
    """
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


@router.post(
    "/{key}/{jurisdiction}/corrections",
    response_model=CorrectionOut,
    summary="Ретроактивно исправить значение параметра (запускает веерный пересчёт)",
    responses={
        404: {"description": "Параметр key/jurisdiction не зарегистрирован"},
        422: {"description": "Не указана провенанс-ссылка на нормативный документ"},
    },
)
def correct(
    key: str,
    jurisdiction: str,
    body: CorrectIn,
    dispatcher=Depends(get_dispatcher),
    config=Depends(settings),
    now: datetime = Depends(get_now),
) -> CorrectionOut:
    """Исправляет значение параметра задним числом и пересчитывает всё, что на нём стояло.

    Самый «тяжёлый» эндпоинт API. Отличие от `POST /reference-parameters`: там
    регистрируется новая версия на непересекающемся интервале, здесь — новое
    значение **поверх** уже действовавшего (типовой случай: регулятор задним
    числом поменял ставку). Старые версии не удаляются, а помечаются
    superseded — история остаётся восстановимой (битемпоральность).

    Побочный эффект — сага: коррекция порождает событие
    `ReferenceParameterCorrected`, диспетчер веером пересчитывает **только те**
    начисления, чьи тарифы реально читают этот параметр, и выпускает по ним
    корректирующие квитанции. Сбой по отдельному лицевому счёту не роняет весь
    веер: такой счёт уходит в dead-letter, остальные досчитываются.

    Коррекция коммитится до диспатча, чтобы обработчики саги (на своих
    соединениях) увидели уже закоммиченную версию.

    В ответе:
    - `version_id` — новая версия параметра;
    - `superseded_count` — сколько версий она перекрыла;
    - `recalculated_versions`, `dead_letters` — зарезервированы под сводку веера,
      сейчас всегда пусты/нули; фактический результат пересчёта смотрите в
      `GET /assessments/{account_id}/{period}` и в таблице dead-letter.
    """
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


@router.post(
    "/{key}/{jurisdiction}/repeal",
    response_model=VersionIdOut,
    summary="Отменить параметр начиная с даты",
    responses={
        404: {"description": "Параметр key/jurisdiction не зарегистрирован"},
        422: {
            "description": "Дата отмены раньше начала действующей версии "
            "или не указан провенанс"
        },
    },
)
def repeal(
    key: str,
    jurisdiction: str,
    body: RepealIn,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> VersionIdOut:
    """Закрывает действие параметра начиная с `repeal_from` (нормативный акт утратил силу).

    Значение не стирается: действующая версия получает конечную границу
    valid-time, поэтому расчёты за прошлые периоды продолжают резолвиться как
    раньше. После `repeal_from` параметр перестаёт резолвиться — начисление по
    тарифу, который его читает, упадёт с `422`
    (`UnresolvedReferenceParameterError`).

    Отмена сама по себе **не** запускает пересчёт (в отличие от `corrections`):
    речь о будущем, а не об исправлении прошлого. `repeal_from` раньше начала
    действующей версии — `422`.
    """
    version = PostgresReferenceParameterRepository(conn).repeal(
        key, jurisdiction, body.repeal_from, _provenance(body.provenance), now=now
    )
    return VersionIdOut(version_id=str(version.version_id))


@router.get(
    "/{key}/{jurisdiction}",
    response_model=RefParamVersionOut,
    summary="Разрешить значение параметра на момент времени (битемпорально)",
    responses={404: {"description": "На заданный valid_on/as_of параметр не резолвится"}},
)
def resolve(
    key: str,
    jurisdiction: str,
    valid_on: datetime = Query(..., description="момент valid-time, на который резолвим"),
    as_of: datetime | None = Query(None, description="tx-time; по умолчанию — сейчас"),
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> RefParamVersionOut:
    """Возвращает версию параметра, действовавшую в заданной точке битемпорального времени.

    Две независимые оси:
    - `valid_on` (обязательный) — **valid-time**: на какой момент реального мира
      нас интересует значение («какая ставка НДС действовала в июне 2026?»);
    - `as_of` (опционально, по умолчанию «сейчас») — **tx-time**: по состоянию
      базы на какой момент отвечаем («что мы *знали* об этой ставке до того, как
      прилетела ретроактивная коррекция?»).

    Фиксируя `as_of` в прошлом, можно воспроизвести расчёт ровно так, как он
    считался тогда, — даже если параметр с тех пор корректировали. Именно этим
    механизмом пользуется пересчёт начислений.

    Если на заданной паре координат ни одна версия не действует (не заведена,
    отменена или ещё не была записана) — `404`.
    """
    from fastapi import HTTPException

    resolved = PostgresReferenceParameterRepository(conn).resolve(
        key, jurisdiction, valid_on=valid_on, as_of_tx=as_of or now
    )
    if resolved is None:
        raise HTTPException(404, f"{key}/{jurisdiction} does not resolve at valid_on={valid_on}")
    return ref_param_version_out(resolved)
