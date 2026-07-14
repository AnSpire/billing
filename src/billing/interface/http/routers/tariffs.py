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


@router.post(
    "",
    status_code=201,
    response_model=TariffVersionOut,
    summary="Создать черновик версии тарифа из текста договора (формализация)",
    responses={
        404: {"description": "Формализатор не знает такой договор (мок-фикстуры)"},
        409: {"description": "Версия (tariff_id, version) уже существует и неизменяема"},
    },
)
def draft(
    body: DraftIn,
    formalizer: ContractFormalizer = Depends(get_formalizer),
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> TariffVersionOut:
    """Первый шаг жизненного цикла тарифа: **draft → validate → publish**.

    `contract_doc` (текст договора/нормативного акта) прогоняется через
    `ContractFormalizer.formalize`, и результат — исполнимая форма расчёта плюс
    список требуемых справочных параметров — сохраняется как черновик версии.

    ⚠️ В этой сборке формализатор — **заглушка на фикстурах**, а не AI-агент: он
    узнаёт только заранее заведённые тексты договоров, на незнакомом ответит
    `404`. Это тот шов, куда встраивается настоящая формализация.

    Черновик ещё ничего не считает: его формулы не скомпилированы, а ссылки на
    справочные параметры не разрешены — этим займётся `validate`. Начислять по
    черновику нельзя, `POST /assessments` принимает только опубликованные версии.

    ⚠️ Повторный `POST` на ту же пару `(tariff_id, version)` — **не** ошибка, пока
    версия не опубликована: он молча сбрасывает её обратно в `draft` (в том числе
    уже прошедшую `validate`), причём тело тарифа при этом не перезаписывается.
    Неизменяемость включается только после `publish` — тогда повтор даёт `409`.
    Для новой редакции договора инкрементируйте `version`, а не переотправляйте ту же.
    """
    result = formalizer.formalize(body.contract_doc)  # UnknownContractError -> 404
    draft, _event = TariffVersion.draft_from_text(body.tariff_id, body.version, result, now=now)
    PostgresTariffVersionRepository(conn).save(draft)
    return tariff_version_out(draft)


def _load(conn: Connection, tariff_id: str, version: int) -> TariffVersion:
    tariff = PostgresTariffVersionRepository(conn).get(tariff_id, version)
    if tariff is None:
        raise HTTPException(404, f"tariff version ({tariff_id}, {version}) not found")
    return tariff


@router.post(
    "/{tariff_id}/versions/{version}/validate",
    response_model=TariffVersionOut,
    summary="Проверить черновик: компиляция Catala + резолв справочных параметров",
    responses={
        404: {"description": "Версия тарифа не найдена"},
        409: {"description": "Версия не в статусе draft — переход недопустим"},
        422: {
            "description": "Catala не скомпилировалась или ссылка на справочный "
            "параметр не разрешается"
        },
    },
)
def validate(
    tariff_id: str,
    version: int,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> TariffVersionOut:
    """Второй шаг: доводит черновик до состояния, в котором по нему можно считать.

    Делает две вещи:
    1. **Компилирует** формализованные формулы (Catala) и складывает готовый
       артефакт — именно его потом дёргает движок расчёта, компиляции в момент
       начисления не происходит.
    2. **Резолвит биндинги** — проверяет, что каждый справочный параметр, который
       читает тариф, реально заведён в нужной юрисдикции и разрешается на дату
       действия. Неразрешённая ссылка — `422`, и это специально: лучше упасть
       здесь, чем на живом начислении.

    Успешный вызов переводит версию `draft → validated`. Валидировать не-черновик
    нельзя (`409`); ошибка компиляции (`422`) оставляет версию черновиком —
    поправьте договор и заведите новую версию.
    """
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


@router.post(
    "/{tariff_id}/versions/{version}/publish",
    response_model=TariffVersionOut,
    summary="Опубликовать проверенную версию (требуется человек-апрувер)",
    responses={
        404: {"description": "Версия тарифа не найдена"},
        409: {"description": "Версия не в статусе validated — переход недопустим"},
        422: {"description": "approved_by пуст: автопубликация запрещена"},
    },
)
def publish(
    tariff_id: str,
    version: int,
    body: PublishIn,
    conn: Connection = Depends(db_connection),
    now: datetime = Depends(get_now),
) -> TariffVersionOut:
    """Третий шаг: `validated → published`. Только с этого момента по тарифу можно начислять.

    `approved_by` обязателен и должен быть непустым — домен отклонит публикацию с
    `422` (`PublishRequiresApprovalError`). Это сознательный барьер: тариф
    рождается из машинной формализации текста договора, и между «машина
    разобрала» и «этим начисляют людям деньги» должен стоять человек, чьё имя
    останется в аудите. Автопубликации не существует ни на каком пути.

    Публиковать можно только `validated` (`409` иначе): опубликовать
    нескомпилированный черновик нельзя в принципе.
    """
    repo = PostgresTariffVersionRepository(conn)
    validated = _load(conn, tariff_id, version)
    published, _event = validated.publish(approved_by=body.approved_by, now=now)
    repo.save(published)
    return tariff_version_out(published)


@router.get(
    "/{tariff_id}/versions/{version}",
    response_model=TariffVersionOut,
    summary="Получить версию тарифа (статус, форма расчёта, биндинги)",
    responses={404: {"description": "Версия тарифа не найдена"}},
)
def get(
    tariff_id: str,
    version: int,
    conn: Connection = Depends(db_connection),
) -> TariffVersionOut:
    """Возвращает версию тарифа в её текущем статусе (`draft` / `validated` / `published`).

    В ответе — формализованная форма расчёта и разрешённые биндинги справочных
    параметров, то есть ровно то, по чему движок будет считать. Полезно, чтобы
    убедиться перед `POST /assessments`, что версия действительно опубликована и
    ссылается на те параметры, на которые вы рассчитываете.

    Версии неизменяемы: содержимое опубликованной версии не поменяется задним
    числом, поэтому на пару `(tariff_id, version)` можно безопасно ссылаться из
    начислений.
    """
    return tariff_version_out(_load(conn, tariff_id, version))
