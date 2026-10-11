"""Каркас приложения: тёмная тема, верхняя навигация, роутинг.

Маршруты: / — чат, /admin — администрирование, /logs — логи.
"""

from __future__ import annotations

import flet as ft

from core.logger import JsonlLogger
from core.settings import SettingsStore
from ui import theme as T
from ui.admin_page import AdminPage
from ui.chat_page import ChatPage
from ui.logs_page import LogsPage

ROUTES = [("/", "Чат", ft.Icons.CHAT_BUBBLE_OUTLINE),
          ("/admin", "Администрирование", ft.Icons.TUNE),
          ("/logs", "Логи", ft.Icons.ARTICLE)]


def main(page: ft.Page) -> None:
    page.title = "Локальная LLM — оптимизация"
    T.apply_theme(page)

    store = SettingsStore()
    logger = JsonlLogger()
    chat_state: dict = {"messages": []}  # история в памяти процесса

    chat_page = ChatPage(page, store, logger, chat_state)
    admin_page = AdminPage(page, store, logger)
    logs_page = LogsPage(page, logger)

    body = ft.Column(expand=True, spacing=0)
    nav_buttons: list[ft.TextButton] = []

    def render_route() -> None:
        route = page.route or "/"
        if route == "/admin":
            body.controls = [admin_page.build()]
        elif route == "/logs":
            body.controls = [logs_page.build()]
        else:
            body.controls = [chat_page.build()]
        for btn, (path, _, _) in zip(nav_buttons, ROUTES):
            active = (route == path) or (route == "/" and path == "/")
            btn.style = ft.ButtonStyle(
                color=T.ACCENT if active else T.TEXT_SECONDARY,
                text_style=ft.TextStyle(
                    size=T.T_SECONDARY,
                    weight=ft.FontWeight.W_600 if active else ft.FontWeight.W_400,
                ),
            )
        page.update()

    def nav_click(path: str):
        def handler(e):
            if page.route != path:
                page.navigate(path)
            else:
                render_route()  # повторный клик — просто обновить содержимое
        return handler

    for path, label, icon in ROUTES:
        nav_buttons.append(
            ft.TextButton(content=label, icon=icon, on_click=nav_click(path))
        )

    page.appbar = ft.AppBar(
        title=ft.Text("Локальная LLM · оптимизация", size=T.T_SECONDARY,
                      weight=ft.FontWeight.W_600, color=T.TEXT),
        bgcolor=T.BG,
        elevation=0,
        actions=[ft.Row(nav_buttons, spacing=T.S1),
                 ft.Container(width=T.S4)],
    )

    page.add(body)
    page.on_route_change = lambda e: render_route()
    logger.log("INFO", "app_started", route=page.route or "/")
    render_route()
