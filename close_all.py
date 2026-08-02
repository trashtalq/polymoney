#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
close_all.py — ЗАКРЫТЬ ВСЕ открытые позиции на Polymarket (ликвидация счёта).

ЗАПУСКАЕШЬ ТОЛЬКО ТЫ, СО СВОЕГО ПК. Ключ читается из polymarket.env и никуда не уходит.

БЕЗОПАСНОСТЬ ПО УМОЛЧАНИЮ:
  python close_all.py            -> СУХОЙ ПРОГОН: ничего не продаёт, только показывает план
  python close_all.py --live     -> реальная продажа, но СНАЧАЛА спросит подтверждение (CLOSE ALL)

Что делает: берёт открытые позиции с чейна, считает по РЕАЛЬНОМУ стакану, за сколько они уйдут
(лучший бид, с учётом глубины), пропускает пыль и рынки с провальным стаканом, продаёт FAK.

Резолвнутые позиции НЕ трогает: их не продают, а редимят (и если они сгорели — там $0).

Опции:
  --min-value 0.20   не продавать позиции дешевле $X (спред съест больше смысла)
  --max-slip 0.25    пропустить, если бид ниже мидпоинта более чем на 25% (пустой стакан)
  --only 0x…         закрыть только один токен (для проверки на одной позиции)
"""
import argparse
import json
import math
import sys
import time

import requests

import live_executor as le          # переиспользуем load_env / clob_fill / clob_mid


def f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_positions(s, funder):
    r = s.get(f"https://data-api.polymarket.com/positions?user={funder}&limit=500", timeout=25)
    d = r.json()
    return d if isinstance(d, list) else []


def main():
    ap = argparse.ArgumentParser(description="Закрыть все позиции на Polymarket")
    ap.add_argument("--live", action="store_true", help="реально продавать (иначе сухой прогон)")
    ap.add_argument("--min-value", type=float, default=0.20, help="не продавать дешевле $X")
    ap.add_argument("--max-slip", type=float, default=0.25, help="пропустить, если бид ниже мида на >X")
    ap.add_argument("--only", default="", help="только этот token_id")
    args = ap.parse_args()

    cfg = le.load_env()
    funder = (cfg.get("FUNDER") or "").strip()
    pk = (cfg.get("PRIVATE_KEY") or "").strip()
    if not funder:
        print("!! нет FUNDER в polymarket.env — не знаю, чей счёт закрывать. Стоп.")
        return 1

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    pos = fetch_positions(s, funder)
    live_pos = [p for p in pos if isinstance(p, dict) and not p.get("redeemable")]
    if args.only:
        live_pos = [p for p in live_pos if str(p.get("asset")) == args.only]
    if not live_pos:
        print("Открытых позиций нет — закрывать нечего.")
        return 0

    print(f"Счёт {funder}")
    print(f"Открытых позиций: {len(live_pos)}\n")
    print(f"{'рынок':<44}{'долей':>9}{'мид':>7}{'бид':>7}{'выручка$':>10}  примечание")
    print("-" * 88)

    plan, skipped = [], []
    total_now = total_get = 0.0
    for p in live_pos:
        tok = str(p.get("asset") or "")
        title = (p.get("title") or "")[:42]
        shares = math.floor(f(p.get("size")) * 100) / 100      # вниз, чтобы не оверселлить
        cur_val = f(p.get("currentValue"))
        total_now += cur_val
        if shares <= 0:
            continue
        mid = le.clob_mid(s, tok)
        fill = le.clob_fill(s, tok, "SELL", shares=shares)     # реальный VWAP по бидам с глубиной
        if not fill:
            skipped.append((title, "стакан пуст"))
            print(f"{title:<44}{shares:>9.2f}{(mid or 0):>7.3f}{'—':>7}{'—':>10}  ПРОПУСК: стакан пуст")
            continue
        vwap, got_sh, full = fill
        proceeds = vwap * got_sh
        note = ""
        if cur_val < args.min_value:
            skipped.append((title, f"пыль <${args.min_value}"))
            note = f"ПРОПУСК: пыль (<${args.min_value})"
        elif mid and vwap < mid * (1 - args.max_slip):
            skipped.append((title, "провальный стакан"))
            note = f"ПРОПУСК: бид на {(1 - vwap / mid) * 100:.0f}% ниже мида"
        elif not full:
            note = "частично (стакана мало)"
        print(f"{title:<44}{got_sh:>9.2f}{(mid or 0):>7.3f}{vwap:>7.3f}{proceeds:>10.2f}  {note}")
        if not note.startswith("ПРОПУСК"):
            plan.append({"tok": tok, "title": title, "shares": got_sh, "vwap": vwap,
                         "proceeds": proceeds})
            total_get += proceeds

    print("-" * 88)
    print(f"Сейчас позиции стоят (по мидам): ${total_now:.2f}")
    print(f"К ПОЛУЧЕНИЮ при продаже:        ${total_get:.2f}   "
          f"(потеря на спреде ~${total_now - total_get:.2f})")
    print(f"К продаже: {len(plan)} позиций · пропущено: {len(skipped)}")

    if not args.live:
        print("\n[СУХОЙ ПРОГОН] Ничего не продано.")
        print("Чтобы закрыть по-настоящему:  python close_all.py --live")
        return 0

    if not plan:
        print("\nНечего продавать.")
        return 0
    if not pk or len(pk) < 40:
        print("\n!! Для реальной продажи нужен PRIVATE_KEY в polymarket.env. Стоп.")
        return 1

    print(f"\n!!! РЕАЛЬНАЯ ПРОДАЖА {len(plan)} позиций примерно на ${total_get:.2f}. Это необратимо.")
    ans = input("Напиши ровно  CLOSE ALL  и нажми Enter (что угодно другое — отмена): ")
    if ans.strip() != "CLOSE ALL":
        print("Отменено — ничего не продано.")
        return 0

    try:
        from polymarket import SecureClient
    except ImportError:
        print("!! нет SDK: pip install --pre polymarket-client")
        return 1
    client = SecureClient.create(private_key=pk, wallet=funder)

    sold = fail = 0
    got = 0.0
    with client:
        for i, it in enumerate(plan, 1):
            try:
                resp = client.place_market_order(token_id=it["tok"], side="SELL",
                                                 shares=float(it["shares"]), order_type="FAK")
                ok = le.resp_ok(resp)
                if ok:
                    sold += 1
                    got += it["proceeds"]
                    print(f"[{i}/{len(plan)}] ПРОДАНО ~{it['shares']:.2f} долей "
                          f"(~${it['proceeds']:.2f}) — {it['title']}", flush=True)
                else:
                    fail += 1
                    print(f"[{i}/{len(plan)}] не прошло: {resp!r} — {it['title']}", flush=True)
            except Exception as ex:  # noqa: BLE001
                fail += 1
                print(f"[{i}/{len(plan)}] ОШИБКА ({ex}) — {it['title']}", flush=True)
            time.sleep(0.4)                                    # не долбим биржу

    print(f"\nИТОГ: продано {sold}, не удалось {fail}. Получено ~${got:.2f}.")
    print("Проверь баланс на polymarket.com. Не продавшееся можно повторить этим же скриптом.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(0)
