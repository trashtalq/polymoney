#!/usr/bin/env python3
"""Нишевый сбор кандидатов: теги Gamma -> средне-объёмные рынки ниш -> их трейдеры.
Идея: специалисты узких тем не видны в топ-объёме и лидерборде. Берём рынки внутри
тематических тегов, пропуская самый мега-рынок тега (там киты), и собираем участников."""
import json
import time

import requests
import wallet_analyzer as wa

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
GAMMA = "https://gamma-api.polymarket.com"
api = wa.DataAPIClient()

have = {w.lower() for w in json.load(open("copy_watchlist.json", encoding="utf-8"))["watchlist"]}


def tags(n=300):
    out, off = [], 0
    while len(out) < n:
        r = s.get(f"{GAMMA}/tags", params={"limit": 100, "offset": off}, timeout=20)
        d = r.json()
        if not d:
            break
        out += d
        off += 100
        time.sleep(0.1)
    return out


def tag_markets(tid, limit=30):
    r = s.get(f"{GAMMA}/markets",
              params={"tag_id": tid, "closed": "true", "limit": limit,
                      "order": "volumeNum", "ascending": "false"}, timeout=20)
    d = r.json()
    if isinstance(d, dict):
        d = d.get("data") or d.get("markets") or []
    rows = []
    for m in d:
        cid = m.get("conditionId") or m.get("condition_id")
        if cid:
            rows.append((wa._f(m, "volumeNum", "volume"), cid))
    return rows


def main():
    tg = tags(300)
    print(f"тегов получено: {len(tg)}", flush=True)
    # рынки ниш: пропускаем топ-1 мега-рынок тега, берём средний хвост (ранги 1..12)
    markets = {}
    for i, t in enumerate(tg, 1):
        tid = t.get("id")
        if not tid:
            continue
        try:
            rows = tag_markets(tid, 30)
        except requests.RequestException:
            continue
        for vol, cid in rows[1:13]:               # пропуск мега-рынка, нишевый хвост
            if 2000 <= vol <= 3_000_000:          # средне-объёмные: не пыль и не мега
                markets[cid] = max(markets.get(cid, 0), vol)
        if i % 50 == 0:
            print(f"  тегов обработано {i}/{len(tg)}, рынков-ниш {len(markets)}", flush=True)
        time.sleep(0.05)
    mlist = [c for c, _ in sorted(markets.items(), key=lambda kv: kv[1], reverse=True)][:600]
    print(f"нишевых рынков к сбору трейдеров: {len(mlist)}", flush=True)

    vol = {}
    for i, cid in enumerate(mlist, 1):
        try:
            for tr in api.trades_for_market(cid, max_trades=400):
                a = (tr.get("proxyWallet") or "").lower()
                if not a or a in have:
                    continue
                usd = wa._f(tr, "usdcSize", "usdc", "value") or wa._f(tr, "size") * wa._f(tr, "price")
                vol[a] = vol.get(a, 0.0) + usd
        except Exception:  # noqa: BLE001
            pass
        if i % 50 == 0:
            print(f"  рынков обработано {i}/{len(mlist)}, пул трейдеров {len(vol)}", flush=True)
    cand = [a for a, v in sorted(vol.items(), key=lambda kv: kv[1], reverse=True) if v >= 2000][:600]
    with open("wide6.jsonl.candidates.json", "w", encoding="utf-8") as f:
        json.dump(cand, f)
    print(f"кандидатов (объём>=$2k, кап 600): {len(cand)} -> wide6.jsonl.candidates.json", flush=True)


if __name__ == "__main__":
    main()
