# Бот на сервере Hostmaster

| | |
|---|---|
| Где | общий сервер Hostmaster, папка `~/proactive-school`, проект Docker Compose `proactive-school` |
| Контейнеры | `school-bot` (бот и фоновые задачи), `school-db` (PostgreSQL 16), `school-redis` (Redis 7) |
| Лимиты | бот 512 МБ / 1 ядро, база 256 МБ / 0,5 ядра, redis 64 МБ / 0,25 ядра; логи по 10 МБ × 5 файлов |
| Обновления от Telegram | long polling: портов и домена нет |
| Доступ | ключ `~/.ssh/hostmaster_bench`; адрес и пользователь — в гайде владельца сервера и в `deploy/.deploy.local` (репозиторий публичный, адрес сюда не кладём) |

## Обновить

Один раз на компьютере: создайте `server/deploy/.deploy.local` (git его не видит) со строкой `DEPLOY_HOST=пользователь@адрес` из гайда сервера.

Дальше из папки `server/` (на Windows — в Git Bash):

```bash
sh deploy/deploy.sh
```

Скрипт копирует код, пересобирает образ и перезапускает только контейнеры этого бота. Миграции базы применяются при старте контейнера. `.env`, `secrets/` и база на сервере не меняются.

`sh deploy/deploy.sh build` только собирает образ, ничего не запуская: так можно заранее проверить сборку.

## Логи и перезапуск

```bash
. deploy/.deploy.local && ssh -i ~/.ssh/hostmaster_bench "$DEPLOY_HOST" 'cd proactive-school && sudo -n docker compose logs --tail=200 school-bot'
```

```bash
. deploy/.deploy.local && ssh -i ~/.ssh/hostmaster_bench "$DEPLOY_HOST" 'cd proactive-school && sudo -n docker compose restart school-bot'
```

Не запускайте `docker compose config` и `docker inspect` без `-f`: они печатают токены из `.env`.

## Что лежит на сервере, а не в репозитории

`~/proactive-school/.env` (права 600), его кладёт владелец:

| Переменная | Нужна | Смысл |
|---|---|---|
| `BOT_TOKEN` | да | токен @proactive_school_bot |
| `ADMIN_IDS` | да | Telegram id суперадминов через запятую |
| `POSTGRES_PASSWORD` | да | пароль базы, длинный случайный |
| `BRIDGE_URL`, `BRIDGE_SECRET` | для таблицы задач | мост к Apps Script статусной таблицы ([bot/README.md](../../bot/README.md#сервер-на-aiogram)) |
| `STATUS_SHEET_ID`, `TASKS_SEE_ALL` | по желанию | задачи сотрудников ([docs/STAFF.md](../docs/STAFF.md)) |
| `SUPPORT_USERNAME` | по желанию | контакт поддержки в `/help` |
| `CONTENT_SHEET_ID`, `CONTENT_CHANNEL_ID` | по желанию | автопостинг; обычно задаются из бота ([docs/CHANNEL.md](../docs/CHANNEL.md)) |

`DATABASE_URL` и `REDIS_URL` задаёт `docker-compose.yml`, в `.env` их не нужно.

`~/proactive-school/secrets/google-service-account.json` (права 600) — ключ сервисного аккаунта Google ([docs/GOOGLE_SHEETS.md](../docs/GOOGLE_SHEETS.md)). Контейнер бота работает от пользователя 1001 — владельца папки на сервере, поэтому ключ ему виден.

## База

Данные PostgreSQL — в томе Docker `proactive-school_pgdata`. Автоматической копии нет. Снять копию вручную на ноутбук:

```bash
. deploy/.deploy.local && ssh -i ~/.ssh/hostmaster_bench "$DEPLOY_HOST" 'cd proactive-school && sudo -n docker compose exec -T school-db pg_dump -U proactive proactive_bot' > proactive-school-$(date +%F).sql
```

## Один токен — один получатель

Сообщения бота может забирать только одна программа. До сервера это делал Apps Script статусной таблицы. Перед первым запуском в таблице нажимают «Бот → Подключить сервер (aiogram)». Откат: `sudo -n docker compose stop school-bot` на сервере, затем в таблице «Бот → Отключить сервер» и «Бот → 2. Включить бота».
