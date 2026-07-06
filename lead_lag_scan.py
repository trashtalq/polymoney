#!/usr/bin/env python3
"""
lead_lag_scan.py — ОХОТНИК ЗА ЛИДЕРАМИ: кто входит в те же позиции РАНЬШЕ наших лучших.

Гипотеза: часть наших форвардных лидеров сами копируют кого-то «выше по течению».
Копировать источник выгоднее: меньше суммарная задержка -> больше эджа доезжает.

Алгоритм:
  1) форвардные лидеры с нашего сервера (/api/prune: flag=lead или total>0 при closed>=8);
  2) по каждому — его недавние BUY (activity), первый вход в (рынок, токен);
  3) по каждому такому рынку — все сделки: кто купил ТОТ ЖЕ токен РАНЬШЕ лидера (>= $50);
  4) агрегация по кандидатам: в скольких рынках лидеров он был раньше (hits), средний
     опережающий лаг, симуляция нашего копирования ЕГО входов (vwap+слиппедж, band, резолв);
  5) гейты (живость, не спорт/погода, не гиперактивный) -> lead_lag_results.json;
     --push заливает прошедших на сервер с меткой «lead-lag» (пароль из env POLY_PW).

Запуск: python lead_lag_scan.py [--push]
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
from market_first_scan import (SLIP, NOTIONAL, winner_map, wallet_profile, SERVER)

# === ПАРАМЕТРЫ ОХОТНИКА (секция самоулучшения: правит агент hunter-improver-weekly) ===
# История правок — в git-логе этого файла. Менять В ПРЕДЕЛАХ разумного (не отключать гейты).
N_LEADERS = 12           # сколько наших лучших разбираем (2026-07-06: 8->12, лидеров стало больше)
MKTS_PER_LEADER = 30     # свежих рынков на лидера (2026-07-06: 20->30, расширение покрытия)
TRADES_PER_MARKET = 3000
MIN_EARLY_USD = 50       # минимум $ ранних покупок кандидата в рынке
MIN_LEAD_SEC = 60        # раньше лидера минимум на минуту (иначе это со-копирование)
MIN_HITS = 4             # кандидат интересен с этого числа рынков-опережений
MAX_MARKETS_TOTAL = 300  # общий потолок рынков за прогон (2026-07-06: 150->300 под 12x30)
LAG_SOURCE_MAX_H = 48    # лаг <= этого = «источник» (его реально копируют); больше = «ранняя-птица»
                         # урок прогона 2026-07-03: лаг днями-неделями — это ранние птицы,
                         # истинный lead-follow — минуты-часы; источники ценнее, идут первыми
# ======================================================================================

LOGF = Path("lead_lag_scan.log")


def log(m):
    line = f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {m}"
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def our_leaders(s: requests.Session) -> list[str]:
    r = s.get(f"{SERVER}/api/prune", timeout=30).json()
    rows = r.get("rows") or []
    good = [x for x in rows if (x.get("flag") == "lead"
            or ((x.get("total") or 0) > 0 and (x.get("closed") or 0) >= 8))]
    good.sort(key=lambda x: x.get("total") or 0, reverse=True)
    return [x["wallet"].lower() for x in good[:N_LEADERS]]


def leader_entries(api: wa.DataAPIClient, wl: str) -> dict:
    """(cid, token) -> первый вход лидера {ts, title}. Только не-заблокированные рынки."""
    try:
        evs = api.activity(wl)
    except Exception as ex:  # noqa: BLE001
        log(f"  {wl[:10]}… activity недоступна ({ex})")
        return {}
    first: dict = {}
    for e in evs:
        if (e.get("type", "").upper() != "TRADE") or (e.get("side", "").upper() != "BUY"):
            continue
        title = e.get("title") or ""
        if ct._blocked_reason(title):
            continue
        cid = e.get("conditionId") or ""
        tok = str(e.get("asset") or "")
        ts = int(e.get("timestamp") or 0)
        if not cid or not tok or not ts:
            continue
        k = (cid, tok)
        if k not in first or ts < first[k]["ts"]:
            first[k] = {"ts": ts, "title": title[:50]}
    # свежие сверху, потолок на лидера
    items = sorted(first.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:MKTS_PER_LEADER]
    return dict(items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="залить прошедших на сервер (env POLY_PW)")
    ap.add_argument("--apply", action="store_true",
                    help="писать watchlist напрямую (ночной запуск НА СЕРВЕРЕ)")
    args = ap.parse_args()
    LOGF.write_text("", encoding="utf-8")

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    api = wa.DataAPIClient()

    leaders = our_leaders(s)
    log(f"лидеры для разбора: {len(leaders)}: " + ", ".join(w[:10] + "…" for w in leaders))

    # кандидат -> статистика опережений
    cand: dict = defaultdict(lambda: {"hits": 0, "wins": 0, "sim_pnl": 0.0,
                                      "lead_min": [], "ahead_of": set()})
    seen_mkts = 0
    for wl in leaders:
        entries = leader_entries(api, wl)
        log(f"{wl[:10]}…: рынков для разбора {len(entries)}")
        for (cid, tok), info in entries.items():
            if seen_mkts >= MAX_MARKETS_TOTAL:
                break
            seen_mkts += 1
            wm = winner_map(s, cid)                    # None = ещё не резолвнут (sim невозможен)
            try:
                trades = api.trades_for_market(cid, max_trades=TRADES_PER_MARKET)
            except Exception:  # noqa: BLE001
                continue
            # ранние покупатели того же токена
            early: dict = defaultdict(lambda: [0.0, 0.0, 0.0])   # w -> [usd, qty, usd*ts]
            for t in trades:
                if (t.get("side") or "").upper() != "BUY" or str(t.get("asset") or "") != tok:
                    continue
                w = (t.get("proxyWallet") or "").lower()
                ts = int(t.get("timestamp") or 0)
                if not w or w == wl or ts >= info["ts"] - MIN_LEAD_SEC:
                    continue
                px, sz = wa._f(t, "price"), wa._f(t, "size")
                if not (0 < px < 1) or sz <= 0:
                    continue
                a = early[w]
                a[0] += px * sz
                a[1] += sz
                a[2] += px * sz * ts
            for w, (usd, qty, usdts) in early.items():
                if usd < MIN_EARLY_USD or qty <= 0:
                    continue
                c = cand[w]
                c["hits"] += 1
                c["ahead_of"].add(wl[:10])
                c["lead_min"].append((info["ts"] - usdts / usd) / 60)
                if wm is not None:                     # симуляция нашего копирования его входа
                    val = wm.get(tok)
                    entry = usd / qty + SLIP
                    if val is not None and ct.MIN_ENTRY_PRICE <= entry <= ct.MAX_ENTRY_PRICE:
                        pnl = NOTIONAL * (val / entry - 1.0) if val > 0 else -NOTIONAL
                        c["sim_pnl"] += pnl
                        c["wins"] += 1 if pnl > 0 else 0
        if seen_mkts >= MAX_MARKETS_TOTAL:
            log(f"достигнут потолок рынков ({MAX_MARKETS_TOTAL})")
            break

    rows = []
    for w, c in cand.items():
        if c["hits"] < MIN_HITS:
            continue
        avg_lead = sum(c["lead_min"]) / len(c["lead_min"])
        rows.append({"wallet": w, "hits": c["hits"], "ahead_of": sorted(c["ahead_of"]),
                     "n_leaders": len(c["ahead_of"]),
                     "avg_lead_min": round(avg_lead, 1),
                     "kind": "источник" if avg_lead <= LAG_SOURCE_MAX_H * 60 else "ранняя-птица",
                     "sim_pnl": round(c["sim_pnl"], 2), "sim_wins": c["wins"]})
    # источники (короткий лаг — их реально копируют) выше ранних птиц при прочих равных
    rows.sort(key=lambda r: (r["kind"] == "источник", r["n_leaders"], r["hits"], r["sim_pnl"]),
              reverse=True)
    Path("lead_lag_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    log(f"кандидатов-«вышестоящих» (hits>={MIN_HITS}): {len(rows)}")

    # гейты как у разведчика + sim в плюс
    passed = []
    for r in rows[:30]:
        if r["sim_pnl"] <= 0:
            continue
        p = wallet_profile(s, r["wallet"])
        if not p["live"] or p["blocked_share"] >= 0.6 or p["trades_per_day"] > 100 or p.get("bot"):
            continue
        r["trades_per_day"] = p["trades_per_day"]
        passed.append(r)
        log(f"  + [{r['kind']}] {r['wallet'][:10]}… раньше {r['n_leaders']} лидеров в {r['hits']} "
            f"рынках, средний лаг {r['avg_lead_min']}мин, sim ${r['sim_pnl']}")
        time.sleep(0.3)
    Path("lead_lag_adds.json").write_text(json.dumps(passed, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    log(f"ИТОГ: прошли гейты {len(passed)} "
        f"(источников {sum(1 for x in passed if x['kind'] == 'источник')})")
    # леджер добычи (append): по нему hunter-improver оценивает, какая добыча работает в форварде
    with open("hunter_ledger.jsonl", "a", encoding="utf-8") as f:
        for x in passed:
            f.write(json.dumps({"d": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                                "w": x["wallet"], "kind": x["kind"], "hits": x["hits"],
                                "n_leaders": x["n_leaders"], "avg_lead_min": x["avg_lead_min"],
                                "sim_pnl": x["sim_pnl"]}, ensure_ascii=False) + "\n")

    # мандат пользователя 2026-07-03: добычу охотника доливать не спрашивая, метка «добыча-охотника»
    if args.apply and passed:
        from market_first_scan import apply_local
        apply_local(passed, "добыча-охотника")
    if args.push and passed:
        pw = os.environ.get("POLY_PW", "")
        r = requests.post(f"{SERVER}/api/add_wallet",
                          json={"pw": pw, "wallets": [x["wallet"] for x in passed],
                                "source": "добыча-охотника"}, timeout=30)
        log(f"push: {r.status_code} {r.json()}")


if __name__ == "__main__":
    main()
