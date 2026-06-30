#!/usr/bin/env python3
"""Сбор кандидатов-политиков: политические теги Gamma -> рынки -> участники,
ранжируем по частоте со-появления (специалисты политики, а не туристы одного выбора)."""
import json
import time
from collections import Counter

import requests
import wallet_analyzer as wa

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
GAMMA = "https://gamma-api.polymarket.com"
api = wa.DataAPIClient()

have = {w.lower() for w in json.load(open("copy_watchlist.json", encoding="utf-8"))["watchlist"]}
have |= {w.lower() for w in json.load(open("sports_watchlist.json", encoding="utf-8"))["watchlist"]}
tags = json.load(open("politics_tags.json", encoding="utf-8"))


def tag_markets(tid, limit=25):
    try:
        r = s.get(f"{GAMMA}/markets", params={"tag_id": tid, "closed": "true", "limit": limit,
                  "order": "volumeNum", "ascending": "false"}, timeout=20)
        d = r.json()
    except requests.RequestException:
        return []
    if isinstance(d, dict):
        d = d.get("data") or d.get("markets") or []
    out = []
    for m in d:
        cid = m.get("conditionId") or m.get("condition_id")
        vol = wa._f(m, "volumeNum", "volume")
        if cid and vol >= 5000:
            out.append(cid)
    return out


def main():
    markets = set()
    for i, tid in enumerate(tags, 1):
        for cid in tag_markets(tid, 25):
            markets.add(cid)
        if i % 30 == 0:
            print(f"  тегов {i}/{len(tags)}, рынков {len(markets)}", flush=True)
        time.sleep(0.04)
    markets = list(markets)
    print(f"политических рынков: {len(markets)}", flush=True)

    freq = Counter()
    for i, cid in enumerate(markets, 1):
        seen = set()
        try:
            for t in api.trades_for_market(cid, max_trades=500):
                a = (t.get("proxyWallet") or "").lower()
                if a and a not in have:
                    seen.add(a)
        except Exception:  # noqa: BLE001
            pass
        for a in seen:
            freq[a] += 1
        if i % 50 == 0:
            print(f"  рынков {i}/{len(markets)}, пул {len(freq)}", flush=True)

    cand = [a for a, c in freq.most_common() if c >= 3][:600]
    with open("wide10.jsonl.candidates.json", "w", encoding="utf-8") as f:
        json.dump(cand, f)
    print(f"участников: {len(freq)} | специалистов (>=3 рынков): {len(cand)} -> wide10", flush=True)


if __name__ == "__main__":
    main()
