#!/usr/bin/env bash
# Битемпоральность ReferenceParameter через curl против задеплоенного API.
#
# Две НЕЗАВИСИМЫЕ оси времени:
#   valid-time (valid_on) — когда значение действует по закону
#   tx-time    (as_of)    — когда система о нём узнала
#
# Ключевая клетка, где раньше был баг: свежий as_of × СТАРАЯ valid_on.
# Ретроактивная коррекция «с июля» не должна пробивать дыру в июне.
#
# ИЗОЛЯЦИЯ: параметры ключуются парой (key, jurisdiction) -> берём одноразовую
# юрисдикцию. Скрипт не трогает vat_rate/RU и не запускает веерный пересчёт
# чужих начислений. Безопасен на боевом стенде.
#
# Запуск:  bash bitemporal_curl.sh            (IP ниже)
#          bash bitemporal_curl.sh 1.2.3.4
set -uo pipefail

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
SERVER_IP="${1:-${SERVER_IP:-158.160.241.142}}"
SERVER_PORT="${SERVER_PORT:-8000}"
SCHEME="${SCHEME:-http}"
# ─────────────────────────────────────────────────────────────────────────────

BASE="$SCHEME://$SERVER_IP:$SERVER_PORT"
SUF="$(date +%s)-$RANDOM"
JUR="TEST-$SUF"
PROBE_ACC="tmp-clock-$SUF"

