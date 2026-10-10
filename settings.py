# -*- coding: utf-8 -*-
"""Персистентные настройки приложения (settings.json в корне проекта).

Хранятся две группы настроек:

1) gate_sim — порог релевантности детерминированного gate (слой 1 режима
   «не знаю»): если топ-скор retrieval ниже порога, LLM не вызывается,
   ассистент отвечает «не знаю» и просит уточнить вопрос.
   Значение по умолчанию 0.78 выбрано по калибровке на корпусе (см. отчёт,
   раздел 10.2).

2) llm — профили подключения к LLM и активный профиль:
     cloud — облачный DeepSeek API; ключ НЕ хранится здесь, берётся из
             переменной окружения DEEPSEEK_API_KEY (см. llm.py).
     local — локальная модель через OpenAI-совместимый сервер LM Studio
             (http://<LAN-IP>:1234/v1); токен LM Studio не является
             секретом уровня облачного ключа и хранится здесь же.
   Переключение профиля и параметры редактируются на странице
   «Администрирование»; действуют на страницы «Чат», «Агент» и chat_cli.py.
"""
import json
import logging
import os

log = logging.getLogger("rag1c")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(ROOT, "settings.json")

DEFAULTS = {
    "gate_sim": 0.78,
    "llm": {
        "active": "cloud",
        "profiles": {
            "cloud": {
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-pro",
                "timeout": 120,
                "api_key": "",          # пусто => брать из DEEPSEEK_API_KEY
            },
            "local": {
                "base_url": "http://192.168.1.50:1234/v1",
                "model": "local-model",  # точный id — из GET /v1/models
                "timeout": 600,          # локальная генерация медленнее API
                "api_key": "",           # токен LM Studio, если включён
            },
        },
    },
}

# допустимые границы порога (косинусная близость e5-large на этом корпусе)
GATE_MIN, GATE_MAX = 0.50, 0.95

# допустимые границы таймаута одного вызова LLM, секунд
TIMEOUT_MIN, TIMEOUT_MAX = 10, 1800

_PROFILE_FIELDS = ("base_url", "model", "timeout", "api_key")


def _merge_llm(data: dict) -> dict:
    """Валидированное слияние секции llm из файла с DEFAULTS.

    Битые/неполные значения заменяются умолчаниями по полю; неизвестные
    профили сохраняются, если у них валидная структура.
    """
    out = {"active": DEFAULTS["llm"]["active"],
           "profiles": {k: dict(v) for k, v in DEFAULTS["llm"]["profiles"].items()}}
    llm = data.get("llm")
    if not isinstance(llm, dict):
        return out
    profs = llm.get("profiles")
    if isinstance(profs, dict):
        for name, p in profs.items():
            if not (isinstance(name, str) and isinstance(p, dict)):
                continue
            if not isinstance(p.get("base_url"), str):
                continue
            merged = dict(out["profiles"].get(name) or
                          {"base_url": "", "model": "", "timeout": 120,
                           "api_key": ""})
            for f in ("base_url", "model", "api_key"):
                if isinstance(p.get(f), str):
                    merged[f] = p[f]
            if isinstance(p.get("timeout"), (int, float)):
                merged["timeout"] = int(p["timeout"])
            out["profiles"][name] = merged
    act = llm.get("active")
    if isinstance(act, str) and act in out["profiles"]:
        out["active"] = act
    return out


def load() -> dict:
    """Настройки из settings.json; при отсутствии/битом файле — DEFAULTS."""
    if not os.path.exists(SETTINGS_PATH):
        return {"gate_sim": DEFAULTS["gate_sim"],
                "llm": _merge_llm({})}
    try:
        data = json.load(open(SETTINGS_PATH, encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("settings.json: ожидается JSON-объект")
        out = {"llm": _merge_llm(data)}
        if isinstance(data.get("gate_sim"), (int, float)):
            out["gate_sim"] = float(data["gate_sim"])
        else:
            out["gate_sim"] = DEFAULTS["gate_sim"]
        return out
    except (OSError, ValueError) as e:
        log.warning("settings: не удалось прочитать %s: %s — используются "
                    "значения по умолчанию", SETTINGS_PATH, e)
        return {"gate_sim": DEFAULTS["gate_sim"], "llm": _merge_llm({})}


def _save(data: dict) -> None:
    """Атомарная запись settings.json (tmp-файл + os.replace)."""
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def get_gate_sim() -> float:
    return load()["gate_sim"]


def set_gate_sim(value: float) -> float:
    """Валидация и сохранение порога. Возвращает сохранённое значение."""
    v = float(value)
    if not (GATE_MIN <= v <= GATE_MAX):
        raise ValueError(
            f"порог релевантности должен быть в диапазоне "
            f"{GATE_MIN:.2f}–{GATE_MAX:.2f}, получено {value!r}")
    data = load()
    data["gate_sim"] = round(v, 4)
    _save(data)
    log.info("settings: gate_sim = %.4f сохранён в %s", data["gate_sim"],
             SETTINGS_PATH)
    return data["gate_sim"]


# ------------------------------------------------------------------ LLM ---

def get_llm() -> dict:
    """Секция llm: {"active": имя, "profiles": {имя: профиль}}."""
    return load()["llm"]


def _validate_profile(name: str, p: dict) -> dict:
    """Проверка и нормализация одного профиля. ValueError при ошибке."""
    base = str(p.get("base_url", "")).strip()
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"профиль «{name}»: base_url должен начинаться с "
                         f"http:// или https://, получено {base!r}")
    model = str(p.get("model", "")).strip()
    if not model:
        raise ValueError(f"профиль «{name}»: не задан id модели "
                         f"(для local — точный id из GET /v1/models)")
    try:
        timeout = int(p.get("timeout", 0))
    except (TypeError, ValueError):
        raise ValueError(f"профиль «{name}»: таймаут должен быть числом "
                         f"секунд, получено {p.get('timeout')!r}")
    if not (TIMEOUT_MIN <= timeout <= TIMEOUT_MAX):
        raise ValueError(f"профиль «{name}»: таймаут вне диапазона "
                         f"{TIMEOUT_MIN}–{TIMEOUT_MAX} с: {timeout}")
    key = str(p.get("api_key", "")).strip()
    return {"base_url": base, "model": model, "timeout": timeout,
            "api_key": key}


def set_llm(active: str, profiles: dict | None = None) -> dict:
    """Валидация и сохранение профилей LLM. Возвращает сохранённую секцию.

    active — имя активного профиля; обязано присутствовать в profiles.
    profiles — {имя: {base_url, model, timeout, api_key}}; пустой или
    None — только переключение активного профиля без правки значений.
    Не переданные сюда профили из файла сохраняются как есть.
    """
    if profiles and not isinstance(profiles, dict):
        raise ValueError("profiles: ожидается словарь профилей")
    data = load()
    merged = data["llm"]["profiles"]
    for name, p in (profiles or {}).items():
        if not isinstance(p, dict):
            raise ValueError(f"профиль «{name}»: ожидается объект настроек")
        merged[name] = _validate_profile(name, p)
    if active not in merged:
        raise ValueError(f"активный профиль {active!r} отсутствует среди "
                         f"сохранённых: {sorted(merged)}")
    data["llm"] = {"active": active, "profiles": merged}
    _save(data)
    log.info("settings: llm.active = %s сохранено в %s", active, SETTINGS_PATH)
    return data["llm"]
