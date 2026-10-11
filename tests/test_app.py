"""Headless-тест: сборка всех страниц без запуска сервера + ядро против mock."""
import asyncio
import sys
import types

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flet as ft
from core.settings import SettingsStore, PRESET_BEFORE, PRESET_AFTER
from core.logger import JsonlLogger
from core.llm import LMStudioClient, LLMError, normalize_base_url, trim_history, estimate_tokens


class FakePage:
    """Минимальный стенд вместо ft.Page: перехватывает update/dialogs."""
    def __init__(self):
        self.route = "/"
        self.session = None
        self.updated = 0
        self.dialogs = []
        self.title = ""
        self.theme_mode = None
        self.bgcolor = None
    def update(self, *a, **k): self.updated += 1
    def show_dialog(self, d): self.dialogs.append(d)
    def navigate(self, r): self.route = r
    def run_task(self, coro): return asyncio.get_event_loop().create_task(coro)


fails = []
def check(name, fn):
    try:
        fn()
        print(f"OK   {name}")
    except Exception as e:
        fails.append(name)
        print(f"FAIL {name}: {type(e).__name__}: {e}")


# ---------- core ----------
check("normalize /v1/model -> /v1",
      lambda: (_ for _ in ()).throw(AssertionError())
      if normalize_base_url("http://192.168.31.181:1234/v1/qwen3.8-27b") != "http://192.168.31.181:1234/v1" else None)
check("normalize без /v1",
      lambda: (_ for _ in ()).throw(AssertionError())
      if normalize_base_url("127.0.0.1:1234") != "http://127.0.0.1:1234/v1" else None)

import tempfile
_tmp = tempfile.mkdtemp()
store = SettingsStore(os.path.join(_tmp, "settings.json"))
_s = store.load()
_s["connection"]["base_url"] = "http://127.0.0.1:1234/v1"
_s["connection"]["api_key"] = "test-key"
store.save(_s)
check("settings save/load", lambda: store.load())
check("presets различаются",
      lambda: (_ for _ in ()).throw(AssertionError())
      if PRESET_BEFORE["temperature"] == PRESET_AFTER["temperature"] else None)

logger = JsonlLogger(os.path.join(_tmp, "logs.jsonl"))
logger.log("INFO", "test_event", foo=1)
logger.log("ERROR", "test_err", bar="x")
check("logger read", lambda: (_ for _ in ()).throw(AssertionError())
      if len(logger.read()) < 2 or logger.read(level="ERROR")[0]["level"] != "ERROR" else None)

client = LMStudioClient("http://127.0.0.1:1234/v1", "qwen3.8-27b", 30, api_key="test-key")

def _unauthorized():
    no_key = LMStudioClient("http://127.0.0.1:1234/v1", "qwen3.8-27b", 30)
    try:
        no_key.chat([{"role": "user", "content": "x"}], PRESET_AFTER)
        raise AssertionError("ожидался 401")
    except LLMError as e:
        assert "401" in str(e) and "Require API Key" in str(e), str(e)
    info = no_key.check_connection()
    assert not info["ok"] and "401" in (info["error"] or ""), info
check("401 без ключа — читаемое сообщение", _unauthorized)

def _chat():
    r = client.chat([{"role": "user", "content": "привет"}], PRESET_AFTER)
    assert r["content"] and r["latency_s"] > 0 and r["usage"]["completion_tokens"] == 64, r
    assert r["tok_s"] and r["tok_s"] > 0, r
check("client.chat против mock", _chat)
def _conn():
    info = client.check_connection()
    assert info["ok"], info
    assert info["active"] and info["active"]["quantization"] == "IQ3_S", info
check("check_connection + квантование", _conn)

msgs = [{"role": "system", "content": "s" * 100}] + [
    {"role": "user" if i % 2 else "assistant", "content": "x" * 400} for i in range(20)]
trimmed = trim_history(msgs, context_window=2048, max_tokens=512, history_turns=4)
check("trim_history бюджет",
      lambda: (_ for _ in ()).throw(AssertionError())
      if len(trimmed) > 9 or trimmed[0]["role"] != "system" else None)

# ---------- UI: сборка страниц ----------
page = FakePage()
chat_state = {"messages": []}

from ui.chat_page import ChatPage
from ui.admin_page import AdminPage
from ui.logs_page import LogsPage

chat = ChatPage(page, store, logger, chat_state)
admin = AdminPage(page, store, logger)
logs = LogsPage(page, logger)

