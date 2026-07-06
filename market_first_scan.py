#!/usr/bin/env python3
"""
market_first_scan.py — МАРКЕТ-ПЕРВЫЙ поиск копируемых кошельков (несмещённый источник).

Идея: лидерборд смещён к удачливым и крупным. Вместо «кошелёк-первый» идём от РЫНКОВ:
берём недавно РЕЗОЛВНУТЫЕ рынки пула (политика/макро/крипто/мир/широкий), вытягиваем
сделки каждого, и по каждому кошельку считаем НЕ его доходность, а симуляцию НАШЕГО
копирования: вход по его vwap + слиппедж, наш band-фильтр, $10 нотионал, резолв.
Скорим bootstrap-CI симулированного копи-PnL по рынкам + требуем БЫСТРЫЙ ОБОРОТ
(медиана вход->резолв в днях) и отсекаем гиперактивных (погодные брекеты и т.п.).

Режимы:
  python market_first_scan.py                     # пул politics, локально, без добавления
  python market_first_scan.py --pool macro        # конкретный пул
  python market_first_scan.py --auto --apply      # НОЧНОЙ на сервере: пул по ротации дня,
                                                  #   прошедших пишет прямо в copy_watchlist.json
  python market_first_scan.py --push              # локально: залить adds на сервер по HTTP
  python market_first_scan.py --cids f.json --source событие   # скан ЗАДАННЫХ рынков (скаут)

Выход: market_scan_results.json (все оценённые), market_scan_adds.json (прошедшие гейты),
market_first_scan.log (лог прогона — отдаётся дашбордом через /api/discovery).
"""
import argparse
import json
import math
import random
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

import copy_trader as ct           # _blocked_reason (спорт/погода), band-константы
import wallet_analyzer as wa       # DataAPIClient с ретраями/пагинацией

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
SERVER = "http://144.31.197.121:5000"

# --- Пулы рынков: теги подбираются по ключевым словам в label/slug тега Gamma.
# Без внешних JSON — скрипт самодостаточен (на сервер деплоятся только *.py).
POOLS = {
    "politics": {"tag_kw": ("politic", "election", "president", "senate", "congress", "governor",
                            "trump", "democrat", "republican", "parliament", "minister", "cabinet",
                            "supreme court", "impeach", "primary", "veto"),
                 "days": 45, "vol": 10_000},
    "macro":    {"tag_kw": ("fed", "rate", "inflation", "econom", "financ", "gdp", "treasur",
                            "tariff", "recession", "jobs", "cpi", "stock", "s&p", "nasdaq"),
                 "days": 90, "vol": 10_000},
    "crypto":   {"tag_kw": ("crypto", "bitcoin", "btc", "ethereum", "eth", "solana", "defi",
                            "token", "stablecoin", "etf"),
                 "days": 60, "vol": 15_000},
    "world":    {"tag_kw": ("geopolit", "war", "ukraine", "russia", "israel", "iran", "china",
                            "nato", "militar", "ceasefire", "korea", "taiwan", "middle east",
                            "sanction", "nuclear"),
                 "days": 60, "vol": 10_000},
    "wide":     {"sweep": True, "days": 21, "vol": 50_000},   # без тегов: свежие крупные резолвы
}
POOL_ORDER = ("politics", "macro", "world", "crypto", "wide")

MAX_MARKETS = 250        # потолок рынков на прогон
TRADES_PER_MARKET = 2000 # базовый потолок сделок с рынка (DESC: последние)
# АДАПТИВНАЯ ГЛУБИНА: на крупных рынках ранние умные входы не попадают в хвост из 2к последних
# сделок — копаем глубже пропорционально объёму (там и сидят лучшие).
def trades_cap(vol: float) -> int:
    if vol >= 500_000:
        return 6000
    if vol >= 100_000:
        return 4000
    return TRADES_PER_MARKET
