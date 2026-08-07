# Gonka host monitoring

Набор serverless-мониторов для сети Gonka. GitHub Actions запускает проверки
по расписанию, Telegram получает только события и изменения состояния.

## Мониторы

- `new_host_bot.py` — новые участники сети и история первого обнаружения.
- `excluded_watcher.py` — новые исключения после cPoC.
- `our_nodes_watcher.py` — доступность собственных нод, присутствие среди
  участников и Confirmation PoC ratio.
- `escrow_balance_watcher.py` — балансы ключей для создания эскроу, алерт
  строго ниже 100 GNK, восстановление и суточное напоминание.
- `bridge_burn_watcher.py` — финализированные burn-транзакции WGNK в Ethereum,
  их очередь и статус обработки bridge на стороне Gonka.
- `bridge_stale_watcher.py` — риски BLS/bridge: концентрация slots,
  inactive/invalidated slots и отставание bridge latest от Ethereum finalized.
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
`config/bridge_stale.json`. Баланс хранится в базовом denom
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
- warning, если ноды с 35% или больше BLS slots сообщают `bridge_latest`, не
  равный одному зафиксированному для запуска Ethereum finalized block;
- отдельный warning, если bridge API недоступен у нод с 35% или больше slots;
- индивидуальный warning, если участник Top-10 по BLS slots отсутствует среди
  подписантов двух последних завершённых bridge-транзакций текущей эпохи;
- индивидуальный warning, если участник Top-10 inactive/исключён после CPoC
  две последовательные проверки; после восстановления отправляется recovery.

Недоступный API классифицируется как `unknown`, а не как `stale`. Значения
`valid_dealers` из BLS-ответа относятся к DKG и не используются как признак
inactive/invalidated. Top-10 availability проверяется независимо от доступности
Ethereum RPC. HTTP 503 или другой сбой API сам по себе не считается отказом
Top-10 peer: он остаётся только диагностическим сигналом `unknown`. Если в
текущей эпохе ещё нет двух сохранённых завершённых bridge-транзакций, состояние
подписей считается неизвестным и индивидуальный warning не отправляется.
Ручной запуск workflow по умолчанию отправляет сводку.

Текущий transaction workflow покрывает WGNK burn (unwrap). Входящие USDT/USDC
нужно добавлять отдельно после определения bridge receiver/event на Ethereum.

## Локальная проверка

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

Мониторы используют несколько публичных источников. Ошибка одного источника
не считается сетевым инцидентом, пока доступен резервный источник. Отсутствие
Confirmation PoC ratio отслеживается отдельно от доступности самой ноды.
