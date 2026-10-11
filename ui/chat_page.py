"""Страница чата: запросы к локальной LLM + режим сравнения «до/после».

Режим сравнения отправляет один и тот же запрос дважды:
с пресетом «до оптимизации» и с текущими настройками — и показывает
ответы рядом с метриками (время, токены, tok/s). Это закрывает пункт
задания «сравнить качество и скорость до и после оптимизации».
"""

from __future__ import annotations

import asyncio

import flet as ft

from core.llm import LMStudioClient, LLMError, trim_history
from core.settings import SettingsStore
from ui import theme as T


def stats_line(stats: dict) -> str:
    parts = []
    if stats.get("latency_s") is not None:
        parts.append(f"{stats['latency_s']} с")
    usage = stats.get("usage") or {}
    if usage.get("completion_tokens") is not None:
        reasoning_toks = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        tok = f"{usage['completion_tokens']} ток."
        if reasoning_toks:
            tok += f" (reasoning: {reasoning_toks})"
        parts.append(tok)
    if stats.get("tok_s"):
        parts.append(f"{stats['tok_s']} ток/с")
    if stats.get("temperature") is not None:
        parts.append(f"temp={stats['temperature']}")
    return " · ".join(parts)


def bubble_user(text: str) -> ft.Control:
    return ft.Row(
        [
            ft.Container(
                content=ft.Text(text, size=T.T_BODY, color=T.TEXT, selectable=True),
                bgcolor=T.SURFACE_2,
                border_radius=ft.BorderRadius.all(T.RADIUS),
                padding=ft.Padding.symmetric(horizontal=T.S3, vertical=T.S3),
                width=640,
            )
        ],
        alignment=ft.MainAxisAlignment.END,
    )


def message_body(text: str) -> list[ft.Control]:
    """Рендер ответа без виджета Markdown: в веб-клиенте Flet он уходит
    в серую заглушку. Код-блоки ```...``` рисуем моноширинным Text
    на тёмной подложке, остальное — обычным выделяемым Text."""
    parts = text.split("```")
    controls: list[ft.Control] = []
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        if i % 2 == 1:  # внутри fenced-блока
            code = part
            # первая строка после ``` часто содержит только имя языка
            first_nl = code.find("\n")
            if first_nl != -1 and " " not in code[:first_nl].strip() and len(code[:first_nl].strip()) <= 15:
                code = code[first_nl + 1:]
            controls.append(
                ft.Container(
                    content=ft.Text(code.rstrip(), size=T.T_SECONDARY, color=T.TEXT,
                                    selectable=True, font_family="Consolas"),
                    bgcolor=T.SURFACE_2,
                    border_radius=ft.BorderRadius.all(T.RADIUS),
                    padding=ft.Padding.all(T.S3),
                    width=608,
                )
            )
        else:
            controls.append(ft.Text(part.strip(), size=T.T_BODY, color=T.TEXT, selectable=True))
    return controls or [ft.Text(text, size=T.T_BODY, color=T.TEXT, selectable=True)]


