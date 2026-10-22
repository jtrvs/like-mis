"""
Нагрузочный тест без внешних зависимостей: K параллельных «пользователей» шлют запросы,
считаем p50/p95/p99 и ошибки при росте нагрузки.
  python scripts/loadtest.py --url http://localhost:8000/api/extract --users 1,10,50,100 --requests 200
"""
import argparse
import json
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SAMPLE = "Пациентка 34 лет. Ds: F41.1 Генерализованное тревожное расстройство. GAD-7 16 баллов. Назначен Авиандр 20 мг 2 р/д. Суицидальные мысли отрицает."


def one(url, body, cookie=""):
    t0 = time.perf_counter()
    try:
        h = {"Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request(url, data=body, headers=h)
        urllib.request.urlopen(req, timeout=30).read()
        ok = True
    except Exception:
        ok = False
    return (time.perf_counter() - t0) * 1000, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000/api/extract")
    ap.add_argument("--users", default="1,10,50,100")
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--text-file", type=Path)
    ap.add_argument("--get", action="store_true", help="GET-запрос страницы (например, карточки пациента)")
    ap.add_argument("--cookie", default="", help="sessionid=... для страниц под входом")
    a = ap.parse_args()
    text = a.text_file.read_text(encoding="utf-8") if a.text_file else SAMPLE
    body = None if a.get else json.dumps({"text": text}).encode()
    rows = []
    for u in [int(x) for x in a.users.split(",")]:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=u) as ex:
            res = list(ex.map(lambda _: one(a.url, body, a.cookie), range(a.requests)))
        wall = time.perf_counter() - t0
        lat = sorted(r[0] for r in res)
        q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))]
        rows.append({"users": u, "rps": round(a.requests / wall, 1), "p50_ms": round(statistics.median(lat), 1),
                     "p95_ms": round(q(0.95), 1), "p99_ms": round(q(0.99), 1), "errors": sum(not r[1] for r in res)})
        print(rows[-1])
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
