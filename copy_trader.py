#!/usr/bin/env python3
"""
copy_trader.py — БУМАЖНОЕ копирование сделок отобранных Polymarket-кошельков.

Идея: идём ВПЕРЁД от момента запуска. С первого запуска историю НЕ копируем
(это был бы бэктест с обрезкой), а ловим НОВЫЕ сделки целей и повторяем их
на бумаге с задержкой. Вход — по ТЕКУЩЕЙ цене на момент копирования, поэтому
в PnL честно зашита плата за задержку (зашёл позже — цена уже другая).

Расчёт позиции — по резолву рынка (выиграл $1 / проиграл $0). Реализованный
PnL по закрытым сделкам = честный результат стратегии. Открытые считаются
отдельно (оценка по текущей цене).

Состояние (книга) и журнал сохраняются между запусками. Запускать можно:
  - разово (--once): один проход, подхватывает новые сделки с прошлого раза;
  - в цикле (--watch --interval 600): сам опрашивает каждые N секунд;
  - только отчёт (--report): ничего не копирует, печатает PnL.

Цели берутся из --wallets (через запятую) или из ranked_watchlist.json (--from-watchlist).

ВАЖНО: первый прогон только фиксирует точку старта по каждому кошельку и
НЕ копирует ничего. Реальное копирование начинается со ВТОРОГО прогона.
"""

import argparse
import json
import time
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# кэш резолва рынков (раз зарезолвлен — не меняется): conditionId -> {token_id: 1.0/0.0}
_RES_CACHE: dict = {}

# --- Защита EV копира (тюнить здесь) ---------------------------------------
# Копир входит с задержкой и слиппеджем, поэтому НЕ всякую сделку цели стоит
# повторять: если цена у края или уже убежала от входа цели — эдж уже не наш.
MAX_ENTRY_PRICE = 0.92   # выше -> почти нет апсайда, копируем чистый риск хвоста
MIN_ENTRY_PRICE = 0.02   # ниже -> лонгшот, где цент слиппеджа = десятки % EV
MAX_ADVERSE_MOVE = 0.06  # цена ушла от входа цели дальше этого -> мы опоздали, эдж истёк

# Персональное отключение EV-фильтра: для этих кошельков копируем ВСЕ входы (без band/adverse/avg_up),
# но по рыночной цене + слиппедж (реалистично). Адреса в lower-case.
NO_FILTER_WALLETS = {"0xdacf9f8d0341fa3770fae5c7ccd9dcfed23e3c74"}

# НЕ копируем ставки на футбол/спорт (даже у оставленных кошельков) — спорт дорого копировать.
SPORT_MARKET_KW = (
    "vs.", " vs ", "o/u", "over/under", "exact score", "both teams to score",
    "win on 20", "1st half", "2nd half", "to score", "clean sheet", "corner",
    "hat-trick", "hattrick", "yard box", "group stage", "round of 16",
    "quarterfinal", "quarter-final", "semifinal", "semi-final", "knockout",
    "world cup", "champions league", "premier league", "la liga", "bundesliga",
    "serie a", "ligue 1", "uefa", "fifa", "copa ", "penalty", "red card",
    "to win the match", "end in a draw", "to be relegated", "to qualify",
)


def _is_sport_market(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in SPORT_MARKET_KW)

# --- Сайзинг по убеждённости: $per-trade = ПОТОЛОК НА ВХОД, вниз масштабируем по ставке цели ---
SIZE_BY_CONVICTION = True
MIN_SIZE_FRAC = 0.2      # не меньше 20% потолка ($20 при $100), иначе дребезг
EMA_ALPHA = 0.2          # сглаживание "обычной" ставки цели

# --- Усреднение: повторяем докупки цели, но только ЛОГИЧНЫЕ (вниз/флэт), с предохранителем ---
MAX_POSITION_MULT = 4    # потолок позиции = 4×per_trade (усреднения допускаются, но не безгранично)
AVG_UP_TOL = 0.05        # докупаем, только если цена не выше нашей средней более чем на это (центы цены)

# --- Теневой бэктест фильтра: фиксированный нотионал на отфильтрованную сделку ---
SHADOW_NOTIONAL = 10.0   # $ на каждый теневой вход (единый размер для честного сравнения)

