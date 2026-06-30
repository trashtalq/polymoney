#!/usr/bin/env python3
"""Системный обход категорий Polymarket тем же конвейером, что и политика:
теги Gamma -> рынки категории -> участники (ранжирование по частоте = специалисты) ->
анализ -> копи-фильтр + живость -> добавление в copy_watchlist.json с источником=категория.

Категории идут ОДНА ЗА ОДНОЙ. Прогресс и итоги — в category_scan.log."""
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import requests
import wallet_analyzer as wa

HERE = os.path.dirname(os.path.abspath(__file__))
GAMMA = "https://gamma-api.polymarket.com"
LOG = os.path.join(HERE, "category_scan.log")

CATS = {
    "крипто": ["crypto", "bitcoin", "ethereum", "btc", "eth", "altcoin", "solana", "defi",
               "stablecoin", "binance", "coinbase", "memecoin", "dogecoin", "xrp", "cardano",
               "token", "etf", "blockchain", "nft"],
    "финансы": ["fed", "interest rate", "inflation", "cpi", "recession", "s&p", "nasdaq",
                "stock", "earnings", "gdp", "unemployment", "jobs", "treasury", "rate cut",
                "economy", "ipo", "dow", "bond", "market cap"],
    "технологии": ["openai", "gpt", "claude", "anthropic", "google", "apple", "tesla",
                   "nvidia", "microsoft", "artificial intelligence", " ai ", "spacex",
                   "chip", "meta", "amazon", "tech", "model", "llm"],
    "наука": ["space", "nasa", "rocket", "launch", "climate", "weather", "hurricane",
              "temperature", "earthquake", "covid", "disease", "vaccine", "science", "mars",
              "moon", "asteroid"],
    "культура": ["movie", "box office", "oscar", "grammy", "netflix", "spotify", "album",
                 "celebrity", "award", "rotten", "emmy", "billboard", "person of the year",
                 "tv", "music", "film"],
}

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})


def log(m):
    line = f"[{datetime.now(timezone.utc):%H:%M}Z] {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def all_tags():
    out, off = [], 0
    while off < 3000:
        r = s.get(f"{GAMMA}/tags", params={"limit": 100, "offset": off}, timeout=20)
        d = r.json()
        if not d:
            break
        out += d
        off += 100
        time.sleep(0.04)
    return out


def tag_markets(tid):
    try:
        r = s.get(f"{GAMMA}/markets", params={"tag_id": tid, "closed": "true", "limit": 25,
                  "order": "volumeNum", "ascending": "false"}, timeout=20)
        d = r.json()
    except requests.RequestException:
        return []
    if isinstance(d, dict):
        d = d.get("data") or d.get("markets") or []
    return [m.get("conditionId") or m.get("condition_id") for m in d
            if (m.get("conditionId") or m.get("condition_id")) and wa._f(m, "volumeNum", "volume") >= 5000]


def have_now():
    h = {w.lower() for w in json.load(open("copy_watchlist.json", encoding="utf-8"))["watchlist"]}
    h |= {w.lower() for w in json.load(open("sports_watchlist.json", encoding="utf-8"))["watchlist"]}
    return h


def copy_ok(c):
    n = c.get("n_resolved", 0)
    return (c.get("resolved_pnl", 0) > 0 and c.get("roi_ci_low", 0) > 0
            and c.get("reward_share", 1) <= 0.15 and c.get("top1_concentration", 1) <= 0.60
            and not c.get("truncated") and c.get("total_staked", 0) >= 3000
            and n >= 15 and (c.get("total_staked", 0) / n) >= 20)


def is_live(a):
    try:
        evs = s.get("https://data-api.polymarket.com/activity",
                    params={"user": a, "limit": 1, "sortBy": "TIMESTAMP",
                            "sortDirection": "DESC"}, timeout=12).json() or []
    except requests.RequestException:
        return False
    return bool(evs) and (int(time.time()) - int(evs[0].get("timestamp", 0))) <= 14 * 86400


def main():
    api = wa.DataAPIClient()
    tags = all_tags()
    log(f"тегов всего: {len(tags)}")
    for cat, kws in CATS.items():
        try:
            have = have_now()
            tids = [t.get("id") for t in tags
                    if t.get("id") and any(k in (t.get("label") or t.get("slug") or "").lower() for k in kws)]
            log(f"[{cat}] тегов: {len(tids)}")
            markets = set()
            for tid in tids:
                for cid in tag_markets(tid):
                    markets.add(cid)
                time.sleep(0.03)
            markets = list(markets)[:600]
            log(f"[{cat}] рынков: {len(markets)} — собираю участников…")
            freq = Counter()
            for i, cid in enumerate(markets, 1):
                seen = set()
                try:
                    for t in api.trades_for_market(cid, max_trades=400):
                        a = (t.get("proxyWallet") or "").lower()
                        if a and a not in have:
                            seen.add(a)
                except Exception:  # noqa: BLE001
                    pass
                for a in seen:
                    freq[a] += 1
            cand = [a for a, c in freq.most_common() if c >= 3][:500]
            ckpt = os.path.join(HERE, f"cat_{cat}.jsonl")
            try:
                os.remove(ckpt)
            except OSError:
                pass
            with open(ckpt + ".candidates.json", "w", encoding="utf-8") as f:
                json.dump(cand, f)
            log(f"[{cat}] специалистов: {len(cand)} (пул {len(freq)}) — анализ…")

            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            with open(os.path.join(HERE, f"cat_{cat}_run.log"), "wb") as af:
                subprocess.run([sys.executable, os.path.join(HERE, "wallet_analyzer.py"),
                                "--checkpoint", ckpt, "--out", os.path.join(HERE, f"cat_{cat}_reg.json")],
                               cwd=HERE, env=env, stdout=af, stderr=af, check=False)

            cards = {}
            for line in open(ckpt, encoding="utf-8"):
                try:
                    d = json.loads(line)
                    cards[d["wallet"].lower()] = d
                except Exception:  # noqa: BLE001
                    pass
            have = have_now()
            passed = [c for c in cards.values() if copy_ok(c) and c["wallet"].lower() not in have]
            passed.sort(key=lambda c: c.get("roi_ci_low", 0) * (c.get("n_resolved", 0) ** 0.5), reverse=True)
            live = [c for c in passed if is_live(c["wallet"].lower())]
            log(f"[{cat}] проанализировано {len(cards)} | копи-кандидатов {len(passed)} | живых {len(live)}")

            if live:
                main_wl = json.load(open("copy_watchlist.json", encoding="utf-8"))
                add = [c["wallet"] for c in live]
                main_wl["watchlist"] += add
                main_wl["count"] = len(main_wl["watchlist"])
                json.dump(main_wl, open("copy_watchlist.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                src = json.load(open("wallet_sources.json", encoding="utf-8"))
                for w in add:
                    src[w.lower()] = cat
                json.dump(src, open("wallet_sources.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                top = ", ".join(f"{c['wallet'][:10]}…(roi {c.get('roi',0)*100:.0f}%,ci {c.get('roi_ci_low',0)*100:.0f}%,n{c.get('n_resolved',0)})"
                                for c in live[:5])
                log(f"[{cat}] ДОБАВЛЕНО {len(add)} -> watchlist {main_wl['count']}. Топ: {top}")
            else:
                log(f"[{cat}] новых копируемых живых нет")
        except Exception as e:  # noqa: BLE001
            log(f"[{cat}] ОШИБКА: {e}")
    log("=== ВСЕ КАТЕГОРИИ ПРОЙДЕНЫ ===")


if __name__ == "__main__":
    main()
