#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
direct_source.py — ПРЯМОЙ источник сигналов из data-api Polymarket (минуя наш сервер).

ЗАЧЕМ. Текущая цепочка: цель торгует -> наш сервер опрашивает раз в 60с -> пишет в книгу ->
бот опрашивает сервер раз в 20-30с -> ордер. Медианная задержка 45-90 секунд. На копи-трейдинге
это съедает основную часть эджа: мы систематически входим хуже цели (в наших же метриках
«Задержка$» доходила до -649).

ЗДЕСЬ: бот сам опрашивает активность своих кошельков напрямую. Один хоп вместо трёх ->
латентность падает до величины интервала опроса (5-15с). Формат ответа ИДЕНТИЧЕН
/api/signals, поэтому вся логика гвардов/лимитов в run_loop не меняется.

Ограничение честно: это всё ещё ОПРОС, а не push. Настоящий следующий шаг — подписка на
события контракта в Polygon по WSS (латентность ~время блока, ~2с). Но она требует RPC-провайдера,
декодирования логов и обработки реоргов, поэтому делается отдельно и после обкатки этого шага.
"""
import time

import requests

import copy_trader as ct

DATA_API = "https://data-api.polymarket.com"


class DirectSource:
    """Опрашивает /activity по каждому кошельку и отдаёт сигналы в формате /api/signals.

    signals: [{t, w, tok, cid, px, out, title}]  — покупки цели
    exits:   [{t, w, tok, cid, frac, out, title}] — продажи цели (frac = доля её холдинга)
    """

    def __init__(self, sess=None, per_wallet_limit=25):
        self.s = sess or requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        self.limit = per_wallet_limit
        self._pos_cache = {}      # (wallet, token) -> (size, ts) — их холдинги, для расчёта frac
        self._pos_ts = {}         # wallet -> когда последний раз тянули позиции

    # ── позиции цели: нужны, чтобы понять, ПОЛНОСТЬЮ ли она вышла (frac) ──
    def _their_size(self, wallet, token, max_age=300):
        key = (wallet, str(token))
        hit = self._pos_cache.get(key)
        if hit and time.time() - hit[1] < max_age:
            return hit[0]
        if time.time() - self._pos_ts.get(wallet, 0) < 20:   # не долбим API чаще раза в 20с
            return hit[0] if hit else None
        self._pos_ts[wallet] = time.time()
        try:
            pos = self.s.get(f"{DATA_API}/positions?user={wallet}&limit=500", timeout=15).json()
        except Exception:  # noqa: BLE001
            return hit[0] if hit else None
        now = time.time()
        for p in pos if isinstance(pos, list) else []:
            self._pos_cache[(wallet, str(p.get("asset")))] = (float(p.get("size") or 0), now)
        got = self._pos_cache.get(key)
        return got[0] if got else 0.0

    def _activity(self, wallet):
        try:
            r = self.s.get(f"{DATA_API}/activity",
                           params={"user": wallet, "limit": self.limit}, timeout=15)
            d = r.json()
            return d if isinstance(d, list) else []
        except Exception:  # noqa: BLE001
            return []

    def fetch(self, since, wallets):
        """Одна «пачка» сигналов новее since по списку кошельков. Формат = /api/signals."""
        signals, exits = [], []
        for w in wallets:
            wl = str(w).lower()
            for e in self._activity(wl):
                if not isinstance(e, dict) or (e.get("type", "") or "").upper() != "TRADE":
                    continue
                ts = int(e.get("timestamp") or 0)
                if ts <= since:
                    continue
                title = e.get("title") or ""
                if ct._blocked_reason(title):        # те же категорийные блоки, что на сервере
                    continue
                tok = str(e.get("asset") or "")
                px = float(e.get("price") or 0)
                if not tok or not (0 < px < 1):
                    continue
                rec = {"t": ts, "w": wl, "tok": tok, "cid": e.get("conditionId") or "",
                       "px": round(px, 4), "out": e.get("outcome") or "", "title": title[:60]}
                side = (e.get("side", "") or "").upper()
                if side == "BUY":
                    rec["spend"] = round(px * float(e.get("size") or 0), 2)
                    signals.append(rec)
                elif side == "SELL":
                    # frac = какую долю СВОЕГО холдинга цель продала. Нужен, чтобы не дёргаться
                    # на мелкие скейл-ауты (EXIT_MIN_FRAC). Осталось ~0 -> вышла полностью.
                    sold = float(e.get("size") or 0)
                    left = self._their_size(wl, tok)
                    rec["frac"] = round(sold / (sold + left), 3) if (left is not None and sold + left > 0) else 1.0
                    exits.append(rec)
        signals.sort(key=lambda x: x["t"])
        exits.sort(key=lambda x: x["t"])
        return {"now": int(time.time()), "paused": False,
                "signals": signals, "exits": exits, "source": "direct"}