# --- ЗЕРКАЛЬНЫЙ РЕЖИМ (выключен): при True вход по цене цели без слиппеджа/фильтра (нереалистично).
# Держим False: вход/выход по РЫНКУ + слиппедж -> P/L отражает реальную плату за задержку копира. ---
MIRROR = False


# ----------------------------- утилиты доступа к полям -----------------------------
def _f(d: dict, *keys, default=0.0) -> float:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def _s(d: dict, *keys, default="") -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return default


# поля события активности (defensive — у Polymarket встречаются разные имена)
def ev_type(e):    return _s(e, "type", "activityType", "action").upper()
def ev_side(e):    return _s(e, "side", "tradeSide").upper()
def ev_ts(e):      return int(_f(e, "timestamp", "time", "ts"))
def ev_token(e):   return _s(e, "asset", "tokenId", "token", "assetId")
def ev_price(e):   return _f(e, "price", "avgPrice")
def ev_cid(e):     return _s(e, "conditionId", "condition_id")
def ev_title(e):   return _s(e, "title", "slug", "question", default="?")
def ev_outcome(e): return _s(e, "outcome")
def ev_wallet(e):  return _s(e, "proxyWallet", "wallet", "user")


def classify(e) -> str:
    """Что это за событие: BUY / SELL / REDEEM / OTHER."""
    t = ev_type(e)
    if t == "TRADE":
        return ev_side(e)            # BUY или SELL
    if t in ("BUY", "SELL", "REDEEM"):
        return t
    return "OTHER"                    # SPLIT / MERGE / REWARD / CONVERSION — игнор