MIN_STANCE_USD = 50      # минимум $ покупок кошелька в рынке, чтобы засчитать позицию
SLIP = 0.01              # наш слиппедж на входе (как в копире)
NOTIONAL = 10.0          # $ на симулированный вход (масштаб /10)
MIN_MARKETS = 6          # минимум рынков с позицией для оценки (меньше — не статистика)
BOOT_N = 1000
LIVE_DAYS = 14           # живость: последняя сделка не старше
TOP_ADD = 25             # столько лучших добавляем за прогон
MAX_MED_HOLD_DAYS = 10   # медианное время вход->резолв: капитал должен ОБОРАЧИВАТЬСЯ (дни)
MAX_TRADES_PER_DAY = 100 # гиперактивные (погодные брекеты по 1к позиций/день и т.п.) — не берём
FDR_Q = 0.10             # контроль ложных открытий (Benjamini-Hochberg): сканируем тысячи
                         # кошельков, сырой ci_low>0 даёт ~5% ложняка НА КАЖДОГО -> q-value

LOGF = Path("market_first_scan.log")


def log(m):
    line = f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {m}"
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _parse_ts(iso: str):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00").replace(" ", "T")).timestamp()
    except (ValueError, AttributeError):
        return None


def pool_tags(s: requests.Session, kws) -> list:
    """id тегов Gamma, чей label/slug содержит любое из ключевых слов пула."""
    ids, off = [], 0
    while True:
        try:
            r = s.get(f"{GAMMA}/tags", params={"limit": 100, "offset": off}, timeout=20)
            d = r.json()
        except Exception:  # noqa: BLE001
            break
        if isinstance(d, dict):
            d = d.get("data") or []
        if not d:
            break
        for t in d:
            lbl = ((t.get("label") or "") + " " + (t.get("slug") or "")).lower()
            if any(k in lbl for k in kws):
                ids.append(str(t.get("id")))
        off += 100
        if len(d) < 100:
            break
        time.sleep(0.03)
    return ids


def _mk_row(m, cutoff, vol_min):
    cid = m.get("conditionId")
    q = m.get("question") or ""
    if not cid or ct._blocked_reason(q):
        return None
    vol = wa._f(m, "volumeNum", "volume")
    if vol < vol_min:
        return None
    ts = _parse_ts(m.get("closedTime") or m.get("endDate") or "")
    if ts is None or ts < cutoff:
        return None
    return {"cid": cid, "q": q[:60], "vol": vol, "ts": ts}


def pool_markets(s: requests.Session, pool: dict) -> list[dict]:
    """Резолвнутые рынки пула за pool.days дней: теги -> markets(closed) или прямой свип."""
    cutoff = time.time() - pool["days"] * 86400
    seen: dict[str, dict] = {}
    if pool.get("sweep"):                              # широкий свип без тегов
        off = 0
        while off < 3000:
            try:
                r = s.get(f"{GAMMA}/markets", params={"closed": "true", "limit": 100, "offset": off,
                          "order": "endDate", "ascending": "false"}, timeout=20)
                ms = r.json()
            except Exception:  # noqa: BLE001
                break
            if isinstance(ms, dict):
                ms = ms.get("data") or ms.get("markets") or []
            if not ms:
                break
            for m in ms:
                row = _mk_row(m, cutoff, pool["vol"])
                if row:
                    seen[row["cid"]] = row
            off += 100
            time.sleep(0.05)
    else:
        tids = pool_tags(s, pool["tag_kw"])
        log(f"тегов в пуле: {len(tids)}")
        for i, tid in enumerate(tids, 1):
            try:
                r = s.get(f"{GAMMA}/markets", params={"tag_id": tid, "closed": "true", "limit": 50,
                          "order": "endDate", "ascending": "false"}, timeout=20)
                ms = r.json()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(ms, dict):
                ms = ms.get("data") or ms.get("markets") or []
            for m in ms:
                row = _mk_row(m, cutoff, pool["vol"])
                if row:
                    seen[row["cid"]] = row
            if i % 30 == 0:
                log(f"теги {i}/{len(tids)}, рынков {len(seen)}")
            time.sleep(0.05)
    return sorted(seen.values(), key=lambda x: x["ts"], reverse=True)[:MAX_MARKETS]


