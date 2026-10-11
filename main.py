"""Точка входа.

Веб-режим, только localhost:
    python main.py  ->  http://127.0.0.1:8550

Страницы: / — чат, /admin — администрирование, /logs — логи.
Предварительно: в LM Studio запустите сервер (Developer → Start Server),
по умолчанию он слушает http://127.0.0.1:1234.
"""

import flet as ft

from ui.app import main

if __name__ == "__main__":
    ft.run(
        main,
        host="127.0.0.1",   # только локальный доступ, без expose в сеть
        port=8550,
        view=ft.AppView.WEB_BROWSER,
    )
