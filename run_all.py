#!/usr/bin/env python3
"""Супервизор: единая точка запуска всей системы для работы 24/7 (локально или в облаке).
Держит живыми: (1) дашборд копи; (2) демон снимков/аудита. Перезапускает упавшее.
Грейсфул-стоп по SIGTERM/SIGINT. Под Docker это ENTRYPOINT — контейнер сам себя чинит."""
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
HOST = os.environ.get("DASH_HOST", "0.0.0.0")
PORT = os.environ.get("DASH_PORT", "5000")
BANKROLL = os.environ.get("BANKROLL", "100000")
PER_TRADE = os.environ.get("PER_TRADE", "100")
INTERVAL = os.environ.get("INTERVAL", "120")

SERVICES = {
    "dashboard": [PY, "copy_dashboard.py", "--from-watchlist", "copy_watchlist.json",
                  "--bankroll", BANKROLL, "--per-trade", PER_TRADE, "--interval", INTERVAL,
                  "--state", "paper_book.json", "--port", PORT, "--host", HOST],
    "snapshot": [PY, "perf_snapshot.py"],
}

procs: dict = {}
stopping = False


def env_for(name):
    e = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if name == "snapshot":
        e["SUPERVISED"] = "1"          # живость дашборда держит супервизор
    return e


def start(name):
    log = open(os.path.join(HERE, f"{name}.log"), "ab")
    procs[name] = subprocess.Popen(SERVICES[name], cwd=HERE, env=env_for(name),
                                   stdout=log, stderr=log)
    print(f"[supervisor] старт {name} pid={procs[name].pid}", flush=True)


def shutdown(*_):
    global stopping
    stopping = True
    print("[supervisor] остановка…", flush=True)
    for name, p in procs.items():
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    for name in SERVICES:
        start(name)
        time.sleep(3)
    print("[supervisor] всё запущено. Слежу за процессами…", flush=True)
    while not stopping:
        for name, p in list(procs.items()):
            if p.poll() is not None:    # процесс умер -> поднимаем
                print(f"[supervisor] {name} упал (код {p.returncode}) -> перезапуск", flush=True)
                time.sleep(2)
                start(name)
        time.sleep(5)


if __name__ == "__main__":
    main()
