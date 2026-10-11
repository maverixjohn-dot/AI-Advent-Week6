"""
Telegram-бот — многопользовательский фасад приватного LLM-сервиса.

Контур: Telegram <-> этот бот <-> LM Studio (OpenAI-совместимый API, 127.0.0.1:1234).

Функции:
- строгий whitelist (админы из ADMIN_IDS + пользователи, добавленные /allow)
- rate limit на пользователя (N сообщений/мин) + 1 активная генерация на пользователя
- глобальный лимит параллельности (GLOBAL_CONCURRENCY = Max Concurrent Predictions в LM Studio)
- история диалогов в SQLite со скользящим окном по токенам
- админ-команды: системный промпт, параметры генерации, контекст, rate limit, reasoning, статистика
- обработка переполнения контекста и недоступности LLM

Запуск: заполнить .env (см. .env.example), затем `python3 bot.py`. Python 3.10+.
"""

import asyncio
import os
import time
from collections import defaultdict, deque

import aiosqlite
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

# ----------------------------- конфигурация -----------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
LM_URL = os.environ.get("LMSTUDIO_URL", "http://127.0.0.1:1234").rstrip("/")
LM_TOKEN = os.environ.get("LMSTUDIO_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "bot.db")
GLOBAL_CONCURRENCY = int(os.environ.get("GLOBAL_CONCURRENCY", "4"))
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=900.0, write=30.0, pool=30.0)
CHARS_PER_TOKEN = 3   # грубая оценка для кириллицы; точный токенизатор не подключаем
TG_MSG_LIMIT = 4000   # с запасом от лимита Telegram 4096

# значения по умолчанию; дальше живут в SQLite и меняются админ-командами
DEFAULT_SETTINGS = {
    "system_prompt": os.environ.get("SYSTEM_PROMPT", "Ты — полезный ассистент. Отвечай кратко и по делу."),
    "temperature": os.environ.get("TEMPERATURE", "0.7"),
    "top_p": os.environ.get("TOP_P", "0.95"),
    "max_tokens": os.environ.get("MAX_TOKENS", "1024"),
    "context_tokens": os.environ.get("CONTEXT_TOKENS", "8000"),
    "rate_limit_per_min": os.environ.get("RATE_LIMIT_PER_MIN", "5"),
    "max_input_chars": os.environ.get("MAX_INPUT_CHARS", "12000"),
    "model": os.environ.get("MODEL", ""),        # пусто — определить через /v1/models при старте
    "reasoning": os.environ.get("REASONING", "auto"),  # auto | on | off
}

# ----------------------------- состояние -----------------------------

settings: dict = {}
db: aiosqlite.Connection
http: httpx.AsyncClient
sem: asyncio.Semaphore
user_locks: defaultdict = defaultdict(asyncio.Lock)   # 1 активная генерация на пользователя
user_requests: defaultdict = defaultdict(deque)       # метки времени для rate limit
started_at = time.monotonic()
active_requests = 0
stats_lock = asyncio.Lock()
counters = {"total": 0, "ok": 0, "err": 0, "lat_sum": 0.0, "completion_tokens": 0}
per_user = defaultdict(lambda: {"total": 0, "ok": 0})


class ContextOverflow(Exception):
    """Сервер отклонил запрос из-за превышения контекста модели."""


# ----------------------------- БД -----------------------------

