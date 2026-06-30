#!/usr/bin/env python3
"""
daily_lb_scan.py — ежедневный авто-скан лидерборда Polymarket.

Логика:
  1) собрать все срезы лидерборда (profit+volume × all/30d/1d);
  2) оставить живых (сделка <= LIVE_DAYS) и не уже в watchlist;
  3) прогнать через wallet_analyzer (тот же строгий фильтр);
  4) добавить прошедших с resolved_pnl>0 в copy_watchlist.json.

Дашборд перечитывает watchlist на каждом цикле -> рестарт НЕ нужен.
Запускать по расписанию (Windows Task Scheduler) раз в день.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
WATCHLIST = HERE / "copy_watchlist.json"
CKPT = HERE / "daily_lb.jsonl"
CAND = HERE / "daily_lb.jsonl.candidates.json"
REG = HERE / "daily_lb_registry.json"
SRC = HERE / "wallet_sources.json"
LOG = HERE / "daily_lb_scan.log"
LIVE_DAYS = 14


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def harvest() -> dict:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    pool = {}
    for metric in ("profit", "volume"):
        for w in ("all", "30d", "1d"):
            try:
                r = s.get(f"https://lb-api.polymarket.com/{metric}",
                          params={"window": w, "limit": 500}, timeout=25)
                if r.status_code == 200:
                    for it in r.json():
                        a = (it.get("proxyWallet") or "").lower()
                        if a:
                            pool[a] = it.get("name") or it.get("pseudonym") or ""
            except requests.RequestException:
                pass
            time.sleep(0.2)
    return pool


def is_live(addr: str, s: requests.Session) -> bool:
    try:
        evs = s.get("https://data-api.polymarket.com/activity",
                    params={"user": addr, "limit": 1, "sortBy": "TIMESTAMP",
                            "sortDirection": "DESC"}, timeout=15).json() or []
    except requests.RequestException:
        return False
    if not evs:
        return False
    return (int(time.time()) - int(evs[0].get("timestamp", 0))) <= LIVE_DAYS * 86400


SPORT_KW = ("vs.", "vs ", "o/u", "score:", " win on ", "both teams", "pole position", "1st half",
            "spread", "eliminated in", "to score", "corner", "hattrick", "yard box", "round of",
            "quarterfinal", "semifinal", "fifa", "world cup", " nba", " nfl", " ufc", "group stage",
            "penalty", "clean sheet", " draw")


def sport_majority(addr: str, s: requests.Session) -> bool:
    """True если большинство реальных сделок кошелька — спорт (не добавляем такие)."""
    try:
        evs = s.get("https://data-api.polymarket.com/activity",
                    params={"user": addr, "limit": 150, "sortBy": "TIMESTAMP",
                            "sortDirection": "DESC"}, timeout=15).json() or []
    except requests.RequestException:
        return False
    tr = [e for e in evs if (e.get("type", "").upper() == "TRADE")]
    if len(tr) < 10:
        return False
    sp = sum(1 for e in tr if any(k in (e.get("title") or "").lower() for k in SPORT_KW))
    return sp / len(tr) >= 0.6


def main() -> None:
    wl_doc = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    have = {w.lower() for w in wl_doc["watchlist"]}

    pool = harvest()
    log(f"лидерборд: {len(pool)} уникальных")

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    cand = [a for a in pool if a not in have and is_live(a, s)]
    log(f"живых новых кандидатов: {len(cand)}")
    if not cand:
        log("новых нет — выход")
        return

    # свежий прогон: чистим прошлый чекпоинт, кэш кандидатов задаём сами (дискавери пропускается)
    CKPT.unlink(missing_ok=True)
    CAND.write_text(json.dumps(cand), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with open(HERE / "daily_lb_analyzer.log", "wb") as af:
        subprocess.run([sys.executable, str(HERE / "wallet_analyzer.py"),
                        "--checkpoint", str(CKPT), "--out", str(REG)],
                       cwd=str(HERE), env=env, stdout=af, stderr=af, check=False)

    cards = {}
    for line in CKPT.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            cards[d["wallet"].lower()] = d
        except Exception:  # noqa: BLE001
            pass
    def copy_ok(c):
        n = c.get("n_resolved", 0)
        return (c.get("resolved_pnl", 0) > 0 and c.get("roi_ci_low", 0) > 0
                and c.get("reward_share", 1) <= 0.15 and c.get("top1_concentration", 1) <= 0.60
                and not c.get("truncated") and c.get("total_staked", 0) >= 3000
                and n >= 15 and (c.get("total_staked", 0) / n) >= 20)

    passed = [c for c in cards.values() if copy_ok(c) and c["wallet"].lower() not in have]
    passed.sort(key=lambda c: c.get("composite", 0), reverse=True)
    # исключаем спорт по реальной активности (спорт больше не добавляем)
    new = [c for c in passed if not sport_majority(c["wallet"].lower(), s)]
    dropped = len(passed) - len(new)

    if not new:
        log(f"прошли фильтр {len(passed)} (спорт отсеяно {dropped}), к добавлению 0")
        return

    wl_doc["watchlist"].extend(c["wallet"] for c in new)
    wl_doc["count"] = len(wl_doc["watchlist"])
    WATCHLIST.write_text(json.dumps(wl_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    # провенанс для колонки «Источник» на дашборде
    try:
        sp = json.loads(SRC.read_text(encoding="utf-8")) if SRC.exists() else {}
        for c in new:
            sp[c["wallet"].lower()] = "лидерборд-авто"
        SRC.write_text(json.dumps(sp, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    names = ", ".join(f"{c['wallet'][:10]}…(roi {c.get('roi',0)*100:.0f}%, n={c.get('n_resolved',0)})"
                      for c in new)
    log(f"ДОБАВЛЕНО {len(new)} (спорт отсеяно {dropped}): {names} -> watchlist теперь {wl_doc['count']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"ОШИБКА: {e}")
        raise
