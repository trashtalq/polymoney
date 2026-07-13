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
ALLOW_FILE = HERE / "copy_allowlist.json"       # белый список кошельков (защита от инъекции сигналов)
_ALLOW = {"mtime": 0, "set": set()}


def load_allowlist() -> set:
    """Локальный белый список кошельков, которые бот СОГЛАСЕН копировать. Даже если наш сервер
    взломан и подсунул в /api/signals чужой кошелёк — его тут нет, и бот его НЕ копирует. Пустой
    список/нет файла = не настроен (не фильтруем). Перечитывается при изменении файла."""
    try:
        m = ALLOW_FILE.stat().st_mtime
        if m != _ALLOW["mtime"]:
            _ALLOW["set"] = {str(w).lower() for w in json.loads(ALLOW_FILE.read_text(encoding="utf-8"))}
            _ALLOW["mtime"] = m
    except Exception:  # noqa: BLE001
        pass
    return _ALLOW["set"]


def seed_allowlist(s, server, group):
    """Если файла нет — создаём из ТЕКУЩЕГО состава группы на сервере (доверие первого запуска).
    Дальше список НЕ обновляется автоматически: инъекция нового кошелька на сервер его не добавит."""
    if ALLOW_FILE.exists():
        return
    try:
        r = s.get(f"{server}/api/state", params={"g": group}, timeout=20).json()
        addrs = sorted({(w.get("wallet") or "").lower()
                        for w in r.get("per_wallet", []) if w.get("wallet")})
        if addrs:
            ALLOW_FILE.write_text(json.dumps(addrs, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"allowlist: создан из текущего ядра ({len(addrs)} кошельков). "
                  f"Новые кошельки добавляй через пульт.", flush=True)
        else:
            print("allowlist: ядро пустое — файл не создан, ЗАЩИТА ВЫКЛ (копирую всех).", flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"allowlist: не удалось засеять ({ex}) — ЗАЩИТА ВЫКЛ (копирую всех).", flush=True)


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
        "DAILY_MAX_USD", "MAX_PRICE", "POLL_SEC", "GROUP", "FUNDER", "MAX_AGE_SEC",
        "MAX_ENTRIES_PER_POS", "MAX_PER_WALLET_DAY", "MAX_RESOLVE_DAYS", "EXIT_MIN_FRAC",
        "SIGNALS_TOKEN")})
    return cfg


def st_load():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"done": [], "last_t": 0, "spent_total": 0.0, "day": "", "spent_day": 0.0}


def st_save(s):
    # атомарно (tmp + replace): обрыв в момент записи не бьёт файл «что уже исполнено» —
    # его порча означала бы ПОВТОРНЫЕ покупки уже отработанных сигналов
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def sig_key(sig):
    return f"{sig['t']}|{sig['tok']}"


_MKT_END = {}          # cid -> unix резолва (0 = неизвестно), кэш на процесс


