"""Сквозные контрактные тесты HTTP-слоя (PRESENTATION.md §10).

Гоняем UC-4 целиком через реальные HTTP-эндпоинты поверх той же тестовой БД,
что и остальные фазы: тариф (draft→validate→publish) → потребление → расчёт →
квитанция → баланс, и сверяем с golden-числами UC-4 (итог 1328.64 при НДС 20%).
Отдельно — идемпотентность приёма потребления и веерный пересчёт после
коррекции справочного параметра.

Изоляция между прогонами (тестовая БД переживает прогон) — через уникальные
account_id/jurisdiction, как в сага-тестах фазы 6/8. Договор-фикстура
«comfort-v1» зашит на юрисдикцию RU, поэтому здесь используем именно её и
уникализируем только аккаунт и tariff_id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from billing.infrastructure.formalization.fixture_contract_formalizer import (
    FixtureContractFormalizer,
)
from billing.interface.http.app import create_app
from billing.interface.http.fixtures import COMFORT_CONTRACT, comfort_fixture
from billing.interface.http.settings import get_settings

# Приём потребления проставляет recorded_at = серверный now, а свёртка расчёта
# фильтрует события по расчётному периоду. Поэтому берём ТЕКУЩИЙ месяц (как в
# проде: потребление регистрируется внутри своего периода), а не фиксированную
# дату — иначе события не попали бы в период и расчёт вышел бы пустым.
_NOW = datetime.now(timezone.utc)
PERIOD = f"{_NOW.year:04d}-{_NOW.month:02d}"
PERIOD_START = f"{_NOW.year:04d}-{_NOW.month:02d}-01T00:00:00Z"
METRIC = "electricity_kwh"


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def jurisdiction() -> str:
    """Уникальная юрисдикция на тест — изоляция в общей тестовой БД
    (``vat_rate/<jurisdiction>`` не пересекается между прогонами)."""
    return _unique("RU")


@pytest.fixture
def client(test_database_url: str, jurisdiction: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BILLING_DATABASE_URL", test_database_url)
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        # договор-фикстура с юрисдикцией этого теста
        c.app.state.formalizer = FixtureContractFormalizer(
            {COMFORT_CONTRACT: comfort_fixture(jurisdiction)}
        )
        yield c
    get_settings.cache_clear()


def _provenance() -> dict:
    return {
        "regulation_ref": "98-FZ",
        "document_id": "doc-1",
        "effective_date": "2024-01-01",
    }


def _register_vat(client: TestClient, jurisdiction: str, value: str = "0.20") -> None:
    r = client.post(
        "/reference-parameters",
        json={
            "key": "vat_rate",
            "jurisdiction": jurisdiction,
            "value": value,
            "valid_from": "2024-01-01T00:00:00Z",
            "provenance": _provenance(),
        },
    )
    assert r.status_code == 201, r.text


def _publish_comfort(client: TestClient) -> str:
    tariff_id = _unique("comfort")
    r = client.post(
        "/tariffs",
        json={"tariff_id": tariff_id, "version": 1, "contract_doc": COMFORT_CONTRACT},
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"

    r = client.post(f"/tariffs/{tariff_id}/versions/1/validate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "validated"

    r = client.post(f"/tariffs/{tariff_id}/versions/1/publish", json={"approved_by": "qa-lead"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    return tariff_id


def test_uc4_end_to_end_through_http(client: TestClient, jurisdiction: str) -> None:
    account_id = _unique("acc")
    _register_vat(client, jurisdiction, "0.20")
    tariff_id = _publish_comfort(client)

    # приём потребления (340 кВт·ч)
    r = client.post(
        f"/accounts/{account_id}/usage",
        json={"metric": METRIC, "quantity": "340", "external_event_id": _unique("evt")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["is_duplicate"] is False

    # расчёт -> сага выпускает квитанцию и проводит начисление
    r = client.post(
        "/assessments",
        json={
            "account_id": account_id,
            "period": PERIOD,
            "tariff_id": tariff_id,
            "tariff_version": 1,
            "metric": METRIC,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["assessment"]["version"] == 1
    assert body["assessment"]["total"]["amount"] == "1328.64"  # golden UC-4
    assert body["invoice"] is not None
    assert body["invoice"]["total"]["amount"] == "1328.64"
    invoice_id = body["invoice"]["invoice_id"]

    # квитанция читается по id
    r = client.get(f"/invoices/{invoice_id}")
    assert r.status_code == 200
    assert r.json()["total"]["amount"] == "1328.64"

    # баланс = сумме квитанции; в леджере одна проведённая дебетовая запись
    r = client.get(f"/accounts/{account_id}/balance")
    assert r.status_code == 200
    assert r.json()["balance"]["amount"] == "1328.64"

    r = client.get(f"/accounts/{account_id}/ledger")
    entries = r.json()
    assert len(entries) == 1
    assert entries[0]["entry_type"] == "posted"
    assert entries[0]["direction"] == "debit"
    assert entries[0]["invoice_id"] == invoice_id


def test_usage_ingestion_is_idempotent(client: TestClient) -> None:
    account_id = _unique("acc")
    ext_id = _unique("evt")
    first = client.post(
        f"/accounts/{account_id}/usage",
        json={"metric": METRIC, "quantity": "100", "external_event_id": ext_id},
    )
    assert first.status_code == 201
    assert first.json()["is_duplicate"] is False

    again = client.post(
        f"/accounts/{account_id}/usage",
        json={"metric": METRIC, "quantity": "100", "external_event_id": ext_id},
    )
    assert again.status_code == 200
    assert again.json()["is_duplicate"] is True


def test_publish_requires_approval(client: TestClient, jurisdiction: str) -> None:
    _register_vat(client, jurisdiction, "0.20")
    tariff_id = _unique("comfort")
    client.post(
        "/tariffs",
        json={"tariff_id": tariff_id, "version": 1, "contract_doc": COMFORT_CONTRACT},
    )
    client.post(f"/tariffs/{tariff_id}/versions/1/validate")
    # publish без approved_by -> домен отклоняет автопубликацию (422)
    r = client.post(f"/tariffs/{tariff_id}/versions/1/publish", json={"approved_by": ""})
    assert r.status_code == 422, r.text


def test_unknown_contract_is_404(client: TestClient) -> None:
    r = client.post(
        "/tariffs",
        json={"tariff_id": _unique("x"), "version": 1, "contract_doc": "no-such-contract"},
    )
    assert r.status_code == 404, r.text


def test_correction_triggers_fan_out_recalculation(client: TestClient, jurisdiction: str) -> None:
    account_id = _unique("acc")
    _register_vat(client, jurisdiction, "0.20")
    tariff_id = _publish_comfort(client)

    client.post(
        f"/accounts/{account_id}/usage",
        json={"metric": METRIC, "quantity": "340", "external_event_id": _unique("evt")},
    )
    r = client.post(
        "/assessments",
        json={
            "account_id": account_id,
            "period": PERIOD,
            "tariff_id": tariff_id,
            "tariff_version": 1,
            "metric": METRIC,
        },
    )
    assert r.json()["assessment"]["total"]["amount"] == "1328.64"

    # коррекция НДС 0.20 -> 0.10 задним числом на период, покрывающий июнь
    r = client.post(
        f"/reference-parameters/vat_rate/{jurisdiction}/corrections",
        json={
            "value": "0.10",
            "valid_from": PERIOD_START,
            "provenance": {**_provenance(), "regulation_ref": "98-FZ amendment"},
        },
    )
    assert r.status_code == 200, r.text

    # веер пересчитал именно этот счёт: активная версия стала v2, сумма изменилась
    r = client.get(f"/assessments/{account_id}/{PERIOD}")
    assert r.status_code == 200
    active = r.json()
    assert active["version"] == 2
    assert active["total"]["amount"] != "1328.64"
