#!/usr/bin/env python3
"""
live_executor.py — ЛОКАЛЬНЫЙ исполнитель реальных сделок на Polymarket.

ЗАПУСКАЕТСЯ ТОЛЬКО НА ТВОЁМ ПК. Приватный ключ читается из локального .env
(polymarket.env рядом со скриптом) и НИКОГДА не уходит на сервер/в репозиторий.
Скрипт ходит на наш сервер лишь за СИГНАЛАМИ (что купила core-когорта) — это
публичные данные, секретов там нет. Подпись ордера и ключ остаются на этой машине.

СТУПЕНЧАТЫЙ ЗАПУСК (по возрастанию доверия, флаги в polymarket.env):
  MODE=dry        — ничего не шлём, только печатаем «что бы купили» (по умолчанию);
  MODE=smoke      — ОДНА живая сделка на $1 и стоп (проверить связку ключ+API+филл);
  MODE=live       — реальное исполнение с лимитами (см. ниже).

Лимиты (действуют в live): DEPOSIT, PER_TRADE_USD, DAILY_MAX_USD, MAX_PRICE.
Гварды: не торгуем дважды один сигнал (t+tok), не превышаем дневной лимит,
не входим у края цены, не тратим больше остатка депозита.

Установка (один раз, на своём ПК):
  pip install py-clob-client python-dotenv requests
  создай polymarket.env (см. образец polymarket.env.example) — впиши PRIVATE_KEY
  python live_executor.py           # стартует в dry — увидишь сигналы без сделок
"""
import json
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
        "DAILY_MAX_USD", "MAX_PRICE", "POLL_SEC", "GROUP",
        "SIGNATURE_TYPE", "FUNDER")})
    return cfg


def st_load():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"done": [], "last_t": 0, "spent_total": 0.0, "day": "", "spent_day": 0.0}


def st_save(s):
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def sig_key(sig):
    return f"{sig['t']}|{sig['tok']}"


_NEG = {}


