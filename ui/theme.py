"""Тёмная тема. Токены — по стандарту ui-2026-minimal:
нейтральная шкала + ОДИН акцент, сетка 8px, радиус 8, границы вместо теней.
"""

import flet as ft

# --- палитра (8 ступеней нейтрали + 1 акцент + семантика) -------------------
BG = "#0F1115"          # фон страницы
SURFACE = "#161A21"     # карточки, поля ввода
SURFACE_2 = "#1C212B"   # приподнятые элементы (пузырь пользователя, ховер)
BORDER = "#262C36"      # 1px границы вместо теней
TEXT = "#E6E8EB"        # основной текст (контраст к BG > 13:1)
TEXT_SECONDARY = "#9AA3AF"  # мета-текст (контраст к BG ≈ 7:1, ≥ AA)
ACCENT = "#4F8CFF"      # единственный акцент: primary-кнопки, активные состояния
ON_ACCENT = "#FFFFFF"
ERROR = "#F26D6D"
SUCCESS = "#4CC38A"
WARNING = "#E5A54B"

# --- типографика (12/14/16/20/28) -------------------------------------------
T_META = 12
T_SECONDARY = 14
T_BODY = 16
T_SUBTITLE = 20
T_TITLE = 28

# --- сетка 8px ---------------------------------------------------------------
S1, S2, S3, S4, S6, S8 = 4, 8, 12, 16, 24, 32

RADIUS = 8
CONTENT_WIDTH = 960  # единая колонка контента


def border(width: int = 1, color: str = BORDER) -> ft.Border:
    return ft.Border.all(width, color)


def apply_theme(page: ft.Page) -> None:
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG
    page.padding = 0
    page.spacing = 0
    page.fonts = {}
    page.theme = ft.Theme(
        color_scheme_seed=ACCENT,
        color_scheme=ft.ColorScheme(
            primary=ACCENT,
            on_primary=ON_ACCENT,
            surface=SURFACE,
            on_surface=TEXT,
            on_surface_variant=TEXT_SECONDARY,
            outline=BORDER,
            error=ERROR,
            on_error=ON_ACCENT,
        ),
        font_family="Segoe UI",
        use_material3=True,
    )


def page_scaffold(content: ft.Control, max_width: int = CONTENT_WIDTH) -> ft.Container:
    """Единая центрированная колонка с отступами по сетке."""
    return ft.Container(
        content=ft.Row(
            [ft.Column([content], width=max_width, tight=True)],
            alignment=ft.MainAxisAlignment.CENTER,
        ),
        padding=ft.Padding.only(left=S4, top=S6, right=S4, bottom=S6),
        expand=True,
    )


def section_title(text: str) -> ft.Text:
    return ft.Text(text, size=T_SUBTITLE, weight=ft.FontWeight.W_600, color=TEXT)


def meta_text(text: str, color: str = TEXT_SECONDARY) -> ft.Text:
    return ft.Text(text, size=T_META, color=color, selectable=True)


def field(label: str, **kwargs) -> ft.TextField:
    """Единый стиль полей ввода: label снаружи, тёмная заливка, радиус 8."""
    defaults = dict(
        label=label,
        filled=True,
        fill_color=SURFACE,
        bgcolor=SURFACE,
        color=TEXT,
        border_color=BORDER,
        focused_border_color=ACCENT,
        border_radius=RADIUS,
        text_size=T_SECONDARY,
        label_style=ft.TextStyle(color=TEXT_SECONDARY, size=T_SECONDARY),
        content_padding=ft.Padding.symmetric(horizontal=S3, vertical=S3),
    )
    defaults.update(kwargs)
    return ft.TextField(**defaults)


def surface_card(content: ft.Control, padding: int = S4) -> ft.Container:
    return ft.Container(
        content=content,
        bgcolor=SURFACE,
        border=border(),
        border_radius=ft.BorderRadius.all(RADIUS),
        padding=ft.Padding.all(padding),
    )


def snack(page: ft.Page, text: str, is_error: bool = False) -> None:
    page.show_dialog(
        ft.SnackBar(
            content=ft.Text(text, color=ON_ACCENT if not is_error else "#FFFFFF"),
            bgcolor=ERROR if is_error else SURFACE_2,
            behavior=ft.SnackBarBehavior.FLOATING,
            duration=3000,
        )
    )
