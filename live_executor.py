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
# Имена файлов состояния можно переопределить через env — так рядом крутятся НЕСКОЛЬКО контуров
# (реал / теневой ядра / теневой «лучших») каждый со своим allowlist, состоянием и флагом «держим».
STATE_FILE = HERE / (os.environ.get("STATE_FILE") or "live_exec_state.json")
ALLOW_FILE = HERE / (os.environ.get("ALLOWLIST_FILE") or "copy_allowlist.json")
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


def hold_only(paper=False) -> bool:
    """Режим «ДЕРЖИМ»: новые входы на паузе, но выходы за целью работают и позиции доживают.
    Горячий флаг (без рестарта). Имя файла можно задать env HOLD_FILE (свой на каждый контур)."""
    f = HERE / (os.environ.get("HOLD_FILE")
                or ("hold_only_paper.json" if paper else "hold_only.json"))
    try:
        return bool(json.loads(f.read_text(encoding="utf-8")).get("hold"))
    except Exception:  # noqa: BLE001
        return False


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
        "SIGNALS_TOKEN", "TG_ENABLED", "TG_TOKEN", "TG_CHAT", "TG_TAG", "TG_SUMMARY_H",
        "TG_DAILY_AT", "TG_PAPER", "DRIFT_EDGE", "DRIFT_MARGIN", "DRIFT_MAX_PRICE", "DRIFT_MIN", "RISK_PCT",
        "RISK_DEPLOY_PCT", "RISK_TRADE_PCT", "RISK_DAY_PCT", "RISK_WALLET_PCT", "RISK_STOP_DD",
        "RISK_TRADE_MAX")})
    return cfg


# ───────────────── Telegram-уведомления (вход/выход/итоги банкролла) ─────────────────
# Токен бота — СЕКРЕТ: живёт только в polymarket.env (gitignored), как приватный ключ.
# По умолчанию ВЫКЛЮЧЕНО (TG_ENABLED=0) — ничего никуда не уходит, пока сам не включишь.
_TG = {"on": False, "token": "", "chat": "", "tag": "", "sess": None, "fails": 0}


def _yes(v):
    return str(v or "0").strip().lower() in ("1", "true", "yes", "on")


def tg_init(cfg, mode=""):
    _TG["on"] = _yes(cfg.get("TG_ENABLED"))
    # В КАНАЛ ПИШЕТ ТОЛЬКО РЕАЛЬНЫЙ БОТ (решение пользователя). Виртуальные контуры (paper/dry)
    # молчат независимо от того, как запущены — из пульта или напрямую. Осознанное исключение:
    # TG_PAPER=1 в polymarket.env (тогда шлют, но с явной меткой «виртуальные»).
    if _TG["on"] and mode in ("paper", "dry") and not _yes(cfg.get("TG_PAPER")):
        _TG["on"] = False
        print(f"Telegram: контур '{mode}' в канал НЕ пишет (пишет только реальный бот). "
              f"Включить: TG_PAPER=1", flush=True)
        return False
    _TG["token"] = (cfg.get("TG_TOKEN") or "").strip()
    _TG["chat"] = (cfg.get("TG_CHAT") or "").strip()
    # МЕТКА КОНТУРА обязательна: реальный и теневой боты пишут в один канал, и без неё сообщения
    # неотличимы (у теневого другая ставка — легко принять виртуальную сделку за настоящую).
    _TG["tag"] = (cfg.get("TG_TAG") or "").strip() or {
        "paper": "🌗 ТЕНЕВОЙ (виртуальные)", "live": "💰 РЕАЛ",
        "smoke": "🧪 SMOKE", "dry": "🧪 DRY"}.get(mode, "")
    if _TG["on"] and not (_TG["token"] and _TG["chat"]):
        print("!! TG_ENABLED=1, но нет TG_TOKEN/TG_CHAT в polymarket.env — уведомления выключены",
              flush=True)
        _TG["on"] = False
    if _TG["on"]:
        _TG["sess"] = requests.Session()
        print(f"Telegram-уведомления: ВКЛ -> {_TG['chat']}", flush=True)
    return _TG["on"]


def tg_send(text, silent=False):
    """Отправка в канал/чат. Тихо игнорирует сбои (уведомления не должны ломать торговлю)."""
    if not _TG["on"] or _TG["fails"] > 20:
        return False
    try:
        r = _TG["sess"].post(
            f"https://api.telegram.org/bot{_TG['token']}/sendMessage",
            json={"chat_id": _TG["chat"], "text": (f"{_TG['tag']} " if _TG["tag"] else "") + text,
                  "parse_mode": "HTML", "disable_web_page_preview": True,
                  "disable_notification": bool(silent)}, timeout=12)
        if r.status_code != 200:
            _TG["fails"] += 1
            if _TG["fails"] <= 3:
                print(f"  [TG] не отправлено ({r.status_code}): {r.text[:120]}", flush=True)
            return False
        _TG["fails"] = 0
        return True
    except Exception:  # noqa: BLE001
        _TG["fails"] += 1
        return False


def resp_reason(resp):
    """Почему ордер отклонён: RejectedOrder.code (fak_not_filled / not_enough_balance / …)."""
    return str(getattr(resp, "code", "") or getattr(resp, "message", "") or "")


def resp_filled_usd(resp, side, fallback):
    """СКОЛЬКО РЕАЛЬНО исполнилось в $. У AcceptedOrder: making_amount = что мы отдали
    (для BUY это USDC), taking_amount = что получили (для SELL это USDC). FAK часто наливается
    ЧАСТИЧНО — раньше в счётчики шла полная задуманная ставка, и лимиты «протекали»."""
    try:
        v = getattr(resp, "making_amount" if side == "BUY" else "taking_amount", None)
        v = float(v) if v is not None else 0.0
        return v if v > 0 else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)


def resp_filled_shares(resp, side, fallback):
    """Сколько ДОЛЕЙ реально прошло: BUY -> taking_amount, SELL -> making_amount."""
    try:
        v = getattr(resp, "taking_amount" if side == "BUY" else "making_amount", None)
        v = float(v) if v is not None else 0.0
        return v if v > 0 else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)


