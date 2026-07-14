#!/usr/bin/env bash
# Комплексный сквозной тест логики расчёта через curl (UC-4 -> UC-7 -> UC-10).
#
# Сценарий:
#   1. тариф «Комфорт» draft -> validate -> publish
#   2. НЕСКОЛЬКО фактов потребления на ДВА лицевых счёта (свёртка суммирует их)
#   3. два начисления за июль: acc-A (340 кВт·ч) и acc-B (500 кВт·ч)
#   4. коррекция vat_rate 0.20 -> 0.25 «с 1 июля» -> веер пересчитывает ОБА счёта
#   5. diff v1 vs v2 по каждому счёту: изменилась только строка НДС
#   6. откат vat_rate обратно в 0.20 -> веер возвращает суммы (v3)
#
# ⚠️ ГЛОБАЛЬНОЕ СОСТОЯНИЕ. vat_rate/RU — общий параметр (юрисдикция зашита в
# фикстуру договора comfort-v1, через HTTP свою не подставить). Поэтому веер
# заденет и ЧУЖИЕ активные начисления за текущий месяц (от прошлых прогонов):
# они тоже получат +2 версии и корректирующие квитанции. Шаг 6 возвращает
# ставку в 0.20, так что итоговые суммы у всех восстанавливаются, а uc4_curl.sh
# со своим golden 1328.64 продолжает сходиться. Если скрипт прервать между
# шагами 4 и 6 — НДС останется 0.25, и его надо вернуть руками.
#
# ⚠️ Потребление всегда попадает в ТЕКУЩИЙ месяц: recorded_at ставит сервер, по
# нему же фильтруется период (осознанное упрощение, открытый вопрос №3 в
# use_case.md). Задним числом факт не записать — поэтому оба начисления за
# текущий месяц, а изоляция коррекции по датам проверяется на уровне резолва
# параметра (шаг 4в: июнь по-прежнему видит 0.20).
#
# Запуск:  bash recalc_diff_curl.sh            (IP ниже)
#          bash recalc_diff_curl.sh 1.2.3.4
set -uo pipefail

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
SERVER_IP="${1:-${SERVER_IP:-158.160.241.142}}"
SERVER_PORT="${SERVER_PORT:-8000}"
SCHEME="${SCHEME:-http}"
# ─────────────────────────────────────────────────────────────────────────────

BASE="$SCHEME://$SERVER_IP:$SERVER_PORT"
SUF="$(date +%s)-$RANDOM"
ACC_A="acc-a-$SUF"
ACC_B="acc-b-$SUF"
TARIFF="comfort-$SUF"
JUR="RU"                                  # зашита в фикстуру comfort-v1
PERIOD="$(date -u +%Y-%m)"                # текущий месяц: recorded_at ставит сервер
MONTH_START="$(date -u +%Y-%m)-01T00:00:00Z"
PREV_MONTH="$(python3 -c "
import datetime as d
t = d.datetime.now(d.timezone.utc).replace(day=1) - d.timedelta(days=1)
print(t.strftime('%Y-%m-15T00:00:00Z'))")"   # середина прошлого месяца — для резолва

