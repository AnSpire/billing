# Объяснение тесткейсов

Этот документ объясняет, **как устроены** и **что проверяют** тесты из
`rental_agreement_tests.catala_en`, и связывает каждый тест с конкретным местом
формализации в `rental_agreement.catala_en`.

## Как вообще работает тест в Catala

В Catala нет отдельного тест-фреймворка со «страницей assert'ов» — тест **сам
является программой** (скоупом), которая вычисляет значение и сравнивает его с
ожидаемым через ключевое слово `assertion`. Анатомия одного теста на примере
`TestLateFeeDay6` (`rental_agreement_tests.catala_en:52`):

```catala
#[test]                                              (1)
declaration scope TestLateFeeDay6:
  output late_fee content money                      (2)
  fee scope Rental_agreement.LateFeeCalculation      (3)
scope TestLateFeeDay6:
  definition fee.payment_day equals 6                (4)
  definition late_fee equals fee.late_fee            (5)
  assertion late_fee = $35.00                        (6)
```

1. `#[test]` — атрибут, помечающий скоуп как тест. По нему тест-раннер находит,
   что нужно запускать.
2. `output late_fee` — тест ничего не принимает на вход (все входы зафиксированы
   внутри), поэтому он **исполним сам по себе**. Именно это позволяет запускать
   его без параметров.
3. `fee scope Rental_agreement.LateFeeCalculation` — подключение **проверяемого**
   скоупа из модуля модели как подскоупа. Это и есть «вызов» тестируемого кода.
4. `definition fee.payment_day equals 6` — задаём вход тестируемому скоупу (это
   «Arrange» в терминах AAA-тестов).
5. `definition late_fee equals fee.late_fee` — забираем результат («Act»).
6. `assertion late_fee = $35.00` — сверяем с эталоном («Assert»). Если равенство
   ложно, интерпретатор Catala падает с `Assertion failed: … ≠ …` и ненулевым
   кодом возврата.

### Механика запуска

Модель объявлена как модуль в `rental_agreement.catala_en:3`
(`> Module Rental_agreement`), а тест-файл подключает его в
`rental_agreement_tests.catala_en:6` (`> Using Rental_agreement`).

Команда `make test` под капотом выполняет:

```
catala interpret --whole-program -I . --stdlib=_build/libcatala rental_agreement_tests.catala_en
```

- `--whole-program` собирает модуль `Rental_agreement` из исходника вместе с
  тест-файлом (в этой раскладке без этого флага модуль между файлами не
  линкуется);
- interpret **без `-s`** исполняет по очереди все скоупы, у которых нет входов —
  то есть ровно наши 10 `#[test]`-скоупов — и проверяет их `assertion`.

На успехе для каждого теста печатается `RESULT` и код возврата `0`. При любом
несработавшем `assertion` — ошибка и код `123`.

## Что проверяет каждая группа тестов

Тесты сгруппированы по трём скоупам модели.

### 1. Штраф за просрочку — скоуп `LateFeeCalculation`

Проверяемая формула — `rental_agreement.catala_en:70`:

```catala
definition late_fee equals
  if payment_day <= terms.grace_period_days then $0.00
  else terms.flat_late_fee
       + terms.daily_late_fee * (decimal of (payment_day - (terms.grace_period_days + 1)))
```

где значения берутся из константы `const_terms` (`rental_agreement.catala_en:35`):
`grace_period_days = 3`, `flat_late_fee = $25`, `daily_late_fee = $5`.

Пять тестов подобраны как **граничные точки** этой кусочной функции — вокруг
перехода grace → штраф и на линейном участке:

| Тест | `payment_day` | Ожидание | Что именно проверяет | Строка теста |
|------|---------------|----------|----------------------|--------------|
| `TestLateFeeDay1`  | 1  | `$0.00`  | внутри grace-периода — ветка `then` (`:71`) | `…tests:14` |
| `TestLateFeeDay3`  | 3  | `$0.00`  | **последний** день grace (граница `<= 3`) — что 3 ещё бесплатно | `…tests:27` |
| `TestLateFeeDay4`  | 4  | `$25.00` | **первый** платный день: срабатывает `flat_late_fee`, но `$5 × (4−4)=0` — надбавки ещё нет | `…tests:40` |
| `TestLateFeeDay6`  | 6  | `$35.00` | линейный участок: `$25 + $5 × (6−4) = $35` — проверяет коэффициент надбавки | `…tests:53` |
| `TestLateFeeDay10` | 10 | `$55.00` | тот же участок дальше: `$25 + $5 × (10−4) = $55` — что надбавка масштабируется линейно | `…tests:66` |

