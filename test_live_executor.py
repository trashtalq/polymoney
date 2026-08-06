#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты ДЕНЕЖНОГО ПУТИ (live_executor.py) — кода, который тратит реальные деньги.

Зачем: за два дня в исполнитель внесено ~15 правок, а покрытия не было вообще (все 25
существующих тестов — про книгу copy_trader). Три бага нашлись случайно, и каждый стоил бы денег:
  * resp_ok считал ОТКЛОНЁННЫЕ ордера купленными;
  * частичный филл учитывался как полный -> лимиты «протекали»;
  * env-переопределения молча игнорировались -> контур торговал не теми настройками.
Все три зафиксированы тестами ниже, чтобы не вернулись.

Сеть НЕ используется: HTTP и SDK подменяются заглушками.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "le", Path(__file__).resolve().parent / "live_executor.py")
le = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(le)


# ─────────────────────────── заглушки ───────────────────────────
class FakeResp:
    """Ответ HTTP-заглушки."""
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        return self._p


class FakeSession:
    """Отдаёт заранее заданные ответы по подстроке URL."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []
        self.headers = {}

    def get(self, url, **kw):
        self.calls.append(url)
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(payload)
        return FakeResp([], 404)


class Accepted:
    """AcceptedOrder из SDK: ok=True + фактические объёмы исполнения."""
    def __init__(self, making, taking):
        self.ok, self.status = True, "matched"
        self.making_amount, self.taking_amount = making, taking


class Rejected:
    """RejectedOrder из SDK: ok=False. ИМЕННО ЭТО раньше считалось успехом."""
    def __init__(self, code="fak_not_filled"):
        self.ok, self.code, self.message = False, code, "no match"


# ─────────────────────────── исполнение ордеров ───────────────────────────
class TestOrderOutcome(unittest.TestCase):
    def test_rejected_order_is_not_success(self):
        """РЕГРЕСС: отклонённый ордер НЕ должен считаться покупкой.
        Раньше resp_ok искал поле `success` (которого у SDK нет) и в конце возвращал True —
        бот отмечал сигнал исполненным, крутил счётчики трат и «держал» несуществующую позицию."""
        for code in ("fak_not_filled", "not_enough_balance", "unmatched"):
            self.assertFalse(le.resp_ok(Rejected(code)), f"отказ {code} принят за успех")

    def test_accepted_order_is_success(self):
        self.assertTrue(le.resp_ok(Accepted(3.0, 5.0)))

    def test_reason_is_extracted(self):
        self.assertEqual(le.resp_reason(Rejected("not_enough_balance")), "not_enough_balance")

    def test_partial_fill_counts_actual_not_intended(self):
        """РЕГРЕСС: в счётчики должно идти РЕАЛЬНО исполненное, иначе лимиты протекают.
        BUY: making_amount = отданные USDC."""
        self.assertAlmostEqual(le.resp_filled_usd(Accepted(2.5, 4.1), "BUY", 3.0), 2.5)
        self.assertAlmostEqual(le.resp_filled_shares(Accepted(2.5, 4.1), "BUY", 0), 4.1)

    def test_sell_fill_reads_opposite_side(self):
        """SELL: отдаём доли (making), получаем USDC (taking)."""
        self.assertAlmostEqual(le.resp_filled_usd(Accepted(10.0, 7.3), "SELL", 0), 7.3)
        self.assertAlmostEqual(le.resp_filled_shares(Accepted(10.0, 7.3), "SELL", 0), 10.0)

    def test_fill_falls_back_when_sdk_silent(self):
        """Нет полей объёма -> берём задуманное, но не роняем бота."""
        class Bare:
            ok = True
        self.assertAlmostEqual(le.resp_filled_usd(Bare(), "BUY", 4.0), 4.0)


# ─────────────────────────── стакан и проскальзывание ───────────────────────────
class TestOrderBookFill(unittest.TestCase):
    BOOK = {"asks": [{"price": "0.50", "size": "100"},     # $50
                     {"price": "0.60", "size": "100"},     # $60
                     {"price": "0.90", "size": "1000"}],
            "bids": [{"price": "0.48", "size": "100"},
                     {"price": "0.40", "size": "100"}]}

    def test_small_order_takes_best_price(self):
        s = FakeSession({"/book": self.BOOK})
        vwap, sh, full = le.clob_fill(s, "T", "BUY", usd=10)
        self.assertAlmostEqual(vwap, 0.50, places=4)
        self.assertTrue(full)

    def test_large_order_eats_depth_and_worsens_price(self):
        """Крупная заявка обязана дорожать: иначе симуляция врёт про масштабирование."""
        s = FakeSession({"/book": self.BOOK})
        vwap, _sh, _f = le.clob_fill(s, "T", "BUY", usd=110)   # съедает два уровня
        self.assertGreater(vwap, 0.50)
        self.assertLess(vwap, 0.60)

    def test_partial_fill_when_book_too_thin(self):
        s = FakeSession({"/book": {"asks": [{"price": "0.50", "size": "10"}], "bids": []}})
        vwap, sh, full = le.clob_fill(s, "T", "BUY", usd=100)
        self.assertFalse(full, "тонкий стакан должен давать ЧАСТИЧНОЕ исполнение")
        self.assertAlmostEqual(sh, 10.0)

    def test_sell_uses_bids_and_is_cheaper_than_buy(self):
        """Спред платим с обеих сторон: продажа обязана быть дешевле покупки."""
        s = FakeSession({"/book": self.BOOK})
        buy, _, _ = le.clob_fill(s, "T", "BUY", usd=10)
        sell, _, _ = le.clob_fill(s, "T", "SELL", shares=10)
        self.assertGreater(buy, sell)

    def test_empty_book_returns_none(self):
        s = FakeSession({"/book": {"asks": [], "bids": []}})
        self.assertIsNone(le.clob_fill(s, "T", "BUY", usd=10))


# ─────────────────────────── позиции и экспозиция ───────────────────────────
class TestHeldPositions(unittest.TestCase):
    POS = [{"asset": "A", "initialValue": "10", "size": "20", "redeemable": False},
           {"asset": "B", "initialValue": "5", "size": "8", "redeemable": True},   # резолвнутая
           {"asset": "C", "initialValue": "7", "size": "9"}]

    def test_resolved_positions_excluded_from_exposure(self):
        """РЕГРЕСС: резолвнутые считались занятой экспозицией -> бот зря блокировал входы
        (реально: считал занятыми $87.94 вместо $46) и раздувал сводку."""
        s = FakeSession({"/positions": self.POS})
        held = le.fetch_held(s, "0xfunder")
        self.assertNotIn("B", held, "резолвнутая позиция не должна занимать лимит")
        self.assertEqual(sum(v["usd"] for v in held.values()), 17.0)

    def test_api_failure_returns_none_not_empty(self):
        """Сбой API != «позиций нет». Пустой словарь обнулил бы экспозицию и снял лимиты."""
        s = FakeSession({"/positions": ConnectionError("нет сети")})
        self.assertIsNone(le.fetch_held(s, "0xfunder"))


# ─────────────────────────── состояние ───────────────────────────
class TestStatePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = le.STATE_FILE
        le.STATE_FILE = self.tmp / "state.json"

    def tearDown(self):
        le.STATE_FILE = self._orig

    def test_roundtrip(self):
        le.st_save({"done": ["a"], "last_t": 42, "spent_total": 1.5})
        self.assertEqual(le.st_load()["last_t"], 42)

    def test_survives_locked_file_without_crashing(self):
        """РЕГРЕСС: PermissionError при подмене файла ронял торгующего бота.
        Сбой записи ЖУРНАЛА не должен останавливать торговлю."""
        le.st_save({"done": [], "last_t": 1})
        orig = os.replace
        calls = {"n": 0}

        def boom(a, b):
            calls["n"] += 1
            raise PermissionError(5, "занято")
        os.replace = boom
        try:
            le.st_save({"done": [], "last_t": 2})      # не должно бросить
        finally:
            os.replace = orig
        self.assertGreater(calls["n"], 1, "должны быть ретраи, а не одна попытка")


# ─────────────────────────── защита от инъекции и режимы ───────────────────────────
class TestGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._a, self._h = le.ALLOW_FILE, le.HERE
        le.ALLOW_FILE = self.tmp / "allow.json"
        le.HERE = self.tmp
        le._ALLOW["mtime"], le._ALLOW["set"] = 0, set()

    def tearDown(self):
        le.ALLOW_FILE, le.HERE = self._a, self._h

    def test_allowlist_is_lowercased(self):
        le.ALLOW_FILE.write_text(json.dumps(["0xAAA", "0xBbB"]), encoding="utf-8")
        self.assertEqual(le.load_allowlist(), {"0xaaa", "0xbbb"})

    def test_missing_allowlist_means_no_filter(self):
        self.assertEqual(le.load_allowlist(), set())

    def test_hold_flag_read(self):
        (self.tmp / "hold_only.json").write_text('{"hold": true}', encoding="utf-8")
        self.assertTrue(le.hold_only(False))

    def test_hold_defaults_off_when_file_broken(self):
        (self.tmp / "hold_only.json").write_text("не json", encoding="utf-8")
        self.assertFalse(le.hold_only(False), "битый флаг не должен останавливать торговлю")


# ─────────────────────────── конфигурация ───────────────────────────
class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._h = le.HERE
        le.HERE = self.tmp
        (self.tmp / "polymarket.env").write_text(
            "MODE=dry\nPAPER_PER_TRADE=10\nPRIVATE_KEY=secret\n", encoding="utf-8")
        self._env = dict(os.environ)

    def tearDown(self):
        le.HERE = self._h
        os.environ.clear()
        os.environ.update(self._env)

    def test_env_overrides_prefixed_keys(self):
        """РЕГРЕСС: PAPER_*/RISK_* не были в белом списке -> настройки контуров молча
        игнорировались, и контур «объём» торговал чужими лимитами."""
        os.environ["PAPER_PER_TRADE"] = "5"
        os.environ["RISK_STOP_DD"] = "0.2"
        cfg = le.load_env()
        self.assertEqual(cfg["PAPER_PER_TRADE"], "5", "env должен побеждать файл")
        self.assertEqual(cfg["RISK_STOP_DD"], "0.2")

    def test_file_values_used_when_no_env(self):
        cfg = le.load_env()
        self.assertEqual(cfg["PAPER_PER_TRADE"], "10")

    def test_tg_only_for_real_money_modes(self):
        """В публичный канал пишет только реальный бот: виртуальные сделки рядом с настоящими
        вводят читателей в заблуждение."""
        base = {"TG_ENABLED": "1", "TG_TOKEN": "t", "TG_CHAT": "@c"}
        self.assertTrue(le.tg_init(dict(base), "live"))
        self.assertFalse(le.tg_init(dict(base), "paper"))
        self.assertFalse(le.tg_init(dict(base), "dry"))
        self.assertTrue(le.tg_init(dict(base, TG_PAPER="1"), "paper"), "явное включение работает")

    def test_tg_off_by_default(self):
        self.assertFalse(le.tg_init({}, "live"), "без TG_ENABLED ничего не уходит")


# ─────────────────────────── теневое исполнение ───────────────────────────
class TestPaperExecution(unittest.TestCase):
    def setUp(self):
        self.p = {"bankroll": 1000.0, "cash": 1000.0, "pos": {}, "meta": {},
                  "realized": 0.0, "buys": 0, "sells": 0}
        self.sess = FakeSession({"/book": {"asks": [{"price": "0.50", "size": "1000"}],
                                           "bids": [{"price": "0.45", "size": "1000"}]}})

    def _client(self):
        c = le.PaperClient(self.p, api=None, slip=0.01)
        c.s = self.sess
        return c

    def test_buy_spends_cash_and_opens_position(self):
        self._client().place_market_order(token_id="T", side="BUY", amount=10, max_price=0.9)
        self.assertAlmostEqual(self.p["cash"], 990.0, places=2)
        self.assertAlmostEqual(self.p["pos"]["T"]["shares"], 20.0, places=2)

    def test_price_cap_blocks_fill_like_real_fak(self):
        with self.assertRaises(Exception):
            self._client().place_market_order(token_id="T", side="BUY", amount=10, max_price=0.40)

    def test_roundtrip_loses_spread(self):
        """Купили по аску, продали по биду -> обязаны потерять спред. Раньше paper продавал по
        мидпоинту и был систематически прибыльнее реальности."""
        c = self._client()
        c.place_market_order(token_id="T", side="BUY", amount=10, max_price=0.9)
        c.place_market_order(token_id="T", side="SELL", shares=self.p["pos"]["T"]["shares"])
        self.assertLess(self.p["cash"], 1000.0, "round-trip обязан стоить спреда")
        self.assertLess(self.p["realized"], 0)

    def test_equity_counts_cash_plus_marks(self):
        class API:
            def midpoints(self, toks):
                return {t: 0.60 for t in toks}
        self.p["pos"]["T"] = {"shares": 100.0, "cost": 50.0}
        self.p["cash"] = 950.0
        eq, pnl = le.paper_equity(self.p, API())
        self.assertAlmostEqual(eq, 1010.0, places=2)
        self.assertAlmostEqual(pnl, 10.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
