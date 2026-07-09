#!/usr/bin/env python3
"""
live_executor.py — ЛОКАЛЬНЫЙ исполнитель реальных сделок на Polymarket (новый SDK).

ЗАПУСКАЕТСЯ ТОЛЬКО НА ТВОЁМ ПК. Приватный ключ читается из локального .env
(polymarket.env рядом со скриптом) и НИКОГДА не уходит на сервер/в репозиторий.
Скрипт ходит на наш сервер лишь за СИГНАЛАМИ (что купила core-когорта) — это
публичные данные, секретов там нет. Подпись ордера и ключ остаются на этой машине.

ВАЖНО: старый py-clob-client Polymarket ЗАБРОСИЛ (ордера отбиваются
'invalid order version'). Работаем на новом официальном SDK:
    pip install --pre polymarket-client            (пакет polymarket-client, импорт polymarket)

СТУПЕНЧАТЫЙ ЗАПУСК (по возрастанию доверия, флаги в polymarket.env):
  MODE=dry        — ничего не шлём, только печатаем «что бы купили» (по умолчанию);
  MODE=smoke      — ОДНА живая сделка на PER_TRADE_USD и стоп (проверить ключ+фонд+филл);
  MODE=live       — реальное исполнение с лимитами (см. ниже).

Лимиты (в live): DEPOSIT, PER_TRADE_USD, DAILY_MAX_USD, MAX_PRICE.
Гварды: форвард-с-запуска (НЕ отыгрываем бэклог), дедуп t+tok, дневной лимит,
край цены, остаток депозита.

Установка (один раз, на своём ПК):
  pip install --pre polymarket-client python-dotenv requests
  copy polymarket.env.example polymarket.env   — впиши PRIVATE_KEY и FUNDER
  python live_executor.py                       — стартует в dry (сделок нет)
"""
import json
import math
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "live_exec_state.json"      # что уже исполнено + счётчики дня (локально)


def load_env():
    """Читаем polymarket.env (KEY=VALUE). Секреты только отсюда, не из кода."""
    cfg = {}
    p = HERE / "polymarket.env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    cfg.update({k: v for k, v in os.environ.items() if k in (
        "MODE", "PRIVATE_KEY", "SERVER", "DEPOSIT", "PER_TRADE_USD",
        "DAILY_MAX_USD", "MAX_PRICE", "POLL_SEC", "GROUP", "FUNDER", "MAX_AGE_SEC")})
    return cfg


def st_load():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"done": [], "last_t": 0, "spent_total": 0.0, "day": "", "spent_day": 0.0}


def st_save(s):
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def sig_key(sig):
    return f"{sig['t']}|{sig['tok']}"


def resp_ok(resp):
    """Успех ордера по ответу нового SDK (OrderResponse — типизированный объект, не dict)."""
    for attr in ("success",):
        v = getattr(resp, attr, None)
        if v is True:
            return True
        if v is False:
            return False
    st = getattr(resp, "status", None)
    if st is not None:
        return str(st).lower() in ("matched", "live", "filled", "success", "delayed", "unmatched")
    if getattr(resp, "order_id", None) or getattr(resp, "id", None):
        return True
    return True                                      # исключения не было -> считаем принятым


