#!/usr/bin/env python3
"""
co_trader_scan.py — СО-ТРЕЙДЕРЫ CORE-КОГОРТЫ: кто регулярно в тех же позициях, что наши лучшие.

Прицельный поиск: берём рынки, где торгует core-восьмёрка (кошельки с меткой core-реал),
и ищем тех, кто РЕГУЛЯРНО оказывается в тех же позициях (та же сторона) с приличными
входами. Ранжируем по ЧАСТОТЕ со-появления у РАЗНЫХ наших (специалисты кластера),
не по объёму (объём тянет маркетмейкеров — выученный урок). Скорим симуляцией нашего
копирования их входов на резолвнутых рынках.

Запуск: python co_trader_scan.py [--push]   (push: env POLY_PW, метка «со-трейдер»)
"""
import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

import copy_trader as ct
import wallet_analyzer as wa
from market_first_scan import SLIP, NOTIONAL, winner_map, wallet_profile, SERVER

MKTS_PER_CORE = 30       # свежих рынков на каждого core-кошелька
MAX_MARKETS_TOTAL = 200  # общий потолок уникальных рынков за прогон
TRADES_PER_MARKET = 3000
MIN_CO_USD = 50          # минимум $ покупок кандидата в рынке
MIN_HITS = 5             # со-появлений (рынков) минимум
MIN_CORES = 2            # минимум РАЗНЫХ core-кошельков, с которыми совпал (кластер, не хвост одного)
TOP_ADD = 15

LOGF = Path("co_trader_scan.log")


def log(m):
    line = f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {m}"
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def core_wallets(s: requests.Session) -> list[str]:
    d = s.get(f"{SERVER}/api/state", timeout=30).json()
    return [w["wallet"].lower() for w in (d.get("per_wallet") or [])
            if w.get("source") == "core-реал"]


def wallet_markets(api: wa.DataAPIClient, wl: str) -> dict:
    """(cid, tok) -> сторона core-кошелька. Свежие не-заблокированные BUY."""
    try:
        evs = api.activity(wl)
    except Exception as ex:  # noqa: BLE001
        log(f"  {wl[:10]}… activity недоступна ({ex})")
        return {}
    out: dict = {}
    for e in evs:
        if (e.get("type", "").upper() != "TRADE") or (e.get("side", "").upper() != "BUY"):
            continue
        if ct._blocked_reason(e.get("title") or ""):
            continue
        cid, tok = e.get("conditionId") or "", str(e.get("asset") or "")
        ts = int(e.get("timestamp") or 0)
        if cid and tok and ts:
            k = (cid, tok)
            out[k] = max(out.get(k, 0), ts)
    items = sorted(out.items(), key=lambda kv: kv[1], reverse=True)[:MKTS_PER_CORE]
    return dict(items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="залить прошедших (env POLY_PW)")
    args = ap.parse_args()
    LOGF.write_text("", encoding="utf-8")

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    api = wa.DataAPIClient()

    cores = core_wallets(s)
    log(f"core-когорта: {len(cores)}: " + ", ".join(w[:10] + "…" for w in cores))
    core_set = set(cores)

    # (cid, tok) -> какие core там (для n_cores кандидата)
    mkt_cores: dict = defaultdict(set)
    for wl in cores:
        for k in wallet_markets(api, wl):
            mkt_cores[k].add(wl)
    mkts = list(mkt_cores.items())[:MAX_MARKETS_TOTAL]
    log(f"уникальных рынков core-когорты: {len(mkts)}")

    cand: dict = defaultdict(lambda: {"hits": 0, "cores": set(), "wins": 0,
                                      "sim_pnl": 0.0, "sim_n": 0})
    for i, ((cid, tok), owners) in enumerate(mkts, 1):
        wm = winner_map(s, cid)                        # None = не резолвнут (со-появление всё равно считаем)
        try:
            trades = api.trades_for_market(cid, max_trades=TRADES_PER_MARKET)
        except Exception:  # noqa: BLE001
            continue
        acc: dict = defaultdict(lambda: [0.0, 0.0])    # w -> [usd, qty] по НАШЕМУ токену
        for t in trades:
            if (t.get("side") or "").upper() != "BUY" or str(t.get("asset") or "") != tok:
                continue
            w = (t.get("proxyWallet") or "").lower()
            if not w or w in core_set:
                continue
            px, sz = wa._f(t, "price"), wa._f(t, "size")
            if not (0 < px < 1) or sz <= 0:
                continue
            acc[w][0] += px * sz
            acc[w][1] += sz
        for w, (usd, qty) in acc.items():
            if usd < MIN_CO_USD or qty <= 0:
                continue
            c = cand[w]
            c["hits"] += 1
            c["cores"] |= owners
            if wm is not None:
                val = wm.get(tok)
                entry = usd / qty + SLIP
                if val is not None and ct.MIN_ENTRY_PRICE <= entry <= ct.MAX_ENTRY_PRICE:
                    pnl = NOTIONAL * (val / entry - 1.0) if val > 0 else -NOTIONAL
                    c["sim_pnl"] += pnl
                    c["sim_n"] += 1
                    c["wins"] += 1 if pnl > 0 else 0
        if i % 25 == 0:
            log(f"рынки {i}/{len(mkts)}, кандидатов {len(cand)}")

    rows = []
    for w, c in cand.items():
        if c["hits"] < MIN_HITS or len(c["cores"]) < MIN_CORES:
            continue
        rows.append({"wallet": w, "hits": c["hits"], "n_cores": len(c["cores"]),
                     "cores": sorted(x[:10] for x in c["cores"]),
                     "sim_pnl": round(c["sim_pnl"], 2), "sim_n": c["sim_n"],
                     "sim_wins": c["wins"]})
    rows.sort(key=lambda r: (r["n_cores"], r["hits"], r["sim_pnl"]), reverse=True)
    Path("co_trader_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    log(f"кандидатов-со-трейдеров (hits>={MIN_HITS}, cores>={MIN_CORES}): {len(rows)}")

    passed = []
    for r in rows[:40]:
        if len(passed) >= TOP_ADD:
            break
        if r["sim_n"] >= 3 and r["sim_pnl"] <= 0:      # резолвы есть и в минус — мимо
            continue
        p = wallet_profile(s, r["wallet"])
        if not p["live"] or p["blocked_share"] >= 0.6 or p["trades_per_day"] > 100 or p.get("bot"):
            continue
        r["trades_per_day"] = p["trades_per_day"]
        passed.append(r)
        log(f"  + {r['wallet'][:10]}… с {r['n_cores']} нашими в {r['hits']} рынках, "
            f"sim ${r['sim_pnl']} ({r['sim_wins']}/{r['sim_n']}), темп {p['trades_per_day']}/д")
        time.sleep(0.3)
    Path("co_trader_adds.json").write_text(json.dumps(passed, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    log(f"ИТОГ: прошли гейты {len(passed)}")

    if args.push and passed:
        pw = os.environ.get("POLY_PW", "")
        r = requests.post(f"{SERVER}/api/add_wallet",
                          json={"pw": pw, "wallets": [x["wallet"] for x in passed],
                                "source": "со-трейдер"}, timeout=30)
        log(f"push: {r.status_code} {r.json()}")


if __name__ == "__main__":
    main()
