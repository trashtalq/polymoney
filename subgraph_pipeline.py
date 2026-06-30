#!/usr/bin/env python3
"""Слой-0 пайплайн: несмещённый срез популяции из субграфа -> анализ -> мёрж.

Поле profit в субграфе недостоверно, поэтому фильтруем по ДОСТОВЕРНЫМ полям:
scaledCollateralVolume в полосе [VMIN,VMAX] (не пыль и не мега-маркетмейкер) + lastTradedTimestamp
в окне. Пагинация по id (случайный по навыку срез). Эдж судит наш анализатор. Детач-устойчив."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

URL = ("https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/"
       "subgraphs/polymarket-orderbook-resync/prod/gn")
PAGE = 100
VMIN, VMAX = 20000.0, 2_000_000.0     # объёмная полоса: отсекаем пыль и мегакитов/MM
LIVE_DAYS = 30
MAX_PAGES = 9000
MAX_CAND = 800
CKPT = "subgraph.jsonl"
STATE = "subgraph_pipe_state.json"
LOG = "subgraph_pipeline.log"

s = requests.Session()


def log(m):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z] {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(after, retries=5):
    qy = ('{ accounts(first:%d, orderBy:id, where:{id_gt:"%s"})'
          '{ id scaledCollateralVolume lastTradedTimestamp } }') % (PAGE, after)
    for i in range(retries):
        try:
            j = s.post(URL, json={"query": qy}, timeout=45).json()
            if "data" in j and j["data"].get("accounts") is not None:
                return j["data"]["accounts"]
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None


def enumerate_pool(have):
    cursor, cand = "0x0", {}
    if os.path.exists(STATE):
        st = json.load(open(STATE, encoding="utf-8"))
        cursor, cand = st.get("cursor", "0x0"), st.get("cand", {})
    cutoff = int(time.time()) - LIVE_DAYS * 86400
    pages = stalls = 0
    while pages < MAX_PAGES and len(cand) < MAX_CAND:
        acc = fetch(cursor)
        if acc is None:
            stalls += 1
            if stalls > 30:
                log("слишком много таймаутов — стоп энумерации на текущем")
                break
            time.sleep(8)
            continue
        if not acc:
            log("конец популяции")
            break
        stalls = 0
        for a in acc:
            try:
                v = float(a.get("scaledCollateralVolume") or 0)
                lt = int(a.get("lastTradedTimestamp") or 0)
            except (TypeError, ValueError):
                continue
            wid = a["id"].lower()
            if VMIN <= v <= VMAX and lt >= cutoff and wid not in have:
                cand[wid] = round(v)
        cursor = acc[-1]["id"]
        pages += 1
        if pages % 25 == 0:
            json.dump({"cursor": cursor, "cand": cand}, open(STATE, "w", encoding="utf-8"))
            log(f"стр {pages}, кандидатов {len(cand)}")
        time.sleep(0.15)
    json.dump({"cursor": cursor, "cand": cand}, open(STATE, "w", encoding="utf-8"))
    return cand


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
    have = {w.lower() for w in json.load(open("copy_watchlist.json", encoding="utf-8"))["watchlist"]}
    have |= {w.lower() for w in json.load(open("sports_watchlist.json", encoding="utf-8"))["watchlist"]}
    log("=== СТАРТ пайплайна субграфа (Слой-0) ===")
    cand = enumerate_pool(have)
    ranked = [w for w, _ in sorted(cand.items(), key=lambda kv: kv[1], reverse=True)]
    log(f"энумерация дала кандидатов: {len(ranked)}")
    if not ranked:
        log("кандидатов нет — выход")
        return
    with open(CKPT + ".candidates.json", "w", encoding="utf-8") as f:
        json.dump(ranked, f)
    try:
        os.remove(CKPT)
    except OSError:
        pass

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    log("анализ кандидатов…")
    with open("subgraph_analyzer.log", "wb") as af:
        subprocess.run([sys.executable, "wallet_analyzer.py", "--checkpoint", CKPT,
                        "--out", "subgraph_registry.json"], env=env, stdout=af, stderr=af, check=False)

    cards = {}
    for line in open(CKPT, encoding="utf-8"):
        try:
            d = json.loads(line)
            cards[d["wallet"].lower()] = d
        except Exception:  # noqa: BLE001
            pass
    have = {w.lower() for w in json.load(open("copy_watchlist.json", encoding="utf-8"))["watchlist"]}
    have |= {w.lower() for w in json.load(open("sports_watchlist.json", encoding="utf-8"))["watchlist"]}
    passed = [c for c in cards.values() if copy_ok(c) and c["wallet"].lower() not in have]
    passed.sort(key=lambda c: c.get("roi_ci_low", 0) * (c.get("n_resolved", 0) ** 0.5), reverse=True)
    live = [c for c in passed if is_live(c["wallet"].lower())]
    log(f"проанализировано {len(cards)} | копи-кандидатов {len(passed)} | живых {len(live)}")

    if live:
        wl = json.load(open("copy_watchlist.json", encoding="utf-8"))
        add = [c["wallet"] for c in live]
        wl["watchlist"] += add
        wl["count"] = len(wl["watchlist"])
        json.dump(wl, open("copy_watchlist.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        src = json.load(open("wallet_sources.json", encoding="utf-8"))
        for w in add:
            src[w.lower()] = "субграф"
        json.dump(src, open("wallet_sources.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        top = ", ".join(f"{c['wallet'][:10]}…(roi {c.get('roi',0)*100:.0f}%,ci {c.get('roi_ci_low',0)*100:.0f}%,n{c.get('n_resolved',0)})" for c in live[:8])
        log(f"ДОБАВЛЕНО {len(add)} -> watchlist {wl['count']}. Топ: {top}")
    else:
        log("новых копируемых живых нет")
    log("=== ПАЙПЛАЙН СУБГРАФА ЗАВЕРШЁН ===")


if __name__ == "__main__":
    main()