def bubble_assistant(text: str, stats: dict | None = None, title: str | None = None,
                     accent: bool = False) -> ft.Control:
    controls: list = []
    if title:
        controls.append(
            ft.Text(
                title,
                size=T.T_META,
                weight=ft.FontWeight.W_600,
                color=T.ACCENT if accent else T.TEXT_SECONDARY,
            )
        )
    reasoning = (stats or {}).get("reasoning") or ""
    if not (text or "").strip():
        # reasoning-модель может вернуть пустой content: весь лимит токенов
        # съело рассуждение (см. reasoning_tokens в метриках)
        usage = (stats or {}).get("usage") or {}
        rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        hint = (f"Видимый ответ пуст: все {rt or ''} токенов ответа ушли во внутреннее "
                f"рассуждение модели. Увеличьте max_tokens (для этой модели — от 1024).")
        controls.append(
            ft.Text(hint, size=T.T_SECONDARY, color=T.WARNING, italic=True, selectable=True)
        )
    else:
        controls.extend(message_body(text))
    if reasoning.strip():
        reasoning_text = reasoning.strip()
        if len(reasoning_text) > 1500:
            reasoning_text = reasoning_text[:1500] + "…"
        controls.append(
            ft.Container(
                content=ft.Column(
                    [ft.Text("Рассуждение модели", size=T.T_META, weight=ft.FontWeight.W_600,
                             color=T.TEXT_SECONDARY),
                     ft.Text(reasoning_text, size=T.T_META, color=T.TEXT_SECONDARY,
                             selectable=True)],
                    spacing=T.S1, tight=True,
                ),
                bgcolor=T.SURFACE_2,
                border_radius=ft.BorderRadius.all(T.RADIUS),
                padding=ft.Padding.all(T.S3),
            )
        )
    if stats:
        controls.append(
            ft.Row(
                [ft.Icon(ft.Icons.SPEED, size=14, color=T.TEXT_SECONDARY),
                 T.meta_text(stats_line(stats))],
                spacing=T.S2,
            )
        )
    return ft.Row(
        [
            ft.Container(
                content=ft.Column(controls, spacing=T.S2, tight=True),
                bgcolor=T.SURFACE,
                border=T.border(),
                border_radius=ft.BorderRadius.all(T.RADIUS),
                padding=ft.Padding.symmetric(horizontal=T.S3, vertical=T.S3),
                width=640,
            )
        ],
        alignment=ft.MainAxisAlignment.START,
    )


def bubble_error(text: str) -> ft.Control:
    return ft.Row(
        [
            ft.Container(
                content=ft.Row(
                    [ft.Icon(ft.Icons.ERROR_OUTLINE, size=16, color=T.ERROR),
                     ft.Text(text, size=T.T_SECONDARY, color=T.ERROR, selectable=True, expand=True)],
                    spacing=T.S2,
                ),
                bgcolor=T.SURFACE,
                border=T.border(color=T.ERROR),
                border_radius=ft.BorderRadius.all(T.RADIUS),
                padding=ft.Padding.symmetric(horizontal=T.S3, vertical=T.S3),
                width=640,
            )
        ],
        alignment=ft.MainAxisAlignment.START,
    )