PASS=0; FAIL=0
FAILED=()
SECTION=""
ok()   { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s  \033[2m(%s)\033[0m\n' "$1" "$2"; }
bad()  {
  FAIL=$((FAIL+1))
  FAILED+=("[$SECTION] $1 — ждали: $2, факт: $3")
  printf '  \033[31m✗ %s\033[0m\n      ждали: %s\n      факт : %s\n' "$1" "$2" "$3"
}
eq()   { [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$2" "$3"; }
# Деньги сравниваем ЧИСЛЕННО, а не строкой: API отдаёт Decimal как есть, без
# выравнивания копеек ("2120", "960", "1384.0" — не "2120.00"). Копейки при этом
# не теряются, но 2120 == 2120.00, и тест не должен падать на форматировании.
money() { python3 -c "
import sys
from decimal import Decimal
try: print(Decimal(sys.argv[1]).quantize(Decimal('0.01')))
except Exception: print(sys.argv[1])" "$1"; }
eqm()  { local e a; e="$(money "$2")"; a="$(money "$3")"
         [ "$e" = "$a" ] && ok "$1" "$a" || bad "$1" "$e" "$a"; }
step() { SECTION="${1%%.*}"; printf '\n\033[1m%s\033[0m\n' "$1"; }
note() { printf '     \033[2m%s\033[0m\n' "$1"; }

call() {
  local method="$1" url="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -sS -X "$method" "$BASE$url" -H 'Content-Type: application/json' -d "$data" -w '|%{http_code}' 2>/dev/null
  else
    curl -sS -X "$method" "$BASE$url" -w '|%{http_code}' 2>/dev/null
  fi
}
code()  { printf '%s' "$1" | awk -F'|' '{print $NF}'; }
body()  { printf '%s' "$1" | sed 's/|[0-9]*$//'; }
field() { printf '%s' "$1" | python3 -c "import json,sys; print(json.load(sys.stdin)$2)" 2>/dev/null || echo "<нет>"; }
prov()  { printf '{"regulation_ref":"%s","document_id":"doc-%s","effective_date":"2024-01-01"}' "$1" "$SUF"; }

# значение vat_rate/RU на момент valid-time
vat_at() { local r; r="$(call GET "/reference-parameters/vat_rate/$JUR?valid_on=$1")"
           if [ "$(code "$r")" = "404" ]; then echo "404"; else field "$(body "$r")" "['value']"; fi; }
# активное начисление: версия / итог
version() { field "$(body "$(call GET "/assessments/$1/$PERIOD")")" "['version']"; }
total()   { field "$(body "$(call GET "/assessments/$1/$PERIOD")")" "['total']['amount']"; }
balance() { field "$(body "$(call GET "/accounts/$1/balance")")" "['balance']['amount']"; }
# строка diff по метке правила: $1=тело diff, $2=rule_label, $3=before|after|changed
diff_line() {
  printf '%s' "$1" | python3 -c "
import json,sys
d = json.load(sys.stdin)
ln = next((l for l in d['line_diffs'] if l['rule_label'] == '$2'), None)
if ln is None: print('<нет строки $2>'); raise SystemExit
f = '$3'
v = ln[f]
print(v if f == 'changed' else ('—' if v is None else v['amount']))" 2>/dev/null || echo "<нет>"
}

printf '\033[1mСервер:\033[0m %s\n\033[1mПериод:\033[0m %s\n\033[1mСчета:\033[0m %s, %s\n\033[1mТариф:\033[0m %s\n' \
  "$BASE" "$PERIOD" "$ACC_A" "$ACC_B" "$TARIFF"

# ═══ 0. ПРЕДУСЛОВИЕ: НДС на стенде равен 0.20 ════════════════════════════════
step "0. Базовая ставка НДС на стенде"
call POST /reference-parameters "$(printf '{"key":"vat_rate","jurisdiction":"%s","value":"0.20","valid_from":"2024-01-01T00:00:00Z","provenance":%s}' "$JUR" "$(prov 98-FZ)")" >/dev/null
VAT0="$(vat_at "$MONTH_START")"
if [ "$VAT0" != "0.20" ]; then
  printf '  \033[31m✗ vat_rate/%s на %s = %s, а не 0.20\033[0m\n' "$JUR" "$PERIOD" "$VAT0"
  printf '    Стенд грязный (прерванный прогон?). Верните ставку и повторите:\n'
  printf "    curl -X POST %s/reference-parameters/vat_rate/%s/corrections -H 'Content-Type: application/json' \\\\\n" "$BASE" "$JUR"
  printf '      -d '"'"'{"value":"0.20","valid_from":"%s","provenance":{"regulation_ref":"98-FZ","document_id":"restore","effective_date":"2024-01-01"}}'"'"'\n' "$MONTH_START"
  exit 1
fi
ok "vat_rate/$JUR действует и равен 0.20" "$VAT0"

# ═══ 1. ТАРИФ ════════════════════════════════════════════════════════════════
step "1. Тариф «Комфорт»: draft -> validate -> publish"
eq "draft из договора comfort-v1" 201 "$(code "$(call POST /tariffs "$(printf '{"tariff_id":"%s","version":1,"contract_doc":"comfort-v1"}' "$TARIFF")")")"
R="$(call POST "/tariffs/$TARIFF/versions/1/validate")"
eq "validate: компиляция Catala + резолв vat_rate" "validated" "$(field "$(body "$R")" "['status']")"
R="$(call POST "/tariffs/$TARIFF/versions/1/publish" '{"approved_by":"qa-lead"}')"
eq "publish (с апрувером)" "published" "$(field "$(body "$R")" "['status']")"
note "база 300 кВт·ч × 3.20, превышение ×1.15, НДС отдельной ставкой"

# ═══ 2. НЕСКОЛЬКО ФАКТОВ ПОТРЕБЛЕНИЯ ═════════════════════════════════════════
usage() { # $1=account $2=qty $3=idx
  code "$(call POST "/accounts/$1/usage" "$(printf '{"metric":"electricity_kwh","quantity":"%s","external_event_id":"evt-%s-%s"}' "$2" "$SUF" "$3")")"
}
step "2. Потребление приходит порциями — свёртка обязана их сложить"
eq "A: 120 кВт·ч -> 201" 201 "$(usage "$ACC_A" 120 a1)"
eq "A: 150 кВт·ч -> 201" 201 "$(usage "$ACC_A" 150 a2)"
eq "A: 70  кВт·ч -> 201" 201 "$(usage "$ACC_A" 70  a3)"
eq "A: тот же external_event_id повторно -> 200 (идемпотентность)" 200 "$(usage "$ACC_A" 70 a3)"
eq "B: 300 кВт·ч -> 201" 201 "$(usage "$ACC_B" 300 b1)"
eq "B: 200 кВт·ч -> 201" 201 "$(usage "$ACC_B" 200 b2)"

SUM_A="$(printf '%s' "$(body "$(call GET "/accounts/$ACC_A/usage?metric=electricity_kwh&period=$PERIOD")")" | python3 -c "
import json,sys; print(int(sum(float(e['quantity']) for e in json.load(sys.stdin))))")"
eq "A: за период накопилось 340 кВт·ч (дубль не записался)" 340 "$SUM_A"

# ═══ 3. ДВА НАЧИСЛЕНИЯ ЗА ИЮЛЬ ═══════════════════════════════════════════════
assess() { call POST /assessments "$(printf '{"account_id":"%s","period":"%s","tariff_id":"%s","tariff_version":1,"metric":"electricity_kwh"}' "$1" "$PERIOD" "$TARIFF")"; }
step "3. Начисляем за $PERIOD по обоим счетам (расчёт -> квитанция -> проводка)"
R="$(assess "$ACC_A")"
eq  "A: начисление создано -> 201" 201 "$(code "$R")"
eqm "A: 340 кВт·ч = 960.00 базы + 147.20 превышения + 221.44 НДС" "1328.64" "$(field "$(body "$R")" "['assessment']['total']['amount']")"
eq  "A: квитанция выпущена сагой" "$PERIOD" "$(field "$(body "$R")" "['invoice']['period']")"
INV_A="$(field "$(body "$R")" "['invoice']['invoice_id']")"

R="$(assess "$ACC_B")"
eq  "B: начисление создано -> 201" 201 "$(code "$R")"
eqm "B: 500 кВт·ч = 960.00 базы + 736.00 превышения + 339.20 НДС" "2035.20" "$(field "$(body "$R")" "['assessment']['total']['amount']")"

eq  "A: версия начисления = 1" 1 "$(version "$ACC_A")"
eqm "A: баланс = сумме квитанции" "1328.64" "$(balance "$ACC_A")"
eqm "B: баланс = сумме квитанции" "2035.20" "$(balance "$ACC_B")"

eq "повторный расчёт того же периода -> 409 (для этого есть /recalculate)" 409 "$(code "$(assess "$ACC_A")")"

# ═══ 4. КОРРЕКЦИЯ ПАРАМЕТРА -> ВЕЕРНЫЙ ПЕРЕСЧЁТ ══════════════════════════════
step "4. Регулятор поднял НДС: 0.20 -> 0.25 начиная с $MONTH_START"
R="$(call POST "/reference-parameters/vat_rate/$JUR/corrections" "$(printf '{"value":"0.25","valid_from":"%s","provenance":%s}' "$MONTH_START" "$(prov 98-FZ-amendment)")")"
eq "коррекция принята -> 200" 200 "$(code "$R")"
eq "перекрыта прежняя версия ставки" 1 "$(field "$(body "$R")" "['superseded_count']")"
note "коррекция сама разослала ReferenceParameterCorrected — /recalculate руками не дёргаем"

step "4а. Веер пересчитал ОБА счёта — не трогали ни один эндпоинт начислений"
eq  "A: версия 1 -> 2" 2 "$(version "$ACC_A")"
eqm "A: итог 1328.64 -> 1384.00 (НДС 276.80)" "1384.00" "$(total "$ACC_A")"
eq  "B: версия 1 -> 2" 2 "$(version "$ACC_B")"
eqm "B: итог 2035.20 -> 2120.00 (НДС 424.00)" "2120.00" "$(total "$ACC_B")"

step "4б. Корректирующая квитанция и компенсирующая проводка (не переписали старые)"
LEDGER_A="$(body "$(call GET "/accounts/$ACC_A/ledger")")"
eq "A: в журнале две проводки — начисление и коррекция" 2 "$(printf '%s' "$LEDGER_A" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
eq "A: коррекция проведена дебетом на дельту 55.36" "debit 55.36" "$(printf '%s' "$LEDGER_A" | python3 -c "
import json,sys
from decimal import Decimal
e = [x for x in json.load(sys.stdin) if x['corrects_invoice_id']][0]
print(e['direction'], Decimal(e['amount']['amount']).quantize(Decimal('0.01')))")"
eq "A: коррекция ссылается на исходную квитанцию" "$INV_A" "$(printf '%s' "$LEDGER_A" | python3 -c "
import json,sys
print([x for x in json.load(sys.stdin) if x['corrects_invoice_id']][0]['corrects_invoice_id'])")"
eqm "A: баланс = 1384.00 (1328.64 + 55.36)" "1384.00" "$(balance "$ACC_A")"

step "4в. Коррекция «с 1 июля» не пробила дыру в прошлом"
eq "vat_rate на середину прошлого месяца -> всё ещё 0.20" "0.20" "$(vat_at "$PREV_MONTH")"
note "начисления прошлых периодов остались бы на 0.20 — веер выбирает их по пересечению периода с valid-time коррекции"

# ═══ 5. DIFF ДВУХ ВЕРСИЙ НАЧИСЛЕНИЯ ══════════════════════════════════════════
step "5. Diff v1 vs v2 — главный ответ на «почему сумма изменилась»"
D="$(body "$(call GET "/assessments/$ACC_A/$PERIOD/diff?v1=1&v2=2")")"
eqm "A: было 1328.64" "1328.64" "$(field "$D" "['total_before']['amount']")"
eqm "A: стало 1384.00" "1384.00" "$(field "$D" "['total_after']['amount']")"
eq  "A: база не менялась (960.00)"        "False" "$(diff_line "$D" base_amount changed)"
eq  "A: превышение не менялось (147.20)"  "False" "$(diff_line "$D" overage_amount changed)"
eq  "A: НДС изменился"                    "True"  "$(diff_line "$D" vat_amount changed)"
eqm "A: НДС было 221.44"                  "221.44" "$(diff_line "$D" vat_amount before)"
eqm "A: НДС стало 276.80"                 "276.80" "$(diff_line "$D" vat_amount after)"
eq  "A: виновник назван поимённо" "['vat_rate']" "$(field "$D" "['changed_parameter_keys']")"
note "дельта 55.36 = ровно то, что ушло в корректирующую квитанцию"

D="$(body "$(call GET "/assessments/$ACC_B/$PERIOD/diff?v1=1&v2=2")")"
eqm "B: было 2035.20" "2035.20" "$(field "$D" "['total_before']['amount']")"
eqm "B: стало 2120.00" "2120.00" "$(field "$D" "['total_after']['amount']")"
eq  "B: изменилась только строка НДС" "True|False|False" \
    "$(diff_line "$D" vat_amount changed)|$(diff_line "$D" base_amount changed)|$(diff_line "$D" overage_amount changed)"

step "5а. Diff — это query по неизменяемым версиям, а не состояние"
eqm "тот же diff воспроизводится повторно" "1384.00" \
    "$(field "$(body "$(call GET "/assessments/$ACC_A/$PERIOD/diff?v1=1&v2=2")")" "['total_after']['amount']")"
D2="$(body "$(call GET "/assessments/$ACC_A/$PERIOD/diff?v1=2&v2=1")")"
eqm "порядок важен: v1=2,v2=1 — «было» становится 1384.00" "1384.00" "$(field "$D2" "['total_before']['amount']")"
eqm "порядок важен: v1=2,v2=1 — «стало» становится 1328.64" "1328.64" "$(field "$D2" "['total_after']['amount']")"
eq  "несуществующая версия -> 404" 404 "$(code "$(call GET "/assessments/$ACC_A/$PERIOD/diff?v1=1&v2=99")")"

# ═══ 6. ОТКАТ: возвращаем стенд в исходное ═══════════════════════════════════
step "6. Откат НДС 0.25 -> 0.20 (тот же веер, в обратную сторону)"
R="$(call POST "/reference-parameters/vat_rate/$JUR/corrections" "$(printf '{"value":"0.20","valid_from":"%s","provenance":%s}' "$MONTH_START" "$(prov 98-FZ-restore)")")"
eq "обратная коррекция -> 200" 200 "$(code "$R")"
eq "vat_rate снова 0.20" "0.20" "$(vat_at "$MONTH_START")"
eq  "A: версия 2 -> 3" 3 "$(version "$ACC_A")"
eqm "A: итог вернулся к 1328.64" "1328.64" "$(total "$ACC_A")"
eq  "B: версия 2 -> 3" 3 "$(version "$ACC_B")"
eqm "B: итог вернулся к 2035.20" "2035.20" "$(total "$ACC_B")"
eqm "A: баланс снова 1328.64 (кредитовая проводка −55.36)" "1328.64" "$(balance "$ACC_A")"
eq  "A: в журнале три проводки — ни одна не переписана" 3 \
   "$(printf '%s' "$(body "$(call GET "/accounts/$ACC_A/ledger")")" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
note "история цела: v1 -> v2 -> v3, три квитанции, три проводки"

# ═════════════════════════════════════════════════════════════════════════════
printf '\n════════════════════════════════════════════════════════════\n'
if [ "$FAIL" -eq 0 ]; then
  printf '\033[1;32m  ВСЁ ЗЕЛЁНОЕ — %s/%s проверок пройдено\033[0m\n' "$PASS" "$((PASS+FAIL))"
else
  printf '\033[1;31m  ПРОВАЛЕНО %s из %s:\033[0m\n\n' "$FAIL" "$((PASS+FAIL))"
  for f in "${FAILED[@]}"; do printf '   \033[31m✗\033[0m %s\n' "$f"; done
fi
printf '════════════════════════════════════════════════════════════\n'

VAT_NOW="$(vat_at "$MONTH_START")"
if [ "$VAT_NOW" != "0.20" ]; then
  printf '\n\033[1;31m  ⚠️  vat_rate/%s остался %s — стенд НЕ восстановлен!\033[0m\n' "$JUR" "$VAT_NOW"
  printf '  Верните вручную:\n'
  printf "  curl -X POST %s/reference-parameters/vat_rate/%s/corrections -H 'Content-Type: application/json' \\\\\n" "$BASE" "$JUR"
  printf '    -d '"'"'{"value":"0.20","valid_from":"%s","provenance":{"regulation_ref":"98-FZ","document_id":"restore","effective_date":"2024-01-01"}}'"'"'\n' "$MONTH_START"
fi

cat <<EOF

Осталось в БД (DELETE-эндпоинтов нет):
  reference_parameter_version : vat_rate/$JUR — +2 версии (коррекция и откат), значение снова 0.20
  tariff_version / artifact   : $TARIFF v1 (published)
  usage_event                 : 5 событий на $ACC_A / $ACC_B (дубль не записался)
  billing_assessment          : по 3 версии на счёт ($PERIOD)
  invoice / ledger_entry      : по 3 квитанции и 3 проводки на счёт
Убрать:
  delete from usage_event where account_id in ('$ACC_A','$ACC_B');
EOF
[ "$FAIL" -eq 0 ]
