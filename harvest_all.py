#!/usr/bin/env python3
"""Булк-харвест: собрать ВСЕХ выживших копи-кандидатов из уже проанализированных
чекпоинтов и добавить живых новых в copy_watchlist.json. Анализ уже оплачен — берём всех."""
import glob
import json
import time
from datetime import datetime, timezone

import requests

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})


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
    cards = {}
    for fn in glob.glob("*.jsonl") + glob.glob("cat_*.jsonl"):
        if "candidates" in fn:
            continue
        for line in open(fn, encoding="utf-8"):
            try:
                d = json.loads(line)
                if "wallet" in d and "n_resolved" in d:
                    w = d["wallet"].lower()
                    # держим лучшую запись по composite/ci_low (свежие переоценки могли улучшить)
                    if w not in cards or d.get("roi_ci_low", 0) > cards[w].get("roi_ci_low", 0):
                        cards[w] = d
            except Exception:  # noqa: BLE001
                pass

    main_wl = json.load(open("copy_watchlist.json", encoding="utf-8"))
    have = {w.lower() for w in main_wl["watchlist"]}
    have |= {w.lower() for w in json.load(open("sports_watchlist.json", encoding="utf-8"))["watchlist"]}

    cand = [c for c in cards.values() if copy_ok(c) and c["wallet"].lower() not in have]
    cand.sort(key=lambda c: c.get("roi_ci_low", 0) * (c.get("n_resolved", 0) ** 0.5), reverse=True)
    print(f"проанализировано уникальных: {len(cards)} | копи-кандидатов вне списка: {len(cand)}")

    live = []
    for c in cand:
        if is_live(c["wallet"].lower()):
            live.append(c)
        time.sleep(0.05)
    print(f"живых новых: {len(live)}")

    if live:
        add = [c["wallet"] for c in live]
        main_wl["watchlist"] += add
        main_wl["count"] = len(main_wl["watchlist"])
        json.dump(main_wl, open("copy_watchlist.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        src = json.load(open("wallet_sources.json", encoding="utf-8"))
        for w in add:
            src.setdefault(w.lower(), "харвест")
        json.dump(src, open("wallet_sources.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    stamp = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z]"
    msg = f"{stamp} харвест: добавлено {len(live)} -> watchlist {main_wl['count']}"
    print(msg)
    with open("harvest_all.log", "a", encoding="utf-8") as f:
        f.write(msg + "\n")


if __name__ == "__main__":
    main()
