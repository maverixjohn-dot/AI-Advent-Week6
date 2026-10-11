# Приватный LLM-сервис: Qwen3.8-27B (LM Studio) + Telegram-бот

Локальная LLM как приватный сервис с HTTP API и чат-интерфейсом через Telegram.

```
Пользователи (интернет)
        ↕ Telegram Bot API
aiogram-бот (этот хост): whitelist, rate limit, очередь,
                         история + скользящее окно контекста,
                         системный промпт, параметры генерации, статистика
        ↕ HTTP 127.0.0.1:1234, Bearer-токен
LM Studio 0.4.x: Qwen3.8-27B UD-IQ3_S, continuous batching (4 слота)
```

Состав: `bot.py` — бот, `load_test.py` — нагрузочный тест, `.env.example` — конфигурация.

## Шаг 1. Настройка LM Studio (на сервере)

1. Обновить LM Studio до 0.4.x.
2. Загрузить модель `Qwen3.8-27B UD-IQ3_S` с параметрами:
   - Context Length: **16384** (поднять до 32768 только при запасе памяти)
   - Max Concurrent Predictions: **4**
   - Flash Attention: **on**
   - KV cache quantization: q8_0, если влезает
   - Speculative decoding (MTP): on, если доступен для этой модели
3. Developer → Server Settings:
   - **API Tokens: on**, создать токен (по умолчанию сервер открыт всем)
   - Serve on local network: **off** (включать только на время сетевого теста)
   - Auto-unload / JIT: **off** — модель должна быть горячей
4. Smoke-тест: 15–20 русскоязычных промптов через чат LM Studio. Если качество
   IQ3_S неудовлетворительно — это последний дешёвый момент сменить модель/квант.

Проверка API с сервера:

```bash
curl http://127.0.0.1:1234/v1/models -H "Authorization: Bearer <TOKEN>"
```

## Шаг 2. Запуск бота

```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.10+
pip install -r requirements.txt
cp .env.example .env   # заполнить BOT_TOKEN, ADMIN_IDS, LMSTUDIO_TOKEN
python3 bot.py
```

Свой Telegram ID можно узнать, написав боту до добавления в whitelist —
бот ответит сообщением с ID.

### systemd (автозапуск)

```ini
# /etc/systemd/system/tg-llm-bot.service
[Unit]
Description=Private LLM Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/opt/tg-llm-bot
EnvironmentFile=/opt/tg-llm-bot/.env
ExecStart=/opt/tg-llm-bot/.venv/bin/python3 /opt/tg-llm-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Шаг 3. Приёмочные тесты (критерии задания)

| # | Критерий | Действие | Ожидаемый результат |
|---|----------|----------|---------------------|
| 1 | Доступ по сети | Включить «Serve on local network», с **другой машины**: `curl http://<IP>:1234/v1/chat/completions` без токена и с токеном | без токена — 401, с токеном — 200. После теста LAN-доступ выключить |
| 2 | Стабильность при параллельных запросах | `python3 load_test.py --model <ID> --token <T> --requests 8 --concurrency 4` | 100% успешность, зафиксировать p50/p95 и агрег. ток/с; 4 запроса идут параллельно, 4 — в очереди |
| 3 | Max context | `python3 load_test.py --model <ID> --token <T> --requests 1 --concurrency 1 --pad-tokens 20000` | предсказуемая ошибка контекста (HTTP 400), не таймаут и не тихая порча ответа |
| 4 | Rate limit | От одного user_id отправить боту 15+ сообщений подряд | отказ с указанием времени ожидания, другие пользователи не затронуты |
| 5 | Долговременная стабильность | `python3 load_test.py --requests 100 --concurrency 3` + 30–60 мин работы бота | нет роста памяти и деградации latency, ошибок < 1% |

В боте также проверить эмуляцию «нескольких пользователей»: 2–3 аккаунта пишут
одновременно, у всех корректные независимые контексты.

## Админ-команды бота

| Команда | Назначение |
|---|---|
| `/allow <id>` / `/ban <id>` / `/users` | управление whitelist |
| `/set_system [текст]` | системный промпт (без аргумента — показать) |
| `/set_params temperature=0.5 max_tokens=2048` | дефолты генерации |
| `/set_context <1024..65536>` | бюджет истории в токенах |
| `/set_rate <1..60>` | лимит сообщений/мин на пользователя |
| `/set_reasoning auto\|on\|off` | управление thinking-режимом модели |
| `/settings`, `/stats`, `/health` | конфигурация, метрики, доступность LM Studio |
| `/reset [id]` | сброс контекста (себе — всем, id — админу) |

Все настройки хранятся в SQLite и применяются без перезапуска, кроме
`GLOBAL_CONCURRENCY` (env, требует перезапуска).

## Известные ограничения

1. **Оценка токенов** — грубая (символы/3), точный токенизатор Qwen не подключён.
   Бюджет контекста в боте намеренно меньше loaded context LM Studio — запас
   компенсирует погрешность.
2. **Гибридная архитектура Qwen3.8 (DeltaNet)**: context shift на стороне сервера
   может не работать — обрезка истории выполняется только в боте, переполнение
   ловится по HTTP 400 и предлагает `/reset`.
3. **Reasoning**: параметр передаётся как `chat_template_kwargs.enable_thinking`;
   если версия LM Studio его отклоняет, бот автоматически повторяет запрос без него.
4. **Приватность**: сервис приватен относительно внешних провайдеров, но админ
   хоста видит все сообщения в логах и базе. Пользователей стоит предупредить.
5. Стриминга нет (решение зафиксировано); длинные ответы режутся на части по 4000 символов.

## Шаг 4. Отчёт

Зафиксировать: конфигурацию LM Studio, результаты 5 тестов (таблица выше),
реальную ёмкость сервиса (сколько одновременных пользователей обслуживается
без деградации). Если скорость на поток при 4 параллельных < 5 ток/с —
фолбэк: модель меньшего калибра.
