"""Страница логов: JSONL-журнал запросов, ошибок и смены настроек."""

from __future__ import annotations

import json

import flet as ft

from core.logger import JsonlLogger
from ui import theme as T

_LEVEL_COLORS = {"INFO": T.TEXT_SECONDARY, "WARN": T.WARNING, "ERROR": T.ERROR}


class LogsPage:
    def __init__(self, page: ft.Page, logger: JsonlLogger):
        self.page = page
        self.logger = logger
        self.level_filter = ft.Dropdown(
            value="ALL",
            options=[
                ft.DropdownOption(key="ALL", text="Все уровни"),
                ft.DropdownOption(key="INFO", text="INFO"),
                ft.DropdownOption(key="WARN", text="WARN"),
                ft.DropdownOption(key="ERROR", text="ERROR"),
            ],
            width=180,
            filled=True,
            fill_color=T.SURFACE,
            bgcolor=T.SURFACE,
            color=T.TEXT,
            border_color=T.BORDER,
            border_radius=T.RADIUS,
            text_size=T.T_SECONDARY,
            on_select=lambda e: self.refresh(),
        )
        self.list_view = ft.ListView(expand=True, spacing=T.S2)
        self.count_text = T.meta_text("")

    def build(self) -> ft.Control:
        view = T.page_scaffold(
            ft.Column(
                [
                    ft.Row(
                        [ft.Icon(ft.Icons.ARTICLE, color=T.ACCENT, size=24),
                         ft.Text("Логи", size=T.T_TITLE, weight=ft.FontWeight.W_600, color=T.TEXT),
                         ft.Container(expand=True),
                         self.level_filter,
                         ft.IconButton(icon=ft.Icons.REFRESH, tooltip="Обновить",
                                       icon_color=T.TEXT_SECONDARY,
                                       on_click=lambda e: self.refresh()),
                         ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Очистить лог",
                                       icon_color=T.ERROR, on_click=self.on_clear)],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.count_text,
                    ft.Divider(color=T.BORDER, height=T.S4),
                    ft.Container(self.list_view, expand=True),
                ],
                expand=True,
                spacing=T.S2,
            )
        )
        self.refresh()
        return view

    def refresh(self) -> None:
        records = self.logger.read(limit=300, level=self.level_filter.value or "ALL")
        self.count_text.value = f"Записей: {len(records)} (последние 300, новые сверху)"
        self.list_view.controls.clear()
        if not records:
            self.list_view.controls.append(
                ft.Container(
                    content=T.meta_text("Лог пуст. Запросы из чата, ошибки и сохранения "
                                        "настроек появятся здесь автоматически."),
                    padding=ft.Padding.only(top=T.S6),
                    alignment=ft.Alignment.CENTER,
                )
            )
        else:
            for rec in records:
                self.list_view.controls.append(self._render_record(rec))
        self.page.update()

    def _render_record(self, rec: dict) -> ft.Control:
        level = rec.get("level", "INFO")
        details = rec.get("details") or {}
        details_text = json.dumps(details, ensure_ascii=False)
        if len(details_text) > 600:
            details_text = details_text[:600] + "…"
        return T.surface_card(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(rec.get("ts", ""), size=T.T_META, color=T.TEXT_SECONDARY),
                            ft.Container(
                                content=ft.Text(level, size=T.T_META, weight=ft.FontWeight.W_600,
                                                color=_LEVEL_COLORS.get(level, T.TEXT_SECONDARY)),
                                border=T.border(color=_LEVEL_COLORS.get(level, T.BORDER)),
                                border_radius=ft.BorderRadius.all(999),
                                padding=ft.Padding.symmetric(horizontal=T.S2, vertical=1),
                            ),
                            ft.Text(rec.get("event", ""), size=T.T_SECONDARY,
                                    weight=ft.FontWeight.W_600, color=T.TEXT),
                        ],
                        spacing=T.S3,
                    ),
                    ft.Text(details_text if details_text != "{}" else "—",
                            size=T.T_META, color=T.TEXT_SECONDARY, selectable=True),
                ],
                spacing=T.S1, tight=True,
            ),
            padding=T.S3,
        )

    def on_clear(self, e) -> None:
        self.logger.clear()
        self.logger.log("INFO", "logs_cleared")
        self.refresh()
