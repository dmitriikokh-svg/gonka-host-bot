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
- `upgrade_adoption_watcher.py` — распространение целевой API-версии по весу.
- `glamsterdam_watcher.py` — дата и статус Ethereum Glamsterdam.

Общие HTTP fallback, атомарная запись JSON и Telegram находятся в
`bot_common.py`. Состояния проверок хранятся в `state/` и коммитятся обратно
workflow-скриптом `scripts/commit_state.sh`.

## Переменные и secrets

- `TELEGRAM_BOT_TOKEN` — secret.
- `TELEGRAM_CHAT_ID` — secret.
- `TELEGRAM_MESSAGE_THREAD_ID` — необязательный secret для Telegram topic.
- `TARGET_API_VERSION` — repository variable.
- `ADOPTION_THRESHOLD` — repository variable.

Конфигурация собственных нод находится в `config/our_nodes.json`, а ключей для
эскроу — в `config/escrow_balances.json`. Баланс хранится в базовом denom
`ngonka`: 1 GNK = 1 000 000 000 ngonka. Ручной запуск workflow
`Check escrow balances` по умолчанию отправляет проверочную сводку; плановые
запуски пишут в Telegram только алерты, восстановления и напоминания.

## Локальная проверка

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

Мониторы используют несколько публичных источников. Ошибка одного источника
не считается сетевым инцидентом, пока доступен резервный источник. Отсутствие
Confirmation PoC ratio отслеживается отдельно от доступности самой ноды.