check("chat.build", lambda: chat.build())
check("admin.build", lambda: admin.build())
check("logs.build", lambda: logs.build())

# чат: реальная отправка против mock (async handler)
async def _send():
    chat.input.value = "тестовый запрос"
    await chat.on_send(None)
    msgs = chat_state["messages"]
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "тестовый запрос", msgs
    assert msgs[1]["role"] == "assistant" and "mock-ответ" in msgs[1]["content"], msgs[1]
    assert msgs[1]["stats"]["tok_s"], msgs[1]["stats"]
check("chat.on_send e2e", lambda: asyncio.run(_send()))

# чат: режим сравнения
async def _send_compare():
    chat.compare_switch.value = True
    chat.input.value = "сравни меня"
    await chat.on_send(None)
    last = chat_state["messages"][-1]
    assert last.get("compare"), last
    assert last["compare"]["before"]["temperature"] == 1.0
    assert last["compare"]["after"]["temperature"] == 0.2
check("chat compare до/после", lambda: asyncio.run(_send_compare()))

# чат: навигация во время запроса (сценарий серого прямоугольника)
async def _nav_during_request():
    chat.compare_switch.value = False
    chat.input.value = "запрос с навигацией"
    task = asyncio.create_task(chat.on_send(None))
    await asyncio.sleep(0.05)           # запрос ушёл, в состоянии pending
    assert chat_state["messages"][-1].get("pending"), "pending не записан в состояние"
    page.route = "/logs"                # пользователь ушёл со страницы
    chat.build()                        # перестроение при навигации
    await task                          # ответ пришёл, page.update пропущен (мы не на чате)
    page.route = "/"                    # пользователь вернулся
    chat.build()
    last = chat_state["messages"][-1]
    assert not last.get("pending"), last
    assert "mock-ответ" in last["content"], last
check("навигация во время запроса", lambda: asyncio.run(_nav_during_request()))

# чат: регрессия — запрос идёт на base_url ИЗ СОХРАНЁННЫХ настроек
async def _uses_saved_url():
    saved = store.load()
    saved["connection"]["base_url"] = "http://127.0.0.1:9999/v1"  # заведомо пустой порт
    store.save(saved)
    chat.input.value = "куда уйдёт запрос"
    await chat.on_send(None)
    last = chat_state["messages"][-1]
    assert last.get("error"), last
    assert "127.0.0.1:9999" in last["content"], last["content"]
    # вернуть рабочий URL
    saved["connection"]["base_url"] = "http://127.0.0.1:1234/v1"
    store.save(saved)
check("чат использует сохранённый base_url", lambda: asyncio.run(_uses_saved_url()))

# reasoning-модель: извлечение reasoning_content / <think>, пустой content
async def _reasoning_cases():
    chat.input.value = "THINK_TEST"
    await chat.on_send(None)
    last = chat_state["messages"][-1]
    assert last["content"] == "Видимый ответ", last["content"]
    assert "внутреннее рассуждение" in last["stats"]["reasoning"], last["stats"]

    chat.input.value = "EMPTY_TEST"
    await chat.on_send(None)
    last = chat_state["messages"][-1]
    assert last["content"] == "", last
    assert "длинное рассуждение" in last["stats"]["reasoning"], last["stats"]
    chat._render_messages()  # рендер пустого ответа: placeholder, без исключений
    chat_state["messages"] = []  # чистим перед следующими тестами
check("reasoning: <think>/reasoning_content/пустой ответ", lambda: asyncio.run(_reasoning_cases()))

# админ: сохранение
def _save():
    admin.temperature.value = 0.5
    admin.max_tokens.value = "777"
    admin.on_save(None)
    s = store.load()
    assert s["generation"]["temperature"] == 0.5 and s["generation"]["max_tokens"] == 777, s
check("admin.on_save", _save)

# админ: проверка соединения
check("admin.on_check", lambda: asyncio.run(admin.on_check(None)))

# админ: пресеты
def _preset():
    admin._load_preset("before")
    assert admin.temperature.value == 1.0
    admin._load_preset("after")
    assert admin.temperature.value == 0.2
check("admin presets", _preset)

# логи
logs.refresh()
check("logs содержат события",
      lambda: (_ for _ in ()).throw(AssertionError())
      if not logger.read() else None)

print("\nИТОГ:", "ВСЕ ТЕСТЫ ПРОШЛИ" if not fails else f"ПРОВАЛЫ: {fails}")
sys.exit(1 if fails else 0)