async def init_db():
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, ts REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, ts REAL NOT NULL, latency REAL NOT NULL,
            prompt_tokens INTEGER, completion_tokens INTEGER, ok INTEGER NOT NULL);
    """)
    for k, v in DEFAULT_SETTINGS.items():
        await db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    await db.commit()


async def load_settings():
    global settings
    async with db.execute("SELECT key, value FROM settings") as cur:
        settings = dict(await cur.fetchall())


async def set_setting(key: str, value: str):
    settings[key] = value
    await db.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    await db.commit()


async def is_allowed(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    async with db.execute("SELECT status FROM users WHERE user_id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return bool(row and row[0] == "allowed")


# ----------------------------- контекст -----------------------------

def est_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


async def get_history(uid: int, budget_tokens: int) -> list[dict]:
    """Последние сообщения, влезающие в бюджет (скользящее окно, старшие отсекаются)."""
    async with db.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 200", (uid,)
    ) as cur:
        rows = await cur.fetchall()
    out, used = [], 0
    for role, content in rows:
        t = est_tokens(content)
        if used + t > budget_tokens:
            break
        out.append({"role": role, "content": content})
        used += t
    out.reverse()
    return out


# ----------------------------- клиент LLM -----------------------------

async def resolve_model():
    if settings.get("model"):
        return
    headers = {"Authorization": f"Bearer {LM_TOKEN}"} if LM_TOKEN else {}
    r = await http.get(f"{LM_URL}/v1/models", headers=headers)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    if ids:
        settings["model"] = ids[0]


async def llm_chat(messages: list[dict]) -> tuple[str, dict]:
    """Один запрос к LM Studio. Возвращает (текст, usage). Ретраи на сетевых ошибках и 5xx."""
    payload = {
        "model": settings["model"],
        "messages": messages,
        "temperature": float(settings["temperature"]),
        "top_p": float(settings["top_p"]),
        "max_tokens": int(settings["max_tokens"]),
        "stream": False,
    }
    if settings.get("reasoning") in ("on", "off"):
        payload["chat_template_kwargs"] = {"enable_thinking": settings["reasoning"] == "on"}
    headers = {"Authorization": f"Bearer {LM_TOKEN}"} if LM_TOKEN else {}

    for attempt in range(3):
        try:
            r = await http.post(f"{LM_URL}/v1/chat/completions", json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code == 400:
            body = r.text.lower()
            if "chat_template_kwargs" in payload and "chat_template_kwargs" in body:
                payload.pop("chat_template_kwargs")  # версия LM Studio не принимает параметр
                continue
            if "context" in body or "exceed" in body or "too large" in body:
                raise ContextOverflow(r.text[:300])
            raise RuntimeError(f"LLM HTTP 400: {r.text[:300]}")
        if r.status_code >= 500 and attempt < 2:
            await asyncio.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"], data.get("usage", {})
    raise RuntimeError("unreachable")


# ----------------------------- утилиты -----------------------------

def rate_ok(uid: int, limit: int) -> tuple[bool, int]:
    now = time.monotonic()
    dq = user_requests[uid]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= limit:
        return False, int(60 - (now - dq[0])) + 1
    dq.append(now)
    return True, 0


def split_text(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


async def record_stat(uid: int, ok: bool, lat: float = 0.0, usage: dict | None = None):
    await db.execute(
        "INSERT INTO stats(user_id, ts, latency, prompt_tokens, completion_tokens, ok) "
        "VALUES (?,?,?,?,?,?)",
        (uid, time.time(), lat, (usage or {}).get("prompt_tokens", 0),
         (usage or {}).get("completion_tokens", 0), 1 if ok else 0))
    await db.commit()
    async with stats_lock:
        counters["total"] += 1
        counters["ok" if ok else "err"] += 1
        counters["lat_sum"] += lat
        counters["completion_tokens"] += (usage or {}).get("completion_tokens", 0)
        per_user[uid]["total"] += 1
        if ok:
            per_user[uid]["ok"] += 1


def is_admin(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id in ADMIN_IDS)


# ----------------------------- хендлеры -----------------------------

dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(m: Message):
    if not m.from_user:
        return
    uid = m.from_user.id
    if not await is_allowed(uid):
        await m.answer(
            f"Доступ ограничен.\nВаш Telegram ID: <code>{uid}</code> — передайте его администратору.",
            parse_mode="HTML")
        return
    await m.answer("Приватный LLM-бот. Напишите сообщение — получите ответ.\n"
                   "/reset — очистить контекст диалога.")


@dp.message(Command("reset"))
async def cmd_reset(m: Message):
    if not m.from_user or not await is_allowed(m.from_user.id):
        return
    uid = m.from_user.id
    args = (m.text or "").split()
    target = uid
    if len(args) > 1 and args[1].isdigit() and is_admin(m):
        target = int(args[1])
    await db.execute("DELETE FROM messages WHERE user_id=?", (target,))
    await db.commit()
    await m.answer("Контекст очищен." if target == uid else f"Контекст пользователя {target} очищен.")


@dp.message(Command("allow"))
async def cmd_allow(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    if not command.args or not command.args.strip().isdigit():
        await m.answer("Использование: /allow <user_id>")
        return
    uid = int(command.args.strip())
    await db.execute(
        "INSERT INTO users(user_id, status) VALUES (?, 'allowed') "
        "ON CONFLICT(user_id) DO UPDATE SET status='allowed'", (uid,))
    await db.commit()
    await m.answer(f"Пользователь {uid} добавлен в whitelist.")


@dp.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    if not command.args or not command.args.strip().isdigit():
        await m.answer("Использование: /ban <user_id>")
        return
    uid = int(command.args.strip())
    await db.execute(
        "INSERT INTO users(user_id, status) VALUES (?, 'banned') "
        "ON CONFLICT(user_id) DO UPDATE SET status='banned'", (uid,))
    await db.commit()
    await m.answer(f"Пользователь {uid} заблокирован.")


@dp.message(Command("users"))
async def cmd_users(m: Message):
    if not is_admin(m):
        return
    async with db.execute("SELECT user_id, status FROM users ORDER BY user_id") as cur:
        rows = await cur.fetchall()
    lines = [f"{uid} — {st}" for uid, st in rows] or ["whitelist пуст"]
    lines.append(f"админы: {', '.join(map(str, ADMIN_IDS)) or 'не заданы'}")
    await m.answer("\n".join(lines))


@dp.message(Command("set_system"))
async def cmd_set_system(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    if not command.args:
        await m.answer(f"Текущий system prompt:\n{settings['system_prompt']}")
        return
    await set_setting("system_prompt", command.args.strip())
    await m.answer("System prompt обновлён.")


@dp.message(Command("set_params"))
async def cmd_set_params(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    allowed = {"temperature": (0.0, 2.0, float), "top_p": (0.05, 1.0, float),
               "max_tokens": (16, 32768, int)}
    if not command.args:
        cur = "\n".join(f"{k} = {settings[k]}" for k in allowed)
        await m.answer(f"{cur}\n\nИзменение: /set_params temperature=0.5 max_tokens=2048")
        return
    applied, errors = [], []
    for pair in command.args.split():
        if "=" not in pair:
            errors.append(pair)
            continue
        k, v = pair.split("=", 1)
        if k not in allowed:
            errors.append(f"{k} (неизвестный параметр)")
            continue
        lo, hi, cast = allowed[k]
        try:
            val = cast(v)
            if not lo <= val <= hi:
                raise ValueError
        except ValueError:
            errors.append(f"{k}={v} (допустимо {lo}..{hi})")
            continue
        await set_setting(k, str(val))
        applied.append(f"{k}={val}")
    msg = []
    if applied:
        msg.append("Применено: " + ", ".join(applied))
    if errors:
        msg.append("Ошибки: " + "; ".join(errors))
    await m.answer("\n".join(msg))


@dp.message(Command("set_context"))
async def cmd_set_context(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    if not command.args or not command.args.strip().isdigit():
        await m.answer(f"Бюджет истории: {settings['context_tokens']} токенов.\n"
                       "Изменение: /set_context 8000 (1024..65536)")
        return
    val = int(command.args.strip())
    if not 1024 <= val <= 65536:
        await m.answer("Допустимо 1024..65536.")
        return
    await set_setting("context_tokens", str(val))
    await m.answer(f"Бюджет истории установлен: {val} токенов.")


@dp.message(Command("set_rate"))
async def cmd_set_rate(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    if not command.args or not command.args.strip().isdigit():
        await m.answer(f"Rate limit: {settings['rate_limit_per_min']} сообщений/мин.\n"
                       "Изменение: /set_rate 5 (1..60)")
        return
    val = int(command.args.strip())
    if not 1 <= val <= 60:
        await m.answer("Допустимо 1..60.")
        return
    await set_setting("rate_limit_per_min", str(val))
    await m.answer(f"Rate limit установлен: {val} сообщений/мин.")


@dp.message(Command("set_reasoning"))
async def cmd_set_reasoning(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    val = (command.args or "").strip().lower()
    if val not in ("auto", "on", "off"):
        await m.answer(f"Reasoning: {settings['reasoning']}.\n"
                       "Изменение: /set_reasoning auto|on|off\n"
                       "auto — не передавать параметр (дефолт модели); "
                       "on/off — enable_thinking через chat_template_kwargs.")
        return
    await set_setting("reasoning", val)
    await m.answer(f"Reasoning: {val}.")


@dp.message(Command("settings"))
async def cmd_settings(m: Message):
    if not is_admin(m):
        return
    hidden = {"system_prompt"}
    lines = [f"{k} = {v}" for k, v in sorted(settings.items()) if k not in hidden]
    lines.append(f"system_prompt = <{len(settings.get('system_prompt', ''))} символов>")
    lines.append(f"GLOBAL_CONCURRENCY = {GLOBAL_CONCURRENCY} (env, только перезапуск)")
    await m.answer("\n".join(lines))


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if not is_admin(m):
        return
    uptime = int(time.monotonic() - started_at)
    total, ok = counters["total"], counters["ok"]
    avg_lat = counters["lat_sum"] / ok if ok else 0.0
    avg_speed = counters["completion_tokens"] / counters["lat_sum"] if counters["lat_sum"] else 0.0
    lines = [
        f"uptime: {uptime // 3600}ч {(uptime % 3600) // 60}м",
        f"запросов: {total} (ok {ok}, err {counters['err']})"
        + (f", success {100 * ok / total:.1f}%" if total else ""),
        f"средняя latency: {avg_lat:.1f}с, агрег. скорость: {avg_speed:.1f} ток/с",
        f"слоты LLM: занято {active_requests}/{GLOBAL_CONCURRENCY}",
        f"модель: {settings.get('model') or 'не определена'}",
    ]
    if per_user:
        top = sorted(per_user.items(), key=lambda kv: -kv[1]["total"])[:5]
        lines.append("топ пользователей: " + ", ".join(f"{u}:{s['total']}" for u, s in top))
    await m.answer("\n".join(lines))


@dp.message(Command("health"))
async def cmd_health(m: Message):
    if not is_admin(m):
        return
    headers = {"Authorization": f"Bearer {LM_TOKEN}"} if LM_TOKEN else {}
    try:
        t0 = time.monotonic()
        r = await http.get(f"{LM_URL}/v1/models", headers=headers, timeout=10)
        dt = time.monotonic() - t0
        ids = [x["id"] for x in r.json().get("data", [])]
        await m.answer(f"LM Studio: HTTP {r.status_code} за {dt:.2f}с\n"
                       f"Модели: {', '.join(ids) or '—'}\n"
                       f"Активная: {settings.get('model') or '—'}")
    except Exception as e:
        await m.answer(f"LM Studio недоступна: {type(e).__name__}: {e}")


@dp.message(F.text)
async def on_text(m: Message):
    if not m.from_user:
        return
    uid = m.from_user.id
    if not await is_allowed(uid):
        await m.answer(f"Доступ ограничен. Ваш ID: <code>{uid}</code>", parse_mode="HTML")
        return
    text = (m.text or "").strip()
    if not text:
        return

    max_chars = int(settings["max_input_chars"])
    if len(text) > max_chars:
        await m.answer(f"Сообщение слишком длинное: {len(text)} символов при лимите {max_chars}.")
        return

    ok_rate, wait = rate_ok(uid, int(settings["rate_limit_per_min"]))
    if not ok_rate:
        await m.answer(f"Лимит {settings['rate_limit_per_min']} сообщений/мин. Подождите ~{wait} с.")
        return

    if not settings.get("model"):
        try:
            await resolve_model()
        except Exception:
            pass
        if not settings.get("model"):
            await m.answer("LLM-сервис недоступен (модель не определена). Админу: /health.")
            return

    lock = user_locks[uid]
    if lock.locked():
        await m.answer("Подождите завершения предыдущего запроса.")
        return

    global active_requests
    async with lock:
        await db.execute(
            "INSERT INTO messages(user_id, role, content, ts) VALUES (?,?,?,?)",
            (uid, "user", text, time.time()))
        await db.commit()

        budget = max(512,
                     int(settings["context_tokens"])
                     - est_tokens(settings["system_prompt"])
                     - est_tokens(text)
                     - int(settings["max_tokens"]) - 128)
        history = await get_history(uid, budget)  # включает только что сохранённое сообщение
        messages = [{"role": "system", "content": settings["system_prompt"]}] + history

        notice = None
        if active_requests >= GLOBAL_CONCURRENCY:
            notice = await m.answer("⏳ Все слоты заняты, запрос поставлен в очередь…")

        await sem.acquire()
        active_requests += 1
        t0 = time.monotonic()
        try:
            reply, usage = await llm_chat(messages)
            lat = time.monotonic() - t0
            await db.execute(
                "INSERT INTO messages(user_id, role, content, ts) VALUES (?,?,?,?)",
                (uid, "assistant", reply, time.time()))
            await db.commit()
            await record_stat(uid, True, lat, usage)
            for part in split_text(reply):
                await m.answer(part)
        except ContextOverflow:
            await record_stat(uid, False, time.monotonic() - t0)
            await m.answer("Диалог превысил лимит контекста модели.\n"
                           "Используйте /reset и переформулируйте запрос.")
        except Exception as e:
            await record_stat(uid, False, time.monotonic() - t0)
            await m.answer(f"Ошибка LLM-сервиса ({type(e).__name__}). "
                           "Попробуйте позже; если повторяется — сообщите администратору.")
        finally:
            active_requests -= 1
            sem.release()
            if notice:
                try:
                    await notice.delete()
                except Exception:
                    pass


# ----------------------------- запуск -----------------------------

async def main():
    global db, http, sem
    sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    db = await aiosqlite.connect(DB_PATH)
    await init_db()
    await load_settings()
    http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    try:
        await resolve_model()
        print(f"[i] Модель: {settings.get('model')}")
    except Exception as e:
        print(f"[!] LM Studio недоступна при старте: {e!r} (проверка — /health)")
    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await http.aclose()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