class ChatPage:
    def __init__(self, page: ft.Page, store: SettingsStore, logger, state: dict):
        self.page = page
        self.store = store
        self.logger = logger
        self.state = state  # {"messages": [...]} — история живёт в памяти приложения
        self.busy = False
        self._init_controls()

    def _init_controls(self) -> None:
        """Создаёт контролы. Вызывается из build() при каждой навигации:
        свежие инстансы вместо перепривязки старых (источник серых заглушек)."""
        self.messages_view = ft.ListView(expand=True, spacing=T.S3, auto_scroll=True)
        self.input = T.field(
            "Сообщение",
            hint_text="Введите запрос. Enter — отправить, Shift+Enter — новая строка",
            multiline=True,
            min_lines=1,
            max_lines=6,
            shift_enter=True,
            on_submit=self.on_send,
            expand=True,
        )
        self.send_btn = ft.FilledButton(
            content="Отправить",
            icon=ft.Icons.SEND,
            on_click=self.on_send,
            style=ft.ButtonStyle(bgcolor=T.ACCENT, color=T.ON_ACCENT),
            disabled=self.busy,
        )
        self.compare_switch = ft.Switch(
            label="Сравнение до/после",
            value=getattr(self, "compare_switch", None).value if hasattr(self, "compare_switch") else False,
            active_color=T.ACCENT,
            on_change=lambda e: self._refresh_header(),
        )
        self.header_status = ft.Text("", size=T.T_META, color=T.TEXT_SECONDARY)

    # ------------------------------------------------------------------ view
    def build(self) -> ft.Control:
        self._init_controls()
        self._refresh_header()
        self._render_messages()
        return T.page_scaffold(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, color=T.ACCENT, size=24),
                            ft.Text("Чат", size=T.T_TITLE, weight=ft.FontWeight.W_600, color=T.TEXT),
                            ft.Container(expand=True),
                            self.compare_switch,
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                tooltip="Очистить чат",
                                icon_color=T.TEXT_SECONDARY,
                                on_click=self.on_clear,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.header_status,
                    ft.Divider(color=T.BORDER, height=T.S4),
                    ft.Container(self.messages_view, expand=True),
                    ft.Row(
                        [self.input, self.send_btn],
                        vertical_alignment=ft.CrossAxisAlignment.END,
                        spacing=T.S2,
                    ),
                ],
                expand=True,
                spacing=T.S2,
            )
        )

    def _refresh_header(self) -> None:
        settings = self.store.load()
        conn, gen = settings["connection"], settings["generation"]
        preset_names = {"before": "пресет «до»", "after": "пресет «после»", "custom": "свои настройки"}
        mode = "режим сравнения" if self.compare_switch.value else preset_names.get(settings["active_preset"], "")
        self.header_status.value = (
            f"Модель: {conn['model']} · {conn['base_url']} · "
            f"temp={gen['temperature']}, max_tokens={gen['max_tokens']}, "
            f"контекст={gen['context_window']} · {mode}"
        )

    def _render_messages(self) -> None:
        self.messages_view.controls.clear()
        history = self.state.get("messages", [])
        if not history:
            self.messages_view.controls.append(
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Icon(ft.Icons.SMART_TOY, size=40, color=T.TEXT_SECONDARY),
                            ft.Text("Запросов пока нет", size=T.T_BODY, color=T.TEXT_SECONDARY),
                            T.meta_text(
                                "Отправьте сообщение — ответ появится здесь вместе с метриками "
                                "(время, токены, ток/с). Включите «Сравнение до/после», чтобы увидеть "
                                "ответы с пресетом «до оптимизации» и текущими настройками рядом."
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=T.S3,
                    ),
                    padding=ft.Padding.only(top=T.S8),
                    alignment=ft.Alignment.CENTER,
                )
            )
            return
        for msg in history:
            self.messages_view.controls.append(self._render_msg(msg))

    @staticmethod
    def _render_msg(msg: dict) -> ft.Control:
        if msg.get("pending"):
            return ft.Row(
                [ft.ProgressRing(width=16, height=16, stroke_width=2, color=T.ACCENT),
                 T.meta_text("Модель генерирует ответ…")],
                spacing=T.S2,
            )
        if msg["role"] == "user":
            return bubble_user(msg["content"])
        if msg.get("error"):
            return bubble_error(msg["content"])
        if msg.get("compare"):
            pair = msg["compare"]
            # фиксированная ширина вместо expand: Expanded внутри wrap-раскладки
            # — ошибка ограничений во Flutter, клиент рисует серую заглушку
            return ft.Row(
                [
                    ft.Container(
                        bubble_assistant(pair["before"]["content"], pair["before"],
                                         title="До оптимизации (дефолт)"),
                        width=460),
                    ft.Container(
                        bubble_assistant(pair["after"]["content"], pair["after"],
                                         title="После оптимизации (текущие настройки)",
                                         accent=True),
                        width=460),
                ],
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.START,
                wrap=True,
                spacing=T.S3,
            )
        return bubble_assistant(msg["content"], msg.get("stats"))

    def _refresh_view(self) -> None:
        """Перерисовка списка сообщений. page.update() — только если
        пользователь сейчас на странице чата: иначе обновляем отсоединённое
        дерево контролов, что даёт рассинхрон и серые заглушки в клиенте."""
        self._render_messages()
        if (self.page.route or "/") == "/":
            self.page.update()

    # ---------------------------------------------------------------- actions
    def on_clear(self, e) -> None:
        self.state["messages"] = []
        self.logger.log("INFO", "chat_cleared")
        self._refresh_view()

    def _client(self) -> tuple[LMStudioClient, dict, dict]:
        settings = self.store.load()
        conn = settings["connection"]
        return (
            LMStudioClient(conn["base_url"], conn["model"], conn.get("timeout", 600),
                           api_key=conn.get("api_key", "")),
            settings["generation"],
            conn,
        )

    def _build_messages(self, gen: dict, user_text: str) -> list:
        msgs: list = []
        if gen.get("system_prompt"):
            msgs.append({"role": "system", "content": gen["system_prompt"]})
        for m in self.state.get("messages", [])[-(gen.get("history_turns", 4) * 2):]:
            if (m["role"] in ("user", "assistant") and not m.get("compare")
                    and not m.get("error") and not m.get("pending")):
                msgs.append({"role": m["role"], "content": m["content"]})
        msgs.append({"role": "user", "content": user_text})
        return trim_history(msgs, gen.get("context_window", 8192),
                            gen.get("max_tokens", 1024), gen.get("history_turns", 4))

    def _query(self, user_text: str, gen: dict) -> dict:
        client, _, conn = self._client()
        messages = self._build_messages(gen, user_text)
        result = client.chat(messages, gen)
        result["temperature"] = gen.get("temperature")
        result["used_params"] = {
            "base_url": conn.get("base_url"),  # фактический URL запроса — для диагностики
            "model": conn.get("model"),
            "temperature": gen.get("temperature"),
            "top_p": gen.get("top_p"),
            "max_tokens": gen.get("max_tokens"),
            "context_window": gen.get("context_window"),
            "system_prompt_chars": len(gen.get("system_prompt") or ""),
            "messages_sent": len(messages),
        }
        return result

    async def on_send(self, e) -> None:
        text = (self.input.value or "").strip()
        if not text or self.busy:
            return
        self.busy = True
        self.send_btn.disabled = True
        self.input.value = ""
        # pending — запись в состоянии, а не контрол: переживает навигацию,
        # рендерится единообразно при любом rebuild страницы
        pending_msg: dict = {"role": "assistant", "pending": True}
        self.state.setdefault("messages", []).append({"role": "user", "content": text})
        self.state["messages"].append(pending_msg)
        self._refresh_view()

        compare = self.compare_switch.value
        try:
            if compare:
                gen_after = self.store.load()["generation"]
                gen_before = self.store.preset("before")
                r_before, r_after = await asyncio.gather(
                    asyncio.to_thread(self._query, text, gen_before),
                    asyncio.to_thread(self._query, text, gen_after),
                )
                self.logger.log(
                    "INFO", "compare",
                    prompt=text[:200],
                    before={"latency_s": r_before["latency_s"], "tok_s": r_before["tok_s"],
                            "usage": r_before["usage"], "params": r_before["used_params"]},
                    after={"latency_s": r_after["latency_s"], "tok_s": r_after["tok_s"],
                           "usage": r_after["usage"], "params": r_after["used_params"]},
                )
                pending_msg.pop("pending")
                pending_msg.update(content=r_after["content"],
                                   compare={"before": r_before, "after": r_after})
            else:
                result = await asyncio.to_thread(self._query, text, self.store.load()["generation"])
                self.logger.log(
                    "INFO", "chat",
                    prompt=text[:200],
                    latency_s=result["latency_s"], tok_s=result["tok_s"],
                    usage=result["usage"], params=result["used_params"],
                )
                pending_msg.pop("pending")
                pending_msg.update(content=result["content"], stats=result)
        except LLMError as exc:
            conn_now = self.store.load()["connection"]
            self.logger.log("ERROR", "chat_error", prompt=text[:200],
                            base_url=conn_now.get("base_url"), model=conn_now.get("model"),
                            error=str(exc))
            pending_msg.pop("pending")
            pending_msg.update(content=str(exc), error=True)
        except Exception as exc:  # неожиданное — тоже в лог, без падения UI
            self.logger.log("ERROR", "chat_unexpected", error=repr(exc))
            pending_msg.pop("pending")
            pending_msg.update(content=f"Неожиданная ошибка: {exc}", error=True)
        finally:
            self.busy = False
            self.send_btn.disabled = False
            self._refresh_view()