def resolve_days(s, cid, now):
    """Сколько ДНЕЙ до резолва рынка (из CLOB end_date_iso). Бот держит позицию ДО резолва
    (выходы не мирроим), поэтому это и есть наш срок заморозки капитала. None = неизвестно."""
    if not cid:
        return None
    if cid not in _MKT_END:
        end = 0
        try:
            m = s.get(f"https://clob.polymarket.com/markets/{cid}", timeout=15).json()
            ed = (m or {}).get("end_date_iso") or ""
            if ed:
                from datetime import datetime
                end = datetime.fromisoformat(ed.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            end = 0
        _MKT_END[cid] = end
    end = _MKT_END[cid]
    return None if not end else (end - now) / 86400


def fetch_held(s, funder):
    """Реальные позиции по токенам с кошелька (data-api): {asset: {"usd": вложено, "shares": долей}}.
    $ — основа лимита входов в позу; shares — сколько ПРОДАТЬ при мирроринге выхода. Берётся с
    ЧЕЙНА -> переживает рестарт и видит входы др. кодом. None при ошибке (используем прошлый снимок)."""
    try:
        pos = s.get(f"https://data-api.polymarket.com/positions?user={funder}&limit=300",
                    timeout=20).json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(pos, list):
        return None
    out = {}
    for p in pos:
        a = str(p.get("asset") or "")
        if not a:
            continue
        cost = p.get("initialValue")
        if cost is None:
            cost = (p.get("size") or 0) * (p.get("avgPrice") or 0)
        d = out.setdefault(a, {"usd": 0.0, "shares": 0.0})
        d["usd"] += float(cost or 0)
        d["shares"] += float(p.get("size") or 0)
    return out


# ════════════ ТЕНЕВОЙ (PAPER) РЕЖИМ: тот же бот на виртуальный банкролл, реальные кэфы ════════════
PAPER_FILE = HERE / "paper15k_state.json"


def pst_load(bankroll):
    if PAPER_FILE.exists():
        try:
            return json.loads(PAPER_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"bankroll": bankroll, "cash": bankroll, "pos": {}, "meta": {},
            "realized": 0.0, "buys": 0, "sells": 0}


def pst_save(p):
    PAPER_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")


def paper_held(p):
    """held-снимок как fetch_held, но по ВИРТУАЛЬНЫМ позициям: {tok:{usd:вложено, shares:долей}}."""
    return {t: {"usd": v["cost"], "shares": v["shares"]} for t, v in p["pos"].items()}


def paper_equity(p, api):
    """Стоимость счёта = кэш + переоценка открытых по РЕАЛЬНЫМ мидпоинтам. Возвращает (equity, pnl)."""
    toks = list(p["pos"].keys())
    marks = {}
    if toks:
        try:
            marks = api.midpoints(toks)
        except Exception:  # noqa: BLE001
            marks = {}
    val = 0.0
    for t, v in p["pos"].items():
        m = marks.get(t)
        px = float(m) if m is not None else (v["cost"] / v["shares"] if v["shares"] else 0)
        val += v["shares"] * px
    eq = round(p["cash"] + val, 2)
    return eq, round(eq - p["bankroll"], 2)


class PaperClient:
    """Имитация SDK-клиента: «исполняет» ордера по РЕАЛЬНОЙ текущей цене (CLOB midpoint) в
    виртуальный портфель. Ключ/деньги НЕ нужны — только замер. Интерфейс как у SecureClient."""
    def __init__(self, pstate, api, slip=0.01):
        self.p = pstate
        self.api = api
        self.slip = slip

    def _mid(self, tok):
        try:
            m = self.api.midpoints([tok]).get(tok)
            return float(m) if m is not None else None
        except Exception:  # noqa: BLE001
            return None

    def place_market_order(self, token_id, side, amount=None, shares=None,
                           order_type=None, max_price=None):
        mid = self._mid(token_id)
        if mid is None or not (0 < mid < 1):
            raise Exception("paper: midpoint недоступен")
        if side == "BUY":
            fill = round(mid + self.slip, 4)
            if max_price and fill > max_price + 1e-9:      # как реальный FAK-no-fill -> спокойный пропуск
                raise Exception("no orders found to match with FAK order")
            sh = float(amount) / fill
            pos = self.p["pos"].setdefault(token_id, {"shares": 0.0, "cost": 0.0})
            pos["shares"] += sh
            pos["cost"] = round(pos["cost"] + float(amount), 4)
            self.p["cash"] = round(self.p["cash"] - float(amount), 2)
            self.p["buys"] += 1
        else:                                              # SELL
            pos = self.p["pos"].get(token_id)
            if pos and pos["shares"] > 0:
                sh = min(float(shares), pos["shares"])
                proceeds = sh * mid
                cost_part = pos["cost"] * (sh / pos["shares"])
                self.p["realized"] = round(self.p["realized"] + proceeds - cost_part, 2)
                self.p["cash"] = round(self.p["cash"] + proceeds, 2)
                pos["shares"] -= sh
                pos["cost"] = round(pos["cost"] - cost_part, 4)
                if pos["shares"] <= 1e-6:
                    self.p["pos"].pop(token_id, None)
                self.p["sells"] += 1
        return type("R", (), {"success": True})()

    def close(self):
        pass


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


def run_loop(mode, client, server, group, deposit, per_trade, daily_max, max_price, poll, max_age,
             max_entries, funder, max_per_wallet, max_resolve_days, exit_min_frac,
             signals_token="", min_price=0.12, paper=False, pstate=None):
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    if signals_token:                                 # сервер закрыл /api/signals токеном -> шлём его
        s.headers["X-Signals-Token"] = signals_token
    seed_allowlist(s, server, group)                  # белый список из текущего ядра, если ещё нет
    state = st_load()
    state.setdefault("spent_by_wallet", {})           # адрес цели -> $ потрачено за день (fallback для dry)
    state.setdefault("tok_wallet", {})                # токен -> кошелёк-источник (для лимита экспозиции)
    session_add = {}          # tok -> $ добавлено ЭТИМ процессом (страхует settle-лаг чейна)
    held = {}                 # tok -> $ реально вложено с кошелька (обновляется каждый цикл)
    smoke_done = False
    primed = False                                    # форвард на КАЖДОМ запуске, не только первом
    cap_usd = max_entries * per_trade                 # потолок $ на одну позу (напр. 2×$1=$2)

    def wallet_exposure(w):
        """Текущая экспозиция бота по кошельку w = сумма НАШИХ ЖИВЫХ позиций, скопированных с него
        (по чейну held + входы этого цикла), через карту токен->источник. Закрылась позиция (в плюс
        ИЛИ в минус) — токен уходит из held -> экспозиция сама падает, лимит на кошелёк освобождается."""
        tw = state["tok_wallet"]
        e = 0.0
        for t, v in held.items():
            if tw.get(t) == w:
                e += v.get("usd", 0.0)
        for t, a in session_add.items():
            if tw.get(t) == w:
                e += a
        return e

    while True:
        try:
            allow = load_allowlist()                   # копи-набор = локальный allowlist (источник правды)
            params = {"since": state["last_t"]}
            if allow:                                  # лента РОВНО по нашим кошелькам (из main-книги)
                params["wallets"] = ",".join(sorted(allow))
            else:                                      # allowlist пуст -> лента группы (fallback)
                params["g"] = group
            r = s.get(f"{server}/api/signals", params=params, timeout=25).json()
        except Exception as ex:  # noqa: BLE001
            print(f"[{time.strftime('%H:%M:%S')}] сервер недоступен ({ex})", flush=True)
            time.sleep(poll)
            continue
        if r.get("error"):                            # сервер отверг запрос (напр. нет/неверный
            print(f"[{time.strftime('%H:%M:%S')}] !! сервер отказал: {r['error']} — "
                  f"проверь SIGNALS_TOKEN в polymarket.env (должен совпадать с серверным)", flush=True)
            time.sleep(poll)                          # SIGNALS_TOKEN) — НЕ молчим, иначе бот «ослеп»
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

        if paper:                                    # теневой режим: held из виртуального портфеля
            held = paper_held(pstate)
            session_add = {}
        elif funder:                                 # реальные вложения по токенам (для лимита позы)
            h = fetch_held(s, funder)
            if h is not None:
                held = h
            session_add = {}                         # held теперь авторитетен за прошлые циклы ->
            #                                          счётчик сеанса покрывает только этот цикл

        today = time.strftime("%Y-%m-%d")
        if state.get("day") != today:                # новый день -> сброс дневных счётчиков
            state["day"] = today
            state["spent_day"] = 0.0
            state["spent_by_wallet"] = {}

        allow = load_allowlist()                      # белый список кошельков (перечитывается на лету)
        for sig in r.get("signals", []):
            k = sig_key(sig)
            state["last_t"] = max(state["last_t"], sig["t"])
            if k in state["done"]:
                continue
            px = sig.get("px") or 0
            title = (sig.get("title") or "")[:48]
            # --- ЗАЩИТА ОТ ИНЪЕКЦИИ СИГНАЛОВ: копируем только кошельки из локального allowlist ---
            if allow and (sig.get("w", "") or "").lower() not in allow:
                print(f"  пропуск: {(sig.get('w','') or '')[:10]}… НЕ в локальном allowlist "
                      f"(сигнал мог быть подсунут) — {title[:36]}", flush=True)
                state["done"].append(k)
                continue
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
            if px < min_price:                        # дешёвый лонгшот: копи-лаг + дисперсия съедают
                print(f"  skip (лонгшот {px} < мин {min_price} — не берём дешёвые лотереи): {title}",
                      flush=True)
                state["done"].append(k)
                continue
            if max_resolve_days:                      # рынок резолвится слишком поздно -> капитал застрянет
                rd = resolve_days(s, sig.get("cid", ""), srv_now)
                if rd is not None and rd > max_resolve_days:
                    print(f"  skip (резолв через {round(rd,1)}д > {max_resolve_days}д, "
                          f"капитал застрянет): {title}", flush=True)
                    state["done"].append(k)
                    continue
            # лимит на позу по РЕАЛЬНОМУ вложению (чейн + входы этого цикла) — не больше cap_usd
            deployed = held.get(sig["tok"], {}).get("usd", 0.0) + session_add.get(sig["tok"], 0.0)
            if deployed + per_trade > cap_usd + 1e-6:
                print(f"  skip (в этой позе уже ~${deployed:.2f}, лимит ${cap_usd:.2f}): {title}",
                      flush=True)
                state["done"].append(k)
                continue
            # ЛИМИТ НА КОШЕЛЁК = сумма его ТЕКУЩИХ открытых позиций (не расход за сутки). Частильщик
            # входит снова, ТОЛЬКО когда его прошлые позиции закрылись (в плюс/минус — неважно, held
            # упал). При funder считаем по реальной экспозиции; без funder (dry) — старый дневной счётчик.
            sig_w = (sig.get("w", "") or "")
            if max_per_wallet:
                wexp = wallet_exposure(sig_w) if (funder or paper) else state["spent_by_wallet"].get(sig_w, 0.0)
                if wexp + per_trade > max_per_wallet + 1e-6:
                    unit = "в позах" if (funder or paper) else "за день"
                    print(f"  skip (с кошелька {sig_w[:8]}… {unit} уже ~${wexp:.2f}, лимит "
                          f"${max_per_wallet:.0f} — ждём закрытия его позиций): {title}", flush=True)
                    state["done"].append(k)
                    continue
            # ЛИМИТ ДЕПОЗИТА = деньги В ОТКРЫТЫХ ПОЗИЦИЯХ СЕЙЧАС, не пожизненный расход. Когда
            # позиция закрывается/продаётся — бюджет освобождается сам (held падает). Считаем по
            # реальным холдингам с чейна (funder) + входы этого цикла. Без funder — старый счётчик.
            exposure = (sum(v.get("usd", 0.0) for v in held.values()) + sum(session_add.values())
                        if (funder or paper) else state["spent_total"])
            if exposure + per_trade > deposit + 1e-6:
                print(f"  skip (в открытых позах ~${exposure:.2f}+${per_trade:g} > депозит ${deposit:g} "
                      f"— ждём, пока освободятся): {title}", flush=True)
                state["done"].append(k)
                continue
            if state["spent_day"] + per_trade > daily_max:
                print(f"  дневной лимит ${daily_max} достигнут — пауза до завтра", flush=True)
                break

            line = f"{sig.get('out', ''):>3} '{title}' @ ~{px}  ${per_trade} (tok…{sig['tok'][-6:]})"

            if mode == "dry":
                print(f"  [DRY] купил бы: {line}", flush=True)
                session_add[sig["tok"]] = session_add.get(sig["tok"], 0.0) + per_trade  # dry-превью лимитов
                state["spent_by_wallet"][sig_w] = state["spent_by_wallet"].get(sig_w, 0.0) + per_trade
                state["tok_wallet"].setdefault(sig["tok"], sig_w)   # токен -> источник (первый закрепляет)
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
                _tag = "PAPER" if paper else ("SMOKE" if mode == "smoke" else "LIVE")
                print(f"  [{_tag}] ордер: {line}" + ("" if paper else f" -> {resp!r}"), flush=True)
                if ok:
                    state["done"].append(k)
                    session_add[sig["tok"]] = session_add.get(sig["tok"], 0.0) + per_trade
                    if paper:                        # метаданные виртуальной позы (для отчёта)
                        pstate.setdefault("meta", {})[sig["tok"]] = {
                            "cid": sig.get("cid", ""), "title": title, "w": sig_w}
                    state["tok_wallet"].setdefault(sig["tok"], sig_w)   # токен -> источник (для лимита)
                    state["spent_by_wallet"][sig_w] = state["spent_by_wallet"].get(sig_w, 0.0) + per_trade
                    state["spent_total"] = round(state["spent_total"] + per_trade, 2)
                    state["spent_day"] = round(state["spent_day"] + per_trade, 2)
                    smoke_done = True
                    st_save(state)
                    if mode == "smoke":
                        print(f"  [SMOKE] ГОТОВО. Потрачено ~${per_trade}. Проверь позицию на "
                              f"polymarket.com, потом ставь MODE=live.", flush=True)
                        return
            except Exception as ex:  # noqa: BLE001
                low = str(ex).lower()
                if "no orders found to match" in low or "no match" in low or (
                        "fak" in low and "kill" in low):
                    # рынок ушёл ВЫШЕ нашего потолка (цена сигнала +2¢): встречных продавцов по
                    # нашей цене нет. Это штатный отказ от догона (не ошибка) — НЕ тратим, помечаем
                    # сигнал пройденным, чтобы не слать пустые заявки на биржу каждый цикл.
                    print(f"  пропуск: рынок ушёл выше нашей цены ~${cap:.2f}, не гонимся — {title}",
                          flush=True)
                    state["done"].append(k)
                    st_save(state)
                elif any(x in low for x in ("ssl", "handshake", "timed out", "timeout", "eof",
                                            "connection", "temporarily", "max retries", "read operation")):
                    # СЕТЕВОЙ обрыв TLS до биржи (не наша вина, часто нестабильный интернет/VPN):
                    # ордер НЕ разместился, сигнал НЕ помечаем -> повторим. Дубль (если запрос всё же
                    # дошёл, а ответ потерялся) отсекут held-проверка позы и MAX_ENTRIES на след. цикле.
                    print(f"  связь с биржей моргнула (сеть) — повторю: {title}", flush=True)
                else:
                    print(f"  !! ордер не прошёл ({ex}) — сигнал НЕ помечен, повторим позже", flush=True)

        # --- ВЫХОДЫ: цель ПРОДАЛА (до резолва) -> продаём свою позицию ЦЕЛИКОМ. Мелкие скейл-ауты
        # (доля < exit_min_frac) игнорим; smoke выходы не трогает (он про проверку входа). ---
        for ex in r.get("exits", []):
            state["last_t"] = max(state["last_t"], ex.get("t", 0))
            xk = "X|" + str(ex.get("t", 0)) + "|" + ex.get("tok", "")
            if xk in state["done"]:
                continue
            tok = ex.get("tok", "")
            xtitle = (ex.get("title") or "")[:48]
            if (ex.get("frac") or 1.0) < exit_min_frac:       # цель вышла лишь частично -> ждём
                state["done"].append(xk)
                continue
            shares = math.floor(held.get(tok, {}).get("shares", 0.0) * 100) / 100   # вниз, не оверселл
            if shares <= 0:                                   # мы это не держим -> нечего продавать
                state["done"].append(xk)
                continue
            if mode == "smoke":
                continue
            if mode == "dry":
                print(f"  [DRY] продал бы всю позу (~{shares} долей): {xtitle}", flush=True)
                state["done"].append(xk)
                continue
            try:
                resp = client.place_market_order(token_id=tok, side="SELL",
                                                 shares=float(shares), order_type="FAK")
                if resp_ok(resp):
                    print(f"  [LIVE] ВЫХОД вслед за целью: продал ~{shares} долей -> {xtitle}",
                          flush=True)
                    held[tok] = {"usd": 0.0, "shares": 0.0}   # локально помечаем закрытым до след. fetch
                    state["done"].append(xk)
                    st_save(state)
                else:
                    print(f"  !! выход не прошёл: {resp!r} — повторим позже", flush=True)
            except Exception as ex2:  # noqa: BLE001
                print(f"  !! выход не прошёл ({ex2}) — повторим позже", flush=True)

        if len(state["done"]) > 4000:                # держим список исполненного компактным
            state["done"] = state["done"][-2000:]
        # карту токен->кошелёк чистим от закрытых позиций (их экспозиция уже освободилась)
        state["tok_wallet"] = {t: w for t, w in state["tok_wallet"].items()
                               if t in held or t in session_add}
        st_save(state)
        if paper:                                    # теневой отчёт: виртуальный банк и PnL
            eq, pnl = paper_equity(pstate, client.api)
            roi = pnl / pstate["bankroll"] * 100 if pstate["bankroll"] else 0
            print(f"  [PAPER] банк ${eq:,.0f} (старт ${pstate['bankroll']:,.0f}) | PnL {pnl:+,.0f}$ "
                  f"({roi:+.1f}%) | реализ {pstate['realized']:+,.0f}$ | позиций {len(pstate['pos'])} "
                  f"| кэш ${pstate['cash']:,.0f} | сделок {pstate['buys']}/{pstate['sells']}", flush=True)
            pst_save(pstate)
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
    min_price = float(cfg.get("MIN_PRICE") or 0.12)   # не берём дешёвые лонгшоты (лаг+дисперсия жрут реал)
    poll = int(cfg.get("POLL_SEC") or 30)
    max_age = int(cfg.get("MAX_AGE_SEC") or 300)     # старше — не копируем (цена уехала)
    max_entries = int(cfg.get("MAX_ENTRIES_PER_POS") or 2)   # не больше N входов в одну позу
    max_per_wallet = float(cfg.get("MAX_PER_WALLET_DAY") or 5)  # не больше $X/день на один кошелёк
    max_resolve_days = float(cfg.get("MAX_RESOLVE_DAYS") or 0)  # 0 = БЕЗ фильтра резолва (капитал
    #   освобождают ВЫХОДЫ вслед за целью, не пред-фильтр; цели флипают и долгие рынки)
    exit_min_frac = float(cfg.get("EXIT_MIN_FRAC") or 0.1)      # выходим, как только цель продала >= этой
    #   доли своего холдинга (0.1 = на первую значимую продажу; 0.01 = на ЛЮБУЮ; выше = только на крупный выход)

    rezolv = f"<={max_resolve_days:g}д" if max_resolve_days else "без фильтра"
    print(f"=== live_executor | MODE={mode.upper()} | лимит-в-позах ${deposit} | ставка ${per_trade} | "
          f"дневной ${daily_max} | на кошелёк ${max_per_wallet}/д | цена {min_price:g}-{max_price:g} | "
          f"резолв {rezolv} | до {max_entries}x в позу | ВЫХОДЫ вслед за целью (>={exit_min_frac:g}) ===",
          flush=True)
    print(f"сигналы: {server}/api/signals?g={group}", flush=True)

    if mode == "paper":
        # ТЕНЕВОЙ БОТ: та же логика/гварды/задержки, но «покупает» по реальным кэфам в виртуальный
        # банк. Ключ НЕ нужен. Отвечает на «а что было бы на $15k при ставке $10 без голода».
        import copy_trader as ct
        bankroll = float(cfg.get("PAPER_BANKROLL") or 15000)
        p_trade = float(cfg.get("PAPER_PER_TRADE") or 10)
        p_wallet = float(cfg.get("PAPER_MAX_PER_WALLET") or 300)
        pstate = pst_load(bankroll)
        pstate["bankroll"] = bankroll                # если поменяли банк в env — подхватываем
        api = ct.API()
        pclient = PaperClient(pstate, api, slip=float(cfg.get("PAPER_SLIP") or 0.01))
        print(f"=== ТЕНЕВОЙ (PAPER) | банк ${bankroll:,.0f} | ставка ${p_trade:g} | на кошелёк ${p_wallet:g} "
              f"| цена {min_price:g}-{max_price:g} | реальные кэфы+задержки, БЕЗ денег ===", flush=True)
        run_loop("paper", pclient, server, group, bankroll, p_trade, bankroll, max_price, poll, max_age,
                 max_entries, "", p_wallet, max_resolve_days, exit_min_frac,
                 signals_token=(cfg.get("SIGNALS_TOKEN") or "").strip(), min_price=min_price,
                 paper=True, pstate=pstate)
        return

    if mode == "dry":
        run_loop(mode, None, server, group, deposit, per_trade, daily_max, max_price, poll, max_age,
                 max_entries, (cfg.get("FUNDER") or "").strip(), max_per_wallet, max_resolve_days,
                 exit_min_frac, signals_token=(cfg.get("SIGNALS_TOKEN") or "").strip(),
                 min_price=min_price)
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
        run_loop(mode, client, server, group, deposit, per_trade, daily_max, max_price, poll, max_age,
                 max_entries, funder, max_per_wallet, max_resolve_days, exit_min_frac,
                 signals_token=(cfg.get("SIGNALS_TOKEN") or "").strip(), min_price=min_price)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено (Ctrl+C). Прогресс сохранён.", flush=True)
        sys.exit(0)
