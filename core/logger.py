"""JSONL-логгер приложения: запросы, ответы, ошибки, смена настроек.

Файл: data_files/logs.jsonl — одна строка = одно событие.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from .settings import DATA_DIR

LOG_PATH = os.path.join(DATA_DIR, "logs.jsonl")
LEVELS = ("INFO", "WARN", "ERROR")


class JsonlLogger:
    def __init__(self, path: str = LOG_PATH, max_bytes: int = 5_000_000):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def log(self, level: str, event: str, **details) -> None:
        record = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level if level in LEVELS else "INFO",
            "event": event,
            "details": details,
        }
        with self._lock:
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def read(self, limit: int = 300, level: str = "ALL") -> list:
        """Последние limit записей, новые сверху. level: ALL|INFO|WARN|ERROR."""
        records: list = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if level != "ALL" and rec.get("level") != level:
                        continue
                    records.append(rec)
        except OSError:
            pass
        return records[-limit:][::-1]

    def clear(self) -> None:
        with self._lock:
            try:
                os.remove(self.path)
            except OSError:
                pass