def run_loop(mode, client, server, group, deposit, per_trade, daily_max, max_price, poll, max_age):
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    state = st_load()
    smoke_done = False
    primed = False                                    # форвард на КАЖДОМ запуске, не только первом

    while True:
        try:
            r = s.get(f"{server}/api/signals", params={"g": group, "since": state["last_t"]},
                      timeout=25).json()
        except Exception as ex:  # noqa: BLE001
            print(f"[{time.strftime('%H:%M:%S')}] сервер недоступен ({ex})", flush=True)
            time.sleep(poll)
            continue
        if r.get("paused"):
            print(f"[{time.strftime('%H:%M:%S')}] копирование на ПАУЗЕ — ждём", flush=True)
            time.sleep(poll)
            continue

        srv_now = int(r.get("now") or time.time())
        # ФОРВАРД С МОМЕНТА ЗАПУСКА (на ЛЮБОМ старте): первый опрос НЕ отыгрывает бэклог — точку
        # отсчёта прыгаем на серверное «сейчас». Иначе после паузы/рестарта бот скупил бы всё,
        # что случилось, пока он был выключен, по уже уехавшим ценам.
        if not primed:
            primed = True
            state["last_t"] = max(state["last_t"], srv_now)
            st_save(state)
            print(f"[{time.strftime('%H:%M:%S')}] старт: форвард с этой точки, "
                  f"бэклог ({len(r.get('signals', []))} сигналов) пропущен. Жду новые сделки ядра…",
                  flush=True)
            time.sleep(poll)
            continue

        today = time.strftime("%Y-%m-%d")
        if state.get("day") != today:                # новый день -> сброс дневного счётчика
            state["day"] = today
            state["spent_day"] = 0.0

        for sig in r.get("signals", []):
            k = sig_key(sig)
            state["last_t"] = max(state["last_t"], sig["t"])
            if k in state["done"]:
                continue
            px = sig.get("px") or 0
            title = (sig.get("title") or "")[:48]
            # --- гварды ---
            age = srv_now - sig.get("t", srv_now)
            if age > max_age:                         # СТАРЫЙ сигнал (цена уже уехала) — не копируем
                print(f"  skip (старый {round(age/60,1)} мин > {round(max_age/60,1)} мин): {title}",
                      flush=True)
                state["done"].append(k)
                continue
            if px <= 0 or px > max_price:
                print(f"  skip (цена {px} вне 0..{max_price}): {title}", flush=True)
                state["done"].append(k)
                continue
            if state["spent_total"] + per_trade > deposit:
                print(f"  СТОП: депозит ${deposit} исчерпан (потрачено ${state['spent_total']:.2f})",
                      flush=True)
                st_save(state)
                if mode != "live":
                    return
                time.sleep(poll)
                break
            if state["spent_day"] + per_trade > daily_max:
                print(f"  дневной лимит ${daily_max} достигнут — пауза до завтра", flush=True)
                break

            line = f"{sig.get('out', ''):>3} '{title}' @ ~{px}  ${per_trade} (tok…{sig['tok'][-6:]})"

            if mode == "dry":
                print(f"  [DRY] купил бы: {line}", flush=True)
                state["done"].append(k)
                continue

            if mode == "smoke" and smoke_done:
                print("  [SMOKE] одна сделка уже сделана — стоп. Проверь филл в Polymarket.",
                      flush=True)
                st_save(state)
                return

            # --- РЕАЛЬНЫЙ РЫНОЧНЫЙ ОРДЕР: BUY на per_trade $, FAK, потолок цены ---
            # потолок = цена сигнала + ~2 цента, ОКРУГЛЁННЫЙ ВВЕРХ ДО ЦЕНТА (2 знака): рынки с
            # шагом 0.01 требуют максимум 2 знака после запятой (иначе tick-size ошибка и ордер
            # не проходит — так терялось ~2/3 сигналов). 2-значная цена конформна и тику 0.001.
            cap = min(max_price, math.ceil((px + 0.02) * 100 - 1e-9) / 100)
            try:
                resp = client.place_market_order(
                    token_id=sig["tok"], side="BUY", amount=float(per_trade),
                    order_type="FAK", max_price=cap)
                ok = resp_ok(resp)
                print(f"  [{'SMOKE' if mode == 'smoke' else 'LIVE'}] ордер: {line} -> {resp!r}",
                      flush=True)
                if ok:
                    state["done"].append(k)
                    state["spent_total"] = round(state["spent_total"] + per_trade, 2)
                    state["spent_day"] = round(state["spent_day"] + per_trade, 2)
                    smoke_done = True
                    st_save(state)
                    if mode == "smoke":
                        print(f"  [SMOKE] ГОТОВО. Потрачено ~${per_trade}. Проверь позицию на "
                              f"polymarket.com, потом ставь MODE=live.", flush=True)
                        return
            except Exception as ex:  # noqa: BLE001
                print(f"  !! ордер не прошёл ({ex}) — сигнал НЕ помечен, повторим позже", flush=True)

        if len(state["done"]) > 4000:                # держим список исполненного компактным
            state["done"] = state["done"][-2000:]
        st_save(state)
        time.sleep(poll)


def main():
    cfg = load_env()
    mode = (cfg.get("MODE") or "dry").lower()
    server = cfg.get("SERVER") or "http://144.31.197.121:5000"
    group = cfg.get("GROUP") or "core"
    deposit = float(cfg.get("DEPOSIT") or 100)
    per_trade = float(cfg.get("PER_TRADE_USD") or 1)
    daily_max = float(cfg.get("DAILY_MAX_USD") or 20)
    max_price = float(cfg.get("MAX_PRICE") or 0.92)
    poll = int(cfg.get("POLL_SEC") or 30)
    max_age = int(cfg.get("MAX_AGE_SEC") or 300)     # старше — не копируем (цена уехала)

    print(f"=== live_executor | MODE={mode.upper()} | депозит ${deposit} | "
          f"ставка ${per_trade} | дневной лимит ${daily_max} | свежесть <={max_age//60}мин ===",
          flush=True)
    print(f"сигналы: {server}/api/signals?g={group}", flush=True)

    if mode == "dry":
        run_loop(mode, None, server, group, deposit, per_trade, daily_max, max_price, poll, max_age)
        return

    # smoke/live: нужен ключ + новый SDK + адрес депозита (FUNDER)
    pk = cfg.get("PRIVATE_KEY", "")
    funder = (cfg.get("FUNDER") or "").strip()
    if not pk or len(pk) < 40:
        print("!! MODE требует PRIVATE_KEY в polymarket.env — стоп. Оставайся в MODE=dry.", flush=True)
        return
    if not funder:
        print("!! MODE требует FUNDER (твой адрес депозита на polymarket.com) в polymarket.env — стоп.",
              flush=True)
        return
    try:
        from polymarket import SecureClient
    except ImportError:
        print("!! нет нового SDK. Установи: pip install --pre polymarket-client", flush=True)
        return
    try:
        client = SecureClient.create(private_key=pk, wallet=funder)
    except Exception as ex:  # noqa: BLE001
        print(f"!! не удалось создать клиент: {ex}", flush=True)
        return
    print(f"кошелёк-депозит: {funder}", flush=True)
    with client:                                     # SDK-клиент как контекст (сессия/очистка)
        run_loop(mode, client, server, group, deposit, per_trade, daily_max, max_price, poll, max_age)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено (Ctrl+C). Прогресс сохранён.", flush=True)
        sys.exit(0)