# ───────────────── РИСК-ДВИЖОК: лимиты от РЕАЛЬНОГО капитала + стоп по просадке ─────────────────
def account_equity(s, client, funder):
    """Капитал счёта = свободный USDC + текущая стоимость ЖИВЫХ позиций.
    Возвращает (equity, cash, positions_value). Без этого лимиты задаются «на глаз» в env и
    легко оказываются отключёнными (реальный случай: DEPOSIT=200000 при $40 на счету)."""
    cash = 0.0
    try:
        ba = client.get_balance_allowance(asset_type="COLLATERAL")
        cash = int(getattr(ba, "balance", 0)) / 1e6
    except Exception:  # noqa: BLE001
        pass
    pv = 0.0
    try:
        pos = s.get(f"https://data-api.polymarket.com/positions?user={funder}&limit=500",
                    timeout=15).json()
        for p in pos if isinstance(pos, list) else []:
            if not p.get("redeemable"):
                pv += float(p.get("currentValue") or 0)
    except Exception:  # noqa: BLE001
        pass
    return round(cash + pv, 2), round(cash, 2), round(pv, 2)


def redeem_won(s, client, funder, state, limit=8):
    """ЗАБРАТЬ выигранные позиции: резолвнутые доли надо редимить, иначе деньги висят мёртвым
    грузом — бот считает капитал свободным, а кэша на счету нет и ордера отбиваются."""
    try:
        pos = s.get(f"https://data-api.polymarket.com/positions?user={funder}&limit=500",
                    timeout=15).json()
    except Exception:  # noqa: BLE001
        return 0
    done = set(state.get("redeemed") or [])
    cids, got = [], 0
    for p in pos if isinstance(pos, list) else []:
        cid = str(p.get("conditionId") or "")
        if p.get("redeemable") and cid and cid not in done and float(p.get("size") or 0) > 0:
            if cid not in cids:
                cids.append(cid)
    for cid in cids[:limit]:
        try:
            client.redeem_positions(condition_id=cid)
            done.add(cid)
            got += 1
            print(f"  [REDEEM] забрали резолвнутую позицию (cid…{cid[-6:]})", flush=True)
            time.sleep(0.4)
        except Exception as ex:  # noqa: BLE001
            print(f"  redeem не прошёл ({str(ex)[:80]}) — повторим позже", flush=True)
    state["redeemed"] = list(done)[-500:]
    return got


def _mkt_link(title, cid=""):
    t = (title or "рынок")[:80]
    return f"<b>{t}</b>"


def st_load():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"done": [], "last_t": 0, "spent_total": 0.0, "day": "", "spent_day": 0.0}


def st_save(s):
    # атомарно (tmp + replace): обрыв в момент записи не бьёт файл «что уже исполнено» —
    # его порча означала бы ПОВТОРНЫЕ покупки уже отработанных сигналов.
    # На Windows os.replace может кратко падать PermissionError (файл открыт антивирусом/
    # индексатором/пультом) — ретраим, и НЕ роняем бота из-за проблемы с записью лога.
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    for attempt in range(5):
        try:
            tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
            return
        except PermissionError:
            time.sleep(0.3 * (attempt + 1))
        except Exception as ex:  # noqa: BLE001
            print(f"  !! не удалось сохранить состояние ({ex}) — продолжаю", flush=True)
            return
    print("  !! состояние не сохранилось (файл занят) — продолжаю, повторю в след. цикле", flush=True)


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


def clob_price(s, tok, side):
    """Лучшая цена стороны стакана CLOB. ВНИМАНИЕ (проверено на живых данных): у Polymarket
    side='buy' отдаёт лучший БИД (что предлагают покупатели), side='sell' — лучший АСК.
    Поэтому МЫ покупаем по side='sell', а продаём по side='buy'. None при сбое."""
    try:
        r = s.get(f"https://clob.polymarket.com/price?token_id={tok}&side={side}", timeout=10).json()
        p = r.get("price") if isinstance(r, dict) else None
        p = float(p) if p is not None else None
        return p if (p is not None and 0 < p < 1) else None
    except Exception:  # noqa: BLE001
        return None


def clob_fill(s, tok, side, usd=None, shares=None):
    """РЕАЛИСТИЧНЫЙ филл по СТАКАНУ: идём по уровням (asks для покупки, bids для продажи) и
    считаем VWAP на нужный объём. Возвращает (vwap, исполнено_долей, полностью_ли).
    Так учитывается и спред, и ГЛУБИНА: крупная заявка съедает уровни и берёт хуже лучшей цены,
    а на тонком рынке исполняется лишь частично — ровно как реальный FAK. None при сбое."""
    try:
        b = s.get(f"https://clob.polymarket.com/book?token_id={tok}", timeout=12).json()
    except Exception:  # noqa: BLE001
        return None
    lv = (b.get("asks") if side == "BUY" else b.get("bids")) or []
    try:
        lv = [(float(x["price"]), float(x["size"])) for x in lv]
    except Exception:  # noqa: BLE001
        return None
    if not lv:
        return None
    lv.sort(key=lambda x: x[0], reverse=(side != "BUY"))    # BUY: дешёвые asks; SELL: дорогие bids
    got_sh = got_usd = 0.0
    for px, sz in lv:
        if not (0 < px < 1) or sz <= 0:
            continue
        if side == "BUY":
            take_usd = min(usd - got_usd, px * sz)
            if take_usd <= 1e-9:
                break
            got_usd += take_usd
            got_sh += take_usd / px
            if got_usd >= usd - 1e-9:
                break
        else:
            take_sh = min(shares - got_sh, sz)
            if take_sh <= 1e-9:
                break
            got_sh += take_sh
            got_usd += take_sh * px
    if got_sh <= 0:
        return None
    vwap = got_usd / got_sh
    full = (got_usd >= (usd or 0) - 1e-6) if side == "BUY" else (got_sh >= (shares or 0) - 1e-6)
    return round(vwap, 5), got_sh, full