def is_neg_risk(s, cid):
    """neg-risk рынки Polymarket (мультибрекеты) торгуются на ОТДЕЛЬНОМ контракте-бирже —
    ордер надо строить с флагом neg_risk, иначе CLOB отбивает 'invalid order version'.
    Определяем по метаданным рынка, кэшируем по conditionId."""
    if not cid:
        return False
    if cid in _NEG:
        return _NEG[cid]
    try:
        m = s.get(f"https://clob.polymarket.com/markets/{cid}", timeout=15).json()
        nr = bool(m.get("neg_risk"))
    except Exception:  # noqa: BLE001
        nr = False
    _NEG[cid] = nr
    return nr


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

    print(f"=== live_executor | MODE={mode.upper()} | депозит ${deposit} | "
          f"ставка ${per_trade} | дневной лимит ${daily_max} ===", flush=True)
    print(f"сигналы: {server}/api/signals?g={group}", flush=True)

    client = None
    if mode in ("smoke", "live"):
        pk = cfg.get("PRIVATE_KEY", "")
        if not pk or len(pk) < 40:
            print("!! MODE требует PRIVATE_KEY в polymarket.env — стоп. "
                  "Оставайся в MODE=dry, пока ключ не задан.", flush=True)
            return
        try:
            from py_clob_client.client import ClobClient          # noqa: F401
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import BUY
        except ImportError:
            print("!! нет py-clob-client. Установи: pip install py-clob-client", flush=True)
            return
        host = "https://clob.polymarket.com"
        funder = (cfg.get("FUNDER") or "").strip()
        sig_type = int(cfg.get("SIGNATURE_TYPE") or 0)
        if funder:
            # аккаунт Polymarket через почту/Google/Magic или браузерный кошелёк: средства на
            # ПРОКСИ-кошельке, ключ лишь подписант. signature_type=1 (email/magic) или 2 (браузер),
            # funder = адрес прокси (твой адрес на polymarket.com).
            client = ClobClient(host, key=pk, chain_id=137,
                                signature_type=sig_type, funder=funder)
            print(f"режим прокси: signature_type={sig_type}, funder={funder}", flush=True)
        else:
            client = ClobClient(host, key=pk, chain_id=137)   # обычный EOA-кошелёк (ключ = адрес)
        try:
            client.set_api_creds(client.create_or_derive_api_creds())
            addr = client.get_address()
            print(f"подписант (ключ): {addr}", flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"!! не удалось инициализировать CLOB-клиент: {ex}", flush=True)
            return
        globals().update(OrderArgs=OrderArgs, OrderType=OrderType, BUY=BUY,
                         PartialCreateOrderOptions=PartialCreateOrderOptions)

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    state = st_load()
    smoke_done = False

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

        # ФОРВАРД С МОМЕНТА ЗАПУСКА: на самом первом опросе НЕ отыгрываем бэклог (иначе бот
        # накупил бы десятки СТАРЫХ сигналов реальными деньгами). Ставим точку отсчёта =
        # серверное «сейчас» и действуем только на сделки, случившиеся ПОСЛЕ старта.
        if state["last_t"] == 0:
            state["last_t"] = int(r.get("now") or time.time())
            st_save(state)
            print(f"[{time.strftime('%H:%M:%S')}] старт: форвард с этой точки, "
                  f"бэклог ({len(r.get('signals', []))} сигналов) пропущен. Жду новые сделки ядра…",
                  flush=True)
            time.sleep(poll)
            continue

        today = time.strftime("%Y-%m-%d")
        if state.get("day") != today:                    # новый день -> сброс дневного счётчика
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

            size = round(per_trade / px, 2)              # доля токена на per_trade долларов
            line = (f"{sig.get('out', ''):>3} '{title}' @ {px}  ${per_trade} "
                    f"(~{size} шт, tok…{sig['tok'][-6:]})")

            if mode == "dry":
                print(f"  [DRY] купил бы: {line}", flush=True)
                state["done"].append(k)
                continue

            # smoke: только ОДНА сделка и выход
            if mode == "smoke" and smoke_done:
                print("  [SMOKE] одна сделка уже сделана — стоп. Проверь филл в Polymarket.",
                      flush=True)
                st_save(state)
                return

            try:                                          # РЕАЛЬНЫЙ ОРДЕР (FOK — заполнить сразу или отменить)
                neg = is_neg_risk(s, sig.get("cid", ""))   # neg-risk рынок -> отдельная сборка ордера
                args = OrderArgs(price=min(max_price, round(px + 0.01, 3)),  # +1 цент запас на филл
                                 size=size, side=BUY, token_id=sig["tok"])
                signed = client.create_order(args, PartialCreateOrderOptions(neg_risk=neg))
                resp = client.post_order(signed, OrderType.FOK)
                ok = bool(resp and (resp.get("success") or resp.get("orderID") or resp.get("status")))
                print(f"  [{'SMOKE' if mode=='smoke' else 'LIVE'}] ордер: {line} -> {resp}", flush=True)
                if ok:
                    state["done"].append(k)
                    state["spent_total"] = round(state["spent_total"] + per_trade, 2)
                    state["spent_day"] = round(state["spent_day"] + per_trade, 2)
                    smoke_done = True
                    st_save(state)
                    if mode == "smoke":
                        print(f"  [SMOKE] ГОТОВО. Потрачено ${per_trade}. Проверь позицию на "
                              f"polymarket.com, потом ставь MODE=live.", flush=True)
                        return
            except Exception as ex:  # noqa: BLE001
                print(f"  !! ордер не прошёл ({ex}) — сигнал НЕ помечен, повторим позже", flush=True)

        # держим список исполненного компактным
        if len(state["done"]) > 4000:
            state["done"] = state["done"][-2000:]
        st_save(state)
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено (Ctrl+C). Прогресс сохранён.", flush=True)
        sys.exit(0)
