"""Клиент LM Studio (OpenAI-совместимый API + REST API v1 для метаданных).

Только HTTP-вызовы и замеры. Логирование — снаружи (ui/core вызывают logger).

Замеры производительности:
- latency_s  — полное время ответа (стена);
- tok_s      — completion_tokens / latency_s (оценка скорости генерации);
- prompt_eval и память процесса LM Studio из клиента недоступны —
  их смотрят в интерфейсе LM Studio / диспетчере задач.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import requests


class LLMError(Exception):
    """Ошибка обращения к локальной LLM."""


def normalize_base_url(url: str) -> str:
    """Приводит base_url к виду http://host:port/v1.

    Защита от типичной ошибки: вставки вроде
    http://host:1234/v1/qwen3.8-27b или http://host:1234 — и то, и другое
    приводится к корректному http://host:1234/v1.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return "http://127.0.0.1:1234/v1"
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or ""
    if "/v1" in path:
        path = path[: path.index("/v1") + 3]
    else:
        path = "/v1"
    return origin + path


class LMStudioClient:
    def __init__(self, base_url: str, model: str, timeout: int = 600, api_key: str = ""):
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.timeout = timeout
        self.api_key = (api_key or "").strip()

    @property
    def origin(self) -> str:
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def headers(self) -> dict:
        """LM Studio с включённым 'Require API Key' принимает Bearer-ключ."""
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @staticmethod
    def _http_error(resp: requests.Response) -> "LLMError":
        if resp.status_code == 401:
            return LLMError(
                "401 Unauthorized: в LM Studio включён 'Require API Key' "
                "(Developer → Server Settings). Введите ключ в поле «API-ключ» "
                "на странице администрирования или отключите опцию в LM Studio."
            )
        if resp.status_code == 404:
            return LLMError(
                "404 Not Found: проверьте Base URL (должен оканчиваться на /v1) "
                "и id модели."
            )
        return LLMError(f"HTTP {resp.status_code}: {resp.text[:400]}")

    # ------------------------------------------------------------------ chat
    def chat(self, messages: list, gen: dict) -> dict:
        """Один запрос к /v1/chat/completions с замером времени и tok/s.

        gen: system_prompt уже учтён в messages снаружи; здесь только
        sampling-параметры. Возвращает dict с content, usage, latency_s, tok_s.
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": float(gen.get("temperature", 0.7)),
            "top_p": float(gen.get("top_p", 1.0)),
            "stream": False,
        }
        max_tokens = gen.get("max_tokens")
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        for key in ("presence_penalty", "frequency_penalty"):
            val = gen.get(key)
            if val:
                payload[key] = float(val)
        if gen.get("seed") is not None:
            payload["seed"] = int(gen["seed"])

        t0 = time.perf_counter()
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self.headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LLMError(
                f"Нет соединения с {self.base_url}. "
                f"Проверьте, что в LM Studio запущен сервер (Developer → Start Server). "
                f"Детали: {exc}"
            ) from exc
        latency = time.perf_counter() - t0

        if resp.status_code != 200:
            raise self._http_error(resp)

        data = resp.json()
        try:
            message = data["choices"][0]["message"]
            content = message.get("content") or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Неожиданный формат ответа: {str(data)[:400]}") from exc

        # Reasoning-модели (qwen3.x): рассуждение приходит в reasoning_content
        # либо вшито в content тегами <think>...</think>. Если модель уперлась
        # в max_tokens внутри рассуждения — видимый content пуст.
        reasoning = message.get("reasoning_content") or ""
        if "<think>" in content:
            m = re.search(r"<think>(.*?)</think>", content, re.S)
            if m:
                reasoning = (reasoning + "\n" + m.group(1)).strip()
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
            else:
                # незакрытый <think> — рассуждение съело весь лимит токенов
                reasoning = (reasoning + "\n" + content.split("<think>", 1)[1]).strip()
                content = ""

        usage = data.get("usage") or {}
        completion_tokens = usage.get("completion_tokens") or 0
        tok_s = round(completion_tokens / latency, 1) if latency > 0 and completion_tokens else None
        return {
            "content": content,
            "reasoning": reasoning,
            "model": data.get("model", self.model),
            "usage": usage,
            "latency_s": round(latency, 2),
            "tok_s": tok_s,
        }

    # --------------------------------------------------------------- metadata
    def list_models_openai(self) -> list:
        """GET /v1/models — список загруженных моделей (OpenAI-совместимо)."""
        resp = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=15)
        if resp.status_code != 200:
            raise self._http_error(resp)
        return resp.json().get("data", [])

    def server_models(self) -> list:
        """GET /api/v1/models — REST API LM Studio: формат, квантование, размер.

        Недоступен в старых версиях LM Studio — тогда возвращаем [].
        """
        try:
            resp = requests.get(f"{self.origin}/api/v1/models", headers=self.headers, timeout=15)
            if resp.status_code != 200:
                return []
            data = resp.json()
            if isinstance(data, dict):
                return data.get("models") or data.get("data") or []
            return data if isinstance(data, list) else []
        except requests.RequestException:
            return []

    def check_connection(self) -> dict:
        """Сводка для страницы администрирования.

        Возвращает: ok, ошибку (если есть), список моделей, и по активной
        модели — формат/квантование/размер/контекст, если LM Studio их отдаёт.
        """
        info: dict = {"ok": False, "error": None, "models": [], "active": None}
        try:
            models = self.list_models_openai()
            info["ok"] = True
            info["models"] = [m.get("id", "?") for m in models]
        except Exception as exc:
            info["error"] = str(exc)
            return info

        for m in self.server_models():
            # поля REST API v1: key / id, type, quantization, size_bytes и пр.
            key = m.get("key") or m.get("id") or ""
            if key and (key == self.model or self.model in key or key in self.model):
                info["active"] = {
                    "key": key,
                    "format": m.get("format"),
                    "quantization": (m.get("quantization") or {}).get("name")
                    if isinstance(m.get("quantization"), dict)
                    else m.get("quantization"),
                    "size_bytes": m.get("size_bytes"),
                    "max_context_length": m.get("max_context_length"),
                    "loaded_context_length": m.get("loaded_context_length"),
                    "state": m.get("state"),
                }
                break
        return info


def estimate_tokens(text: str) -> int:
    """Грубая оценка: ~4 символа на токен (для русского — ближе к 3)."""
    return max(1, len(text) // 3)


def trim_history(messages: list, context_window: int, max_tokens: int, history_turns: int) -> list:
    """Обрезка истории под контекстное окно.

    Оставляет system + последние history_turns пар сообщений; затем
    выбрасывает самые старые пары, пока оценка токенов не влезет в
    (context_window - max_tokens - запас 256).
    """
    system = [m for m in messages if m.get("role") == "system"][:1]
    dialog = [m for m in messages if m.get("role") != "system"]
    dialog = dialog[-(history_turns * 2):]

    budget = max(512, context_window - max_tokens - 256)

    def total(msgs: list) -> int:
        return sum(estimate_tokens(m.get("content", "")) for m in msgs)

    while dialog and total(system + dialog) > budget:
        dialog = dialog[2:]  # выбрасываем старейшую пару вопрос-ответ
    return system + dialog