def clob_mid(s, tok):
    """Текущая midpoint-цена токена из CLOB (для доразмера под задержку и потолка FAK). None при сбое."""
    try:
        r = s.get(f"https://clob.polymarket.com/midpoint?token_id={tok}", timeout=10).json()
        m = r.get("mid") if isinstance(r, dict) else None
        return float(m) if m is not None else None
    except Exception:  # noqa: BLE001
        return None


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
        if p.get("redeemable"):
            # РЕЗОЛВНУТАЯ позиция: рынок закрыт, риска больше нет — это НЕ занятая экспозиция.
            # Раньше такие считались «деньгами в открытых позах» и (а) завышали занятость лимитов
            # (бот зря блокировал входы), (б) раздували сводку. Пропускаем.
            continue
        cost = p.get("initialValue")
        if cost is None:
            cost = (p.get("size") or 0) * (p.get("avgPrice") or 0)
        d = out.setdefault(a, {"usd": 0.0, "shares": 0.0})
        d["usd"] += float(cost or 0)
        d["shares"] += float(p.get("size") or 0)
    return out


# ════════════ ТЕНЕВОЙ (PAPER) РЕЖИМ: тот же бот на виртуальный банкролл, реальные кэфы ════════════
PAPER_FILE = HERE / (os.environ.get("PAPER_STATE_FILE") or "paper15k_state.json")


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


def paper_settle(pstate, api, batch=30):
    """РЕЗОЛВ виртуальных позиций: рынок разрешён (оракул CLOB) -> редимим долю в кэш
    (winner 1.0/0.0), реализуем PnL, убираем из открытых. Иначе резолвнутые позиции копятся
    ВЕЧНО, выигрыш не возвращается в кэш, а экспозиция забивается мёртвым грузом. Стаггером."""
    import copy_trader as ct
    keys = list(pstate.get("pos", {}).keys())
    if not keys:
        return
    i = pstate.get("_settle_i", 0) % len(keys)
    for tok in keys[i:i + batch]:
        pos = pstate["pos"].get(tok)
        cid = pos.get("cid") if pos else None
        if not cid:
            continue
        res = ct._oracle(api, cid)                    # {token: 1.0/0.0} если рынок разрешён, иначе None
        if not res or res.get(str(tok)) is None:
            continue
        wp = float(res[str(tok)])
        sh, cost = pos["shares"], pos["cost"]
        proceeds = sh * wp
        pstate["realized"] = round(pstate["realized"] + proceeds - cost, 2)
        pstate["cash"] = round(pstate["cash"] + proceeds, 2)
        entry = pos.get("entry") or (cost / sh if sh else 0)
        pstate.setdefault("closed", []).append({
            "title": pos.get("title", ""), "outcome": pos.get("outcome", ""), "cid": cid,
            "w": pos.get("w", ""), "entry": round(entry, 4), "target_px": pos.get("target_px"),
            "exit_px": round(wp, 4), "shares": round(sh, 2), "realized": round(proceeds - cost, 2),
            "t_open": pos.get("t_open"), "t_close": int(time.time()), "resolved": True})
        pstate["pos"].pop(tok, None)
    if len(pstate.get("closed", [])) > 300:
        pstate["closed"] = pstate["closed"][-300:]
    pstate["_settle_i"] = (i + batch) % max(1, len(keys))


class PaperClient:
    """Имитация SDK-клиента максимально близко к реальному исполнению: покупаем по лучшему ASK,
    продаём по лучшему BID (реальный стакан CLOB) — то есть платим спред с ОБЕИХ сторон, как в
    жизни. Если стакан недоступен — откат на midpoint±slip. Ключ/деньги НЕ нужны."""
    def __init__(self, pstate, api, slip=0.01, sess=None):
        self.p = pstate
        self.api = api
        self.slip = slip
        self.s = sess or requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

    def _mid(self, tok):
        try:
            m = self.api.midpoints([tok]).get(tok)
            return float(m) if m is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _fill_px(self, tok, side):
        """Цена, по которой РЕАЛЬНО исполнился бы наш рыночный ордер (противоположная сторона
        стакана): МЫ покупаем по аску (у API это side='sell'), продаём по биду (side='buy')."""
        px = clob_price(self.s, tok, "sell" if side == "BUY" else "buy")
        if px is not None:
            return px                                       # лучший ASK / BID — спред учтён
        mid = self._mid(tok)                                # откат: мид ± слиппедж
        if mid is None or not (0 < mid < 1):
            return None
        return round(mid + self.slip, 4) if side == "BUY" else round(max(0.001, mid - self.slip), 4)

    def place_market_order(self, token_id, side, amount=None, shares=None,
                           order_type=None, max_price=None):
        # 1) пробуем РЕАЛЬНЫЙ филл по стакану (VWAP + глубина + частичное исполнение)
        deep = clob_fill(self.s, token_id, side,
                         usd=float(amount) if side == "BUY" else None,
                         shares=float(shares) if side != "BUY" else None)
        if deep:
            fill, got_sh, _full = deep
        else:                                               # откат: лучшая цена стороны / мид±slip
            fill = self._fill_px(token_id, side)
            got_sh = (float(amount) / fill) if (fill and side == "BUY") else float(shares or 0)
        if fill is None or not (0 < fill < 1):
            raise Exception("paper: стакан недоступен")
        mid = fill                                          # цена нашего исполнения (для журнала)
        if side == "BUY":
            if max_price and fill > max_price + 1e-9:      # как реальный FAK-no-fill -> спокойный пропуск
                raise Exception("no orders found to match with FAK order")
            sh = got_sh                                     # сколько долей реально дал стакан
            spent = round(sh * fill, 4)                     # частичный филл -> тратим меньше
            pos = self.p["pos"].setdefault(token_id, {"shares": 0.0, "cost": 0.0})
            pos["shares"] += sh
            pos["cost"] = round(pos["cost"] + spent, 4)
            self.p["cash"] = round(self.p["cash"] - spent, 2)
            self.p["buys"] += 1
        else:                                              # SELL
            pos = self.p["pos"].get(token_id)
            if pos and pos["shares"] > 0:
                sh = min(float(shares), pos["shares"], got_sh or float(shares))
                proceeds = sh * mid
                cost_part = pos["cost"] * (sh / pos["shares"])
                self.p["realized"] = round(self.p["realized"] + proceeds - cost_part, 2)
                self.p["cash"] = round(self.p["cash"] + proceeds, 2)
                entry = pos.get("entry") or (pos["cost"] / pos["shares"] if pos["shares"] else 0)
                self.p.setdefault("closed", []).append({   # журнал закрытой сделки (для сверки)
                    "title": pos.get("title", ""), "outcome": pos.get("outcome", ""),
                    "cid": pos.get("cid", ""), "w": pos.get("w", ""),
                    "entry": round(entry, 4), "target_px": pos.get("target_px"),
                    "exit_px": round(mid, 4), "shares": round(sh, 2),
                    "realized": round(proceeds - cost_part, 2),
                    "t_open": pos.get("t_open"), "t_close": int(time.time())})
                if len(self.p["closed"]) > 300:
                    self.p["closed"] = self.p["closed"][-300:]
                pos["shares"] -= sh
                pos["cost"] = round(pos["cost"] - cost_part, 4)
                if pos["shares"] <= 1e-6:
                    self.p["pos"].pop(token_id, None)
                self.p["sells"] += 1
        return type("R", (), {"success": True})()

    def close(self):
        pass


