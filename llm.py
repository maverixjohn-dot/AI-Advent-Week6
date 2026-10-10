# -*- coding: utf-8 -*-
"""Клиент LLM поверх OpenAI-совместимого API с профилями подключения.

Профили (cloud/local), активный профиль, base_url, модель и таймаут
хранятся в settings.json и редактируются на странице «Администрирование»
(см. settings.py). Профиль читается на каждый вызов — переключение в
админке действует сразу, без перезапуска приложения.

Ключи:
  cloud (DeepSeek) — ТОЛЬКО из переменной окружения DEEPSEEK_API_KEY;
  local (LM Studio) — из settings.json; если авторизация выключена,
  подставляется заглушка (сервер её игнорирует).

Документация API: https://api-docs.deepseek.com/ (cloud),
LM Studio — OpenAI-совместимый endpoint /v1 (local).
"""
import logging
import os
import time

from openai import APIError, APITimeoutError, OpenAI, RateLimitError

import settings

MAX_RETRIES = 2        # повторы при 429/5xx/таймауте

log = logging.getLogger("rag1c")

_client = None
_client_key = None     # (base_url, api_key, timeout) открытого клиента


def active_profile() -> dict:
    """Активный профиль из settings.json + поле name."""
    return profile_named(settings.get_llm()["active"])


def profile_named(name: str) -> dict:
    """Профиль по имени из settings.json + поле name."""
    cfg = settings.get_llm()
    if name not in cfg["profiles"]:
        raise ValueError(f"неизвестный профиль LLM {name!r}; "
                         f"доступны: {sorted(cfg['profiles'])}")
    p = dict(cfg["profiles"][name])
    p["name"] = name
    return p


def _api_key_for(profile: dict) -> str:
    """cloud — ключ из окружения; local — из settings или заглушка."""
    if profile.get("name") == "cloud" or "deepseek" in profile["base_url"]:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "Не задана переменная окружения DEEPSEEK_API_KEY. "
                "Windows (постоянно): setx DEEPSEEK_API_KEY \"sk-...\" и "
                "открыть новую консоль; текущая сессия PowerShell: "
                "$env:DEEPSEEK_API_KEY=\"sk-...\".")
        return key
    return profile.get("api_key") or "local-no-auth"


def _get_client(profile: dict) -> OpenAI:
    """Клиент под профиль; пересоздаётся при смене параметров."""
    global _client, _client_key
    key = (profile["base_url"], _api_key_for(profile), profile["timeout"])
    if _client is None or _client_key != key:
        _client = OpenAI(api_key=key[1], base_url=key[0], timeout=key[2])
        _client_key = key
    return _client


def chat(messages: list[dict], *, model: str | None = None,
         temperature: float = 0.0, max_tokens: int = 2048,
         response_format: dict | None = None,
         profile: str | None = None) -> dict:
    """Один вызов chat/completions через профиль (non-stream).

    profile — имя профиля из settings.json ("cloud"/"local"); None —
    активный профиль. Позволяет одному процессу вызывать обе модели
    (двойной ответ в чате, сравнительные прогоны).

    response_format — например {"type": "json_object"} для JSON-ответа.
    Возвращает dict(text, model, profile, usage{prompt,completion,total},
    latency_s).
    """
    profile = profile_named(profile) if profile else active_profile()
    model = model or profile["model"]
    # выключатель thinking — специфика DeepSeek; прочим серверам не шлём
    extra = {"thinking": {"type": "disabled"}} if profile["name"] == "cloud" \
        else None
    # llama.cpp/LM Studio принимают только json_schema|text; json_object
    # там даёт 400. Парсер grounded извлекает JSON из текста, поэтому для
    # локального профиля enforcement просто снимаем (инструкция JSON
    # остаётся в системном промпте)
    rf = response_format
    if rf is not None and profile["name"] != "cloud" \
            and rf.get("type") == "json_object":
        log.warning("llm[%s]: response_format=json_object не поддержан "
                    "сервером — снят (JSON обеспечивается промптом)",
                    profile["name"])
        rf = None
    t0 = time.monotonic()
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            kwargs = {"extra_body": extra} if extra is not None else {}
            if rf is not None:
                kwargs["response_format"] = rf
            resp = _get_client(profile).chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                **kwargs,
            )
            dt = time.monotonic() - t0
            u = resp.usage
            usage = {
                "prompt": getattr(u, "prompt_tokens", 0),
                "completion": getattr(u, "completion_tokens", 0),
                "total": getattr(u, "total_tokens", 0),
            }
            log.info("llm[%s] %s: tokens in=%s out=%s, %.1fs",
                     profile["name"], model, usage["prompt"],
                     usage["completion"], dt)
            return {"text": (resp.choices[0].message.content or "").strip(),
                    "model": model, "profile": profile["name"],
                    "usage": usage, "latency_s": round(dt, 2)}
        except (RateLimitError, APITimeoutError, APIError) as e:
            last_err = e
            log.warning("llm[%s] попытка %d не удалась: %s",
                        profile["name"], attempt + 1, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(
        f"LLM ({profile['name']}): запрос не удался после "
        f"{MAX_RETRIES + 1} попыток: {last_err}")


def ping(profile: dict) -> dict:
    """Проверка соединения с сервером профиля: GET /v1/models, таймаут 10 с.

    profile — словарь {name, base_url, model, timeout, api_key}
    (можно передать несохранённые значения из формы админки).
    Возвращает dict(models=[id...], latency_s); ошибка — исключение.
    """
    client = OpenAI(api_key=_api_key_for(profile),
                    base_url=profile["base_url"], timeout=10)
    t0 = time.monotonic()
    try:
        page = client.models.list()
    except APIError as e:
        raise RuntimeError(
            f"{type(e).__name__}: {e} — проверьте Base URL (должен "
            f"оканчиваться на /v1, без id модели в конце) и API-ключ") from e
    ids = [m.id for m in (page.data or [])]
    if not ids:
        raise RuntimeError(
            "сервер ответил, но список моделей пуст — проверьте Base URL: "
            "он должен оканчиваться на /v1 (без id модели в конце)")
    return {"models": ids, "latency_s": round(time.monotonic() - t0, 2)}