# ----------------------------- клиент API -----------------------------
class API:
    def __init__(self, pause: float = 0.2):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "copytrader/1.0", "Accept": "application/json"})
        self.pause = pause
        self.page = 500

    def _get(self, path: str, params: dict, retries: int = 5):
        url = DATA_API + path
        for attempt in range(retries):
            try:
                r = self.s.get(url, params=params, timeout=30)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", min(60, 5 * (attempt + 1))))
                    print(f"[rate-limit] жду {wait}s…", flush=True)
                    time.sleep(wait)
                    continue
                if r.status_code == 400 and params.get("offset", 0) > 0:
                    return None                      # упёрлись в потолок offset -> данных дальше нет
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                print(f"[retry] сеть/обрыв, жду {2 ** attempt}s…", flush=True)
                time.sleep(2 ** attempt)
        return None

    def activity(self, wallet: str, limit: int = 500) -> list:
        """Свежая активность, новейшее сверху."""
        out = self._get("/activity", {"user": wallet, "limit": limit, "offset": 0,
                                       "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        return out or []

    def midpoints(self, token_ids: list, chunk: int = 100) -> dict:
        """Текущая midpoint-цена по списку токенов из CLOB (батчами).
        Нужно, чтобы оценивать ВСЕ удерживаемые позиции, а не только те, что есть в снапшотах целей."""
        out: dict = {}
        ids = [t for t in token_ids if t]
        for i in range(0, len(ids), chunk):
            batch = ids[i:i + chunk]
            try:
                r = self.s.post(CLOB_API + "/midpoints",
                                json=[{"token_id": t} for t in batch], timeout=30)
                if r.status_code != 200:
                    continue
                for t, v in (r.json() or {}).items():
                    try:
                        out[t] = float(v)
                    except (TypeError, ValueError):
                        pass
            except requests.RequestException:
                pass
            time.sleep(self.pause)
        return out

    def market_resolution(self, cid: str):
        """Независимый оракул резолва: CLOB /markets/{cid} отдаёт tokens с winner даже
        после удаления стакана. Возвращает {token_id: 1.0/0.0} если рынок РАЗРЕШЁН, иначе None."""
        try:
            r = self.s.get(f"{CLOB_API}/markets/{cid}", timeout=20)
            if r.status_code != 200:
                return None
            d = r.json()
            toks = d.get("tokens") or []
            if not d.get("closed") or not any(t.get("winner") for t in toks):
                return None                          # ещё открыт или закрыт, но не разрешён UMA
            return {str(t.get("token_id")): (1.0 if t.get("winner") else 0.0)
                    for t in toks if t.get("token_id")}
        except requests.RequestException:
            return None

    def positions(self, wallet: str, cap: int = 3500) -> list:
        """Текущие позиции (для цен и определения резолва). С потолком."""
        out, offset = [], 0
        while len(out) < cap:
            page = self._get("/positions", {"user": wallet, "limit": self.page, "offset": offset})
            if not page:
                break
            out.extend(page)
            if len(page) < self.page:
                break
            offset += self.page
            time.sleep(self.pause)
        return out[:cap]


# ----------------------------- книга (состояние) -----------------------------
def load_book(path: str, bankroll: float) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"bankroll": bankroll, "cash": bankroll, "started": int(time.time()),
            "positions": {}, "seen": {}, "realized": 0.0, "n_copied": 0, "n_skipped": 0,
            "typical": {}, "skipped": [], "skipped_realized": 0.0, "topups": 0.0,
            "thold": {}, "log": []}


def save_book(path: str, book: dict) -> None:
    Path(path).write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------- операции копи -----------------------------
def _record_skip(book: dict, e: dict, base: float, reason: str,
                 per_trade: float, slippage: float, wallet: str) -> bool:
    """Пропущенный вход -> теневая запись для бэктеста фильтра. Возвращает False (вход не сделан).
    Нотионал = per_trade (полный потолок) — оценка 'что если бы скопировали по максимуму'."""
    book["n_skipped"] = book.get("n_skipped", 0) + 1
    entry = min(0.999, base + slippage)
    if 0 < entry < 1:
        sk = book.setdefault("skipped", [])
        sk.append({"t": ev_ts(e), "w": wallet, "reason": reason,
                   "tok": ev_token(e), "cid": ev_cid(e),
                   "title": ev_title(e)[:46], "outcome": ev_outcome(e),
                   "entry": round(entry, 4), "their_px": round(ev_price(e), 4),
                   "notional": SHADOW_NOTIONAL, "qty": SHADOW_NOTIONAL / entry,
                   "resolved": False, "pnl": None, "val": None})
        if len(sk) > 3000:                            # не растим журнал бесконечно
            del sk[:len(sk) - 3000]
    return False


# ----------------------------- ЗЕРКАЛЬНЫЕ операции (полная копия цели) -----------------------------
def _mirror_buy(book: dict, e: dict, per_trade: float, wallet: str) -> bool:
    """Вход по цене ЦЕЛИ, без EV-фильтра/слиппеджа. Сайзинг и потолок позиции — как риск-слой."""
    if _is_sport_market(ev_title(e)):                 # не копируем футбол/спорт
        book["n_skipped"] = book.get("n_skipped", 0) + 1
        return False
    px = ev_price(e)
    if not (0 < px < 1):
        return False
    tok = ev_token(e)
    key = f"{wallet}|{tok}"
    th = book.setdefault("thold", {})
    th[key] = th.get(key, 0.0) + _f(e, "size")        # отслеживаем холдинг цели (для долей выхода)
    want = per_trade
    if SIZE_BY_CONVICTION:
        their_usd = _f(e, "usdcSize", "usdcValue", "usdc", "cash", "value") or _f(e, "size") * px
        typ = book.setdefault("typical", {}).get(wallet)
        if typ and typ > 0 and their_usd > 0:
            want = per_trade * min(1.0, max(MIN_SIZE_FRAC, their_usd / typ))
        if their_usd > 0:
            book["typical"][wallet] = their_usd if not typ else (1 - EMA_ALPHA) * typ + EMA_ALPHA * their_usd
    pos = book["positions"].get(key)
    invested = pos["cost"] if pos else 0.0
    room = per_trade * MAX_POSITION_MULT - invested
    if room < 1:
        return False                                  # позиция у потолка (риск-кап сохраняем)
    spend = min(want, room)
    if spend < 1:
        return False
    if book["cash"] < spend:
        deficit = spend - book["cash"]
        book["bankroll"] += deficit
        book["cash"] += deficit
        book["topups"] = book.get("topups", 0.0) + deficit
    qty = spend / px
    if pos:
        pos["qty"] += qty
        pos["cost"] += spend
        pos["fills"] = pos.get("fills", 1) + 1
    else:
        book["positions"][key] = {"token": tok, "qty": qty, "cost": spend, "wallet": wallet,
                                  "cid": ev_cid(e), "title": ev_title(e), "outcome": ev_outcome(e),
                                  "fills": 1, "opened": ev_ts(e)}
    book["cash"] -= spend
    book["n_copied"] += 1
    book["log"].append({"t": ev_ts(e), "w": wallet, "act": "BUY", "px": round(px, 4),
                        "their_px": round(px, 4), "spend": round(spend, 2),
                        "out": ev_outcome(e), "title": ev_title(e)[:46]})
    return True


def _mirror_sell(book: dict, e: dict, wallet: str) -> bool:
    """Цель продаёт долю -> мы продаём ТУ ЖЕ долю своей позиции по цене цели (P/L как у цели)."""
    tok = ev_token(e)
    key = f"{wallet}|{tok}"
    th = book.setdefault("thold", {})
    held = th.get(key, 0.0)
    size = _f(e, "size")
    frac = min(1.0, size / held) if held > 0 else 1.0
    th[key] = max(0.0, held - size)
    pos = book["positions"].get(key)
    if not pos or pos["qty"] <= 0:
        return False
    px = ev_price(e)
    if not (0 < px < 1):
        px = pos["cost"] / pos["qty"] if pos["qty"] else 0.5
    qty_sold = pos["qty"] * frac
    cost_part = pos["cost"] * frac
    proceeds = qty_sold * px
    book["cash"] += proceeds
    book["realized"] += proceeds - cost_part
    book["log"].append({"t": ev_ts(e), "w": pos["wallet"], "act": "SELL", "px": round(px, 4),
                        "pnl": round(proceeds - cost_part, 2), "out": pos.get("outcome", ""),
                        "title": ev_title(e)[:46]})
    pos["qty"] -= qty_sold
    pos["cost"] -= cost_part
    if pos["qty"] <= 1e-9:
        book["positions"].pop(key, None)
    return True


def _mirror_redeem(book: dict, e: dict, wallet: str) -> bool:
    """Цель гасит выигрыш -> гасим остаток позиции по $1."""
    tok = ev_token(e)
    key = f"{wallet}|{tok}"
    book.setdefault("thold", {}).pop(key, None)
    pos = book["positions"].get(key)
    if not pos or pos["qty"] <= 0:
        return False
    proceeds = pos["qty"] * 1.0
    book["realized"] += proceeds - pos["cost"]
    book["cash"] += proceeds
    book["log"].append({"t": ev_ts(e), "w": pos["wallet"], "act": "REDEEM", "val": 1.0,
                        "pnl": round(proceeds - pos["cost"], 2), "out": pos.get("outcome", ""),
                        "title": ev_title(e)[:46]})
    book["positions"].pop(key, None)
    return True


def copy_buy(book: dict, e: dict, per_trade: float, slippage: float, cur=None, wallet: str = "") -> bool:
    if MIRROR:
        return _mirror_buy(book, e, per_trade, wallet)
    # вход по ТЕКУЩЕЙ цене (если есть) + проскальзывание; иначе по цене сделки цели
    base = cur if (cur is not None and 0 < cur < 1) else ev_price(e)
    if not (0 < base < 1):
        return False
    # трекаем холдинг цели (даже если вход отфильтруем) — для пропорциональных выходов
    _th = book.setdefault("thold", {})
    _k = f"{wallet}|{ev_token(e)}"
    _th[_k] = _th.get(_k, 0.0) + _f(e, "size")
    if _is_sport_market(ev_title(e)):                  # не копируем футбол/спорт (даже у no-filter)
        return _record_skip(book, e, base, "sport", per_trade, slippage, wallet)
    nf = (wallet or "").lower() in NO_FILTER_WALLETS   # персональное отключение EV-фильтра
    # --- фильтр копируемости: пропускаем сделки с разрушенным EV (кроме no-filter кошельков) ---
    their = ev_price(e)
    if not nf:
        if not (MIN_ENTRY_PRICE <= base <= MAX_ENTRY_PRICE):
            return _record_skip(book, e, base, "band", per_trade, slippage, wallet)
        if 0 < their < 1 and (base - their) > MAX_ADVERSE_MOVE:
            return _record_skip(book, e, base, "adverse", per_trade, slippage, wallet)
    px = min(0.999, base + slippage)
    # --- сайзинг по убеждённости: ставка цели относительно её ОБЫЧНОЙ ставки ---
    want = per_trade
    if SIZE_BY_CONVICTION:
        their_usd = _f(e, "usdcSize", "usdcValue", "usdc", "cash", "value") or _f(e, "size") * their
        typ = book.setdefault("typical", {}).get(wallet)
        if typ and typ > 0 and their_usd > 0:
            want = per_trade * min(1.0, max(MIN_SIZE_FRAC, their_usd / typ))
        if their_usd > 0:                            # EMA "обычной" ставки цели
            book["typical"][wallet] = their_usd if not typ else (1 - EMA_ALPHA) * typ + EMA_ALPHA * their_usd
    tok = ev_token(e)
    key = f"{wallet}|{tok}"                           # позиции РАЗДЕЛЬНО по кошелькам
    pos = book["positions"].get(key)
    invested = pos["cost"] if pos else 0.0
    if pos and not nf:                               # усреднение: повторяем, если логично (no-filter — всегда)
        avg = pos["cost"] / pos["qty"] if pos["qty"] > 0 else px
        if px > avg + AVG_UP_TOL:
            return _record_skip(book, e, base, "avg_up", per_trade, slippage, wallet)
    room = per_trade * MAX_POSITION_MULT - invested  # общий потолок позиции (один вход всё равно <= per_trade)
    if room < 1:
        return _record_skip(book, e, base, "cap", per_trade, slippage, wallet)
    spend = min(want, room)                           # размер как прежде — кэш НЕ ограничивает (бумага)
    if spend < 1:
        return False                                 # нет места под потолком позиции
    if book["cash"] < spend:                          # баланс не должен заканчиваться -> доливаем капитал
        deficit = spend - book["cash"]                # +deficit и в кэш, и в банкролл -> PnL($) не меняется
        book["bankroll"] += deficit
        book["cash"] += deficit
        book["topups"] = book.get("topups", 0.0) + deficit
    qty = spend / px
    if pos:
        pos["qty"] += qty
        pos["cost"] += spend
        pos["fills"] = pos.get("fills", 1) + 1
    else:
        book["positions"][key] = {"token": tok, "qty": qty, "cost": spend,
                                  "wallet": wallet, "cid": ev_cid(e),
                                  "title": ev_title(e), "outcome": ev_outcome(e),
                                  "fills": 1, "opened": ev_ts(e)}
    book["cash"] -= spend
    book["n_copied"] += 1
    book["log"].append({"t": ev_ts(e), "w": wallet, "act": "BUY", "px": round(px, 4),
                        "their_px": round(ev_price(e), 4), "spend": round(spend, 2),
                        "out": ev_outcome(e), "title": ev_title(e)[:46]})
    return True


def copy_sell(book: dict, e: dict, slippage: float, cur=None, wallet: str = "") -> bool:
    if MIRROR:
        return _mirror_sell(book, e, wallet)
    tok = ev_token(e)
    key = f"{wallet}|{tok}"
    # доля выхода = сколько цель продала от своего холдинга -> ту же долю продаём и мы
    th = book.setdefault("thold", {})
    held = th.get(key, 0.0)
    size = _f(e, "size")
    frac = min(1.0, size / held) if held > 0 else 1.0
    th[key] = max(0.0, held - size)
    pos = book["positions"].get(key)
    if not pos or pos["qty"] <= 0:
        return False                                 # мы это не держим — нечего продавать
    base = cur if (cur is not None and 0 < cur < 1) else ev_price(e)
    if not (0 < base < 1):
        base = ev_price(e)
    px = max(0.001, base - slippage)                 # НАШ реальный выход по рынку + слиппедж
    qty_sold = pos["qty"] * frac
    cost_part = pos["cost"] * frac
    proceeds = qty_sold * px
    book["cash"] += proceeds
    book["realized"] += proceeds - cost_part
    book["log"].append({"t": ev_ts(e), "w": pos["wallet"], "act": "SELL", "px": round(px, 4),
                        "pnl": round(proceeds - cost_part, 2), "out": pos.get("outcome", ""),
                        "title": ev_title(e)[:46]})
    pos["qty"] -= qty_sold
    pos["cost"] -= cost_part
    if pos["qty"] <= 1e-9:
        book["positions"].pop(key, None)
    return True


def copy_redeem(book: dict, e: dict, wallet: str = "") -> bool:
    if MIRROR:
        return _mirror_redeem(book, e, wallet)
    # цель гасит ВЫИГРЫШНЫЕ токены -> наша позиция гасится по $1
    tok = ev_token(e)
    key = f"{wallet}|{tok}"
    book.setdefault("thold", {}).pop(key, None)      # цель вышла полностью -> холдинг обнулён
    pos = book["positions"].get(key)
    if not pos or pos["qty"] <= 0:
        return False
    proceeds = pos["qty"] * 1.0
    book["realized"] += proceeds - pos["cost"]
    book["cash"] += proceeds
    book["log"].append({"t": ev_ts(e), "w": pos["wallet"], "act": "REDEEM", "val": 1.0,
                        "pnl": round(proceeds - pos["cost"], 2), "out": pos.get("outcome", ""),
                        "title": ev_title(e)[:46]})
    book["positions"].pop(key, None)
    return True


def settle(book: dict, wallet: str, tok: str, val: float) -> None:
    """Рынок зарезолвился: гасим нашу позицию по val (1.0 победа / 0.0 поражение)."""
    key = f"{wallet}|{tok}"
    pos = book["positions"].get(key)
    if not pos or pos["qty"] <= 0:
        return
    proceeds = pos["qty"] * val
    book["realized"] += proceeds - pos["cost"]
    book["cash"] += proceeds
    book["log"].append({"t": int(time.time()), "w": pos["wallet"], "act": "SETTLE", "val": val,
                        "pnl": round(proceeds - pos["cost"], 2), "out": pos.get("outcome", ""),
                        "title": pos.get("title", "")[:46]})
    book["positions"].pop(key, None)


def settle_shadow(book: dict, resolved: dict) -> None:
    """Теневой расчёт ОТФИЛЬТРОВАННЫХ сделок: что было бы, если бы фильтр их пропустил.
    resolved: {token: 1.0/0.0}. Считаем PnL по тем теневым записям, чей рынок зарезолвился."""
    for rec in book.get("skipped", []):
        if rec.get("resolved"):
            continue
        val = resolved.get(rec["tok"])
        if val is None:
            continue
        pnl = rec["qty"] * val - rec["notional"]
        rec["resolved"] = True
        rec["val"] = val
        rec["pnl"] = round(pnl, 2)
        book["skipped_realized"] = book.get("skipped_realized", 0.0) + pnl


# ----------------------------- один цикл опроса -----------------------------
def cycle(api: API, book: dict, wallets: list, per_trade: float, slippage: float) -> dict:
    marks: dict = {}
    resolved_all: dict = {}
    for w in wallets:
        wl = w.lower()
        # 1) позиции цели -> текущие цены и что зарезолвилось
        try:
            poss = api.positions(wl)
        except Exception as ex:                      # noqa: BLE001
            poss = []
            print(f"{wl[:10]}…: позиции недоступны ({ex})")
        resolved: dict = {}
        for p in poss:
            tok = _s(p, "asset", "tokenId", "token")
            if not tok:
                continue
            cp = _f(p, "curPrice")
            marks[tok] = cp
            if cp <= 0.01 or cp >= 0.99 or p.get("redeemable"):
                resolved[tok] = 1.0 if cp >= 0.5 else 0.0

        # 2) новые сделки цели (новейшее сверху -> берём новее last_ts, сортируем по возрастанию)
        evs = api.activity(wl, limit=500)
        last = book["seen"].get(wl, 0)
        new = sorted([e for e in evs if ev_ts(e) > last], key=ev_ts)

        if last == 0:
            # ПЕРВЫЙ запуск для кошелька: только фиксируем точку старта, ничего не копируем
            book["seen"][wl] = max([ev_ts(e) for e in evs], default=int(time.time()))
            print(f"{wl[:10]}…: старт — форвард с этой точки, история НЕ копируется")
        else:
            acted = 0
            for e in new:
                k = classify(e)
                cur = marks.get(ev_token(e))
                if k == "BUY":
                    acted += copy_buy(book, e, per_trade, slippage, cur, wl)
                elif k == "SELL":
                    acted += copy_sell(book, e, slippage, cur, wl)
                elif k == "REDEEM":
                    acted += copy_redeem(book, e, wl)
            if new:
                book["seen"][wl] = max(ev_ts(e) for e in new)
            print(f"{wl[:10]}…: новых событий {len(new)}, скопировано действий {acted}")

        # 3) расчёт по резолву наших открытых позиций этой цели
        for tok, val in resolved.items():
            settle(book, wl, tok, val)
        resolved_all.update(resolved)

    # дотягиваем текущие цены по ВСЕМ удерживаемым токенам (усреднённые/старые позиции,
    # которых уже нет в снапшотах целей -> иначе их P/L показывался бы нулевым)
    held = {p["token"] for p in book["positions"].values()}
    missing = [t for t in held if not (marks.get(t) and marks[t] > 0)]
    if missing:
        try:
            for t, v in api.midpoints(missing).items():
                if v and v > 0:
                    marks[t] = v
        except Exception as ex:                      # noqa: BLE001
            print(f"[midpoints] недоступны ({ex})")

    # НЕЗАВИСИМЫЙ резолв: гасим наши открытые позиции по факту резолва рынка, даже если цель
    # уже вышла (редимнула) — иначе позиция висит вечно. Цены нет -> рынок мог зарезолвиться.
    need: dict = {}
    for p in book["positions"].values():
        tok, cid = p["token"], p.get("cid")
        mk = marks.get(tok)
        if cid and not (mk is not None and mk > 0):
            need.setdefault(cid, []).append(p)
    settled_n = 0
    for cid, plist in need.items():
        res = _RES_CACHE.get(cid)
        if res is None:
            res = api.market_resolution(cid)
            if res:
                _RES_CACHE[cid] = res
        if not res:
            continue
        for p in list(plist):
            val = res.get(str(p["token"]))
            if val is not None:
                settle(book, p["wallet"], p["token"], val)
                settled_n += 1
    if settled_n:
        print(f"независимый резолв: погашено {settled_n} зависших позиций", flush=True)

    settle_shadow(book, resolved_all)                # теневой расчёт отфильтрованных сделок
    return marks


# ----------------------------- отчёт -----------------------------
def report(book: dict, marks: dict | None = None) -> None:
    from collections import defaultdict
    marks = marks or {}
    print("\n================ БУМАЖНОЕ КОПИ — ОТЧЁТ ================")
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(book["started"]))
    days = max(0, (int(time.time()) - book["started"]) / 86400)
    print(f"старт: {started}  ({days:.1f} дн назад)   банкролл: ${book['bankroll']:,.0f}")

    def mark_val(p):
        mk = marks.get(p["token"])
        return p["qty"] * mk if (mk is not None and mk > 0) else p["cost"]

    open_val = sum(mark_val(p) for p in book["positions"].values())
    realized = book["realized"]
    total = book["cash"] + open_val
    pnl = total - book["bankroll"]
    roi = pnl / book["bankroll"] if book["bankroll"] else 0.0
    print(f"\nскопировано сделок:           {book['n_copied']}")
    print(f"отфильтровано (защита EV):    {book.get('n_skipped', 0)}")
    print(f"РЕАЛИЗОВАННЫЙ PnL (закрытые): ${realized:,.2f}   <- честный результат стратегии")
    print(f"открытых позиций:             {len(book['positions'])}  (оценка ${open_val:,.2f})")
    print(f"кэш ${book['cash']:,.2f} + открытые ${open_val:,.2f} = ${total:,.2f}")
    print(f"ИТОГО PnL (вкл. открытые):    ${pnl:,.2f}   ({roi:+.1%} от банкролла)")

    # --- разбивка по кошелькам: кто тащит, кто сливает ---
    stat = defaultdict(lambda: {"copied": 0, "closed": 0, "wins": 0, "realized": 0.0, "open_val": 0.0})
    for r in book["log"]:
        w = r.get("w", "?")
        if r.get("act") == "BUY":
            stat[w]["copied"] += 1
        if "pnl" in r:                                # SELL / REDEEM / SETTLE = закрытие
            stat[w]["closed"] += 1
            stat[w]["realized"] += r["pnl"]
            stat[w]["wins"] += 1 if r["pnl"] > 0 else 0
    for p in book["positions"].values():
        stat[p.get("wallet", "?")]["open_val"] += mark_val(p)

    if stat:
        print("\nпо кошелькам (сортировка по реализованному PnL):")
        print(f"  {'кошелёк':<14}{'скоп':>5}{'закр':>5}{'винр':>6}{'реализ$':>11}{'откр$':>10}")
        for w, s in sorted(stat.items(), key=lambda kv: kv[1]["realized"], reverse=True):
            wr = (s["wins"] / s["closed"]) if s["closed"] else 0.0
            print(f"  {w[:14]:<14}{s['copied']:>5}{s['closed']:>5}{wr:>5.0%}"
                  f"${s['realized']:>10,.0f}${s['open_val']:>9,.0f}")

    if book["positions"]:
        items = sorted(book["positions"].values(), key=lambda p: p["cost"], reverse=True)
        print(f"\nоткрытые позиции (топ {min(12, len(items))} по вложению):")
        for p in items[:12]:
            how = f"@{marks[p['token']]:.3f}" if marks.get(p["token"]) else "(по стоим.)"
            print(f"  {p['wallet'][:8]}… {p['title'][:36]:36} ${p['cost']:>7,.0f}->${mark_val(p):>7,.0f} {how}")
        if len(items) > 12:
            print(f"  …и ещё {len(items) - 12} позиций")
    print("======================================================")


