# Gonka host monitoring

Набор serverless-мониторов для сети Gonka. GitHub Actions запускает проверки
по расписанию, Telegram получает только события и изменения состояния.

## Мониторы

- `new_host_bot.py` — новые, вернувшиеся и ушедшие хосты, их периоды
  участия, вес, доля сети, место, модели, ML-ноды и API.
- `excluded_watcher.py` — новые исключения после cPoC с причиной, блоком,
  весом, местом, моделями, числом ML-нод и API.
- `our_nodes_watcher.py` — доступность собственных нод, присутствие среди
  участников и Confirmation PoC rate из chain group data.
- `model_coefficients_watcher.py` — изменения коэффициентов PoC-моделей.
- `escrow_balance_watcher.py` — балансы ключей для создания эскроу, алерт
  строго ниже 100 GNK, восстановление и суточное напоминание.
- `bridge_burn_watcher.py` — финализированные burn-транзакции WGNK в Ethereum,
  их очередь и статус обработки bridge на стороне Gonka.
- `bridge_stale_watcher.py` — риски BLS/bridge: концентрация slots,
  inactive/invalidated slots и отставание bridge latest от Ethereum finalized.
- `chain_halt_watcher.py` — независимая проверка liveness Gonka chain по
  кворуму Tendermint RPC.
- `chain_load_watcher.py` — аномальный объём raw transaction bytes в последних
  блоках и риск чрезмерной chain load/state inflation.
- `upgrade_adoption_watcher.py` — распространение целевой API-версии по весу.
- `glamsterdam_watcher.py` — дата и статус Ethereum Glamsterdam.

Общие HTTP fallback, атомарная запись JSON и Telegram находятся в
`bot_common.py`. Состояния проверок хранятся в `state/` и коммитятся обратно
workflow-скриптом `scripts/commit_state.sh`.

## Переменные и secrets

- `TELEGRAM_BOT_TOKEN` — secret.
- `TELEGRAM_CHAT_ID` — secret.
- `TELEGRAM_MESSAGE_THREAD_ID` — обязательный secret для Telegram topic. Если
  он отсутствует или некорректен, бот не отправляет сообщение в General, а
  завершает проверку ошибкой.
- `TELEGRAM_SECONDARY_CHAT_ID` — необязательный secret второго канала. Если он
  задан, каждое уведомление отправляется также в этот канал без topic.
- `ETHEREUM_RPC_URLS` — необязательный secret: один или несколько Ethereum RPC
  через запятую. Они используются раньше публичных резервных RPC.
- `TARGET_API_VERSION` — repository variable.
- `ADOPTION_THRESHOLD` — repository variable.

Конфигурация собственных нод находится в `config/our_nodes.json`, ключей для
эскроу — в `config/escrow_balances.json`, bridge burn — в
`config/bridge_burn.json`, а BLS/bridge stale — в
`config/bridge_stale.json`. Источники коэффициентов моделей находятся в
`config/model_coefficients.json`. Баланс хранится в базовом denom
`ngonka`: 1 GNK = 1 000 000 000 ngonka. Ручной запуск workflow
`Check escrow balances` по умолчанию отправляет проверочную сводку; плановые
запуски пишут в Telegram только алерты, восстановления и напоминания.

Workflow `Check bridge WGNK burns` запланирован каждые пять минут со смещением
от начала часа. GitHub Actions может фактически запустить его позже. Монитор
читает только финализированные Ethereum-блоки и ищет событие ERC-20 `Transfer`
контракта WGNK на нулевой адрес. Новая транзакция впервые проверяется в Gonka
через 5 минут. Через 10 минут `BRIDGE_PENDING` или отсутствие bridge receipt
создаёт warning; две просроченные транзакции создают один critical. Ошибка всех
Gonka API отслеживается отдельно и не считается зависшей транзакцией.
Перед удалением завершённой транзакции из очереди монитор сохраняет её эпоху и
список подписавших validators. Последние 20 таких записей используются как
фактическое liveness-доказательство для Top-10 bridge peers.

Workflow `Check bridge stale and BLS risk` запускается каждые 5 минут. Он
читает реальное распределение BLS slots текущей подписанной эпохи и применяет
пять независимых правил:

- warning, если Top-3 контролируют большинство: `total_slots // 2 + 1`;
- warning, если 35% или больше BLS slots принадлежат адресам, отсутствующим в
  Cosmos `group_members` этой эпохи (не могут голосовать в bridge);
- warning, если ноды с 35% или больше BLS slots две проверки подряд отстают
  от одного зафиксированного для запуска Ethereum finalized block более чем
  на 64 блока;
- отдельный warning, если bridge API недоступен у нод с 35% или больше slots;
- индивидуальный warning, если участник Top-10 по BLS slots отсутствует среди
  подписантов двух последних завершённых bridge-транзакций текущей эпохи;