def winner_map(s: requests.Session, cid: str):
    """{token_id: 1/0}, либо None если не резолвнут."""
    try:
        d = s.get(f"{CLOB}/markets/{cid}", timeout=20).json()
    except Exception:  # noqa: BLE001
        return None
    toks = d.get("tokens") or []
    if not d.get("closed") or not any(t.get("winner") for t in toks):
        return None
    return {str(t.get("token_id")): (1.0 if t.get("winner") else 0.0) for t in toks}


def scan(mkts: list[dict], s: requests.Session) -> list[dict]:
    """Ядро: рынки -> сделки -> симуляция нашего копирования -> скоринг по кошелькам."""
    api = wa.DataAPIClient()
    sims: dict[str, list] = defaultdict(list)
    done = 0
    for m in mkts:
        wm = winner_map(s, m["cid"])
        if not wm:
            continue
        acc: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))  # w->tok->[usd,qty,usd*ts]
        try:
            trades = api.trades_for_market(m["cid"], max_trades=trades_cap(m.get("vol", 0)))
        except Exception as ex:  # noqa: BLE001
            log(f"  {m['cid'][:10]}… trades недоступны ({ex})")
            continue
        tot_usd = tot_sz = 0.0
        for t in trades:
            if (t.get("side") or "").upper() != "BUY":
                continue
            w = (t.get("proxyWallet") or "").lower()
            px, sz = wa._f(t, "price"), wa._f(t, "size")
            if not w or not (0 < px < 1) or sz <= 0:
                continue
            tot_usd += px * sz
            tot_sz += sz
            a = acc[w][str(t.get("asset") or "")]
            a[0] += px * sz
            a[1] += sz
            a[2] += px * sz * wa._f(t, "timestamp")
        # ВЕС РЫНКА: победа на крупном СПОРНОМ рынке информативнее победы на пустыре/однозначном.
        # contested = 4p(1-p) по средней цене сэмпла (угадать 50/50 >> купить очевидные 0.95)
        pbar = (tot_usd / tot_sz) if tot_sz > 0 else 0.5
        wmkt = math.log1p(max(0.0, m.get("vol") or tot_usd)) * max(0.05, 4.0 * pbar * (1.0 - pbar))
        for w, toks in acc.items():
            tok, (usd, qty, usdts) = max(toks.items(), key=lambda kv: kv[1][0])
            if usd < MIN_STANCE_USD or qty <= 0:
                continue
            val = wm.get(tok)
            if val is None:
                continue
            entry = usd / qty + SLIP
            if not (ct.MIN_ENTRY_PRICE <= entry <= ct.MAX_ENTRY_PRICE):
                continue
            pnl = NOTIONAL * (val / entry - 1.0) if val > 0 else -NOTIONAL
            hold = max(0.0, (m["ts"] - usdts / usd) / 86400)
            sims[w].append((round(pnl, 3), round(hold, 1), round(wmkt, 3)))
        done += 1
        if done % 25 == 0:
            log(f"рынки {done}/{len(mkts)}, кошельков в пуле {len(sims)}")

    rows = score_rows(sims)
    Path("market_scan_results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"оценено кошельков (>= {MIN_MARKETS} рынков): {len(rows)}; q<= {FDR_Q}: "
        f"{sum(1 for r in rows if r['q'] <= FDR_Q)}")
    return rows