PASS=0; FAIL=0
FAILED=()          # что именно упало — печатаем списком в самом конце
SECTION=""
ok()   { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s  \033[2m(%s)\033[0m\n' "$1" "$2"; }
bad()  {
  FAIL=$((FAIL+1))
  FAILED+=("[$SECTION] $1 — ждали: $2, факт: $3")
  printf '  \033[31m✗ %s\033[0m\n      ждали: %s\n      факт : %s\n' "$1" "$2" "$3"
}
eq()   { [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$2" "$3"; }
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

# resolve -> печатает значение или "404"
value_at() { # $1=key $2=valid_on [$3=as_of]
  local url="/reference-parameters/$1/$JUR?valid_on=$2"
  [ $# -ge 3 ] && url="$url&as_of=$3"
  local r; r="$(call GET "$url")"
  if [ "$(code "$r")" = "404" ]; then echo "404"; else field "$(body "$r")" "['value']"; fi
}

printf '\033[1mСервер:\033[0m %s\n\033[1mЮрисдикция (одноразовая):\033[0m %s\n' "$BASE" "$JUR"

# ═══ A. ОТКРЫТАЯ КОРРЕКЦИЯ «С ДАТЫ» ══════════════════════════════════════════
step "A1. Регистрируем vat_rate = 0.20, действует с 2024-01-01 (открытый интервал)"
R="$(call POST /reference-parameters "$(printf '{"key":"vat_rate","jurisdiction":"%s","value":"0.20","valid_from":"2024-01-01T00:00:00Z","provenance":%s}' "$JUR" "$(prov 98-FZ)")")"
eq "регистрация -> 201" 201 "$(code "$R")"

step "A2. Пересечение valid-time отвергается (exclusion constraint в БД, не код)"
R="$(call POST /reference-parameters "$(printf '{"key":"vat_rate","jurisdiction":"%s","value":"0.99","valid_from":"2025-01-01T00:00:00Z","provenance":%s}' "$JUR" "$(prov 98-FZ)")")"
eq "регистрация внахлёст -> 409" 409 "$(code "$R")"

step "A3. Засекаем tx-метку ПО ЧАСАМ СЕРВЕРА (recorded_at служебного события)"
call POST "/accounts/$PROBE_ACC/usage" '{"metric":"electricity_kwh","quantity":"1","external_event_id":"clock-'"$SUF"'"}' >/dev/null
T0="$(body "$(call GET "/accounts/$PROBE_ACC/usage?metric=electricity_kwh")" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['recorded_at'])")"
note "T0 = $T0  (после регистрации, до коррекции)"

step "A4. Ретроактивная коррекция: 0.20 -> 0.25 начиная с 2026-07-01"
R="$(call POST "/reference-parameters/vat_rate/$JUR/corrections" "$(printf '{"value":"0.25","valid_from":"2026-07-01T00:00:00Z","provenance":%s}' "$(prov 98-FZ-amendment)")")"
eq "коррекция -> 200" 200 "$(code "$R")"
eq "перекрыта ровно одна версия" 1 "$(field "$(body "$R")" "['superseded_count']")"

step "A5. Ось tx-time: что система ЗНАЛА тогда vs что знает сейчас"
eq "as_of=T0 (до коррекции), июль-2026 -> старое 0.20" "0.20" "$(value_at vat_rate 2026-07-15T00:00:00Z "$T0")"
eq "as_of=сейчас,        июль-2026 -> новое  0.25" "0.25" "$(value_at vat_rate 2026-07-15T00:00:00Z)"
note "прошлое знание не переписано — старый расчёт воспроизводим"

step "A6. Ось valid-time: коррекция «с июля» НЕ пробивает дыру в прошлом  ← был баг"
eq "as_of=сейчас, июнь-2025 -> 0.20 (коррекция сюда не дотянулась)" "0.20" "$(value_at vat_rate 2025-06-01T00:00:00Z)"
eq "as_of=сейчас, июнь-2026 -> 0.20 (за день до начала коррекции)" "0.20" "$(value_at vat_rate 2026-06-30T00:00:00Z)"
note "раньше здесь было 404 -> пересчёт старого периода падал с 422"

step "A7. Обрезок сохранил СВОЙ провенанс, новая версия — провенанс коррекции"
R="$(call GET "/reference-parameters/vat_rate/$JUR?valid_on=2025-06-01T00:00:00Z")"
eq "июнь-2025: ссылка на исходную норму" "98-FZ" "$(field "$(body "$R")" "['provenance']['regulation_ref']")"
eq "июнь-2025: интервал обрезан началом коррекции" "2026-07-01T00:00:00Z" "$(field "$(body "$R")" "['valid_to']")"
R="$(call GET "/reference-parameters/vat_rate/$JUR?valid_on=2026-07-15T00:00:00Z")"
eq "июль-2026: ссылка на норму-поправку" "98-FZ-amendment" "$(field "$(body "$R")" "['provenance']['regulation_ref']")"

step "A8. До первой регистрации система не знала ничего"
eq "as_of=2020 -> 404" "404" "$(value_at vat_rate 2025-06-01T00:00:00Z 2020-01-01T00:00:00Z)"

# ═══ B. КОРРЕКЦИЯ «ОКНОМ» — выживают И голова, И хвост ═══════════════════════
step "B1. Регистрируем social_norm = 6.0 на ОГРАНИЧЕННЫЙ интервал [2024-01-01, 2027-01-01)"
R="$(call POST /reference-parameters "$(printf '{"key":"social_norm","jurisdiction":"%s","value":"6.0","valid_from":"2024-01-01T00:00:00Z","valid_to":"2027-01-01T00:00:00Z","provenance":%s}' "$JUR" "$(prov 'ПП-354')")")"
eq "регистрация -> 201" 201 "$(code "$R")"

step "B2. Коррекция окном: 6.0 -> 9.9 только на 2025 год"
R="$(call POST "/reference-parameters/social_norm/$JUR/corrections" "$(printf '{"value":"9.9","valid_from":"2025-01-01T00:00:00Z","valid_to":"2026-01-01T00:00:00Z","provenance":%s}' "$(prov 'ПП-354-поправка')")")"
eq "коррекция -> 200" 200 "$(code "$R")"

step "B3. Старый интервал расщепился на три части, а не схлопнулся в одну"
eq "июнь-2024 -> 6.0  (ГОЛОВА до окна)"    "6.0" "$(value_at social_norm 2024-06-01T00:00:00Z)"
eq "июнь-2025 -> 9.9  (само окно)"          "9.9" "$(value_at social_norm 2025-06-01T00:00:00Z)"
eq "июнь-2026 -> 6.0  (ХВОСТ после окна)"  "6.0" "$(value_at social_norm 2026-06-01T00:00:00Z)"
eq "июнь-2027 -> 404  (за концом исходного интервала)" "404" "$(value_at social_norm 2027-06-01T00:00:00Z)"
note "коррекция ограничена с обеих сторон — переутверждены оба обрезка"

# ═══ C. ОТМЕНА: закрывает будущее, не стирая прошлое ══════════════════════════
step "C1. Отменяем vat_rate с 2027-01-01"
R="$(call POST "/reference-parameters/vat_rate/$JUR/repeal" "$(printf '{"repeal_from":"2027-01-01T00:00:00Z","provenance":%s}' "$(prov 98-FZ-repeal)")")"
eq "repeal -> 200" 200 "$(code "$R")"

step "C2. После отмены не резолвится, но всё прошлое цело"
eq "июнь-2027 (после отмены) -> 404"          "404"  "$(value_at vat_rate 2027-06-01T00:00:00Z)"
eq "июль-2026 (до отмены)    -> 0.25"         "0.25" "$(value_at vat_rate 2026-07-15T00:00:00Z)"
eq "июнь-2025 (обрезок жив)  -> 0.20"         "0.20" "$(value_at vat_rate 2025-06-01T00:00:00Z)"
eq "июнь-2025 глазами T0     -> 0.20"         "0.20" "$(value_at vat_rate 2025-06-01T00:00:00Z "$T0")"
note "отмена — не удаление: старые расчёты по-прежнему воспроизводимы"

# ═════════════════════════════════════════════════════════════════════════════
printf '\n════════════════════════════════════════════════════════════\n'
if [ "$FAIL" -eq 0 ]; then
  printf '\033[1;32m  ВСЁ ЗЕЛЁНОЕ — %s/%s проверок пройдено\033[0m\n' "$PASS" "$((PASS+FAIL))"
else
  printf '\033[1;31m  ПРОВАЛЕНО %s из %s:\033[0m\n\n' "$FAIL" "$((PASS+FAIL))"
  for f in "${FAILED[@]}"; do printf '   \033[31m✗\033[0m %s\n' "$f"; done
  printf '\n\033[2m  Если упало A6 («дыра в прошлом») — на сервере старый код,\n  фикс correct() ещё не задеплоен.\033[0m\n'
fi
printf '════════════════════════════════════════════════════════════\n'

cat <<EOF

Осталось в БД (изолировано одноразовой юрисдикцией, чужого не тронуто):
  reference_parameter_version : версии vat_rate/$JUR и social_norm/$JUR
  usage_event                 : 1 служебное событие на счёте $PROBE_ACC
Убрать:
  delete from usage_event where account_id = '$PROBE_ACC';
  delete from reference_parameter_version where jurisdiction = '$JUR';
EOF
[ "$FAIL" -eq 0 ]