def resp_ok(resp):
    """Успех ордера. КРИТИЧНО: у SDK поле называется `ok` (AcceptedOrder.ok=True /
    RejectedOrder.ok=False). Раньше проверялось несуществующее `success`, до `ok` дело не доходило,
    и функция сваливалась в `return True` -> ОТКЛОНЁННЫЕ ордера (в т.ч. fak_not_filled,
    not_enough_balance) считались КУПЛЕННЫМИ: бот отмечал сигнал исполненным, крутил счётчики
    трат и думал, что держит позицию, которой нет."""
    ok = getattr(resp, "ok", None)
    if ok is True:
        return True
    if ok is False:
        return False
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
             signals_token="", min_price=0.12, paper=False, pstate=None,
             chase_match=False, chase_max_mult=2.0, summary_h=24.0, daily_at_min=1410,
             drift_edge=0.22, drift_margin=0.03, drift_max_price=0.70, drift_min=0.02,
             risk_pct=True, risk_deploy=0.65, risk_trade=0.02, risk_day=0.15,
             risk_wallet=0.10, risk_stop_dd=0.10, risk_trade_max=100.0):
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
            if allow:
                # Лента по нашим кошелькам ПЛЮС те, чьи позиции мы ЕЩЁ ДЕРЖИМ. Иначе, убрав кошелёк
                # из состава, мы перестали бы получать его ВЫХОДЫ и уже открытые позиции зависли бы
                # до резолва. Новые ВХОДЫ всё равно гейтятся по allow (проверка ниже) — добавка
                # влияет только на мирроринг выходов по унаследованным позициям.
                hold_ws = {w for t, w in (state.get("tok_wallet") or {}).items() if t in held and w}
                params["wallets"] = ",".join(sorted(set(allow) | hold_ws))
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
            state["day_pnl"] = 0.0                   # заработок за день (по закрытым сделкам)
            state["day_buys"] = 0
            state["day_sells"] = 0

        allow = load_allowlist()                      # белый список кошельков (перечитывается на лету)
        # ── РИСК-ДВИЖОК (только реал): лимиты как % от КАПИТАЛА + стоп по дневной просадке ──
        if not paper and funder and client and risk_pct and time.time() - state.get("risk_t", 0) > 120:
            state["risk_t"] = time.time()
            eq, cash_, pv_ = account_equity(s, client, funder)
            if eq > 0:
                state["equity"] = eq
                if state.get("eq_day") != today:      # фиксируем капитал на начало дня
                    state["eq_day"] = today
                    state["eq_day_start"] = eq
                    state["risk_halt"] = False
                base = float(state.get("eq_day_start") or eq)
                deposit = round(eq * risk_deploy, 2)          # максимум в позициях
                # ставка: % от капитала, но НЕ выше потолка ликвидности. Замер стакана показал:
                # ордер $10 -> VWAP 0.640, $500 -> 0.664 (2.4% проскальзывания), $5000 -> 0.895 (40%).
                # Крупная ставка на тонком рынке сама съедает эдж, поэтому жёсткий кап.
                per_trade = max(1.0, min(risk_trade_max, round(eq * risk_trade, 2)))
                daily_max = round(eq * risk_day, 2)
                max_per_wallet = round(eq * risk_wallet, 2)
                cap_usd = max_entries * per_trade
                dd = (eq - base) / base if base else 0
                if dd <= -risk_stop_dd and not state.get("risk_halt"):
                    state["risk_halt"] = True
                    print(f"  !! СТОП-ЛОСС ДНЯ: капитал {eq:.2f} против {base:.2f} ({dd:+.1%}) — "
                          f"входы остановлены до завтра", flush=True)
                    tg_send(f"🛑 <b>СТОП ДНЯ</b>\nпросадка {dd:+.1%} (капитал ${eq:.2f} "
                            f"против ${base:.2f})\nновые входы остановлены, позиции держим")
                print(f"  [РИСК] капитал ${eq:.2f} (кэш ${cash_:.2f} + позиции ${pv_:.2f}) | "
                      f"ставка ${per_trade:g} | в позах до ${deposit:g} | на кошелёк ${max_per_wallet:g}"
                      + (" | ХОЛТ" if state.get("risk_halt") else ""), flush=True)
            # забрать выигранные (иначе капитал заперт в резолвнутых позициях)
            if time.time() - state.get("redeem_t", 0) > 900:
                state["redeem_t"] = time.time()
                redeem_won(s, client, funder, state)
        # «ДЕРЖИМ» = ручной флаг ИЛИ сработавший стоп-лосс дня: входы стоп, выходы работают
        holding = hold_only(paper) or bool(state.get("risk_halt"))
        if holding:
            print(f"[{time.strftime('%H:%M:%S')}] режим ДЕРЖИМ: новые входы на паузе, "
                  f"выходы за целью работают", flush=True)
        for sig in r.get("signals", []):
            k = sig_key(sig)
            state["last_t"] = max(state["last_t"], sig["t"])
            if holding:                                # держим: НОВЫЕ входы не берём (не отыгрываем позже)
                state["done"].append(k)
                continue
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
            # ── EV-ГЕЙТ УЕХАВШЕЙ ЦЕНЫ ──────────────────────────────────────────────────────────
            # Покупая по p, получаем 1/p долей на $1 -> в плюсе только если истинная вероятность
            # q > p. Оценка q по эджу цели: q = цена_цели + drift_edge (историч. винрейт минус
            # средняя цена входа; у нашей когорты ~+15..21пп). Отсюда максимально допустимая цена:
            #     allowed = (цена_цели + drift_edge) / (1 + drift_margin)
            # плюс ЖЁСТКИЙ потолок drift_max_price (выше него не берём вообще — там апсайда нет).
            # Пример: цель 0.30 -> берём до ~0.50; цена уже 0.70 -> НЕ берём (EV -27%).
            cur_mid = clob_mid(s, sig["tok"])
            # ВАЖНО: гейт срабатывает ТОЛЬКО если цена ушла ВВЕРХ от цены цели. Если рынок стоит
            # или упал — мы берём не хуже цели, догона нет, и ограничивать нечего (решают обычные
            # MIN_PRICE/MAX_PRICE). Иначе бот отказывался от выгодных входов «цена улетела 0.845
            # -> 0.835», хотя цена СНИЗИЛАСЬ.
            allowed = max_price
            if cur_mid and cur_mid > px + drift_min:       # цена убежала ЗАМЕТНО (не тик шума)
                allowed = min(max_price, drift_max_price, (px + drift_edge) / (1 + drift_margin))
                if cur_mid > allowed + 1e-9:
                    ev = (px + drift_edge) / cur_mid - 1
                    print(f"  skip (догон невыгоден: цель {px:.3f} -> сейчас {cur_mid:.3f}, "
                          f"потолок {allowed:.3f}, EV {ev:+.0%}): {title}", flush=True)
                    state["done"].append(k)
                    continue
            # ДОРАЗМЕР ПОД ЗАДЕРЖКУ («платим за лаг»): множим ставку на текущая/цена_цели, чтобы
            # взять столько же долей, сколько цель. Потолок множителя = chase_max_mult.
            stake = per_trade
            chase_mid = cur_mid if chase_match else None
            if chase_match and chase_mid and px > 0 and chase_mid > px:
                stake = round(per_trade * min(chase_max_mult, chase_mid / px), 2)
            # лимит на позу по РЕАЛЬНОМУ вложению (чейн + входы этого цикла) — не больше cap_usd
            deployed = held.get(sig["tok"], {}).get("usd", 0.0) + session_add.get(sig["tok"], 0.0)
            if deployed + stake > cap_usd + 1e-6:
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
                if wexp + stake > max_per_wallet + 1e-6:
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
            if exposure + stake > deposit + 1e-6:
                print(f"  skip (в открытых позах ~${exposure:.2f}+${stake:g} > депозит ${deposit:g} "
                      f"— ждём, пока освободятся): {title}", flush=True)
                state["done"].append(k)
                continue
            if state["spent_day"] + stake > daily_max:
                print(f"  дневной лимит ${daily_max} достигнут — пауза до завтра", flush=True)
                break

            entry_px = chase_mid if (chase_match and chase_mid) else px
            line = (f"{sig.get('out', ''):>3} '{title}' @ ~{entry_px}  ${stake:g}"
                    + (f" (x{stake/per_trade:.1f} догон)" if stake > per_trade + 1e-6 else "")
                    + f" (tok…{sig['tok'][-6:]})")

            if mode == "dry":
                print(f"  [DRY] купил бы: {line}", flush=True)
                session_add[sig["tok"]] = session_add.get(sig["tok"], 0.0) + stake  # dry-превью лимитов
                state["spent_by_wallet"][sig_w] = state["spent_by_wallet"].get(sig_w, 0.0) + stake
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
            # Потолок FAK = EV-порог (округляем ВВЕРХ до цента: рынки с шагом 0.01 требуют 2 знака).
            # Так ордер нальётся на уехавшей цене, но НЕ дороже, чем математически выгодно.
            cap = min(max_price, math.ceil(min(allowed, entry_px + 0.02) * 100 - 1e-9) / 100) \
                if not chase_match else min(max_price, math.ceil(allowed * 100 - 1e-9) / 100)
            try:
                resp = client.place_market_order(
                    token_id=sig["tok"], side="BUY", amount=float(stake),
                    order_type="FAK", max_price=cap)
                ok = resp_ok(resp)
                _tag = "PAPER" if paper else ("SMOKE" if mode == "smoke" else "LIVE")
                if not ok:                                # ОТКЛОНЁН (fak_not_filled и т.п.)
                    why = resp_reason(resp)
                    if "fak" in why.lower() or "unmatched" in why.lower():
                        print(f"  пропуск: не налилось по нашей цене ({why}) — {title}", flush=True)
                        state["done"].append(k)           # рынок ушёл: не долбим повторами
                    else:
                        print(f"  !! ордер отклонён ({why}) — {title}", flush=True)
                    st_save(state)
                    continue
                # РЕАЛЬНО исполнено (FAK часто наливается частично) — считаем по факту, не по замыслу
                stake = round(resp_filled_usd(resp, "BUY", stake), 4) if not paper else stake
                print(f"  [{_tag}] ордер: {line}" + ("" if paper else f" -> исполнено ${stake:g}"),
                      flush=True)
                if ok:
                    state["done"].append(k)
                    session_add[sig["tok"]] = session_add.get(sig["tok"], 0.0) + stake
                    if paper:                        # обогащаем виртуальную позу для журнала/сверки
                        pr = pstate["pos"].get(sig["tok"])
                        if pr is not None:
                            pr["title"] = title
                            pr["outcome"] = sig.get("out", "")
                            pr["cid"] = sig.get("cid", "")
                            pr["w"] = sig_w
                            pr.setdefault("target_px", round(float(sig.get("px") or 0), 4))  # цена ЦЕЛИ
                            pr.setdefault("t_open", int(time.time()))
                            pr["entry"] = round(pr["cost"] / pr["shares"], 4) if pr["shares"] else 0
                    state["tok_wallet"].setdefault(sig["tok"], sig_w)   # токен -> источник (для лимита)
                    if not paper:                    # цена ЦЕЛИ по токену — для расчёта задержки в пульте
                        state.setdefault("tok_target", {})[sig["tok"]] = round(float(sig.get("px") or 0), 4)
                    tg_send(f"🟢 ВХОД {_mkt_link(title)}\n"
                            f"ставка: <b>{sig.get('out','')}</b> · цена ~{entry_px:.3f} · "
                            f"${stake:g}\nсигнал от {sig_w[:8]}… (его цена {px:.3f})", silent=True)
                    state["spent_by_wallet"][sig_w] = state["spent_by_wallet"].get(sig_w, 0.0) + stake
                    state["spent_total"] = round(state["spent_total"] + stake, 2)
                    state["spent_day"] = round(state["spent_day"] + stake, 2)
                    state["n_buys"] = int(state.get("n_buys", 0)) + 1   # РЕАЛЬНЫЕ покупки (не done)
                    state["day_buys"] = int(state.get("day_buys", 0)) + 1
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
                print(f"  выход цели мелкий ({(ex.get('frac') or 0)*100:.0f}% < "
                      f"{exit_min_frac*100:.0f}%) — держим: {xtitle[:36]}", flush=True)
                state["done"].append(xk)
                continue
            shares = math.floor(held.get(tok, {}).get("shares", 0.0) * 100) / 100   # вниз, не оверселл
            invested = held.get(tok, {}).get("usd", 0.0)      # для PnL в уведомлении
            hs = held.get(tok, {}).get("shares", 0.0)
            entry_avg = (invested / hs) if hs else 0          # НАША средняя цена входа
            if shares <= 0:                                   # мы это не держим -> нечего продавать
                print(f"  выход цели, но позиции у нас нет (не копировали вход): {xtitle[:40]}",
                      flush=True)
                state["done"].append(xk)
                continue
            if mode == "smoke":
                continue
            if mode == "dry":
                print(f"  [DRY] продал бы всю позу (~{shares} долей): {xtitle}", flush=True)
                state["done"].append(xk)
                continue
            # оценка цены выхода ДО ордера (по бидам стакана) — чтобы честно показать результат
            exit_est = clob_fill(s, tok, "SELL", shares=shares) if not paper else None
            try:
                resp = client.place_market_order(token_id=tok, side="SELL",
                                                 shares=float(shares), order_type="FAK")
                if resp_ok(resp):
                    print(f"  [LIVE] ВЫХОД вслед за целью: продал ~{shares} долей -> {xtitle}",
                          flush=True)
                    # РЕЗУЛЬТАТ сделки: у paper точный (журнал), у реала — по стакану на момент продажи
                    e_px = x_px = None
                    pnl = None
                    if paper and pstate.get("closed"):
                        last = pstate["closed"][-1]
                        if last.get("cid") == ex.get("cid") or last.get("title") == xtitle:
                            e_px, x_px, pnl = last.get("entry"), last.get("exit_px"), last.get("realized")
                    elif exit_est:
                        x_px, got_sh, _f = exit_est
                        e_px = round(entry_avg, 3)
                        pnl = round(x_px * got_sh - invested, 2)
                    if pnl is not None:
                        state["day_pnl"] = round(float(state.get("day_pnl", 0)) + pnl, 2)
                        state["day_sells"] = int(state.get("day_sells", 0)) + 1
                    roi_txt = f" ({pnl / invested * 100:+.1f}%)" if (pnl is not None and invested) else ""
                    body = (f"вход {e_px} → выход {x_px}\n" if (e_px and x_px) else "")
                    body += f"продано ~{shares:g} долей · вложено ${invested:.2f}"
                    if pnl is not None:
                        body += f"\nрезультат: <b>{pnl:+.2f}$</b>{roi_txt}"
                    tg_send(f"🔴 ВЫХОД вслед за целью {_mkt_link(xtitle)}\n{body}")
                    held[tok] = {"usd": 0.0, "shares": 0.0}   # локально помечаем закрытым до след. fetch
                    state["done"].append(xk)
                    st_save(state)
                else:
                    print(f"  !! выход не прошёл: {resp!r} — повторим позже", flush=True)
            except Exception as ex2:  # noqa: BLE001
                print(f"  !! выход не прошёл ({ex2}) — повторим позже", flush=True)

        state["hb"] = int(time.time())               # heartbeat: пульт видит, что бот жив
        state["hb_cycles"] = int(state.get("hb_cycles", 0)) + 1
        if len(state["done"]) > 4000:                # держим список исполненного компактным
            state["done"] = state["done"][-2000:]
        # карту токен->кошелёк чистим от закрытых позиций (их экспозиция уже освободилась)
        state["tok_wallet"] = {t: w for t, w in state["tok_wallet"].items()
                               if t in held or t in session_add}
        st_save(state)
        if paper:                                    # теневой отчёт: виртуальный банк и PnL
            paper_settle(pstate, client.api)         # редимим резолвнутые позы в кэш (стаггером)
            eq, pnl = paper_equity(pstate, client.api)
            roi = pnl / pstate["bankroll"] * 100 if pstate["bankroll"] else 0
            pstate["equity"], pstate["pnl"], pstate["roi"] = eq, pnl, round(roi, 2)
            pstate["ts"] = int(time.time())          # чтобы пульт читал свежесть/цифры из файла
            print(f"  [PAPER] банк ${eq:,.0f} (старт ${pstate['bankroll']:,.0f}) | PnL {pnl:+,.0f}$ "
                  f"({roi:+.1f}%) | реализ {pstate['realized']:+,.0f}$ | позиций {len(pstate['pos'])} "
                  f"| кэш ${pstate['cash']:,.0f} | сделок {pstate['buys']}/{pstate['sells']}", flush=True)
            pst_save(pstate)
        # ── ИТОГ ДНЯ в телеграм: раз в сутки после времени TG_DAILY_AT (локальное, деф. 23:30) ──
        _now = time.localtime()
        _due = (_now.tm_hour * 60 + _now.tm_min) >= daily_at_min
        if _TG["on"] and _due and state.get("tg_sum_day") != today:
            state["tg_sum_day"] = today
            if not paper:
                inv_ = sum(v.get("usd", 0.0) for v in held.values())
                d_pnl = float(state.get("day_pnl", 0))
                tg_send(f"🌙 <b>ИТОГ ДНЯ</b> · {today}\n"
                        f"заработано за день: <b>{d_pnl:+.2f}$</b>\n"
                        f"сделок: {int(state.get('day_buys', 0))} входов / "
                        f"{int(state.get('day_sells', 0))} выходов\n"
                        f"потрачено на входы: ${state.get('spent_day', 0):.2f}\n"
                        f"сейчас открыто: {len(held)} позиций на ${inv_:,.2f}\n"
                        f"покупок за всё время: {int(state.get('n_buys', 0))}")
            else:
                eq_, pnl_ = paper_equity(pstate, client.api)
                roi_ = pnl_ / pstate["bankroll"] * 100 if pstate["bankroll"] else 0
                tg_send(f"🌙 <b>ИТОГ ДНЯ</b> · {today}\n"
                        f"банк: <b>${eq_:,.0f}</b> (старт ${pstate['bankroll']:,.0f})\n"
                        f"PnL всего: <b>{pnl_:+,.2f}$</b> ({roi_:+.2f}%)\n"
                        f"сделок: {int(state.get('day_buys', 0))} входов / "
                        f"{int(state.get('day_sells', 0))} выходов\n"
                        f"открыто: {len(pstate['pos'])} позиций · кэш ${pstate['cash']:,.0f}")
        # ── дополнительный периодический отчёт (если задан TG_SUMMARY_H > 0) ──
        if _TG["on"] and summary_h > 0 and time.time() - state.get("tg_sum_t", 0) > summary_h * 3600:
            state["tg_sum_t"] = time.time()
            if paper:
                eq_, pnl_ = paper_equity(pstate, client.api)
                roi_ = pnl_ / pstate["bankroll"] * 100 if pstate["bankroll"] else 0
                tg_send(f"📊 <b>ИТОГ</b>\nбанк: <b>${eq_:,.0f}</b> (старт ${pstate['bankroll']:,.0f})\n"
                        f"PnL: <b>{pnl_:+,.2f}$</b> ({roi_:+.2f}%)\n"
                        f"реализовано: {pstate['realized']:+,.2f}$ · открытых: {len(pstate['pos'])}\n"
                        f"сделок: {pstate['buys']} входов / {pstate['sells']} выходов")
            else:
                # held уже без резолвнутых -> это ЖИВЫЕ позиции и реальная занятая экспозиция
                inv = sum(v.get("usd", 0.0) for v in held.values())
                tg_send(f"📊 <b>ИТОГ</b>\nоткрытых позиций: <b>{len(held)}</b> на <b>${inv:,.2f}</b>\n"
                        f"потрачено сегодня: ${state.get('spent_day', 0):.2f}\n"
                        f"покупок за всё время: {int(state.get('n_buys', 0))}\n"
                        f"свободно из лимита ${deposit:g}: ${max(0.0, deposit - inv):,.2f}")
        time.sleep(poll)


