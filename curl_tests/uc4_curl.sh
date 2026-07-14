#!/usr/bin/env bash
# UC-4 end-to-end через curl — повторение tests/stage9::test_uc4_end_to_end_through_http.
# Ожидаемый итог: 1328.64 (340 кВт·ч; база 300 по 3.20, превышение с надбавкой 15%, НДС 20%).
#
# Запуск:  bash uc4_curl.sh            — использует SERVER_IP/PORT ниже
#          bash uc4_curl.sh 1.2.3.4    — IP аргументом
#          SERVER_IP=1.2.3.4 bash uc4_curl.sh
set -euo pipefail

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
SERVER_IP="${1:-${SERVER_IP:-158.160.241.142}}"   # <-- ВПИШИТЕ IP СЕРВЕРА
SERVER_PORT="${SERVER_PORT:-8000}"
SCHEME="${SCHEME:-http}"
# ─────────────────────────────────────────────────────────────────────────────

BASE="$SCHEME://$SERVER_IP:$SERVER_PORT"

SUF="$(date +%s)-$RANDOM"
ACCOUNT="acc-$SUF"
TARIFF="comfort-$SUF"
EVENT="evt-$SUF"
PERIOD="$(date -u +%Y-%m)"    # ТЕКУЩИЙ месяц по UTC: recorded_at ставит сервер,
                              # расчёт фильтрует потребление по периоду
JURISDICTION="RU"             # зашита в фикстуру договора comfort-v1

j()    { python3 -m json.tool 2>/dev/null || cat; }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

printf '\033[1mСервер:\033[0m %s\n\033[1mПериод:\033[0m %s\n\033[1mСоздаём:\033[0m account=%s tariff=%s\n' \
  "$BASE" "$PERIOD" "$ACCOUNT" "$TARIFF"

step "0. health"
curl -fsS "$BASE/health" | j

step "1. vat_rate=0.20 для юрисдикции $JURISDICTION"
echo "   (409 'overlapping valid-time' = параметр уже заведён, это нормально — идём дальше)"
curl -sS -w '\n-> HTTP %{http_code}\n' -X POST "$BASE/reference-parameters" \
  -H 'Content-Type: application/json' -d '{
    "key": "vat_rate", "jurisdiction": "'"$JURISDICTION"'", "value": "0.20",
    "valid_from": "2024-01-01T00:00:00Z",
    "provenance": {"regulation_ref": "98-FZ", "document_id": "doc-1", "effective_date": "2024-01-01"}
  }' | j

step "2. draft тарифа $TARIFF из договора comfort-v1  (ждём status=draft)"
curl -fsS -X POST "$BASE/tariffs" -H 'Content-Type: application/json' \
  -d '{"tariff_id": "'"$TARIFF"'", "version": 1, "contract_doc": "comfort-v1"}' | j

step "3. validate — компиляция Catala + резолв vat_rate  (ждём status=validated)"
curl -fsS -X POST "$BASE/tariffs/$TARIFF/versions/1/validate" | j

step "4. publish — approved_by обязателен  (ждём status=published)"
curl -fsS -X POST "$BASE/tariffs/$TARIFF/versions/1/publish" \
  -H 'Content-Type: application/json' -d '{"approved_by": "qa-lead"}' | j

step "5. потребление 340 кВт·ч на $ACCOUNT  (ждём is_duplicate=false)"
curl -fsS -X POST "$BASE/accounts/$ACCOUNT/usage" -H 'Content-Type: application/json' \
  -d '{"metric": "electricity_kwh", "quantity": "340", "external_event_id": "'"$EVENT"'"}' | j

step "5a. тот же external_event_id ещё раз — идемпотентность  (ждём is_duplicate=true)"
curl -fsS -X POST "$BASE/accounts/$ACCOUNT/usage" -H 'Content-Type: application/json' \
  -d '{"metric": "electricity_kwh", "quantity": "340", "external_event_id": "'"$EVENT"'"}' | j

step "6. расчёт за $PERIOD -> сага выпускает квитанцию и проводит начисление"
CALC="$(curl -fsS -X POST "$BASE/assessments" -H 'Content-Type: application/json' \
  -d '{"account_id": "'"$ACCOUNT"'", "period": "'"$PERIOD"'",
       "tariff_id": "'"$TARIFF"'", "tariff_version": 1, "metric": "electricity_kwh"}')"
echo "$CALC" | j
INVOICE_ID="$(echo "$CALC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["invoice"]["invoice_id"])')"

step "7. квитанция $INVOICE_ID читается по id"
curl -fsS "$BASE/invoices/$INVOICE_ID" | j

step "8. баланс лицевого счёта (ждём 1328.64)"
curl -fsS "$BASE/accounts/$ACCOUNT/balance" | j

step "9. журнал проводок (ждём одну запись: posted / debit)"
curl -fsS "$BASE/accounts/$ACCOUNT/ledger" | j

step "ИТОГ: сверка с golden UC-4"
echo "$CALC" | python3 -c '
import json, sys
b = json.load(sys.stdin)
total = b["assessment"]["total"]["amount"]
inv   = b["invoice"]["total"]["amount"]
ok = total == "1328.64" == inv
print(f"  assessment.total = {total}")
print(f"  invoice.total    = {inv}")
print("  =>", "\033[32mOK — совпало с golden 1328.64\033[0m" if ok
           else "\033[31mРАСХОЖДЕНИЕ: ждали 1328.64\033[0m")
sys.exit(0 if ok else 1)'

cat <<EOF

Осталось в БД (ничего не удаляется — DELETE-эндпоинтов нет):
  reference_parameter_version : vat_rate/$JURISDICTION  (если создан этим прогоном; ОБЩИЙ для всех)
  tariff_version              : $TARIFF v1 (published)
  tariff_artifact             : артефакт Catala для $TARIFF v1
  usage_event                 : 1 событие ($EVENT), дубль не записался
  billing_assessment          : 1 начисление ($ACCOUNT, $PERIOD, v1)
  invoice                     : $INVOICE_ID
  ledger_entry                : 1 проводка (posted/debit)
EOF
