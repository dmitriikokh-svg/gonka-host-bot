# Реестр алертов Gonka host bot

Реестр фиксирует operational ownership после переноса production-запусков на
сервер. Все `.github/workflows/check-*.yml` оставлены только для ручного
`workflow_dispatch`; production cadence задаётся внешним серверным scheduler.

Telegram-направления:

- `MONITORING` — `TELEGRAM_CHAT_ID` + обязательный
  `TELEGRAM_MESSAGE_THREAD_ID`, а также необязательный
  `TELEGRAM_SECONDARY_CHAT_ID` через общий helper;
- `NONE` — этот переход не отправляется данным репозиторием.

Статусы ownership:

- `OWNER` — есть известное пересечение, но operational alert остаётся здесь;
- `BACKUP` — сигнал сохраняется здесь, а уведомлением владеет другой репозиторий;
- `DUPLICATE_PENDING_REMOVAL` — известный дубликат ещё отправляется и ожидает
  удаления;
- `UNIQUE` — известного пересечения с `gonka-model-watch` или
  `gonka-heartbeat` нет.

На момент актуализации реестра активных событий со статусом
`DUPLICATE_PENDING_REMOVAL` нет.

## `new_host_bot.py`

Источник: единый snapshot `/v1/epochs/current/participants` с epoch и active
participants.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `host_new` | Новый участник сети | Адрес впервые появился после baseline | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `host_returned` | Возврат известного участника | Неактивный адрес снова появился | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `host_left` | Выход участника из active set | Активный адрес отсутствует в следующем обработанном snapshot | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `network_weight_change` | Резкое изменение общего веса сети | Соседние полные эпохи, `abs(delta) >= 20%` | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

## `excluded_watcher.py`

Источник: `excluded_participants` и active participants из
`/v1/epochs/current/participants`.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `cpoc_exclusion` | Новое исключение после Confirmation PoC | Новый participant ID относительно сохранённого baseline | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

## `our_nodes_watcher.py`

Источники: active participants snapshot, `/v1/versions` собственных нод и
`current_epoch_group_data.validation_weights`.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `our_node_unavailable_or_absent` | Недоступность endpoint либо отсутствие обязательного participant в эпохе | Переход ноды в `down`; внутри проверки до 3 health attempts | CRITICAL | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `our_node_recovered` | Восстановление endpoint/участия | `down -> up` | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `confirmation_poc_low` | Низкая доля подтверждённого веса | Rate `< 30%` две проверки подряд в одной эпохе | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `confirmation_poc_recovered` | Восстановление Confirmation PoC | Активный low-rate alert и rate `>= 30%` | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `confirmation_poc_unavailable` | Потеря наблюдаемости метрики | Две последовательные проверки без корректной метрики | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `confirmation_poc_available` | Восстановление чтения метрики | Первая корректная метрика после unavailable alert | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

## `model_coefficients_watcher.py`

Источники: `params.poc_params.models` из chain params; эпоха берётся из
`current_epoch_group_data` только для подписи сообщения.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `poc_model_added` | Обнаружение новой модели и обновление полного state | Новый `model_id` | INFO | NONE | `dahl-ai/gonka-model-watch` | `gonka-model-watch` обслуживает уведомление | BACKUP |
| `poc_model_coefficient_changed` | Изменение коэффициента существующей модели | Каноническое Decimal-значение изменилось | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Для этого события не подтверждено | UNIQUE |
| `poc_model_removed` | Удаление модели из PoC params | Сохранённый `model_id` отсутствует в новом полном списке | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Для этого события не подтверждено | UNIQUE |
| `poc_params_unavailable` | Потеря всех params-источников | Три последовательных неуспешных запуска | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `poc_params_available` | Восстановление params-источника | Первый успешный запуск после unavailable alert | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |

Добавленная модель никогда не включается в сообщение этого watcher, в том
числе когда одновременно изменился коэффициент другой модели. State при этом
всегда содержит полный актуальный список.

## `escrow_balance_watcher.py`

Источник: Cosmos Bank balances для включённых accounts из
`config/escrow_balances.json`; denom `ngonka` отображается как GNK.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `escrow_balance_low` | Недостаток средств для создания escrow | Баланс строго `< 100 GNK` | CRITICAL | MONITORING | `dmitriikokh-svg/gonka-host-bot` | `gonka-heartbeat` проверяет `< 80 GNK` | OWNER |
| `escrow_balance_low_reminder` | Напоминание об активном low balance | Не чаще одного раза за 24 часа | CRITICAL | MONITORING | `dmitriikokh-svg/gonka-host-bot` | `gonka-heartbeat`, другой порог | OWNER |
| `escrow_balance_recovered` | Баланс пополнен | Активный alert и баланс `>= 100 GNK` | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | `gonka-heartbeat`, другой порог | OWNER |
| `escrow_balance_unavailable` | Потеря наблюдаемости баланса | Три последовательных неуспешных проверки account | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `escrow_balance_available` | Восстановление наблюдаемости | Успешное чтение после unavailable alert | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `escrow_balance_summary` | Ручная проверочная сводка | Только явно запрошенный summary | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

Escrow monitor остаётся включённым и имеет статус `OWNER`: порог этого
репозитория 100 GNK даёт более раннее предупреждение, чем текущие 80 GNK в
Heartbeat.

## `bridge_burn_watcher.py`

