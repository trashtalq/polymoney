#!/usr/bin/env python3
"""Слой 0 — несмещённый энумератор популяции из субграфа Polymarket (Goldsky).
Пагинация по id (порядок адресов не коррелирует с навыком => случайный срез),
сбор кошельков с грубым профитом >= порога и активностью в окне. Чекпоинт курсора + ретраи.
Публичный инстанс таймаутит на больших страницах/сортировках -> идём по 100 по id."""
import json
import os
import time
from datetime import datetime, timezone

import requests

URL = ("https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/"
       "subgraphs/polymarket-orderbook-resync/prod/gn")
PAGE = 100
MIN_PROFIT = 10000.0          # грубый профит из ордербук-филлов (пре-фильтр, не истина)
LIVE_DAYS = 30                # окно для DISCOVERY шире, чем для копи
MAX_PAGES = 6000             # бюджет: ~600k аккаунтов (~34% популяции)
MAX_CAND = 1200             # хватит для последующего анализа
STATE = "subgraph_enum_state.json"
OUT = "subgraph_candidates.json"
LOG = "subgraph_enum.log"

s = requests.Session()


def log(m):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z] {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(after, retries=4):
    qy = ('{ accounts(first:%d, orderBy:id, where:{id_gt:"%s"})'
          '{ id scaledProfit lastTradedTimestamp } }') % (PAGE, after)
    for i in range(retries):
        try:
            r = s.post(URL, json={"query": qy}, timeout=45)
            j = r.json()
            if "data" in j and j["data"].get("accounts") is not None:
                return j["data"]["accounts"]
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def main():
    cursor = "0x0"
    cand = {}
    if os.path.exists(STATE):
        st = json.load(open(STATE, encoding="utf-8"))
        cursor = st.get("cursor", "0x0")
        cand = st.get("cand", {})
    have = {w.lower() for w in json.load(open("copy_watchlist.json", encoding="utf-8"))["watchlist"]}
    have |= {w.lower() for w in json.load(open("sports_watchlist.json", encoding="utf-8"))["watchlist"]}
    cutoff = int(time.time()) - LIVE_DAYS * 86400

    log(f"старт энумерации с cursor={cursor[:12]} (уже кандидатов {len(cand)})")
    pages = 0
    scanned = 0
    while pages < MAX_PAGES and len(cand) < MAX_CAND:
        acc = fetch(cursor)
        if acc is None:
            log("страница не отдалась после ретраев — пауза 10с")
            time.sleep(10)
            continue
        if not acc:
            log("конец популяции (пустая страница)")
            break
        for a in acc:
            scanned += 1
            try:
                p = float(a.get("scaledProfit") or 0)
                lt = int(a.get("lastTradedTimestamp") or 0)
            except (TypeError, ValueError):
                continue
            wid = a["id"].lower()
            if p >= MIN_PROFIT and lt >= cutoff and wid not in have:
                cand[wid] = round(p, 0)
        cursor = acc[-1]["id"]
        pages += 1
        if pages % 25 == 0:
            json.dump({"cursor": cursor, "cand": cand}, open(STATE, "w", encoding="utf-8"))
            log(f"стр {pages}, просканировано {scanned}, кандидатов {len(cand)}")
        time.sleep(0.15)

    json.dump({"cursor": cursor, "cand": cand}, open(STATE, "w", encoding="utf-8"))
    ranked = sorted(cand.items(), key=lambda kv: kv[1], reverse=True)
    json.dump([w for w, _ in ranked], open(OUT, "w", encoding="utf-8"))
    log(f"ГОТОВО: просканировано {scanned}, кандидатов {len(cand)} -> {OUT}")
    log("топ-10 по грубому профиту: " + ", ".join(f"{w[:10]}=${v:,.0f}" for w, v in ranked[:10]))


if __name__ == "__main__":
    main()