def show_recent(book: dict, n: int = 15) -> None:
    if not book["log"]:
        return
    print(f"\nпоследние {n} действий:")
    for r in book["log"][-n:]:
        when = time.strftime("%m-%d %H:%M", time.localtime(r.get("t", 0)))
        extra = (f"pnl=${r['pnl']:+,.0f}" if "pnl" in r else f"${r.get('spend',0):,.0f}")
        print(f"  {when}  {r['act']:6} {extra:>14}  {r.get('title','')}")


# ----------------------------- проверка схемы активности -----------------------------
def cmd_check(api: API, wallet: str) -> None:
    evs = api.activity(wallet.lower(), limit=8)
    print(f"последние {len(evs)} событий {wallet[:12]}… (проверь, что поля распознаются):\n")
    for e in evs[:8]:
        print(f"  type={ev_type(e):8} side={ev_side(e):4} class={classify(e):6} "
              f"ts={ev_ts(e)} price={ev_price(e):.4f} size={_f(e,'size'):.1f} "
              f"tok={ev_token(e)[:10]}… {ev_title(e)[:34]}")
    print("\nесли class/price/ts пустые или нули — схема другая, пришли сырой JSON одного события:")
    if evs:
        print(json.dumps(evs[0], ensure_ascii=False, indent=2)[:1200])


