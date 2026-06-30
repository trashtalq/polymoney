"""
pm-edge :: wallet_analyzer.py

Цель: из всех кошельков Polymarket отобрать тех, кто (а) активен, (б) ставит
постоянно, (в) имеет ПОДТВЕРЖДЁННЫЙ положительный edge, а не везение/MM-объём.

На выходе НЕ "ROI = X%", а scorecard с размером выборки и bootstrap-CI, потому
что без этого ты копируешь чужой удачный стрик с задержкой.

ВАЖНО: имена некоторых полей Data API (особенно USDC-сумма на REDEEM/SPLIT/MERGE)
надо сверить с живым ответом. Запусти сначала:  python wallet_analyzer.py --inspect <addr>
и поправь маппинг в _cashflow() под реальные ключи.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CACHE_DIR = Path("./.wallet_cache")
CACHE_DIR.mkdir(exist_ok=True)

# Пороги отбора. Тюнить под реальность после первого прогона.
MIN_RESOLVED_BETS = 100        # меньше — статистики нет, не оцениваем
MAX_REWARD_SHARE = 0.15        # доля REWARD в кэшфлоу выше -> это LP/MM, выкид
MAX_TOP1_CONCENTRATION = 0.40  # >40% профита из одного рынка -> один колл, не эдж
MIN_TOTAL_STAKED = 5_000       # очень низкий порог: важна ПРИБЫЛЬНОСТЬ, не размер (мелкие копируются чище — не двигают цену)
MIN_AVG_STAKE = 20             # средняя ставка ниже -> только отсев совсем пыли/фарм-бота
MIN_OBSERVED_VOLUME = 5_000    # ПРЕ-фильтр ДО анализа: не видели движений на эту сумму -> пропуск (экономит время)
BOOTSTRAP_N = 2000
WHALE_MIN_STAKED = 250_000     # ручной watchlist «крупные, но недобрали выборку»: крупный стейк
WHALE_MIN_BETS = 10            # меньше — совсем шум, не берём даже в watchlist
MIN_EDGE = 0.03               # минимальный ci_low для прохода: эдж ниже бесполезен после издержек копирования


# --------------------------------------------------------------------------- #
# HTTP                                                                          #
# --------------------------------------------------------------------------- #
class DataAPIClient:
    def __init__(self, base: str = DATA_API, page_size: int = 500, pause: float = 0.15):
        self.base = base.rstrip("/")
        self.page_size = page_size
        self.pause = pause
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "pm-edge-wallet-analyzer/1.0"})

    def _get(self, path: str, params: dict[str, Any], retries: int = 5) -> Any:
        url = f"{self.base}{path}"
        for attempt in range(retries):
            try:
                r = self.s.get(url, params=params, timeout=30)
                if r.status_code == 429:            # rate limit (Cloudflare) -> ДЛИННЫЙ ВИДИМЫЙ бэкофф
                    wait = int(r.headers.get("Retry-After", min(60, 5 * (attempt + 1))))
                    print(f"[rate-limit] 429, жду {wait}s (попытка {attempt + 1}/{retries})…", flush=True)
                    time.sleep(wait)
                    continue
                if r.status_code == 400:            # Data API упирается в потолок offset
                    if params.get("offset", 0) == 0:
                        r.raise_for_status()        # 400 на 1-й странице = реальная ошибка
                    return None                     # дальше данных нет -> стоп пагинации
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        return None

    def _paginate(self, path: str, params: dict[str, Any]) -> list[dict]:
        """offset-пагинация. Останавливается на пустой странице или на потолке offset."""
        out: list[dict] = []
        offset = 0
        while True:
            page = self._get(path, {**params, "limit": self.page_size, "offset": offset})
            if page is None:   # потолок offset у Data API -> история обрезана
                print(f"[warn] {path}: API не отдаёт дальше offset={offset}, "
                      f"история обрезана на {len(out)} событиях", flush=True)
                break
            if not page:
                break
            out.extend(page)
            if len(page) < self.page_size:
                break
            offset += self.page_size
            time.sleep(self.pause)
        return out

    # Полная история событий кошелька. DESC -> при обрезке на потолке offset
    # остаётся СВЕЖАЯ история (релевантна для слежки), а не самая древняя.
    def activity(self, wallet: str) -> list[dict]:
        return self._paginate("/activity", {"user": wallet, "sortBy": "TIMESTAMP",
                                             "sortDirection": "DESC"})

    def positions(self, wallet: str, cap: int = 3500) -> list[dict]:
        """Позиции с потолком. Обрезает СПИСОК РЫНКОВ (каждый посчитан целиком),
        а не события внутри рынка — поэтому по оставшимся PnL честный.
        Пагинация от крупного, режется хвост мелких. Достигнут cap -> кошелёк обрезан."""
        out: list[dict] = []
        offset = 0
        while len(out) < cap:
            page = self._get("/positions", {"user": wallet,
                                            "limit": self.page_size, "offset": offset})
            if not page:
                break
            out.extend(page)
            if len(page) < self.page_size:
                break
            offset += self.page_size
            time.sleep(self.pause)
        return out[:cap]

    def recent_trades(self, max_trades: int = 20000) -> list[dict]:
        """Глобальная лента трейдов БЕЗ фильтра по рынку — самый широкий срез
        активных кошельков сразу по всем рынкам. Каждый трейд несёт proxyWallet и usdcSize."""
        out: list[dict] = []
        offset = 0
        while len(out) < max_trades:
            page = self._get("/trades", {"limit": self.page_size, "offset": offset})
            if not page:
                break
            out.extend(page)
            if len(page) < self.page_size:
                break
            offset += self.page_size
            time.sleep(self.pause)
        return out

    def holders(self, condition_id: str, limit: int = 20) -> list[dict]:
        """Плоский список холдеров. Ответ API ВЛОЖЕННЫЙ:
        [{ "token": ..., "holders": [ {proxyWallet, amount, ...} ] }].
        Лимит API capped at 20 на токен."""
        res = self._get("/holders", {"market": condition_id, "limit": min(limit, 20)})
        out: list[dict] = []
        for group in (res or []):
            out.extend(group.get("holders", []))
        return out

    def trades_for_market(self, condition_id: str, max_trades: int = 1000) -> list[dict]:
        """Трейды по рынку — для сбора активных кошельков (в каждом трейде есть proxyWallet)."""
        out: list[dict] = []
        offset = 0
        while len(out) < max_trades:
            page = self._get("/trades", {"market": condition_id,
                                         "limit": self.page_size, "offset": offset})
            if not page:
                break
            out.extend(page)
            if len(page) < self.page_size:
                break
            offset += self.page_size
            time.sleep(self.pause)
        return out

    # Лидерборд: эндпоинты /profit и /volume (НЕ /leaderboard). Формат окна в доках
    # расходится — вызов best-effort, на ошибке discovery идёт через holders+trades.
    def leaderboard(self, window: str = "all", by: str = "profit", limit: int = 100) -> list[dict]:
        path = "/profit" if by == "profit" else "/volume"
        res = self._get(path, {"window": window, "limit": limit})
        return res or []


class GammaClient:
    """Gamma API — метаданные рынков. Нужен для авто-поиска топовых рынков по объёму."""

    def __init__(self, base: str = GAMMA_API):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "pm-edge-wallet-analyzer/1.0"})

    def top_closed_markets(self, n: int = 5, scan: int = 500) -> list[tuple[float, str, str]]:
        """(volume, conditionId, question) для топ-N зарезолвленных рынков по объёму."""
        params = {"closed": "true", "limit": scan, "order": "volumeNum", "ascending": "false"}
        r = self.s.get(f"{self.base}/markets", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = data.get("data") or data.get("markets") or []
        rows: list[tuple[float, str, str]] = []
        for m in data:
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid:
                continue
            vol = m.get("volumeNum") or m.get("volume") or 0
            try:
                vol = float(vol)
            except (TypeError, ValueError):
                vol = 0.0
            rows.append((vol, cid, m.get("question") or m.get("title") or ""))
        rows.sort(reverse=True)  # client-side, если server-side order проигнорирован
        return rows[:n]

    def sweep_markets(self, n: int = 300, include_active: bool = False, page: int = 500) -> list[tuple]:
        """ШИРОКИЙ свип: топ-N рынков по объёму через пагинацию Gamma (closed и опц. active).
        Возвращает (volume, conditionId, question, is_closed). Это и есть расширение базы:
        не топ-5, а топ-сотни-тысячи рынков, включая средне-объёмные и текущие."""
        best: dict[str, tuple] = {}
        buckets: list[dict] = [{"closed": "true"}]
        if include_active:
            buckets.append({"closed": "false", "active": "true"})
        for q in buckets:
            got, offset = 0, 0
            while got < n:
                params = {**q, "limit": page, "offset": offset, "order": "volumeNum", "ascending": "false"}
                try:
                    r = self.s.get(f"{self.base}/markets", params=params, timeout=30)
                    r.raise_for_status()
                    data = r.json()
                except requests.RequestException:
                    break
                if isinstance(data, dict):
                    data = data.get("data") or data.get("markets") or []
                if not data:
                    break
                for m in data:
                    cid = m.get("conditionId") or m.get("condition_id")
                    if not cid:
                        continue
                    vol = _f(m, "volumeNum", "volume")
                    if cid not in best or vol > best[cid][0]:
                        best[cid] = (vol, cid, m.get("question") or m.get("title") or "",
                                     q.get("closed") == "true")
                    got += 1
                if len(data) < page:
                    break
                offset += page
                time.sleep(0.1)
        return sorted(best.values(), reverse=True)


# --------------------------------------------------------------------------- #
# Кэш сырой активности (тянуть тысячи событий на сотни кошельков дорого)         #
# --------------------------------------------------------------------------- #
def load_activity(api: DataAPIClient, wallet: str, refresh: bool = False) -> list[dict]:
    f = CACHE_DIR / f"act_{wallet.lower()}.json"
    if f.exists() and not refresh:
        return json.loads(f.read_text())
    data = api.activity(wallet)
    f.write_text(json.dumps(data))
    return data


def load_positions(api: DataAPIClient, wallet: str, refresh: bool = False,
                   cap: int = 3500) -> tuple[list[dict], bool]:
    """Возвращает (позиции, обрезан_ли). Обрезан = упёрлись в потолок -> на ручную проверку."""
    f = CACHE_DIR / f"pos_{wallet.lower()}.json"
    if f.exists() and not refresh:
        data = json.loads(f.read_text())
    else:
        data = api.positions(wallet, cap=cap)
        f.write_text(json.dumps(data))
    return data, len(data) >= cap


# --------------------------------------------------------------------------- #
# Реконструкция кэшфлоу по рынкам из активности                                  #
# --------------------------------------------------------------------------- #
@dataclass
class MarketFlow:
    condition_id: str
    title: str = ""
    cash_in: float = 0.0      # SELL + REDEEM + MERGE
    cash_out: float = 0.0     # BUY + SPLIT
    reward: float = 0.0       # REWARD (LP-доход, в edge не идёт)
    tokens_net: float = 0.0   # купленные - проданные - погашенные (≈0 => закрыт)
    n_trades: int = 0
    first_ts: int = 0
    last_ts: int = 0
    buy_sizes: list[float] = field(default_factory=list)
    buy_prices: list[float] = field(default_factory=list)
    saw_redeem: bool = False
    saw_buy: bool = False
    saw_sell: bool = False
    mark_resolved: bool = False   # позиция дорезолвлена из /positions (гибрид PnL)

    @property
    def realized_pnl(self) -> float:
        return self.cash_in - self.cash_out

    @property
    def closed(self) -> bool:
        # закрыт: редим, дорезолвлен из /positions, либо токены обнулены продажами
        return self.saw_redeem or self.mark_resolved or abs(self.tokens_net) < 1e-6

    @property
    def hold_seconds(self) -> int:
        return max(0, self.last_ts - self.first_ts)

    @property
    def avg_entry_price(self) -> float:
        tot = sum(self.buy_sizes)
        if tot <= 0:
            return 0.0
        return sum(p * s for p, s in zip(self.buy_prices, self.buy_sizes)) / tot


def _f(d: dict, *keys: str, default: float = 0.0) -> float:
    """достать первое присутствующее числовое поле (имена в API плавают)."""
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                pass
    return default


def build_flows(activity: Iterable[dict]) -> dict[str, MarketFlow]:
    flows: dict[str, MarketFlow] = {}
    for ev in activity:
        cid = ev.get("conditionId") or ev.get("condition_id") or ""
        if not cid:
            continue
        mf = flows.setdefault(cid, MarketFlow(condition_id=cid, title=ev.get("title", "")))
        typ = (ev.get("type") or "").upper()
        side = (ev.get("side") or "").upper()
        ts = int(_f(ev, "timestamp"))
        size = _f(ev, "size", "tokens")            # кол-во токенов
        price = _f(ev, "price")                     # цена за токен
        usdc = _f(ev, "usdcValue", "usdcSize", "usdc", "cash", "value", "amount")  # денежная сумма, если есть

        mf.first_ts = ts if mf.first_ts == 0 else min(mf.first_ts, ts)
        mf.last_ts = max(mf.last_ts, ts)

        if typ == "TRADE" and side == "BUY":
            cost = usdc or size * price
            mf.cash_out += cost
            mf.tokens_net += size
            mf.buy_sizes.append(size)
            mf.buy_prices.append(price)
            mf.n_trades += 1
            mf.saw_buy = True
        elif typ == "TRADE" and side == "SELL":
            mf.cash_in += usdc or size * price
            mf.tokens_net -= size
            mf.n_trades += 1
            mf.saw_sell = True
        elif typ == "REDEEM":
            # выигрышные токены гасятся по $1; если usdc нет — приблизим size*1.0
            mf.cash_in += usdc or size
            mf.tokens_net -= size
            mf.saw_redeem = True
        elif typ == "SPLIT":
            mf.cash_out += usdc or size      # 1 USDC -> 1 yes + 1 no
        elif typ == "MERGE":
            mf.cash_in += usdc or size       # 1 yes + 1 no -> 1 USDC
        elif typ == "REWARD":
            mf.reward += usdc or size
    return flows


# --------------------------------------------------------------------------- #
# Scorecard                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Scorecard:
    wallet: str
    n_markets_total: int = 0
    n_resolved: int = 0
    total_staked: float = 0.0
    realized_pnl: float = 0.0
    roi: float = 0.0
    roi_ci_low: float = 0.0          # 5-й перцентиль bootstrap
    roi_ci_high: float = 0.0
    win_rate: float = 0.0
    resolved_pnl: float = 0.0     # PnL только на зарезолвленных рынках — для копи-холда с задержкой
    n_settled: int = 0            # сколько рынков реально зарезолвилось
    top1_concentration: float = 0.0
    reward_share: float = 0.0
    two_sided_share: float = 0.0     # доля рынков где и купил и продал (MM-тэлл)
    median_hold_hours: float = 0.0
    avg_entry_price: float = 0.0
    active_months: int = 0
    positive_months: int = 0
    passes: bool = False
    truncated: bool = False        # упёрлись в потолок позиций -> на ручную проверку, в зачёт не идёт
    reasons: list[str] = field(default_factory=list)
    composite: float = 0.0


def _bootstrap_roi(pnls: list[float], stakes: list[float], n: int = BOOTSTRAP_N) -> tuple[float, float]:
    if not pnls:
        return 0.0, 0.0
    idx = range(len(pnls))
    rois = []
    for _ in range(n):
        sample = [random.choice(idx) for _ in idx]
        p = sum(pnls[i] for i in sample)
        s = sum(stakes[i] for i in sample)
        rois.append(p / s if s > 0 else 0.0)
    rois.sort()
    lo = rois[int(0.05 * len(rois))]
    hi = rois[int(0.95 * len(rois))]
    return lo, hi


def _inject_resolved_holdings(flows: dict, positions: list[dict]) -> None:
    """Гибрид PnL: для ЕЩЁ УДЕРЖИВАЕМЫХ позиций добираем их стоимость по финальной цене
    из /positions. Чинит перекос activity (досиженные проигрыши/выигрыши выпадали).
    Только для рынков из activity-окна — иначе приписали бы стоимость без затрат."""
    for p in positions or []:
        cid = p.get("conditionId") or p.get("condition_id")
        if not cid or cid not in flows:
            continue
        cp = _f(p, "curPrice")
        if not (cp <= 0.005 or cp >= 0.995 or p.get("redeemable")):
            continue                       # ещё не зарезолвлен
        size = _f(p, "size")
        if size <= 0:
            continue                       # ничего не держит -> в activity уже учтено
        mf = flows[cid]
        mf.cash_in += size * cp            # стоимость удержания по финалу (≈0 у проигравших)
        mf.tokens_net -= size
        mf.mark_resolved = True


def analyze_pos(wallet: str, positions: list[dict]) -> Scorecard:
    """ПОЛНЫЙ расчёт по /positions (без обрезки activity на 3500 событиях).
    PnL позиции = realizedPnl + cashPnl; стоимость ≈ totalBought*avgPrice.
    Бутстрап на полной выборке честно ловит джекпоты на лонгшотах."""
    sc = Scorecard(wallet=wallet, n_markets_total=len(positions))
    pnls: list[float] = []
    costs: list[float] = []
    by_month: dict[str, float] = defaultdict(float)
    ep_num = ep_den = 0.0
    for p in positions:
        cost = _f(p, "totalBought") * _f(p, "avgPrice")
        if cost <= 0:
            cost = abs(_f(p, "initialValue"))
        if cost <= 1e-9:
            continue
        pnl = _f(p, "realizedPnl") + _f(p, "cashPnl")
        pnls.append(pnl)
        costs.append(cost)
        m = (p.get("endDate") or "")[:7]
        if m:
            by_month[m] += pnl
        ap = _f(p, "avgPrice")
        if ap > 0:
            ep_num += ap * cost
            ep_den += cost

    sc.n_resolved = len(pnls)            # здесь = число позиций с активностью
    if not pnls:
        sc.reasons.append("нет позиций с активностью")
        return sc

    sc.realized_pnl = sum(pnls)
    sc.total_staked = sum(costs)
    sc.roi = sc.realized_pnl / sc.total_staked if sc.total_staked > 0 else 0.0
    sc.roi_ci_low, sc.roi_ci_high = _bootstrap_roi(pnls, costs)
    sc.win_rate = sum(1 for x in pnls if x > 0) / len(pnls)

    gains = sorted((x for x in pnls if x > 0), reverse=True)
    tot_gain = sum(gains)
    sc.top1_concentration = (gains[0] / tot_gain) if gains and tot_gain > 0 else 1.0

    sc.active_months = len(by_month)
    sc.positive_months = sum(1 for v in by_month.values() if v > 0)
    sc.avg_entry_price = ep_num / ep_den if ep_den > 0 else 0.0
    sc.reward_share = 0.0                # /positions не содержит REWARD-событий
    sc.median_hold_hours = 0.0           # в /positions нет времени входа

    # PnL на РЕЗОЛВНУТЫХ рынках: для копи-холда с задержкой важно это, а не флип-прибыль
    settled = [(_f(p, "realizedPnl") + _f(p, "cashPnl")) for p in positions
               if _f(p, "curPrice") <= 0.01 or _f(p, "curPrice") >= 0.99 or p.get("redeemable")]
    sc.resolved_pnl = sum(settled)
    sc.n_settled = len(settled)

    _apply_filters(sc)
    return sc


def analyze(wallet: str, activity: list[dict], positions: list[dict] | None = None) -> Scorecard:
    flows = build_flows(activity)
    if positions:
        _inject_resolved_holdings(flows, positions)
    sc = Scorecard(wallet=wallet, n_markets_total=len(flows))

    resolved = [m for m in flows.values() if m.closed and m.saw_buy]
    sc.n_resolved = len(resolved)
    if not resolved:
        sc.reasons.append("нет закрытых рынков")
        return sc

    pnls = [m.realized_pnl for m in resolved]
    stakes = [m.cash_out for m in resolved]
    sc.realized_pnl = sum(pnls)
    sc.total_staked = sum(stakes)
    sc.roi = sc.realized_pnl / sc.total_staked if sc.total_staked > 0 else 0.0
    sc.roi_ci_low, sc.roi_ci_high = _bootstrap_roi(pnls, stakes)
    sc.win_rate = sum(1 for p in pnls if p > 0) / len(pnls)

    # концентрация: доля профита из самого прибыльного рынка
    gains = sorted((p for p in pnls if p > 0), reverse=True)
    total_gain = sum(gains)
    sc.top1_concentration = (gains[0] / total_gain) if gains and total_gain > 0 else 1.0

    total_reward = sum(m.reward for m in flows.values())
    gross_flow = sum(m.cash_in + m.cash_out for m in flows.values()) or 1.0
    sc.reward_share = total_reward / gross_flow

    sc.two_sided_share = sum(1 for m in flows.values() if m.saw_buy and m.saw_sell) / len(flows)

    holds = sorted(m.hold_seconds for m in resolved if m.hold_seconds > 0)
    sc.median_hold_hours = (holds[len(holds) // 2] / 3600) if holds else 0.0

    ep = [m.avg_entry_price for m in resolved if m.avg_entry_price > 0]
    sc.avg_entry_price = sum(ep) / len(ep) if ep else 0.0

    # консистентность по месяцам
    by_month: dict[str, float] = defaultdict(float)
    for m in resolved:
        if m.last_ts:
            key = time.strftime("%Y-%m", time.gmtime(m.last_ts))
            by_month[key] += m.realized_pnl
    sc.active_months = len(by_month)
    sc.positive_months = sum(1 for v in by_month.values() if v > 0)

    _apply_filters(sc)
    return sc


def _apply_filters(sc: Scorecard) -> None:
    reasons: list[str] = []
    if sc.n_resolved < MIN_RESOLVED_BETS:
        reasons.append(f"мало выборки ({sc.n_resolved} < {MIN_RESOLVED_BETS})")
    if sc.roi_ci_low < MIN_EDGE:
        reasons.append(f"эдж ниже порога (ci_low={sc.roi_ci_low:.1%} < {MIN_EDGE:.0%}) — мал/недостоверен для копирования")
    if sc.top1_concentration > MAX_TOP1_CONCENTRATION:
        reasons.append(f"профит из одного рынка ({sc.top1_concentration:.0%})")
    if sc.reward_share > MAX_REWARD_SHARE:
        reasons.append(f"высокая доля REWARD ({sc.reward_share:.0%}) — похоже LP/MM")
    if sc.total_staked < MIN_TOTAL_STAKED:
        reasons.append(f"мало капитала в игре (${sc.total_staked:,.0f} < ${MIN_TOTAL_STAKED:,.0f}) — ретейл/фарм")
    avg_stake = sc.total_staked / sc.n_resolved if sc.n_resolved else 0.0
    if avg_stake < MIN_AVG_STAKE:
        reasons.append(f"пылевые ставки (avg ${avg_stake:,.0f}) — фарм/бот, не копируемо")
    if sc.active_months >= 3 and sc.positive_months / sc.active_months < 0.5:
        reasons.append("плюсовых месяцев < половины")

    sc.reasons = reasons
    sc.passes = len(reasons) == 0

    # composite только для прошедших: edge_low * log(выборка) * консистентность * log(капитал)
    if sc.passes:
        consistency = sc.positive_months / max(1, sc.active_months)
        sc.composite = (sc.roi_ci_low * math.log10(sc.n_resolved) * consistency
                        * math.log10(max(10.0, sc.total_staked)))


# --------------------------------------------------------------------------- #
# Discovery кандидатов                                                          #
# --------------------------------------------------------------------------- #
def discover_candidates(api: DataAPIClient, seed: list[str], markets: list[str]) -> dict[str, float]:
    """Возвращает {wallet: наблюдаемый объём $} — объём идёт на пре-фильтр и сортировку."""
    vol: dict[str, float] = {a.lower(): 0.0 for a in seed}

    # лидерборд /profit,/volume мёртв (за Cloudflare) — выпилен, чтобы не спамил «недоступен»
    # и не тупил ~15с на ретраях 404. Источники: holders + trades по рынкам (свип) + глобальная лента.

    # по каждому рынку: холдеры + активные трейдеры (объём по usdcSize)
    for cid in markets:
        before = len(vol)
        try:
            for h in api.holders(cid):
                a = h.get("proxyWallet")
                if a:
                    vol.setdefault(a.lower(), 0.0)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] holders({cid[:12]}…) упал: {e}")
        try:
            ts = api.trades_for_market(cid, max_trades=2000)
            for t in ts:
                a = t.get("proxyWallet")
                if not a:
                    continue
                a = a.lower()
                usd = _f(t, "usdcSize", "usdc", "cash", "value")
                if usd == 0:
                    usd = _f(t, "size") * _f(t, "price")
                vol[a] = vol.get(a, 0.0) + usd
            print(f"  {cid[:12]}…: {len(ts)} трейдов, пул +{len(vol) - before}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] trades({cid[:12]}…) упал: {e}")

    return vol


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #
def cmd_inspect(api: DataAPIClient, wallet: str, max_pages: int = 10) -> None:
    """Тянет активность постранично С ПРОГРЕССОМ и печатает по одному полному
    примеру каждого типа. Не ждёт всю историю молча (старый баг 'нет вывода')."""
    print(f"запрашиваю активность {wallet} …", flush=True)
    counts, example = defaultdict(int), {}
    offset, page_size, total = 0, api.page_size, 0
    for page_i in range(max_pages):
        page = api._get("/activity", {"user": wallet, "limit": page_size,
                                      "offset": offset, "sortBy": "TIMESTAMP",
                                      "sortDirection": "DESC"})
        if not page:
            break
        total += len(page)
        for e in page:
            key = (e.get("type"), e.get("side"))
            counts[key] += 1
            example.setdefault(key, e)
        print(f"  страница {page_i + 1}: +{len(page)} (всего {total})  типы: {dict(counts)}",
              flush=True)
        if ("REDEEM", None) in example and total >= page_size:
            break  # увидели REDEEM — дальше тянуть незачем
        if len(page) < page_size:
            break
        offset += page_size
        time.sleep(api.pause)

    if total == 0:
        print("\nПУСТО: API вернул 0 событий.")
        print("Проверь сеть/адрес — открой этот URL прямо в браузере:")
        print(f"  https://data-api.polymarket.com/activity?user={wallet}&limit=5")
        print("Если в браузере тоже пусто/ошибка — проблема в доступе к data-api.polymarket.com, "
              "а не в коде.")
        return

    print("\nпо одному примеру каждого типа:")
    for key, ev in example.items():
        print(f"\n--- {key} ---")
        print(json.dumps(ev, indent=2, ensure_ascii=False))


def _write_registry(cards: list[dict], path: str) -> None:
    passed = sorted([c for c in cards if c.get("passes")],
                    key=lambda c: c.get("composite", 0.0), reverse=True)
    Path(path).write_text(json.dumps({
        "generated": int(time.time()),
        "n_analyzed": len(cards),
        "n_passed": len(passed),
        "watchlist": [c["wallet"] for c in passed],
        "scorecards": cards,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_candidates(api: DataAPIClient, args) -> list[str]:
    """Дискавери + объёмный пре-фильтр + сортировка биггест-ферст -> список кошельков.
    Вынесено в функцию, чтобы кэшировать результат и не дёргать API заново на резюме."""
    seed = [s.strip() for s in (args.seed or "").split(",") if s.strip()]
    markets = [s.strip() for s in (args.holder_markets or "").split(",") if s.strip()]
    if getattr(args, "auto_markets", 0):
        try:
            top = GammaClient().sweep_markets(n=args.auto_markets,
                                              include_active=getattr(args, "include_active", False))
            n_closed = sum(1 for r in top if r[3])
            print(f"Gamma свип: {len(top)} рынков (закрытых {n_closed}, активных {len(top) - n_closed}). "
                  f"Верх по объёму:")
            for vol, cid, q, closed in top[:8]:
                print(f"  ${vol:>14,.0f}  [{'C' if closed else 'A'}] {cid[:12]}…  {q[:48]}")
            markets = [r[1] for r in top] + markets
        except Exception as e:  # noqa: BLE001
            print(f"[warn] Gamma свип упал ({e}); задай рынки вручную через --holder-markets")

    vol_map = discover_candidates(api, seed, markets)
    if getattr(args, "global_feed", 0):
        try:
            gt = api.recent_trades(max_trades=args.global_feed)
            for t in gt:
                w = (t.get("proxyWallet") or "").lower()
                if w:
                    vol_map[w] = vol_map.get(w, 0.0) + _f(t, "usdcSize", "usdc", "value", "size")
            print(f"глобальная лента: +{len(gt)} трейдов в пул")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] глобальная лента недоступна ({e})")

    print(f"кандидатов в пуле: {len(vol_map)}")
    min_vol = getattr(args, "min_vol", 0.0)
    ranked = sorted(vol_map.items(), key=lambda kv: kv[1], reverse=True)
    candidates = [w for w, v in ranked if v >= min_vol]
    print(f"объёмный пре-фильтр (>= ${min_vol:,.0f}): к анализу {len(candidates)}, "
          f"отсеяно мелких {len(ranked) - len(candidates)}")
    return candidates


def cmd_run(api: DataAPIClient, args) -> None:
    cand_file = Path(args.checkpoint + ".candidates.json")
    if cand_file.exists() and not args.refresh:
        candidates = json.loads(cand_file.read_text(encoding="utf-8"))
        print(f"кандидаты из кэша {cand_file.name}: {len(candidates)} (дискавери пропущен; "
              f"чтобы пересобрать пул — удали этот файл или запусти с --refresh)")
    else:
        candidates = _build_candidates(api, args)
        cand_file.write_text(json.dumps(candidates), encoding="utf-8")
        print(f"кандидаты сохранены в {cand_file.name} — резюме продолжит с того же места")

    if getattr(args, "limit_wallets", None):
        candidates = candidates[: args.limit_wallets]
        print(f"ограничено первыми {len(candidates)} (--limit-wallets)")

    # ЧЕКПОИНТ: возобновление + крэш-устойчивость для ночного прогона
    ckpt = Path(args.checkpoint)
    done: dict[str, dict] = {}
    if ckpt.exists():
        for line in ckpt.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                done[d["wallet"]] = d
            except Exception:  # noqa: BLE001
                pass
        print(f"чекпоинт {ckpt.name}: уже обработано {len(done)} — пропускаю их")

    cards: list[dict] = list(done.values())
    fout = ckpt.open("a", encoding="utf-8")
    try:
        for i, w in enumerate(candidates, 1):
            if w in done:
                continue
            try:
                pos, trunc = load_positions(api, w, refresh=args.refresh)
                sc = analyze_pos(w, pos)
                sc.truncated = trunc
                if trunc:                              # упёрся в потолок -> отдельный лог, в зачёт не идёт
                    sc.passes = False
                    sc.reasons.append(f"обрезан по потолку {len(pos)} позиций — ручная проверка")
                    with open(args.truncated_log, "a", encoding="utf-8") as tf:
                        tf.write(w + "\n")
                d = asdict(sc)
                fout.write(json.dumps(d, ensure_ascii=False) + "\n")
                fout.flush()                          # каждая строка на диск сразу
                cards.append(d)
                flag = "PASS" if sc.passes else "skip"
                print(f"[{i}/{len(candidates)}] {w[:10]}… {flag} "
                      f"n={sc.n_resolved} "
                      f"staked=${sc.total_staked:,.0f} roi={sc.roi:.1%} ci_low={sc.roi_ci_low:.1%}",
                      flush=True)
                if len(cards) % 25 == 0:              # инкрементальная запись реестра каждые 25
                    _write_registry(cards, args.out)
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(candidates)}] {w[:10]}… ОШИБКА {e}", flush=True)
    finally:
        fout.close()

    _write_registry(cards, args.out)
    passed = sorted([c for c in cards if c.get("passes")],
                    key=lambda c: c.get("composite", 0.0), reverse=True)
    print(f"\nготово: обработано {len(cards)}, прошли фильтр {len(passed)} -> {args.out}")
    for c in passed[:30]:
        print(f"  {c['wallet']}  composite={c.get('composite',0):.3f}  "
              f"roi={c.get('roi',0):.1%} [{c.get('roi_ci_low',0):.1%}..{c.get('roi_ci_high',0):.1%}]  "
              f"n={c.get('n_resolved',0)}  staked=${c.get('total_staked',0):,.0f}  "
              f"months={c.get('positive_months',0)}/{c.get('active_months',0)}")


def _is_whale_watch(c: dict) -> bool:
    """Крупный, в плюсе, не LP/MM, не один колл — но НЕ добрал выборку (n<100).
    Статистически не подтверждён, годится только для ручного наблюдения."""
    n = c.get("n_resolved", 0)
    return (
        not c.get("passes", False)
        and WHALE_MIN_BETS <= n < MIN_RESOLVED_BETS                 # недобрал выборку
        and c.get("total_staked", 0) >= WHALE_MIN_STAKED            # но крупные деньги
        and c.get("roi_ci_low", 0) > 0                             # и в плюсе (пусть шатко)
        and c.get("reward_share", 1.0) <= MAX_REWARD_SHARE         # не LP/MM
        and c.get("top1_concentration", 1.0) <= MAX_TOP1_CONCENTRATION  # не один колл
        and not c.get("truncated", False)                              # не обрезан
    )


def cmd_whales(args) -> None:
    """Бакет «крупные, но недобрали выборку» из уже собранных данных.
    Только чтение файлов — API не трогает, ночному прогону не мешает."""
    cards: dict[str, dict] = {}
    src = Path(args.checkpoint)
    if src.exists():
        for line in src.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                cards[d["wallet"]] = d                              # дедуп: последняя запись
            except Exception:  # noqa: BLE001
                pass
    else:
        reg = Path(args.out)
        if reg.exists():
            for d in json.loads(reg.read_text(encoding="utf-8")).get("scorecards", []):
                cards[d["wallet"]] = d
    if not cards:
        print(f"Нет данных: ни {args.checkpoint}, ни {args.out} не найдены/пусты. "
              f"Сначала запусти прогон.")
        return

    whales = [c for c in cards.values() if _is_whale_watch(c)]
    # ранжируем по «долларовому нижнему эджу»: стейк * нижняя граница ROI
    whales.sort(key=lambda c: c.get("total_staked", 0) * c.get("roi_ci_low", 0), reverse=True)

    Path(args.whales_out).write_text(json.dumps({
        "generated": int(time.time()),
        "note": ("Крупные кошельки, не добравшие выборку (n<100). НЕ подтверждены статистически "
                 "(плюс перекос проигрышей-до-экспирации завышает ROI на малом n). Только ручное наблюдение."),
        "criteria": {"min_staked": WHALE_MIN_STAKED,
                     "bets_range": [WHALE_MIN_BETS, MIN_RESOLVED_BETS],
                     "roi_ci_low": ">0"},
        "count": len(whales),
        "whales": [c["wallet"] for c in whales],
        "scorecards": whales,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"крупных-но-недобравших: {len(whales)} из {len(cards)} обработанных -> {args.whales_out}")
    for c in whales[:40]:
        print(f"  {c['wallet']}  staked=${c.get('total_staked',0):,.0f}  "
              f"roi={c.get('roi',0):.1%} [ci_low {c.get('roi_ci_low',0):.1%}]  "
              f"n={c.get('n_resolved',0)}  months={c.get('positive_months',0)}/{c.get('active_months',0)}")


def cmd_inspect_positions(api: DataAPIClient, wallet: str) -> None:
    """Дамп /positions: каким полем считать ЧЕСТНЫЙ PnL (с учётом проигрышей-до-экспирации).
    Сравниваем realizedPnl vs cashPnl и поведение на выигравшей/проигравшей позиции."""
    print(f"тяну /positions {wallet} …", flush=True)
    pos = api.positions(wallet)
    print(f"позиций: {len(pos)}")
    if not pos:
        print(f"ПУСТО. Проверь в браузере: https://data-api.polymarket.com/positions?user={wallet}&limit=5")
        return

    keys = set()
    for p in pos:
        keys |= set(p.keys())
    print("ключи позиции:", sorted(keys))

    print("\nсуммы по ВСЕМ позициям:")
    print(f"  realizedPnl = {sum(_f(p, 'realizedPnl') for p in pos):,.0f}")
    print(f"  cashPnl     = {sum(_f(p, 'cashPnl') for p in pos):,.0f}")

    def is_resolved(p: dict) -> bool:
        cp = _f(p, "curPrice")
        return cp <= 0.001 or cp >= 0.999 or bool(p.get("redeemable"))

    res = [p for p in pos if is_resolved(p)]
    print(f"\nresolved-позиций (curPrice ~0/~1 или redeemable): {len(res)} из {len(pos)}")
    print(f"  sum realizedPnl(resolved) = {sum(_f(p, 'realizedPnl') for p in res):,.0f}")
    print(f"  sum cashPnl(resolved)     = {sum(_f(p, 'cashPnl') for p in res):,.0f}")

    won = next((p for p in res if _f(p, "curPrice") >= 0.999), None)
    lost = next((p for p in res if _f(p, "curPrice") <= 0.001), None)
    for label, p in (("ВЫИГРАЛ (curPrice~1)", won), ("ПРОИГРАЛ (curPrice~0)", lost)):
        print(f"\n--- пример: {label} ---")
        print(json.dumps(p, indent=2, ensure_ascii=False) if p else "  (не найдено в выборке)")


def _positions_pnl(positions: list[dict]) -> dict:
    """Честный PnL по /positions. Для зарезолвленной позиции pnl = realizedPnl + cashPnl
    (включает проигрыши-до-экспирации, которые реконструкция из activity теряет).
    invested ≈ totalBought*avgPrice (totalBought — это КОЛ-ВО токенов, не доллары)."""
    pnls: list[float] = []
    invs: list[float] = []
    for p in positions:
        cp = _f(p, "curPrice")
        if not (cp <= 0.01 or cp >= 0.99 or p.get("redeemable")):
            continue  # рынок ещё не зарезолвлен — пропускаем
        inv = _f(p, "totalBought") * _f(p, "avgPrice")
        if inv <= 0:
            inv = abs(_f(p, "initialValue"))
        if inv <= 0:
            continue
        pnls.append(_f(p, "realizedPnl") + _f(p, "cashPnl"))
        invs.append(inv)
    staked = sum(invs)
    pnl = sum(pnls)
    ci_lo, ci_hi = _bootstrap_roi(pnls, invs) if pnls else (0.0, 0.0)
    return {"n": len(pnls), "staked": staked, "pnl": pnl,
            "roi": (pnl / staked if staked > 0 else 0.0), "ci_low": ci_lo, "ci_high": ci_hi}


def _merged_pnl(activity: list[dict], positions: list[dict]) -> dict:
    """ЧЕСТНЫЙ PnL = леджер из activity (полная история, правильный n, редимы-выигрыши)
    + дооценка остатка УДЕРЖАННЫХ токенов по финальной цене из /positions (проигрыши-до-экспирации).
    Ключ — (conditionId, outcome), чтобы оценивать нужную сторону. Лимитация: activity режется на 3500."""
    cash_in: dict = defaultdict(float)   # SELL + REDEEM + MERGE
    cash_out: dict = defaultdict(float)  # BUY + SPLIT
    held: dict = defaultdict(float)      # чистый остаток токенов на руках
    for e in activity:
        cid, outc = e.get("conditionId"), e.get("outcome")
        if not cid:
            continue
        k = (cid, outc)
        typ = (e.get("type") or "").upper()
        side = (e.get("side") or "").upper()
        size, price = _f(e, "size"), _f(e, "price")
        usd = _f(e, "usdcSize", "usdcValue", "usdc", "cash", "value") or size * price
        if typ == "TRADE" and side == "BUY":
            cash_out[k] += usd
            held[k] += size
        elif typ == "TRADE" and side == "SELL":
            cash_in[k] += usd
            held[k] -= size
        elif typ == "REDEEM":
            cash_in[k] += usd or size
            held[k] -= size
        elif typ == "MERGE":
            cash_in[k] += usd or size
        elif typ == "SPLIT":
            cash_out[k] += usd or size

    cp = {(p.get("conditionId"), p.get("outcome")): _f(p, "curPrice") for p in positions}

    pnls: list[float] = []
    invs: list[float] = []
    for k in set(cash_out) | set(cash_in):
        co, ci, h = cash_out[k], cash_in[k], held[k]
        if co <= 0:
            continue                              # не покупал — нет базы, пропуск
        if h > 1e-6:                              # ещё держит токены — нужна судьба рынка
            price = cp.get(k)
            if price is None or not (price <= 0.01 or price >= 0.99):
                continue                          # рынка нет в снапшоте либо ещё открыт -> не резолв
            ci += h * price                       # дооценка: проигравший ~0, выигравший ~tokens
        pnls.append(ci - co)
        invs.append(co)

    staked, pnl = sum(invs), sum(pnls)
    ci_lo, ci_hi = _bootstrap_roi(pnls, invs) if pnls else (0.0, 0.0)
    return {"n": len(pnls), "staked": staked, "pnl": pnl,
            "roi": (pnl / staked if staked > 0 else 0.0), "ci_low": ci_lo, "ci_high": ci_hi}


def cmd_verify(api: DataAPIClient, args) -> None:
    """Пересчёт ЧЕСТНОГО ROI по /positions для шортлиста (PASS + whale-бакет, либо --seed).
    Показывает activity-ROI (потолок) против positions-ROI (учитывает проигрыши) + вердикт."""
    cards: dict[str, dict] = {}
    src = Path(args.checkpoint)
    if src.exists():
        for line in src.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                cards[d["wallet"]] = d
            except Exception:  # noqa: BLE001
                pass
    elif Path(args.out).exists():
        for d in json.loads(Path(args.out).read_text(encoding="utf-8")).get("scorecards", []):
            cards[d["wallet"]] = d
    if not cards:
        print("Нет данных прогона. Сначала запусти прогон.")
        return

    seed = [s.strip().lower() for s in (args.seed or "").split(",") if s.strip()]
    if seed:
        shortlist = [cards.get(a, {"wallet": a}) for a in seed]
    else:
        shortlist = [c for c in cards.values() if c.get("passes") or _is_whale_watch(c)]
    print(f"к честной сверке: {len(shortlist)} кошельков")

    results: list[dict] = []
    for i, c in enumerate(shortlist, 1):
        w = c["wallet"]
        try:
            h = _merged_pnl(load_activity(api, w), api.positions(w))
            old_roi, old_ci = c.get("roi", 0.0), c.get("roi_ci_low", 0.0)
            verdict = "OK" if (h["n"] >= MIN_RESOLVED_BETS and h["ci_low"] > 0
                               and h["staked"] >= MIN_TOTAL_STAKED) else "FAIL"
            results.append({"wallet": w, "old_roi": old_roi, "old_ci_low": old_ci,
                            **h, "verdict": verdict})
            print(f"[{i}/{len(shortlist)}] {w[:10]}…  activity={old_roi:.1%}(ci {old_ci:.1%}) "
                  f"-> positions={h['roi']:.1%}(ci {h['ci_low']:.1%})  n={h['n']}  "
                  f"staked=${h['staked']:,.0f}  {verdict}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(shortlist)}] {w[:10]}… ОШИБКА {e}", flush=True)

    Path(args.verify_out).write_text(json.dumps({
        "generated": int(time.time()),
        "note": ("positions учитывает проигрыши-до-экспирации (честнее), но может недосчитать "
                 "давно погашенные выигрыши. Доверяем тем, у кого ОБА ROI положительны и ci_low>0."),
        "results": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = [r for r in results if r["verdict"] == "OK"]
    print(f"\nвыдержали честную проверку: {len(ok)}/{len(results)} -> {args.verify_out}")


def _structural_ok(c: dict) -> bool:
    """Все гейты КРОМЕ величины эджа: выборка, капитал, не пыль, не MM, не один колл, в плюсе.
    Нужно для --rank — крутить пороги эджа без переанализа."""
    n = c.get("n_resolved", 0)
    am = c.get("active_months", 0)
    pm = c.get("positive_months", 0)
    avg = c.get("total_staked", 0) / n if n else 0.0
    return (n >= MIN_RESOLVED_BETS
            and c.get("roi_ci_low", 0) > 0
            and c.get("total_staked", 0) >= MIN_TOTAL_STAKED
            and avg >= MIN_AVG_STAKE
            and c.get("reward_share", 1.0) <= MAX_REWARD_SHARE
            and c.get("top1_concentration", 1.0) <= MAX_TOP1_CONCENTRATION
            and not (am >= 3 and pm / max(1, am) < 0.5)
            and not c.get("truncated", False))


def cmd_rank(args) -> None:
    """По готовому чекпоинту: сколько кошельков выживает при разных порогах эджа (БЕЗ переанализа).
    Пишет финальный список при выбранном --min-edge."""
    cards: dict[str, dict] = {}
    src = Path(args.checkpoint)
    if src.exists():
        for line in src.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                cards[d["wallet"]] = d
            except Exception:  # noqa: BLE001
                pass
    elif Path(args.out).exists():
        for d in json.loads(Path(args.out).read_text(encoding="utf-8")).get("scorecards", []):
            cards[d["wallet"]] = d
    if not cards:
        print("Нет данных прогона. Сначала запусти прогон.")
        return

    trunc = [c for c in cards.values() if c.get("truncated")]
    if trunc:
        print(f"обрезанных по потолку позиций (НЕ в зачёте, см. {args.truncated_log}): {len(trunc)}\n")
    ok = [c for c in cards.values() if _structural_ok(c)]
    print(f"обработано {len(cards)}, структурно валидных (выборка/капитал/не MM/в плюсе): {len(ok)}\n")
    print("выживаемость по порогу эджа (ci_low):")
    for thr in (0.01, 0.02, 0.03, 0.05, 0.10):
        cnt = sum(1 for c in ok if c.get("roi_ci_low", 0) >= thr)
        mark = "   <- выбран" if abs(thr - args.min_edge) < 1e-9 else ""
        print(f"  >= {thr:>4.0%}: {cnt}{mark}")

    final = sorted([c for c in ok if c.get("roi_ci_low", 0) >= args.min_edge],
                   key=lambda c: c.get("roi_ci_low", 0), reverse=True)
    Path(args.rank_out).write_text(json.dumps({
        "generated": int(time.time()),
        "min_edge": args.min_edge,
        "count": len(final),
        "watchlist": [c["wallet"] for c in final],
        "scorecards": final,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nпри min-edge {args.min_edge:.0%}: {len(final)} кошельков -> {args.rank_out}")
    for c in final[:30]:
        rp = c.get("resolved_pnl", 0.0)
        tag = "[флип-профиль]" if rp <= 0 else "[резолв-профиль]"
        print(f"  {c['wallet']}  roi={c.get('roi',0):.1%} [ci_low {c.get('roi_ci_low',0):.1%}]  "
              f"n={c.get('n_resolved',0)}  staked=${c.get('total_staked',0):,.0f}  "
              f"resolved_pnl=${rp:,.0f}/{c.get('n_settled',0)}  {tag}")


def main() -> None:
    p = argparse.ArgumentParser(description="Polymarket wallet quality analyzer (pm-edge)")
    sub = p.add_subparsers(dest="cmd")

    p.add_argument("--inspect", metavar="ADDR", help="дамп сырой активности одного кошелька")
    p.add_argument("--inspect-positions", metavar="ADDR", help="дамп /positions кошелька (сверка честного PnL)")
    p.add_argument("--seed", help="comma-separated адреса для затравки")
    p.add_argument("--holder-markets", help="comma-separated conditionId зарезолвленных рынков для discovery через холдеров")
    p.add_argument("--refresh", action="store_true", help="игнорировать кэш")
    p.add_argument("--limit-wallets", type=int, help="анализировать не больше N кошельков (быстрый тест)")
    p.add_argument("--auto-markets", type=int, default=0, help="широкий свип топ-N рынков по объёму (Gamma) для дискавери")
    p.add_argument("--include-active", action="store_true", help="включать в свип активные (незакрытые) рынки — ловит тех, кто торгует сейчас")
    p.add_argument("--global-feed", type=int, default=0, help="добавить N трейдов из глобальной ленты (самый широкий источник кошельков)")
    p.add_argument("--min-vol", type=float, default=0.0, help="пропускать кошельки с наблюдаемым объёмом < $X (дешёвая отсечка пыли)")
    p.add_argument("--checkpoint", default="wallet_progress.jsonl", help="файл чекпоинта для возобновления ночного прогона")
    p.add_argument("--out", default="wallet_registry.json")
    p.add_argument("--whales", action="store_true", help="не запускать прогон, а собрать бакет «крупные, но недобрали выборку» из чекпоинта")
    p.add_argument("--whales-out", default="whale_watchlist.json", help="куда писать ручной watchlist китов")
    p.add_argument("--verify", action="store_true", help="пересчитать честный ROI по /positions для шортлиста (PASS + whale-бакет)")
    p.add_argument("--verify-out", default="verify_report.json", help="куда писать отчёт честной сверки")
    p.add_argument("--min-edge", type=float, default=0.03, help="минимальный ci_low для прохода (эдж; дефолт 3%)")
    p.add_argument("--min-staked", type=float, default=5_000, help="минимальный суммарный стейк (размер не важен — ставь хоть 0; дефолт $5k)")
    p.add_argument("--rank", action="store_true", help="по чекпоинту: выживаемость при разных порогах эджа (без переанализа)")
    p.add_argument("--rank-out", default="ranked_watchlist.json", help="куда писать итоговый список при выбранном --min-edge")
    p.add_argument("--truncated-log", default="truncated_wallets.log", help="лог кошельков, обрезанных по потолку позиций (на ручную проверку)")

    args = p.parse_args()

    global MIN_EDGE, MIN_TOTAL_STAKED    # CLI переопределяет пороги
    MIN_EDGE = args.min_edge
    MIN_TOTAL_STAKED = args.min_staked

    if args.rank:
        cmd_rank(args)
    elif args.inspect:
        cmd_inspect(DataAPIClient(), args.inspect)
    elif args.inspect_positions:
        cmd_inspect_positions(DataAPIClient(), args.inspect_positions)
    elif args.verify:
        cmd_verify(DataAPIClient(), args)
    elif args.whales:
        cmd_whales(args)                 # чистое чтение файлов, API не трогаем — прогон не мешаем
    else:
        cmd_run(DataAPIClient(), args)


if __name__ == "__main__":
    main()