- индивидуальный warning, если участник Top-10 inactive/исключён после CPoC
  две последовательные проверки; после восстановления отправляется recovery.

Отставание до 64 Ethereum-блоков включительно допустимо; `bridge_latest` выше
зафиксированного finalized также не считается stale. Недоступный API
классифицируется как `unknown`, а не как `stale`. Значения
`valid_dealers` из BLS-ответа относятся к DKG и не используются как признак
inactive/invalidated. Top-10 availability проверяется независимо от доступности
Ethereum RPC. HTTP 503 или другой сбой API сам по себе не считается отказом
Top-10 peer: он остаётся только диагностическим сигналом `unknown`. Если в
текущей эпохе ещё нет двух сохранённых завершённых bridge-транзакций, состояние
подписей считается неизвестным и индивидуальный warning не отправляется.
Ручной запуск workflow по умолчанию отправляет сводку.

Текущий transaction workflow покрывает WGNK burn (unwrap). Входящие USDT/USDC
нужно добавлять отдельно после определения bridge receiver/event на Ethereum.

## Host presence monitor

`new_host_bot.py` берёт и номер эпохи
`active_participants.epoch_group_id`, и список
`active_participants.participants` из одного snapshot. Пустой или
некорректный snapshot не меняет state. Повторная проверка уже
обработанной эпохи игнорируется, как и snapshot старой эпохи.

События классифицируются так:

- `new` — адрес раньше никогда не встречался;
- `returned` — известный неактивный адрес снова появился;
- `left` — активный в предыдущем обработанном snapshot адрес исчез.

Структурированная история хранится в `state/host_presence.json`, а переходы
дублируются в `state/host_events.csv`. При первом запуске текущая эпоха
становится baseline без Telegram-собщений. Legacy-файлы `state/hosts.json`
и `state/host_log.csv` не удаляются: они доказывают, что адрес раньше
встречался, но не используются для выдумывания старых непрерывных
периодов. Точная история начинается с `history_complete_from_epoch`.
Если между snapshot пропущены эпохи, монитор разрывает наблюдаемый
период и отдельно хранит эпоху обнаружения отсутствия.

Доля хоста равна его весу, делённому на сумму весов всех участников.
Если хотя бы один вес некорректен, доля и общий вес считаются неполными.
Порог warning находится в `config/host_monitor.json` и равен ±20%.
Сравниваются только две соседние эпохи с полными весами и ненулевым
предыдущим total weight.

## Confirmation PoC rate

`our_nodes_watcher.py` получает номер эпохи и участников из одного snapshot:
`active_participants.epoch_group_id` и
`active_participants.participants`. Для Confirmation PoC используется только
chain API `current_epoch_group_data` с fallback между node3, node2 и node1.
Соответствующая эпоха находится в `epoch_group_data.epoch_index`, а строки
участников — в `epoch_group_data.validation_weights`; адрес строки хранится в
`member_address`.

Rate конкретного participant рассчитывается так:

```text
confirmation_weight / weight × 100%
```

Здесь `weight` — вес participant из его строки `validation_weights`, а не
суммарный вес сети. Нулевые и отрицательные значения, дубликаты адресов и
`confirmation_weight > weight` делают payload некорректным. Эпоха group data
обязана совпадать с эпохой participants snapshot. Значение ниже 30% должно
повториться в двух последовательных проверках одной эпохи; 30% или выше даёт
recovery. При смене эпохи счётчик низких значений начинается заново.

Недоступность метрики считается отдельно и не создаёт ложный low-rate alert.
В исследованном payload нет достоверного признака завершения CPoC, поэтому
phase gating не применяется. Telegram содержит только краткую причину, а
полная ошибка сохраняется в `state/our_nodes_state.json` и workflow log.

## Коэффициенты PoC-моделей

`model_coefficients_watcher.py` раз в час читает с fallback endpoint
`/chain-api/productscience/inference/inference/params`. Список находится в
`params.poc_params.models`, идентификатор — в `model_id`, коэффициент — в
`weight_scale_factor.value × 10^weight_scale_factor.exponent`. Вычисления и
сравнение выполняются через `Decimal`, поэтому `0.78`, `0.780` и строка
`"0.78"` эквивалентны.

Первый успешный запуск создаёт `state/model_coefficients.json` как baseline
без Telegram-сообщения. Затем одно INFO-сообщение перечисляет только
добавленные, удалённые и изменившиеся модели. Reminders не отправляются.
Недоступность всех params-источников даёт отдельный yellow alert после трёх
последовательных запусков; прежний baseline при этом сохраняется. Полная
ошибка остаётся в state и workflow log.

## Chain halt monitor