# ----------------------------- main -----------------------------
def resolve_wallets(args) -> list:
    if args.wallets:
        return [w.strip() for w in args.wallets.split(",") if w.strip()]
    if args.from_watchlist:
        d = json.loads(Path(args.from_watchlist).read_text(encoding="utf-8"))
        return d.get("watchlist", [])
    return []


def main() -> None:
    p = argparse.ArgumentParser(description="Бумажное копи отобранных Polymarket-кошельков")
    p.add_argument("--wallets", help="адреса целей через запятую")
    p.add_argument("--from-watchlist", help="взять цели из файла ranked_watchlist.json")
    p.add_argument("--bankroll", type=float, default=10_000, help="стартовый банкролл $ (дефолт 10000)")
    p.add_argument("--per-trade", type=float, default=100, help="ставка $ на одну копируемую сделку (дефолт 100)")
    p.add_argument("--slippage", type=float, default=0.01, help="проскальзывание на исполнении, в долях цены (дефолт 0.01 = 1 цент)")
    p.add_argument("--state", default="paper_book.json", help="файл состояния (книга)")
    p.add_argument("--interval", type=int, default=600, help="период опроса в секундах для --watch (дефолт 600)")
    p.add_argument("--watch", action="store_true", help="крутить цикл опроса бесконечно")
    p.add_argument("--once", action="store_true", help="один проход и выход")
    p.add_argument("--report", action="store_true", help="только отчёт, ничего не копировать")
    p.add_argument("--check", help="проверить схему активности кошелька и выйти")
    args = p.parse_args()

    api = API()

    if args.check:
        cmd_check(api, args.check)
        return

    book = load_book(args.state, args.bankroll)

    if args.report:
        report(book)
        show_recent(book)
        return

    wallets = resolve_wallets(args)
    if not wallets:
        print("Не заданы цели. Укажи --wallets addr1,addr2 или --from-watchlist ranked_watchlist.json")
        return
    print(f"целей: {len(wallets)}  банкролл ${args.bankroll:,.0f}  ставка ${args.per_trade:,.0f}/сделку  "
          f"проскальзывание {args.slippage}")

    def one_pass():
        marks = cycle(api, book, wallets, args.per_trade, args.slippage)
        save_book(args.state, book)
        report(book, marks)

    if args.watch:
        print(f"режим watch: опрос каждые {args.interval}s. Ctrl+C для остановки.")
        try:
            while True:
                one_pass()
                show_recent(book, 8)
                print(f"\n…сплю {args.interval}s\n")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nостановлено. Состояние сохранено.")
    else:
        one_pass()
        show_recent(book)


if __name__ == "__main__":
    main()