def _betacf(a, b, x):
    """Продолженная дробь для неполной беты (Numerical Recipes)."""
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = FPMIN if abs(d) < FPMIN else d
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = FPMIN if abs(d) < FPMIN else d
        c = 1.0 + aa / c
        c = FPMIN if abs(c) < FPMIN else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = FPMIN if abs(d) < FPMIN else d
        c = 1.0 + aa / c
        c = FPMIN if abs(c) < FPMIN else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def _ibeta(a, b, x):
    """Регуляризованная неполная бета I_x(a,b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
          + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(ln)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_sf(t: float, df: float) -> float:
    """Односторонний хвост Стьюдента P(T > t). Аналитический p вместо бутстрапного:
    бутстрап упирается в пол 1/(BOOT_N+1) и BH-поправка на тысячи кошельков глохнет."""
    if t < 0:
        return 1.0 - t_sf(-t, df)
    x = df / (df + t * t)
    return 0.5 * _ibeta(df / 2.0, 0.5, x)


def score_rows(sims: dict) -> list[dict]:
    """Скоринг: взвешенное по весам рынков среднее + бутстрап-p + ЭБ-усадка + BH-FDR.
    sims: wallet -> [(pnl, hold_days, w_mkt), ...].
    - взвешенное среднее: победы на весомых рынках значат больше;
    - p (односторонний, бутстрап): вероятность такого эджа при нулевом;
    - eb_mean: эмпирико-байесовская усадка среднего к популяционному — «100% на 6 рынках»
      стягивается к реальности пропорционально малости выборки;
    - q (Benjamini-Hochberg): контроль доли ложных открытий по ВСЕЙ просканированной
      популяции — ответ на множественное тестирование тысяч кошельков за прогон."""
    prelim = []
    for w, recs in sims.items():
        n = len(recs)
        if n < MIN_MARKETS:
            continue
        pl = [p for p, _h, _w in recs]
        wts = [max(1e-9, x) for _p, _h, x in recs]
        holds = sorted(h for _p, h, _w in recs)
        mean = sum(pl) / n
        var = sum((x - mean) ** 2 for x in pl) / max(1, n - 1)
        wmean = sum(p * x for p, x in zip(pl, wts)) / sum(wts)
        boots = []
        for _ in range(BOOT_N):
            idx = [random.randrange(n) for _ in range(n)]
            bw = sum(wts[i] for i in idx)
            boots.append(sum(pl[i] * wts[i] for i in idx) / bw)
        boots.sort()
        sd = math.sqrt(var)
        if sd < 1e-9:                                  # все исходы одинаковые
            p = 1e-9 if wmean > 0 else 1.0
        else:
            p = t_sf(wmean / (sd / math.sqrt(n)), n - 1)
        prelim.append({"wallet": w, "n_markets": n, "wins": sum(1 for x in pl if x > 0),
                       "sim_pnl": round(sum(pl), 2), "sim_mean": round(mean, 3),
                       "w_mean": round(wmean, 3), "var": var,
                       "p": max(p, 1e-12),
                       "ci_low": round(boots[int(0.05 * BOOT_N)], 3),
                       "med_hold_days": holds[n // 2]})
    if not prelim:
        return []
    # --- эмпирико-байесовская усадка: prior из самой популяции (метод моментов) ---
    means = [r["w_mean"] for r in prelim]
    grand = sum(means) / len(means)
    sw_num = sum((r["n_markets"] - 1) * r["var"] for r in prelim)
    sw_den = sum(r["n_markets"] - 1 for r in prelim)
    s2w = max(1e-9, sw_num / max(1, sw_den))                      # внутрикошельковый шум
    var_means = sum((x - grand) ** 2 for x in means) / max(1, len(means) - 1)
    mean_inv_n = sum(1.0 / r["n_markets"] for r in prelim) / len(prelim)
    # межкошельковый разброс; ПОЛ в 10% наблюдаемого разброса средних: в популяции из 99% шума
    # метод моментов уводит s2b в 0 и prior схлопывает ВСЕХ (включая настоящих) в среднее.
    # Открытия делает FDR (q), EB-усадка — для ранжирования/сайзинга, ей нельзя быть вырожденной.
    s2b = max(1e-9, 0.10 * var_means, var_means - s2w * mean_inv_n)
    for r in prelim:
        prec = r["n_markets"] / s2w
        r["eb_mean"] = round((r["w_mean"] * prec + grand / s2b) / (prec + 1.0 / s2b), 3)
        del r["var"]
    # --- BH-FDR по всем оценённым ---
    order = sorted(range(len(prelim)), key=lambda i: prelim[i]["p"])
    n_all = len(prelim)
    qv = [0.0] * n_all
    prev = 1.0
    for rank_from_end in range(n_all, 0, -1):
        i = order[rank_from_end - 1]
        q = min(prev, prelim[i]["p"] * n_all / rank_from_end)
        qv[i] = q
        prev = q
    for i, r in enumerate(prelim):
        r["q"] = round(qv[i], 4)
    prelim.sort(key=lambda r: (r["eb_mean"], -r["q"]), reverse=True)
    return prelim


def wallet_profile(s: requests.Session, addr: str) -> dict:
    """Один запрос activity(150) -> живость + доля спорт/погода + темп сделок."""
    try:
        evs = s.get(f"{DATA}/activity", params={"user": addr, "limit": 150, "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC"}, timeout=15).json() or []
    except Exception:  # noqa: BLE001
        return {"live": False, "blocked_share": 0.0, "trades_per_day": 0.0}
    tr = [e for e in evs if (e.get("type", "").upper() == "TRADE")]
    live = bool(evs) and (time.time() - int(evs[0].get("timestamp", 0))) <= LIVE_DAYS * 86400
    blocked = (sum(1 for e in tr if ct._blocked_reason(e.get("title") or "")) / len(tr)) if len(tr) >= 10 else 0.0
    rate = 0.0
    if len(tr) >= 30:
        span = int(tr[0].get("timestamp", 0)) - int(tr[-1].get("timestamp", 0))
        if span > 3600:
            rate = len(tr) / (span / 86400)
    # энтропия часа суток: у людей циркадные дыры (сон), у ботов равномерные 24/7.
    # Высокая энтропия + высокий темп = бот; сам по себе ни один признак не приговор.
    hent = 0.0
    if len(tr) >= 50:
        cnt = Counter((int(e.get("timestamp") or 0) // 3600) % 24 for e in tr)
        tot = sum(cnt.values())
        hent = -sum((c / tot) * math.log(c / tot) for c in cnt.values()) / math.log(24)
    bot = bool(len(tr) >= 50 and hent > 0.965 and rate > 30)
    return {"live": live, "blocked_share": blocked, "trades_per_day": round(rate, 1),
            "hour_entropy": round(hent, 3), "bot": bot}


def gate(rows: list[dict], s: requests.Session, have: set) -> list[dict]:
    """Гейты: FDR q<=порога + ЭБ-среднее>0, винрейт>=50%, быстрый оборот, не в списке,
    живой, не спорт, не гиперактивный, не бот (энтропия часа)."""
    adds = []
    for r in rows:
        if len(adds) >= TOP_ADD:
            break
        if r.get("q", 1.0) > FDR_Q or r.get("eb_mean", 0) <= 0:
            continue
        if r["wins"] / r["n_markets"] < 0.5 or r["wallet"] in have:
            continue
        if r["med_hold_days"] > MAX_MED_HOLD_DAYS:
            log(f"  {r['wallet'][:10]}… медленный оборот (медиана {r['med_hold_days']}д) — мимо")
            continue
        p = wallet_profile(s, r["wallet"])
        if not p["live"]:
            continue
        if p["blocked_share"] >= 0.6:
            log(f"  {r['wallet'][:10]}… спорт/погода-мажоритарный ({p['blocked_share']:.0%}) — мимо")
            continue
        if p["trades_per_day"] > MAX_TRADES_PER_DAY:
            log(f"  {r['wallet'][:10]}… гиперактивный ({p['trades_per_day']}/день) — мимо")
            continue
        if p["bot"]:
            log(f"  {r['wallet'][:10]}… бот-паттерн (энтропия часа {p['hour_entropy']}, "
                f"{p['trades_per_day']}/д) — мимо")
            continue
        adds.append(r)
        log(f"  + {r['wallet'][:10]}… рынков {r['n_markets']}, винрейт "
            f"{100 * r['wins'] // r['n_markets']}%, sim ${r['sim_pnl']}, eb {r['eb_mean']}, "
            f"q {r['q']}, оборот {r['med_hold_days']}д, темп {p['trades_per_day']}/д")
        time.sleep(0.3)
    return adds


def load_have() -> set:
    """Кто уже есть/был: watchlist + вручную удалённые (их не возвращаем)."""
    have = set()
    try:
        have |= {w.lower() for w in json.load(open("copy_watchlist.json", encoding="utf-8"))["watchlist"]}
    except Exception:  # noqa: BLE001
        pass
    try:
        srcs = json.load(open("wallet_sources.json", encoding="utf-8"))
        have |= {w for w, sl in srcs.items() if sl == "удалён-вручную"}
    except Exception:  # noqa: BLE001
        pass
    return have


def apply_local(adds: list[dict], source: str):
    """Прямая запись в watchlist/sources (для запуска НА СЕРВЕРЕ: hot-reload подхватит)."""
    if not adds:
        return
    p = Path("copy_watchlist.json")
    d = json.loads(p.read_text(encoding="utf-8"))
    have = {w.lower() for w in d.get("watchlist", [])}
    sp = Path("wallet_sources.json")
    try:
        srcs = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    except Exception:  # noqa: BLE001
        srcs = {}
    deleted = {w for w, sl in srcs.items() if sl == "удалён-вручную"}
    new = [a["wallet"] for a in adds if a["wallet"] not in have and a["wallet"] not in deleted]
    d["watchlist"].extend(new)
    d["count"] = len(d["watchlist"])
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    for w in new:
        srcs[w] = source
    sp.write_text(json.dumps(srcs, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"APPLY: добавлено в watchlist {len(new)} (дублей/удалённых пропущено {len(adds) - len(new)})")


def push_http(adds: list[dict], source: str, pw: str):
    """Заливка на сервер по HTTP (для локальных прогонов)."""
    if not adds:
        print("нечего добавлять")
        return
    r = requests.post(f"{SERVER}/api/add_wallet",
                      json={"pw": pw, "wallets": [a["wallet"] for a in adds], "source": source},
                      timeout=30)
    print(r.status_code, r.json())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", choices=list(POOLS), default="politics")
    ap.add_argument("--auto", action="store_true", help="пул по ротации дня года (для ночного cron)")
    ap.add_argument("--apply", action="store_true", help="писать watchlist напрямую (запуск на сервере)")
    ap.add_argument("--push", action="store_true", help="залить adds на сервер по HTTP (спросит пароль)")
    ap.add_argument("--cids", help="json-файл со списком conditionId — скан заданных рынков (скаут)")
    ap.add_argument("--source", default=None, help="метка источника (деф. скаут-<пул>)")
    args = ap.parse_args()

    LOGF.write_text("", encoding="utf-8")
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

    if args.push and not args.cids and Path("market_scan_adds.json").exists() and not args.auto:
        import getpass
        push_http(json.loads(Path("market_scan_adds.json").read_text(encoding="utf-8")),
                  args.source or "маркет-скан", getpass.getpass("пароль: "))
        return

    if args.cids:
        cid_list = json.loads(Path(args.cids).read_text(encoding="utf-8"))
        now = time.time()
        mkts = [{"cid": c, "q": "", "vol": 0, "ts": now} for c in cid_list]
        pool_name = "cids"
        # для cids ts рынка неизвестен -> hold считается от сегодняшней даты (резолв уже был);
        # гейт оборота у скаута мягче не делаем — кто прошёл, тот прошёл
    else:
        pool_name = POOL_ORDER[datetime.now(timezone.utc).timetuple().tm_yday % len(POOL_ORDER)] \
            if args.auto else args.pool
        pool = POOLS[pool_name]
        log(f"ПУЛ: {pool_name} (окно {pool['days']}д, vol>={pool['vol']})")
        mkts = pool_markets(s, pool)
    log(f"рынков к разбору: {len(mkts)}")
    if not mkts:
        log("рынков нет — выход")
        return

    rows = scan(mkts, s)
    adds = gate(rows, s, load_have())
    Path("market_scan_adds.json").write_text(
        json.dumps(adds, ensure_ascii=False, indent=1), encoding="utf-8")
    source = args.source or f"скаут-{pool_name}"
    log(f"ИТОГ [{pool_name}]: кандидатов к добавлению {len(adds)}")
    if args.apply:
        apply_local(adds, source)


if __name__ == "__main__":
    main()
