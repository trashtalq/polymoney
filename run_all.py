#!/usr/bin/env python3
"""Супервизор: единая точка запуска всей системы для работы 24/7 (локально или в облаке).
Держит живыми: (1) дашборд копи; (2) демон снимков/аудита. Перезапускает упавшее.
Грейсфул-стоп по SIGTERM/SIGINT. Под Docker это ENTRYPOINT — контейнер сам себя чинит."""
import os
import signal
import subprocess
import sys
import threading
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


def daily_scheduler():
    """Раз в сутки гоняет авто-скан лидерборда (со встроенным исключением спорта).
    Без cron — отдельным потоком супервизора. Первый запуск через 15 мин после старта."""
    if os.environ.get("DAILY_SCAN", "1") != "1":
        return
    time.sleep(900)
    while not stopping:
        try:
            with open(os.path.join(HERE, "daily_lb_scan.log"), "ab") as lg:
                subprocess.run([PY, "daily_lb_scan.py"], cwd=HERE,
                               env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                               stdout=lg, stderr=lg)
        except Exception as e:  # noqa: BLE001
            print(f"[scheduler] daily_lb_scan ошибка: {e}", flush=True)
        for _ in range(24 * 60):          # спим сутки, чутко к остановке
            if stopping:
                return
            time.sleep(60)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=daily_scheduler, daemon=True).start()
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