Пары `Day3`/`Day4` вместе фиксируют **точное положение границы** `<= grace`
(если кто-то случайно поменяет `<=` на `<`, `Day3` начнёт требовать штраф и тест
упадёт). Пара `Day4`/`Day6` фиксирует, что «+$5 за каждый день **после** 4-го»
трактуется именно как `payment_day - 4`, а не `payment_day - 3` (иначе на дне 4
получилось бы $30).

### 2. Итог к оплате за месяц — скоуп `MonthlyRentStatement`

Проверяемая формула — `rental_agreement.catala_en:94`:

```catala
definition total_due equals terms.base_rent + fee.late_fee
```

(`base_rent = $685`). Этот скоуп сам вызывает `LateFeeCalculation` как подскоуп
(`rental_agreement.catala_en:87`, `:92-93`), поэтому тесты заодно проверяют, что
**композиция** скоупов и проброс `payment_day` вниз работают.

| Тест | `payment_day` | Ожидание | Что проверяет | Строка теста |
|------|---------------|----------|---------------|--------------|
| `TestTotalOnTime` | 2 | `$685.00` | оплата в срок → только аренда, штраф $0 (ветка «нет надбавки» доходит до итога) | `…tests:81` |
| `TestTotalLate`   | 4 | `$710.00` | просрочка → `$685 + $25 = $710`; проверяет, что штраф действительно **прибавляется** к аренде | `…tests:94` |

### 3. Возврат депозита — скоуп `SecurityDepositRefund`

Проверяемые формулы — `rental_agreement.catala_en:119` (клининг) и `:121` (возврат):

```catala
definition cleaning_charge equals
  if returned_clean then $0.00 else terms.cleaning_fee
definition refund equals
  if terms.security_deposit - damages - cleaning_charge < $0.00 then $0.00
  else terms.security_deposit - damages - cleaning_charge
```

(`security_deposit = $685`, `cleaning_fee = $200`).

| Тест | Входы | Ожидание | Что проверяет | Строка теста |
|------|-------|----------|---------------|--------------|
| `TestRefundClean` | `damages=$0`, `returned_clean=true` | `$685.00` | базовый случай: чисто и без ущерба → возвращается весь депозит; ветка `then` в `cleaning_charge` (`:120`) | `…tests:109` |
| `TestRefundDirtyWithDamage` | `damages=$100`, `returned_clean=false` | `$385.00` | оба вычета сразу: `$685 − $200 − $100 = $385`; ветка `else` клининга + основная ветка возврата | `…tests:123` |
| `TestRefundFloored` | `damages=$1000`, `returned_clean=false` | `$0.00` | **инвариант неотрицательности**: `$685 − $200 − $1000 < 0` → срабатывает floor-ветка `then` (`:122`), возврат не уходит в минус | `…tests:137` |

`TestRefundFloored` — самый ценный из трёх: он проверяет не арифметику, а
защитное условие `if … < $0.00 then $0.00`. Без него результат был бы
отрицательным (`−$515`), что для «возврата» бессмысленно.

## Карта покрытия

```
Модель (rental_agreement.catala_en)          Тесты (rental_agreement_tests.catala_en)
────────────────────────────────────         ─────────────────────────────────────────
LateFeeCalculation      :60–77   ──────────►  Day1, Day3, Day4, Day6, Day10   (5)
  ветка grace (then)    :71      ──────────►  Day1, Day3
  ветка штраф (else)    :74–76   ──────────►  Day4, Day6, Day10
MonthlyRentStatement    :84–94   ──────────►  TotalOnTime, TotalLate          (2)
SecurityDepositRefund   :110–125 ──────────►  RefundClean, RefundDirty…, …Floored (3)
  cleaning then/else    :120     ──────────►  RefundClean / RefundDirty, Floored
  floor-ветка           :122     ──────────►  RefundFloored
```

Каждая ветвь `if/then/else` в модели покрыта хотя бы одним тестом.

## Как тесты ловят регрессию

Тесты — исполняемые, а не декоративные. Если, например, в
`rental_agreement.catala_en:70` заменить `<=` на `<` (сдвинуть границу
grace-периода), то `TestLateFeeDay3` перестанет получать `$0.00` и запуск
завершится так:

```
┌─[ERROR]─
│  Assertion failed: $25.00 ≠ $0.00
├─➤ rental_agreement_tests.catala_en:33 …
└─
exit=123
```

Ненулевой код возврата останавливает `make test` — то есть любая правка модели,
меняющая поведение денежных пунктов, немедленно видна.
