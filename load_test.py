"""
Нагрузочный тест приватного LLM-сервиса (приёмочные тесты 2, 3 и soak-тест).

Примеры:
  # тест 2: параллельность — 8 запросов при 4 слотах
  python3 load_test.py --model qwen3.8-27b --token XXX --requests 8 --concurrency 4

  # тест 3: max context — промпт, раздутый до ~20000 токенов
  python3 load_test.py --model qwen3.8-27b --token XXX --requests 1 --concurrency 1 --pad-tokens 20000

  # soak: 100 запросов, 3 параллельных
  python3 load_test.py --model qwen3.8-27b --token XXX --requests 100 --concurrency 3
"""

import argparse
import asyncio
import statistics
import time
from collections import Counter

import httpx

FILLER = ("Сложные технологии меняют подходы к проектированию распределённых систем, "
          "а инженеры оценивают надёжность, задержки, пропускную способность "
          "и стоимость эксплуатации принятых архитектурных решений. ")

BASE_PROMPT = "Кратко объясни, чем очередь сообщений отличается от потоковой обработки."


async def one_request(client: httpx.AsyncClient, model: str, prompt: str,
                      max_tokens: int, results: list):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": False,
    }
    t0 = time.monotonic()
    try:
        r = await client.post("/v1/chat/completions", json=payload)
        lat = time.monotonic() - t0
        if r.status_code == 200:
            usage = r.json().get("usage", {})
            results.append(("ok", lat,
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0)))
        else:
            results.append((f"HTTP {r.status_code}", lat, 0, 0))
    except Exception as e:
        results.append((type(e).__name__, time.monotonic() - t0, 0, 0))


async def run(args):
    prompt = BASE_PROMPT
    if args.pad_tokens:
        repeat = max(1, args.pad_tokens * 3 // len(FILLER))
        prompt = FILLER * repeat + "\n\nСделай вывод одним абзацем."

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    timeout = httpx.Timeout(connect=10.0, read=900.0, write=30.0, pool=30.0)
    limits = httpx.Limits(max_connections=args.concurrency + 2)
    results: list = []

    async with httpx.AsyncClient(base_url=args.url, headers=headers,
                                 timeout=timeout, limits=limits) as client:
        sem = asyncio.Semaphore(args.concurrency)

        async def worker():
            async with sem:
                await one_request(client, args.model, prompt, args.max_tokens, results)

        t0 = time.monotonic()
        await asyncio.gather(*(worker() for _ in range(args.requests)))
        wall = time.monotonic() - t0

    ok = [r for r in results if r[0] == "ok"]
    bad = [r for r in results if r[0] != "ok"]
    lats = sorted(r[1] for r in ok)

    print(f"\n=== {args.requests} запросов, параллельность {args.concurrency}, "
          f"max_tokens={args.max_tokens}, pad={args.pad_tokens} ===")
    print(f"успешно: {len(ok)}/{args.requests} ({100 * len(ok) / args.requests:.1f}%)")
    if bad:
        print("ошибки:", dict(Counter(r[0] for r in bad)))
    if ok:
        p95 = lats[min(len(lats) - 1, int(0.95 * len(lats)))]
        total_completion = sum(r[3] for r in ok)
        speeds = [r[3] / r[1] for r in ok if r[1] > 0 and r[3] > 0]
        print(f"latency p50 / p95 / max: "
              f"{statistics.median(lats):.1f}с / {p95:.1f}с / {lats[-1]:.1f}с")
        print(f"completion-токенов суммарно: {total_completion}, "
              f"агрегированная скорость: {total_completion / wall:.1f} ток/с")
        if speeds:
            print(f"средняя скорость на запрос: {statistics.mean(speeds):.1f} ток/с")
        if ok[0][2]:
            print(f"prompt-токены (последний отчёт сервера): {ok[-1][2]}")
    print(f"общее время теста: {wall:.1f}с")


def main():
    p = argparse.ArgumentParser(description="Нагрузочный тест LLM-сервиса (LM Studio)")
    p.add_argument("--url", default="http://127.0.0.1:1234")
    p.add_argument("--token", default="", help="API-токен LM Studio")
    p.add_argument("--model", required=True, help="id модели из /v1/models")
    p.add_argument("--requests", type=int, default=8)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--pad-tokens", type=int, default=0,
                   help="раздуть промпт примерно до N токенов (тест max context)")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
