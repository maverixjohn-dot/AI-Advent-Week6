"""Настройки приложения и пресеты «до оптимизации» / «после оптимизации».

Хранение: data_files/settings.json. Все правки — только через SettingsStore,
UI ничего не знает про JSON.
"""

from __future__ import annotations

import copy
import json
import os
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_files")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

# Пресет «до оптимизации»: типичные дефолты, которые стоят у большинства
# после установки LM Studio. Никакого системного промпта, высокая температура.
# max_tokens=1024, а не 512: у reasoning-моделей (qwen3.x) меньший лимит
# целиком съедается рассуждением, и видимый ответ приходит пустым.
PRESET_BEFORE: dict = {
    "system_prompt": "",
    "temperature": 1.0,
    "top_p": 1.0,
    "max_tokens": 1024,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "context_window": 4096,
    "history_turns": 10,
    "seed": None,
}

# Пресет «после оптимизации»: заточен под детерминированные,
# структурированные ответы (типичный кейс: помощник/аналитик по документам).
PRESET_AFTER: dict = {
    "system_prompt": (
        "Ты — технический ассистент. Правила ответа:\n"
        "1. Отвечай на русском, кратко и по существу, без вступлений.\n"
        "2. Если данных не хватает — прямо скажи об этом, не выдумывай.\n"
        "3. Код оформляй в markdown-блоки с указанием языка.\n"
        "4. Формат ответа: сначала суть (1–3 предложения), затем детали списком."
    ),
    "temperature": 0.2,
    "top_p": 0.9,
    "max_tokens": 2048,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.3,
    "context_window": 16384,
    "history_turns": 4,
    "seed": 42,
}

DEFAULT_SETTINGS: dict = {
    "connection": {
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "qwen3.8-27b",
        "timeout": 600,
        "api_key": "",  # нужен, если в LM Studio включён Require API Key
    },
    "generation": copy.deepcopy(PRESET_AFTER),
    "active_preset": "after",  # before | after | custom
}

_PRESETS = {"before": PRESET_BEFORE, "after": PRESET_AFTER}


class SettingsStore:
    """Потокобезопасное чтение/запись settings.json."""

    def __init__(self, path: str = SETTINGS_PATH):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            self.save(DEFAULT_SETTINGS)

    def load(self) -> dict:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = copy.deepcopy(DEFAULT_SETTINGS)
        # дозаполняем отсутствующие ключи дефолтами
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        merged["connection"].update(data.get("connection", {}))
        merged["generation"].update(data.get("generation", {}))
        merged["active_preset"] = data.get("active_preset", "custom")
        return merged

    def save(self, settings: dict) -> None:
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)

    def apply_preset(self, name: str) -> dict:
        """Возвращает настройки с подставленным пресетом (не сохраняет)."""
        settings = self.load()
        if name in _PRESETS:
            settings["generation"] = copy.deepcopy(_PRESETS[name])
            settings["active_preset"] = name
        return settings

    @staticmethod
    def preset(name: str) -> dict:
        return copy.deepcopy(_PRESETS[name])
