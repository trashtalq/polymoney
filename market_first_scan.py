#!/usr/bin/env python3
"""
market_first_scan.py — МАРКЕТ-ПЕРВЫЙ поиск копируемых кошельков (несмещённый источник).

Идея: лидерборд смещён к удачливым и крупным. Вместо «кошелёк-первый» идём от РЫНКОВ:
берём недавно РЕЗОЛВНУТЫЕ политические рынки (лучшая категория по ci_low), вытягиваем
все сделки каждого, и по каждому кошельку считаем НЕ его доходность, а симуляцию
НАШЕГО копирования: вход по его vwap + слиппедж, наш band-фильтр (0.02..0.92),
$10 нотионал, резолв. Скорим bootstrap-CI симулированного копи-PnL по рынкам.

Так отсеиваются «некопируемые гении» (лонгшоты, поздние входы у 0.95+) ещё на скане,
и находятся стабильные специалисты-середняки, которых лидерборд не покажет никогда.

Выход: market_scan_results.json (все), market_scan_adds.json (прошедшие гейты).
Добавление на сервер: push_adds() -> POST /api/add_wallet (пароль спросит).
"""
import json
import random
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

import copy_trader as ct           # _blocked_reason (спорт/погода), слиппедж-константы
import wallet_analyzer as wa       # DataAPIClient с ретраями/пагинацией

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

DAYS_BACK = 120          # берём рынки, резолвнутые за столько дней
MIN_MARKET_VOL = 10_000  # мельче — стакан пустой, сделки шумные
MAX_MARKETS = 250        # потолок рынков на прогон
TRADES_PER_MARKET = 2000 # потолок сделок с рынка (DESC: последние; band-фильтр правит поздних)
MIN_STANCE_USD = 50      # минимум $ покупок кошелька в рынке, чтобы засчитать позицию
SLIP = 0.01              # наш слиппедж на входе (как в копире)
NOTIONAL = 10.0          # $ на симулированный вход (масштаб /10)
MIN_MARKETS = 6          # минимум рынков с позицией для оценки (меньше — не статистика)
BOOT_N = 1000
LIVE_DAYS = 14           # живость: последняя сделка не старше
TOP_ADD = 25             # столько лучших добавляем за прогон

LOGF = Path("market_first_scan.log")


def log(m):
    line = f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {m}"
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def recent_political_markets(s: requests.Session) -> list[dict]:
    """Резолвнутые политические рынки за DAYS_BACK дней: теги -> markets(closed), свежее сверху."""
    tags = json.load(open("politics_tags.json", encoding="utf-8"))
    cutoff = time.time() - DAYS_BACK * 86400
    seen: dict[str, dict] = {}
    for i, tid in enumerate(tags, 1):
        try:
            r = s.get(f"{GAMMA}/markets", params={"tag_id": tid, "closed": "true", "limit": 50,
                      "order": "endDate", "ascending": "false"}, timeout=20)
            ms = r.json()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(ms, dict):
            ms = ms.get("data") or ms.get("markets") or []
        for m in ms:
            cid = m.get("conditionId")
            q = m.get("question") or ""
            if not cid or cid in seen or ct._blocked_reason(q):
                continue
            vol = wa._f(m, "volumeNum", "volume")
            if vol < MIN_MARKET_VOL:
                continue
            iso = m.get("closedTime") or m.get("endDate") or ""
            try:
                ts = datetime.fromisoformat(iso.replace("Z", "+00:00").replace(" ", "T")).timestamp()
            except ValueError:
                continue
            if ts >= cutoff:
                seen[cid] = {"cid": cid, "q": q[:60], "vol": vol, "ts": ts}
        if i % 30 == 0:
            log(f"теги {i}/{len(tags)}, рынков {len(seen)}")
        time.sleep(0.05)
    out = sorted(seen.values(), key=lambda x: x["ts"], reverse=True)[:MAX_MARKETS]
    return out


def winner_map(s: requests.Session, cid: str):
    """{token_id: 1/0} + {outcome_lower: 1/0}, либо None если не резолвнут."""
    try:
        d = s.get(f"{CLOB}/markets/{cid}", timeout=20).json()
    except Exception:  # noqa: BLE001
        return None
    toks = d.get("tokens") or []
    if not d.get("closed") or not any(t.get("winner") for t in toks):
        return None
    tokm = {str(t.get("token_id")): (1.0 if t.get("winner") else 0.0) for t in toks}
    outm = {(t.get("outcome") or "").lower(): (1.0 if t.get("winner") else 0.0) for t in toks}
    return tokm, outm