`chain_halt_watcher.py` опрашивает все RPC из `config/chain_halt.json`
независимо, а не как fallback-цепочку. Chain halt подтверждается,
только если одновременно:

- доступно не меньше `minimum_confirming_sources` корректных RPC;
- все они отвечают для `expected_chain_id` и не находятся в `catching_up`;
- возраст последнего блока у каждого больше `halt_after_seconds`;
- разброс высот не превышает `maximum_height_spread`.

Свежий блок хотя бы у одного корректного RPC означает, что сеть жива.
Один старый или недоступный RPC не считается chain halt. Недостаток
согласованных источников после нескольких проверок даёт отдельный
жёлтый алерт о потере наблюдаемости. Красный алерт отправляется при
переходе в halt, затем по интервалу идут reminders. Recovery требует
`recovery_confirmations` свежих проверок подряд.
В Telegram ошибки RPC показываются краткими категориями, например
`timeout`, `HTTP 503`, `invalid response`, `wrong chain ID` и `node syncing`.
Полная диагностика сохраняется в `state/chain_halt.json` и выводится
в workflow/systemd logs.

Разовый запуск, как в GitHub Actions:

```bash
CHAIN_MONITOR_MODE=once python3 chain_halt_watcher.py
```

Постоянный процесс с интервалом 30 секунд:

```bash
CHAIN_MONITOR_MODE=daemon CHAIN_POLL_INTERVAL_SECONDS=30 \
  python3 chain_halt_watcher.py
```

Для server-развёртывания используйте
`deploy/chain-halt.env.example` и `deploy/gonka-chain-halt.service.example`, заменив в них
пользователя, пути и Telegram secrets. После проверки daemon-запуска
отключите schedule workflow `Check Gonka chain halt`, чтобы два runner
не писали в один `state/chain_halt.json` и не дублировали алерты.
При переезде меняются только env/config и устанавливается systemd unit;
Python-логика однократной проверки остаётся той же.

## Chain load monitor

`chain_load_watcher.py` фиксирует latest height через Tendermint `/status`,
затем получает окно последних `window_blocks` блоков через `/block?height=...`.
Каждая транзакция строго декодируется из base64, после чего считается только
длина исходных protobuf bytes:

```text
sum_tx_bytes = Σ len(base64_decode(tx))
```

Размер JSON/base64 и gas в эту метрику не входят. Порог `warning_bytes` равен
50 000 000 decimal bytes, а условие превышения строгое:
`sum_tx_bytes > warning_bytes`. Первое новое окно выше порога даёт warning,
второе подряд — critical. Recovery требует два новых корректных окна на уровне
порога или ниже. Повторный poll той же или более старой высоты не меняет
счётчики и не дублирует алерты.

Весь snapshot собирается с одного RPC. Если источник отказал на любом блоке,
его частичный результат отбрасывается, и полное окно заново собирается через
следующий URL из `config/chain_load.json` либо `CHAIN_LOAD_RPC_URLS`. Malformed
base64 делает весь snapshot недостоверным. Недоступность всех RPC считается
потерей наблюдаемости, а не нормальной или аномальной нагрузкой. Telegram
получает краткие категории, полная диагностика остаётся в
`state/chain_load.json` и logs.

Type URL извлекаются из raw protobuf эвристически и используются только для
triage. Multi-message transaction получает объединённую подпись типов, но её
bytes и count учитываются ровно один раз. Alert определяется исключительно
общим `sum_tx_bytes`.

Разовый локальный smoke test без Telegram и без изменения state:

```bash
python3 chain_load_watcher.py --once --no-notify
```

Режимы для production:

```bash
CHAIN_LOAD_MODE=once python3 chain_load_watcher.py

CHAIN_LOAD_MODE=daemon CHAIN_LOAD_POLL_INTERVAL_SECONDS=60 \
  python3 chain_load_watcher.py
```

Для server-развёртывания используйте `deploy/chain-load.env.example` и
`deploy/gonka-chain-load.service.example`. После включения daemon отключите
schedule workflow `Check Gonka chain load`, чтобы два процесса не писали один
state и не отправляли повторные сообщения.

Gas alert пока не реализован. Follow-up: исследовать
`/block_results?height=...`, начать сохранять gas usage, накопить baseline за
7–14 дней и только после этого определить окно и порог. Недоступность
`block_results` не должна влиять на основной tx-bytes monitor.

## Локальная проверка

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

Мониторы используют несколько публичных источников. Ошибка одного источника
не считается сетевым инцидентом, пока доступен резервный источник. Отсутствие
Confirmation PoC rate отслеживается отдельно от доступности самой ноды.

Локальный разовый запуск этих проверок:

```bash
python3 our_nodes_watcher.py
python3 model_coefficients_watcher.py
```
