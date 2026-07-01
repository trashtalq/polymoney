#!/usr/bin/env python3
"""Ночной демон: (1) ЧАСОВЫЕ снимки форвардной статистики по кошелькам в perf_history_*.jsonl
(временной ряд для трендовой прополки); (2) honest-fill аудит (плата за копи-задержку);
(3) сторож живости — поднимает дашборд, если порт не отвечает.

Снимки берём из /api/state (там уже посчитаны realized/unrealized/per_wallet), liveness = доступность API."""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

HERE = __file__.rsplit("\\", 1)[0] if "\\" in __file__ else "."
INTERVAL = 3600
TARGETS = [
    {"port": 5000, "name": "main", "wl": "copy_watchlist.json", "state": "paper_book.json", "log": "dashboard.log"},
    # спорт-контур (:5001) отключён по решению — спорт дорого копировать (задержка +4.4%)
]
LOG = "perf_snapshot.log"


def log(m):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z] {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fill_cost(state_file):
    """Плата за копи-задержку на входах: переплата против цены цели ($ и %)."""
    try:
        b = json.load(open(state_file, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    spend = extra = 0.0
    for r in b.get("log", []):
        if r.get("act") == "BUY" and r.get("px") and r.get("their_px"):
            px, tp, sp = r["px"], r["their_px"], r.get("spend", 0) or 0
            if px > 0 and tp > 0:
                spend += sp
                extra += sp * (px - tp) / px
    return {"spend": round(spend, 2), "delay_cost": round(extra, 2),
            "delay_pct": round(extra / spend * 100, 3) if spend else 0.0}


def snapshot(t):
    d = requests.get(f"http://localhost:{t['port']}/api/state", timeout=25).json()
    rec = {
        "t": int(time.time()), "iso": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "port": t["port"], "realized": d.get("realized"), "unrealized": d.get("unrealized"),
        "pnl": d.get("pnl"), "n_open": d.get("n_open"), "n_copied": d.get("n_copied"),
        "topups": d.get("topups"), "fill": fill_cost(t["state"]),
        "wallets": [{"w": w["wallet"], "src": w.get("source"), "real": w["realized"],
                     "unreal": w["unrealized"], "total": w["total"], "closed": w["closed"],
                     "open": w.get("open_n"), "spent": w.get("spent"), "flag": w.get("flag")}
                    for w in d.get("per_wallet", [])],
    }
    with open(f"perf_history_{t['port']}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fc = rec["fill"] or {}
    log(f"[{t['name']}] снимок: реализ ${rec['realized']} нереализ ${rec['unrealized']} "
        f"откр {rec['n_open']} | задержка-кост ${fc.get('delay_cost')} ({fc.get('delay_pct')}%) "
        f"| кошельков {len(rec['wallets'])}")


def restart(t):
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env = None
    import os
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    out = open(t["log"], "ab")
    subprocess.Popen([sys.executable, "copy_dashboard.py",
                      "--from-watchlist", t["wl"], "--bankroll", "1000", "--per-trade", "1",
                      "--interval", "120", "--state", t["state"], "--port", str(t["port"])],
                     stdout=out, stderr=out, creationflags=flags, env=env, close_fds=True)
    log(f"[{t['name']}] дашборд не отвечал -> перезапущен на :{t['port']}")


def main():
    log("=== демон снимков+сторож запущен (интервал 1ч) ===")
    while True:
        for t in TARGETS:
            try:
                snapshot(t)
            except Exception as e:  # noqa: BLE001
                log(f"[{t['name']}] /api/state недоступен ({e})")
                import os
                if os.environ.get("SUPERVISED"):
                    continue               # под run_all живость держит супервизор — не дублируем
                try:
                    restart(t)
                    time.sleep(15)
                    snapshot(t)            # снять сразу после подъёма
                except Exception as e2:  # noqa: BLE001
                    log(f"[{t['name']}] не удалось перезапустить/снять: {e2}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