def scan() -> dict:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    api = wa.DataAPIClient()

    mkts = recent_political_markets(s)
    log(f"рынков к разбору: {len(mkts)} (резолв за {DAYS_BACK}д, vol>={MIN_MARKET_VOL})")

    # wallet -> список симулированных PnL по рынкам
    sims: dict[str, list] = defaultdict(list)
    done = 0
    for m in mkts:
        wm = winner_map(s, m["cid"])
        if not wm:
            continue
        tokm, outm = wm
        # покупки по кошелькам: stance = токен с макс $ покупок
        acc: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))  # w->tok->[usd,qty]
        try:
            trades = api.trades_for_market(m["cid"], max_trades=TRADES_PER_MARKET)
        except Exception as ex:  # noqa: BLE001
            log(f"  {m['cid'][:10]}… trades недоступны ({ex})")
            continue
        for t in trades:
            if (t.get("side") or "").upper() != "BUY":
                continue
            w = (t.get("proxyWallet") or "").lower()
            px, sz = wa._f(t, "price"), wa._f(t, "size")
            if not w or not (0 < px < 1) or sz <= 0:
                continue
            tok = str(t.get("asset") or "")
            a = acc[w][tok]
            a[0] += px * sz
            a[1] += sz
        for w, toks in acc.items():
            tok, (usd, qty) = max(toks.items(), key=lambda kv: kv[1][0])
            if usd < MIN_STANCE_USD or qty <= 0:
                continue
            val = tokm.get(tok)
            if val is None:                            # asset не сматчился с токенами CLOB -> пропуск
                continue
            entry = usd / qty + SLIP                   # наш вход: его vwap + слиппедж
            if not (ct.MIN_ENTRY_PRICE <= entry <= ct.MAX_ENTRY_PRICE):
                continue                               # наш band-фильтр это не скопировал бы
            pnl = NOTIONAL * (val / entry - 1.0) if val > 0 else -NOTIONAL
            sims[w].append(round(pnl, 3))
        done += 1
        if done % 25 == 0:
            log(f"рынки {done}/{len(mkts)}, кошельков в пуле {len(sims)}")

    # скоринг: bootstrap-CI среднего PnL по рынкам
    rows = []
    for w, pl in sims.items():
        n = len(pl)
        if n < MIN_MARKETS:
            continue
        mean = sum(pl) / n
        boots = []
        for _ in range(BOOT_N):
            sample = [random.choice(pl) for _ in range(n)]
            boots.append(sum(sample) / n)
        boots.sort()
        ci_low = boots[int(0.05 * BOOT_N)]
        rows.append({"wallet": w, "n_markets": n, "wins": sum(1 for x in pl if x > 0),
                     "sim_pnl": round(sum(pl), 2), "sim_mean": round(mean, 3),
                     "ci_low": round(ci_low, 3)})
    rows.sort(key=lambda r: r["ci_low"], reverse=True)
    Path("market_scan_results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"оценено кошельков (>= {MIN_MARKETS} рынков): {len(rows)}; ci_low>0: "
        f"{sum(1 for r in rows if r['ci_low'] > 0)}")
    return {"rows": rows, "session": s}


def is_live(s: requests.Session, addr: str) -> bool:
    try:
        evs = s.get(f"{DATA}/activity", params={"user": addr, "limit": 1, "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC"}, timeout=15).json() or []
    except Exception:  # noqa: BLE001
        return False
    return bool(evs) and (time.time() - int(evs[0].get("timestamp", 0))) <= LIVE_DAYS * 86400


def sport_majority(s: requests.Session, addr: str) -> bool:
    """Большинство реальных сделок — спорт/погода -> не берём (копир их режет, толку ноль)."""
    try:
        evs = s.get(f"{DATA}/activity", params={"user": addr, "limit": 150, "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC"}, timeout=15).json() or []
    except Exception:  # noqa: BLE001
        return False
    tr = [e for e in evs if (e.get("type", "").upper() == "TRADE")]
    if len(tr) < 10:
        return False
    bl = sum(1 for e in tr if ct._blocked_reason(e.get("title") or ""))
    return bl / len(tr) >= 0.6


def main():
    LOGF.write_text("", encoding="utf-8")
    have = {w.lower() for w in json.load(open("copy_watchlist.json", encoding="utf-8"))["watchlist"]}
    res = scan()
    rows, s = res["rows"], res["session"]

    # гейты: ci_low>0, винрейт>=50%, не в списке, живой, не спорт-мажоритарный
    adds = []
    for r in rows:
        if len(adds) >= TOP_ADD:
            break
        if r["ci_low"] <= 0 or r["wins"] / r["n_markets"] < 0.5 or r["wallet"] in have:
            continue
        if not is_live(s, r["wallet"]):
            continue
        if sport_majority(s, r["wallet"]):
            log(f"  {r['wallet'][:10]}… спорт-мажоритарный — мимо")
            continue
        adds.append(r)
        log(f"  + {r['wallet'][:10]}… рынков {r['n_markets']}, винрейт "
            f"{100 * r['wins'] // r['n_markets']}%, sim ${r['sim_pnl']}, ci_low {r['ci_low']}")
        time.sleep(0.3)
    Path("market_scan_adds.json").write_text(
        json.dumps(adds, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"ИТОГ: кандидатов к добавлению {len(adds)} -> market_scan_adds.json")
    log("добавить на сервер: python market_first_scan.py --push (после деплоя /api/add_wallet)")


def push_adds(pw: str):
    adds = json.loads(Path("market_scan_adds.json").read_text(encoding="utf-8"))
    if not adds:
        print("нечего добавлять")
        return
    r = requests.post("http://144.31.197.121:5000/api/add_wallet",
                      json={"pw": pw, "wallets": [a["wallet"] for a in adds],
                            "source": "маркет-скан"}, timeout=30)
    print(r.status_code, r.json())


if __name__ == "__main__":
    import sys
    if "--push" in sys.argv:
        import getpass
        push_adds(getpass.getpass("пароль: "))
    else:
        main()