def main():
    cfg = load_env()
    tg_init(cfg, (cfg.get("MODE") or "dry").lower())  # телеграм: метка контура в каждом сообщении
    summary_h = float(cfg.get("TG_SUMMARY_H") or 0)      # доп. периодический отчёт (0 = не слать)
    _at = (cfg.get("TG_DAILY_AT") or "23:30").strip()     # во сколько слать ИТОГ ДНЯ (локальное время)
    try:
        _h, _m = _at.split(":"); daily_at_min = int(_h) * 60 + int(_m)
    except Exception:  # noqa: BLE001
        daily_at_min = 23 * 60 + 30
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
    chase_match = bool(int(float(cfg.get("CHASE_MATCH") or 0)))  # догон: докупать до долей цели (плата за лаг)
    chase_max_mult = float(cfg.get("CHASE_MAX_MULT") or 2.0)     # потолок множителя ставки при догоне
    # EV-гейт уехавшей цены: allowed = (цена_цели + DRIFT_EDGE) / (1 + DRIFT_MARGIN), но не выше
    # DRIFT_MAX_PRICE. DRIFT_EDGE = предполагаемый эдж кошелька в пп (винрейт минус средняя цена).
    drift_edge = float(cfg.get("DRIFT_EDGE") or 0.22)
    drift_margin = float(cfg.get("DRIFT_MARGIN") or 0.03)
    drift_max_price = float(cfg.get("DRIFT_MAX_PRICE") or 0.70)
    drift_min = float(cfg.get("DRIFT_MIN") or 0.02)   # сдвиг меньше этого = рыночный шум, не догон
    # РИСК ОТ КАПИТАЛА: лимиты считаются как % от equity (кэш+позиции) каждые 2 мин, а не из env
    # «на глаз». RISK_PCT=0 -> старое поведение (фиксированные DEPOSIT/PER_TRADE_USD из env).
    risk_pct = _yes(cfg.get("RISK_PCT") if cfg.get("RISK_PCT") is not None else "1")
    risk_deploy = float(cfg.get("RISK_DEPLOY_PCT") or 0.65)   # максимум капитала в позициях
    risk_trade = float(cfg.get("RISK_TRADE_PCT") or 0.02)     # ставка на сделку
    risk_day = float(cfg.get("RISK_DAY_PCT") or 0.15)         # дневной оборот
    risk_wallet = float(cfg.get("RISK_WALLET_PCT") or 0.10)   # экспозиция на один кошелёк
    risk_stop_dd = float(cfg.get("RISK_STOP_DD") or 0.10)     # стоп: просадка капитала за день
    risk_trade_max = float(cfg.get("RISK_TRADE_MAX") or 100)  # потолок ставки в $ (ликвидность!)

    rezolv = f"<={max_resolve_days:g}д" if max_resolve_days else "без фильтра"
    print(f"=== live_executor | MODE={mode.upper()} | лимит-в-позах ${deposit} | ставка ${per_trade} | "
          f"дневной ${daily_max} | на кошелёк ${max_per_wallet}/д | цена {min_price:g}-{max_price:g} | "
          f"резолв {rezolv} | до {max_entries}x в позу | догон до EV-порога (эдж {drift_edge:g}, "
          f"потолок {drift_max_price:g}) | ВЫХОДЫ вслед за целью (>={exit_min_frac:g}) ===",
          flush=True)
    print(f"сигналы: {server}/api/signals?g={group}", flush=True)

    if mode == "paper":
        # ТЕНЕВОЙ БОТ: та же логика/гварды/задержки, но «покупает» по реальным кэфам в виртуальный
        # банк. Ключ НЕ нужен. Все лимиты — СВОИ (PAPER_*), с откатом на общие/дефолты.
        import copy_trader as ct
        def pf(key, fb):                             # PAPER_-переопределение с откатом на общий/дефолт
            v = cfg.get(key)
            return float(v) if v not in (None, "") else float(fb)
        bankroll = pf("PAPER_BANKROLL", 15000)
        p_trade = pf("PAPER_PER_TRADE", 10)
        p_wallet = pf("PAPER_MAX_PER_WALLET", 300)
        p_maxpx = pf("PAPER_MAX_PRICE", max_price)
        p_minpx = pf("PAPER_MIN_PRICE", min_price)
        p_entries = int(pf("PAPER_MAX_ENTRIES_PER_POS", max_entries))
        p_age = int(pf("PAPER_MAX_AGE_SEC", max_age))
        p_resolve = pf("PAPER_MAX_RESOLVE_DAYS", max_resolve_days)
        p_exit = pf("PAPER_EXIT_MIN_FRAC", exit_min_frac)
        p_poll = int(pf("PAPER_POLL_SEC", poll))
        p_chase = bool(int(pf("PAPER_CHASE_MATCH", 0)))
        p_chase_mult = pf("PAPER_CHASE_MAX_MULT", 2.0)
        pstate = pst_load(bankroll)
        pstate["bankroll"] = bankroll                # если поменяли банк в env — подхватываем
        api = ct.API()
        pclient = PaperClient(pstate, api, slip=float(cfg.get("PAPER_SLIP") or 0.01))
        print(f"=== ТЕНЕВОЙ (PAPER) | банк ${bankroll:,.0f} | ставка ${p_trade:g} | на кошелёк ${p_wallet:g} "
              f"| цена {p_minpx:g}-{p_maxpx:g} | до {p_entries}x | реальные кэфы+задержки, БЕЗ денег ===",
              flush=True)
        run_loop("paper", pclient, server, group, bankroll, p_trade, bankroll, p_maxpx, p_poll, p_age,
                 p_entries, "", p_wallet, p_resolve, p_exit,
                 signals_token=(cfg.get("SIGNALS_TOKEN") or "").strip(), min_price=p_minpx,
                 paper=True, pstate=pstate, chase_match=p_chase, chase_max_mult=p_chase_mult,
                 summary_h=summary_h, daily_at_min=daily_at_min,
                 drift_edge=drift_edge, drift_margin=drift_margin,
                 drift_max_price=drift_max_price, drift_min=drift_min,
                 risk_pct=risk_pct, risk_deploy=risk_deploy, risk_trade=risk_trade,
                 risk_day=risk_day, risk_wallet=risk_wallet, risk_stop_dd=risk_stop_dd,
                 risk_trade_max=risk_trade_max)
        return

    if mode == "dry":
        run_loop(mode, None, server, group, deposit, per_trade, daily_max, max_price, poll, max_age,
                 max_entries, (cfg.get("FUNDER") or "").strip(), max_per_wallet, max_resolve_days,
                 exit_min_frac, signals_token=(cfg.get("SIGNALS_TOKEN") or "").strip(),
                 min_price=min_price, chase_match=chase_match, chase_max_mult=chase_max_mult,
                 summary_h=summary_h, daily_at_min=daily_at_min,
                 drift_edge=drift_edge, drift_margin=drift_margin,
                 drift_max_price=drift_max_price, drift_min=drift_min,
                 risk_pct=risk_pct, risk_deploy=risk_deploy, risk_trade=risk_trade,
                 risk_day=risk_day, risk_wallet=risk_wallet, risk_stop_dd=risk_stop_dd,
                 risk_trade_max=risk_trade_max)
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
                 signals_token=(cfg.get("SIGNALS_TOKEN") or "").strip(), min_price=min_price,
                 chase_match=chase_match, chase_max_mult=chase_max_mult, summary_h=summary_h, daily_at_min=daily_at_min,
                 drift_edge=drift_edge, drift_margin=drift_margin,
                 drift_max_price=drift_max_price, drift_min=drift_min,
                 risk_pct=risk_pct, risk_deploy=risk_deploy, risk_trade=risk_trade,
                 risk_day=risk_day, risk_wallet=risk_wallet, risk_stop_dd=risk_stop_dd,
                 risk_trade_max=risk_trade_max)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено (Ctrl+C). Прогресс сохранён.", flush=True)
        sys.exit(0)
