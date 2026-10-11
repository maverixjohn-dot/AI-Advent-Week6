"""Страница администрирования.

Блоки:
1. Подключение — base_url, модель, таймаут + проверка соединения
   (показывает формат/квантование/размер загруженной модели из LM Studio REST API).
2. Параметры генерации — системный промпт, temperature, top_p, max_tokens,
   context window, штрафы, seed, глубина истории.
3. Пресеты «до оптимизации» / «после оптимизации» — для сравнения.
"""

from __future__ import annotations

import asyncio

import flet as ft

from core.llm import LMStudioClient, normalize_base_url
from core.settings import SettingsStore
from ui import theme as T


def _fmt_size(size_bytes) -> str:
    if not size_bytes:
        return "—"
    return f"{size_bytes / 1024**3:.2f} GB"


class AdminPage:
    def __init__(self, page: ft.Page, store: SettingsStore, logger):
        self.page = page
        self.store = store
        self.logger = logger

        # --- подключение
        self.base_url = T.field("Base URL", width=380, hint_text="http://127.0.0.1:1234/v1")
        self.model = T.field("Модель (id)", width=260, hint_text="qwen3.8-27b")
        self.timeout = T.field("Таймаут, с", width=140, keyboard_type=ft.KeyboardType.NUMBER)
        self.api_key = T.field(
            "API-ключ", width=380, password=True, can_reveal_password=True,
            hint_text="только если в LM Studio включён Require API Key",
        )

        # --- генерация
        self.system_prompt = T.field(
            "Системный промпт",
            multiline=True, min_lines=6, max_lines=12,
            hint_text="Инструкции модели: роль, формат ответа, ограничения",
        )
        self.temperature = ft.Slider(min=0.0, max=2.0, divisions=20, label="{value}",
                                     active_color=T.ACCENT)
        self.top_p = ft.Slider(min=0.05, max=1.0, divisions=19, label="{value}",
                               active_color=T.ACCENT)
        self.max_tokens = T.field("Максимум токенов", width=200,
                                  keyboard_type=ft.KeyboardType.NUMBER,
                                  hint_text="например, 2048")
        self.context_window = T.field("Контекстное окно", width=200,
                                      keyboard_type=ft.KeyboardType.NUMBER,
                                      hint_text="например, 16384")
        self.presence_penalty = T.field("Presence penalty", width=180, hint_text="0.0")
        self.frequency_penalty = T.field("Frequency penalty", width=180, hint_text="0.3")
        self.seed = T.field("Seed", width=140, hint_text="пусто = случайный")
        self.history_turns = T.field("Глубина истории (пар)", width=200,
                                     keyboard_type=ft.KeyboardType.NUMBER, hint_text="4")

        self.temp_value = T.meta_text("")
        self.top_p_value = T.meta_text("")
        self.conn_result = ft.Column(spacing=T.S2, tight=True)

    # ------------------------------------------------------------------ view
    def build(self) -> ft.Control:
        self._fill_from_settings()
        return T.page_scaffold(
            ft.Column(
                [
                    ft.Row(
                        [ft.Icon(ft.Icons.TUNE, color=T.ACCENT, size=24),
                         ft.Text("Администрирование", size=T.T_TITLE,
                                 weight=ft.FontWeight.W_600, color=T.TEXT)],
                        spacing=T.S3,
                    ),
                    ft.Divider(color=T.BORDER, height=T.S4),
                    self._connection_block(),
                    ft.Container(height=T.S4),
                    self._generation_block(),
                    ft.Container(height=T.S4),
                    self._presets_block(),
                ],
                scroll=ft.ScrollMode.AUTO,
                expand=True,
                spacing=T.S2,
            )
        )

    def _connection_block(self) -> ft.Control:
        return T.surface_card(
            ft.Column(
                [
                    T.section_title("Подключение (LM Studio)"),
                    T.meta_text(
                        "Сервер запускается в LM Studio: Developer → Start Server. "
                        "Путь после /v1 (например, id модели) будет отрезан автоматически."
                    ),
                    ft.Row([self.base_url, self.model, self.timeout], wrap=True, spacing=T.S3),
                    ft.Row([self.api_key], wrap=True, spacing=T.S3),
                    ft.Row(
                        [
                            ft.FilledButton(
                                content="Сохранить настройки",
                                icon=ft.Icons.SAVE,
                                on_click=self.on_save,
                                style=ft.ButtonStyle(bgcolor=T.ACCENT, color=T.ON_ACCENT),
                            ),
                            ft.OutlinedButton(
                                content="Проверить соединение",
                                icon=ft.Icons.NETWORK_CHECK,
                                on_click=self.on_check,
                                style=ft.ButtonStyle(color=T.ACCENT,
                                                     side=ft.BorderSide(1, T.ACCENT)),
                            ),
                        ],
                        spacing=T.S3,
                    ),
                    self.conn_result,
                ],
                spacing=T.S3,
            )
        )

    def _generation_block(self) -> ft.Control:
        self.temperature.on_change = lambda e: self._sync_slider(self.temperature, self.temp_value)
        self.top_p.on_change = lambda e: self._sync_slider(self.top_p, self.top_p_value)
        return T.surface_card(
            ft.Column(
                [
                    T.section_title("Параметры генерации"),
                    self.system_prompt,
                    ft.Row(
                        [
                            ft.Column(
                                [ft.Text("Temperature", size=T.T_SECONDARY, color=T.TEXT),
                                 ft.Row([ft.Container(self.temperature, width=280), self.temp_value])],
                                tight=True, spacing=T.S1,
                            ),
                            ft.Column(
                                [ft.Text("Top-p", size=T.T_SECONDARY, color=T.TEXT),
                                 ft.Row([ft.Container(self.top_p, width=280), self.top_p_value])],
                                tight=True, spacing=T.S1,
                            ),
                        ],
                        wrap=True, spacing=T.S8,
                    ),
                    ft.Row(
                        [self.max_tokens, self.context_window, self.history_turns],
                        wrap=True, spacing=T.S3,
                    ),
                    T.meta_text(
                        "Контекстное окно здесь — лимит для обрезки истории на стороне приложения. "
                        "Реальный предел контекста задаётся в LM Studio при загрузке модели "
                        "(Load Model → Context Length) и не должен быть меньше этого значения."
                    ),
                    ft.Row(
                        [self.presence_penalty, self.frequency_penalty, self.seed],
                        wrap=True, spacing=T.S3,
                    ),
                ],
                spacing=T.S3,
            )
        )

    def _presets_block(self) -> ft.Control:
        return T.surface_card(
            ft.Column(
                [
                    T.section_title("Пресеты для сравнения"),
                    T.meta_text(
                        "«До оптимизации»: temp=1.0, без системного промпта, max_tokens=1024, "
                        "контекст 4096. «После оптимизации»: temp=0.2, структурирующий промпт, "
                        "max_tokens=2048, контекст 16384, seed=42. Загрузка пресета не сохраняет "
                        "настройки — нажмите «Сохранить настройки»."
                    ),
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                content="Загрузить «до оптимизации»",
                                icon=ft.Icons.HISTORY,
                                on_click=lambda e: self._load_preset("before"),
                                style=ft.ButtonStyle(color=T.TEXT, side=ft.BorderSide(1, T.BORDER)),
                            ),
                            ft.OutlinedButton(
                                content="Загрузить «после оптимизации»",
                                icon=ft.Icons.SCIENCE,
                                on_click=lambda e: self._load_preset("after"),
                                style=ft.ButtonStyle(color=T.ACCENT, side=ft.BorderSide(1, T.ACCENT)),
                            ),
                        ],
                        spacing=T.S3, wrap=True,
                    ),
                ],
                spacing=T.S3,
            )
        )

    # ---------------------------------------------------------------- helpers
    def _fill_from_settings(self, settings: dict | None = None) -> None:
        settings = settings or self.store.load()
        conn, gen = settings["connection"], settings["generation"]
        self.base_url.value = conn.get("base_url", "")
        self.model.value = conn.get("model", "")
        self.timeout.value = str(conn.get("timeout", 600))
        self.api_key.value = conn.get("api_key", "")
        self.system_prompt.value = gen.get("system_prompt", "")
        self.temperature.value = float(gen.get("temperature", 0.7))
        self.top_p.value = float(gen.get("top_p", 1.0))
        self.max_tokens.value = str(gen.get("max_tokens", 1024))
        self.context_window.value = str(gen.get("context_window", 8192))
        self.presence_penalty.value = str(gen.get("presence_penalty", 0.0))
        self.frequency_penalty.value = str(gen.get("frequency_penalty", 0.0))
        self.seed.value = "" if gen.get("seed") is None else str(gen.get("seed"))
        self.history_turns.value = str(gen.get("history_turns", 4))
        self._sync_slider(self.temperature, self.temp_value)
        self._sync_slider(self.top_p, self.top_p_value)

    @staticmethod
    def _sync_slider(slider: ft.Slider, label: ft.Text) -> None:
        label.value = f"{slider.value:.2f}"

    def _load_preset(self, name: str) -> None:
        settings = self.store.apply_preset(name)
        self._fill_from_settings(settings)
        self.logger.log("INFO", "preset_loaded", preset=name)
        T.snack(self.page, f"Пресет «{name}» загружен в форму. Сохраните, чтобы применить.")
        self.page.update()

    # ---------------------------------------------------------------- actions
    def on_save(self, e) -> None:
        try:
            settings = self.store.load()
            settings["connection"] = {
                "base_url": normalize_base_url(self.base_url.value),
                "model": (self.model.value or "").strip() or "local-model",
                "timeout": max(10, int(self.timeout.value or 600)),
                "api_key": (self.api_key.value or "").strip(),
            }
            seed_raw = (self.seed.value or "").strip()
            settings["generation"] = {
                "system_prompt": self.system_prompt.value or "",
                "temperature": round(float(self.temperature.value), 2),
                "top_p": round(float(self.top_p.value), 2),
                "max_tokens": max(16, int(self.max_tokens.value or 1024)),
                "context_window": max(1024, int(self.context_window.value or 8192)),
                "presence_penalty": float(self.presence_penalty.value or 0),
                "frequency_penalty": float(self.frequency_penalty.value or 0),
                "seed": int(seed_raw) if seed_raw else None,
                "history_turns": max(0, int(self.history_turns.value or 4)),
            }
            settings["active_preset"] = "custom"
            self.store.save(settings)
        except ValueError:
            T.snack(self.page, "Проверьте числовые поля: таймаут, токены, контекст, штрафы, seed.",
                    is_error=True)
            return
        self.base_url.value = settings["connection"]["base_url"]
        self.logger.log("INFO", "settings_saved",
                        connection=settings["connection"],
                        generation={k: v for k, v in settings["generation"].items()
                                    if k != "system_prompt"},
                        system_prompt_chars=len(settings["generation"]["system_prompt"]))
        T.snack(self.page,
                f"Настройки сохранены: {settings['connection']['base_url']}, "
                f"модель {settings['connection']['model']}. "
                f"Новые запросы в чате используют их.")
        self.page.update()

    async def on_check(self, e) -> None:
        self.conn_result.controls = [
            ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2, color=T.ACCENT),
                    T.meta_text("Запрос к серверу…")], spacing=T.S2)
        ]
        self.page.update()
        client = LMStudioClient(self.base_url.value, (self.model.value or "").strip(),
                                int(self.timeout.value or 600),
                                api_key=self.api_key.value or "")
        info = await asyncio.to_thread(client.check_connection)

        controls: list = []
        if not info["ok"]:
            controls.append(
                ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE, color=T.ERROR, size=18),
                        ft.Text(f"Сервер недоступен: {info['error']}",
                                color=T.ERROR, size=T.T_SECONDARY, selectable=True, expand=True)],
                       spacing=T.S2)
            )
            self.logger.log("ERROR", "connection_check_failed", error=info["error"])
        else:
            controls.append(
                ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE, color=T.SUCCESS, size=18),
                        ft.Text(f"Сервер отвечает. Модели: {', '.join(info['models']) or '—'}",
                                color=T.TEXT, size=T.T_SECONDARY, selectable=True, expand=True)],
                       spacing=T.S2)
            )
            active = info.get("active")
            if active:
                quant = active.get("quantization") or "не сообщается"
                controls.append(
                    T.surface_card(
                        ft.Column(
                            [
                                ft.Text("Загруженная модель", size=T.T_SECONDARY,
                                        weight=ft.FontWeight.W_600, color=T.TEXT),
                                T.meta_text(f"ID: {active.get('key')}"),
                                T.meta_text(f"Формат: {active.get('format') or '—'}"),
                                T.meta_text(f"Квантование: {quant}"),
                                T.meta_text(f"Размер на диске: {_fmt_size(active.get('size_bytes'))}"),
                                T.meta_text(
                                    f"Контекст: загружено {active.get('loaded_context_length') or '—'} "
                                    f"из {active.get('max_context_length') or '—'} макс."
                                ),
                                T.meta_text(
                                    "Квантование — свойство GGUF-файла. Чтобы сменить квант, "
                                    "скачайте в LM Studio другой вариант модели (например, Q4_K_M "
                                    "вместо IQ3_S) и загрузите его."
                                ),
                            ],
                            spacing=T.S1, tight=True,
                        ),
                        padding=T.S3,
                    )
                )
            else:
                controls.append(
                    T.meta_text(
                        "Метаданные модели недоступны (старая версия LM Studio или модель "
                        "не найдена по id). Список /v1/models при этом вернулся корректно."
                    )
                )
            self.logger.log("INFO", "connection_check_ok", models=info["models"],
                            active=info.get("active"))
        self.conn_result.controls = controls
        self.page.update()