Источники: Ethereum finalized JSON-RPC и `eth_getLogs` для WGNK `Transfer` на
zero address; Gonka `bridge_transaction` API для статуса обработки.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `bridge_transaction_overdue` | Одиночная незавершённая burn-транзакция | Первая проверка после 5 минут; warning при возрасте `>= 10 минут` | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_queue_critical` | Несколько зависших burn-транзакций | Не меньше 2 overdue transactions | CRITICAL | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_transaction_completed` | Завершение ранее предупреждённой транзакции | `BRIDGE_COMPLETED` после warning | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_queue_downgraded` | Снятие critical при оставшейся просрочке | Overdue count падает с 2+ до 1 | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_queue_recovered` | Полное восстановление очереди | Overdue count падает до 0 | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_source_unavailable` | Потеря Ethereum или Gonka источника | Три последовательных неуспешных проверки источника | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_source_available` | Восстановление источника | Успешная проверка после source alert | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_burn_summary` | Ручная диагностическая сводка | Только явно запрошенный summary | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

## `bridge_stale_watcher.py`

Источники: BLS epoch data, Cosmos group members, participant status/API,
`/v1/bridge/block/latest`, Ethereum finalized и история подписантов завершённых
bridge transactions.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `bls_top3_concentration` | Концентрация voting slots | Top-3 имеют `total_slots // 2 + 1` или больше | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bls_inactive_slots` | Slots участников, не способных голосовать | Inactive/invalidated slots `>= 35%` | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_stale_slots` | Значимое отставание bridge | Lag больше 64 Ethereum blocks у `>= 35%` slots две проверки подряд | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_unknown_slots` | Значимая потеря bridge visibility | Unknown status у `>= 35%` slots | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_top10_peer_problem` | Неучастие значимого bridge peer | Нет подписи в 2 последних completed transactions либо inactive две проверки подряд | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_stale_recovery` | Recovery любого активного bridge/BLS сигнала | Условие соответствующего сигнала больше не выполнено | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_stale_source_unavailable` | Потеря chain/Ethereum source | Три последовательных неуспешных проверки | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `bridge_stale_summary` | Ручная диагностическая сводка | Только явно запрошенный summary | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

## `chain_halt_watcher.py`

Источник: независимые Tendermint `/status` RPC из `config/chain_halt.json`.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `chain_halt` | Подтверждённая остановка chain | Минимум 2 согласованных RPC; block age `> 60 секунд`; spread `<= 2` | CRITICAL | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `chain_halt_reminder` | Напоминание об остановке | Каждые 30 минут при активном halt | CRITICAL | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `chain_halt_recovered` | Восстановление выпуска блоков | Две свежие проверки подряд | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `chain_halt_observability_lost` | Недостаток/расхождение источников | Три последовательных некорректных assessments | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |
| `chain_halt_observability_recovered` | Восстановление quorum visibility | Первый корректный assessment после observability alert | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет подтверждённого | UNIQUE |

## `chain_load_watcher.py`

Источник: coherent window из Tendermint `/status` и `/block` одного RPC;
метрика — сумма декодированных raw protobuf transaction bytes.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `chain_load_warning` | Аномальный объём транзакций | `sum_tx_bytes > 50,000,000` в окне 10 новых blocks | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `chain_load_critical` | Продолжающаяся аномальная нагрузка | Второе последовательное breached window | CRITICAL | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `chain_load_critical_reminder` | Напоминание о critical load | Новое breached window и не чаще одного раза за 30 минут | CRITICAL | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `chain_load_recovered` | Нагрузка вернулась к норме | Два новых clean windows подряд | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `chain_load_unavailable` | Полное окно нельзя собрать | Три последовательных неуспешных запуска по всем RPC | WARNING | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `chain_load_available` | Восстановление snapshot collection | Первый корректный snapshot после unavailable alert | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

## `upgrade_adoption_watcher.py`

Источники: active participants, epoch stage и публичные `/v1/versions`; цели
задаются `TARGET_API_VERSION`, `TARGET_MLNODE_VERSION` и
`ADOPTION_PERCENT` (default 80%).

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `upgrade_epoch_digest` | Сводка API/MLNode rollout | Один раз на эпоху в inference window: `claim_money + 2000` blocks и более чем за 500 blocks до next PoC | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `api_adoption_event` | Существенное изменение API rollout | Новая target version, достижение target, ухудшение unknown band — сразу; рост `>= 5 п.п.` — после 2 одинаковых checks | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |
| `mlnode_adoption_event` | Существенное изменение MLNode rollout | Новая target version — сразу; рост fully updated hosts либо `>= 5 п.п.` target nodes — после 2 одинаковых checks | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

Telegram разрешён только в `Inference` вне активного Confirmation PoC.

## `glamsterdam_watcher.py`

Источник: `ethereum/forkcast` `src/data/upgrades.ts`.

| Событие | Назначение | Порог/условие | Уровень | Telegram | Владеющий репозиторий | Известное пересечение | Статус |
|---|---|---|---|---|---|---|---|
| `glamsterdam_changed` | Изменение даты/статуса Ethereum Glamsterdam | Изменился `activationDate` или `status` после baseline | INFO | MONITORING | `dmitriikokh-svg/gonka-host-bot` | Нет | UNIQUE |

## Компоненты без Telegram-алертов

`analytics_db_probe.py` — диагностический read-only probe. Он печатает только
credential-safe JSON, не пишет `state/` и не относится к alert registry.
