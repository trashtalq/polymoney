#!/usr/bin/env python3
"""Тесты чистой логики копи-системы (без сети): фильтры рынков, пересчёты книги,
атомарная запись, гварды исполнителя. Это СТРАХОВКА ДЕНЕГ: фильтры и пересчёты —
накопленные решения по датам, и их легко сломать следующей правкой незаметно.

Запуск:  python -m unittest test_polymoney -v
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import copy_trader as ct
import live_executor as le


# ----------------------------- фильтр рынков (_blocked_reason) -----------------------------
class TestBlockedReason(unittest.TestCase):
    CASES = [
        # твит-каунт-корзины (решение 2026-07-12)
        ("Will Elon post 180-199 tweets July 11-18?", "tweets"),
        ("<40 tweets by Elon this week?", "tweets"),
        ("Elon Musk 200+ tweets this week?", "tweets"),
        # приоритет: твит-каунт раньше Зеленского
        ("Zelensky to post 10+ tweets this week?", "tweets"),
        # посты Зеленского (та же лотерея, что твит-каунт)
        ("Zelensky posts about Putin this week?", "zelensky"),
        ("Will Zelenskyy tweet about the ceasefire?", "zelensky"),
        # киберспорт (решение 2026-07-12) — ловится раньше спорта
        ("G2 to win 2-0 vs T1 at MSI 2026?", "esports"),
        ("Faker to win League of Legends Worlds?", "esports"),
        # спорт
        ("Real Madrid vs. Barcelona: Real to win?", "sport"),
        ("Arsenal to score first?", "sport"),
        ("Will Manchester United win the Premier League?", "sport"),
        # mention-рынки НЕ режутся спорт-фильтром (решение 2026-07-08)
        ("Will announcers say 'penalty' 3+ times?", None),
        # погода
        ("Highest temperature in London this week?", "weather"),
        ("Hurricane to make landfall in Florida in July?", "weather"),
        # чистые рынки — не блокируются (в т.ч. ложные срабатывания, от которых уходили)
        ("Fed rate cut in September?", None),
        ("Will Ukraine ceasefire hold through August?", None),
        ("Will Stormy Daniels testify?", None),          # НЕ "storm"-погода
        ("Trump to say tariff 25 times?", None),         # mention без спорт-слов
    ]

    def test_cases(self):
        for title, want in self.CASES:
            with self.subTest(title=title):
                self.assertEqual(ct._blocked_reason(title), want)

    def test_empty(self):
        self.assertIsNone(ct._blocked_reason(""))
        self.assertIsNone(ct._blocked_reason(None))


# ----------------------------- классификация событий -----------------------------
class TestClassify(unittest.TestCase):
    def test_trade_sides(self):
        self.assertEqual(ct.classify({"type": "TRADE", "side": "buy"}), "BUY")
        self.assertEqual(ct.classify({"type": "TRADE", "side": "SELL"}), "SELL")

    def test_redeem_and_other(self):
        self.assertEqual(ct.classify({"activityType": "REDEEM"}), "REDEEM")
        self.assertEqual(ct.classify({"type": "SPLIT"}), "OTHER")
        self.assertEqual(ct.classify({}), "OTHER")

    def test_field_fallbacks(self):
        e = {"tokenId": "abc", "avgPrice": "0.55", "condition_id": "cid1"}
        self.assertEqual(ct.ev_token(e), "abc")
        self.assertAlmostEqual(ct.ev_price(e), 0.55)
        self.assertEqual(ct.ev_cid(e), "cid1")


# ----------------------------- пересчёты книги -----------------------------
def base_book(**over):
    """Минимальная консистентная книга: base=1000, две позиции (политика + спорт),
    в логе два входа и два закрытия (политика +5, спорт −3)."""
    b = {
        "bankroll": 1000.0, "cash": 982.0, "topups": 0.0, "realized": 2.0,   # 1000+2-20 вложенных
        "started": 100, "n_copied": 2, "n_skipped": 0, "skipped_realized": 0.0,
        "positions": {
            "w1|t1": {"wallet": "w1", "token": "t1", "qty": 20.0, "cost": 10.0,
                      "title": "Fed rate cut in September?", "opened": 10},
            "w2|t2": {"wallet": "w2", "token": "t2", "qty": 20.0, "cost": 10.0,
                      "title": "Real Madrid vs. Barcelona", "opened": 20},
        },
        "log": [
            {"t": 1, "w": "w1", "act": "BUY", "spend": 10.0, "title": "Fed rate cut in September?"},
            {"t": 2, "w": "w2", "act": "BUY", "spend": 10.0, "title": "Real Madrid vs. Barcelona"},
            {"t": 3, "w": "w1", "act": "SETTLE", "pnl": 5.0, "title": "Fed rate cut in September?"},
            {"t": 4, "w": "w2", "act": "SETTLE", "pnl": -3.0, "title": "Liverpool to win the match"},
        ],
        "skipped": [], "seen": {"w1": 1, "w2": 2}, "typical": {"w1": 7.0}, "thold": {},
    }
    b.update(over)
    return b


def check_cash_equation(tc, book):
    """Инвариант книги: cash = base + topups + realized - invested."""
    base = round(book["bankroll"] - book["topups"], 2)
    invested = sum(p["cost"] for p in book["positions"].values())
    tc.assertAlmostEqual(book["cash"], base + book["topups"] + book["realized"] - invested, places=2)


class TestPurgeBlocked(unittest.TestCase):
    def test_removes_sport_and_recomputes(self):
        b = base_book()
        r = ct.purge_blocked(b)
        self.assertEqual(r["removed_positions"], 1)                 # спорт-позиция ушла
        self.assertEqual(list(b["positions"]), ["w1|t1"])
        self.assertEqual(len(b["log"]), 2)                          # спорт-записи выпилены
        self.assertAlmostEqual(b["realized"], 5.0)                  # только политика
        self.assertEqual(b["n_copied"], 1)
        self.assertAlmostEqual(b["bankroll"], 1000.0)               # base без доливов
        check_cash_equation(self, b)


class TestPurgeWallet(unittest.TestCase):
    def test_removes_wallet_everywhere(self):
        b = base_book(skipped=[{"w": "w2", "reason": "band", "resolved": True, "pnl": 1.0},
                               {"w": "w1", "reason": "band", "resolved": True, "pnl": 2.0}],
                      skipped_realized=3.0)
        r = ct.purge_wallet(b, "W2")                                # регистр не важен
        self.assertEqual(r["removed_positions"], 1)
        self.assertNotIn("w2", b["seen"])
        self.assertTrue(all((x.get("w") or "").lower() != "w2" for x in b["log"]))
        self.assertTrue(all((x.get("w") or "").lower() != "w2" for x in b["skipped"]))
        self.assertAlmostEqual(b["realized"], 5.0)
        self.assertAlmostEqual(b["skipped_realized"], 2.0)
        check_cash_equation(self, b)


class TestRescale(unittest.TestCase):
    def test_scale_preserves_entry_price(self):
        b = base_book(pnl_history=[[100, 2.0, 4.0]],
                      realacct={"base": 100.0, "cash": 50.0, "realized": 10.0,
                                "taken": 1, "missed": 0, "missed_spend": 5.0},
                      day_baseline={"date": "2026-07-13", "per_wallet": {"w1": 10.0}})
        p = b["positions"]["w1|t1"]
        entry_before = p["cost"] / p["qty"]
        ct.rescale_book(b, 0.1)
        self.assertAlmostEqual(b["bankroll"], 100.0)
        self.assertAlmostEqual(p["cost"] / p["qty"], entry_before)  # цена входа не меняется
        self.assertAlmostEqual(b["log"][2]["pnl"], 0.5)
        self.assertAlmostEqual(b["pnl_history"][0][2], 0.4)
        self.assertAlmostEqual(b["realacct"]["cash"], 5.0)
        self.assertAlmostEqual(b["day_baseline"]["per_wallet"]["w1"], 1.0)
        check_cash_equation(self, b)

    def test_rescale_is_scale_invariant_for_ratios(self):
        b = base_book()
        ct.rescale_book(b, 0.01)
        ct.rescale_book(b, 100.0)                                   # туда-обратно
        self.assertAlmostEqual(b["bankroll"], 1000.0, places=2)
        self.assertAlmostEqual(b["realized"], 2.0, places=2)


class TestResetBook(unittest.TestCase):
    def test_keeps_base_and_flags(self):
        b = base_book(bankroll=1200.0, topups=200.0, hard_cash=True)
        r = ct.reset_book(b)
        self.assertAlmostEqual(r["base"], 1000.0)
        self.assertAlmostEqual(b["bankroll"], 1000.0)
        self.assertAlmostEqual(b["cash"], 1000.0)
        self.assertEqual(b["positions"], {})
        self.assertEqual(b["log"], [])
        self.assertEqual(b["seen"], {})                             # форвард заново
        self.assertTrue(b["hard_cash"])
        self.assertEqual(b["typical"], {"w1": 7.0})                 # обученный сайзинг не теряем


class TestRenorm(unittest.TestCase):
    def test_mixed_scale_repair(self):
        b = base_book()
        # загрязнённая позиция ($100-эра, cost>10) с t=1000 и чистая /100-позиция
        b["positions"] = {
            "w1|big": {"wallet": "w1", "token": "big", "qty": 200.0, "cost": 100.0,
                       "title": "A?", "opened": 1000},
            "w1|small": {"wallet": "w1", "token": "small", "qty": 10.0, "cost": 5.0,
                         "title": "B?", "opened": 500},
        }
        b["log"] = [{"t": 900, "w": "w1", "act": "SETTLE", "pnl": 1.0, "title": "C?"},
                    {"t": 1100, "w": "w1", "act": "SETTLE", "pnl": -20.0, "title": "D?"}]
        ct.renorm_book(b, target_base=10000.0, thresh=10.0)
        self.assertAlmostEqual(b["positions"]["w1|big"]["cost"], 10.0)    # /10
        self.assertAlmostEqual(b["positions"]["w1|small"]["cost"], 50.0)  # x10
        self.assertAlmostEqual(b["realized"], 1.0 * 10 + (-20.0) * 0.1)   # до t0 x10, после /10
        check_cash_equation(self, b)


class TestMaterializeSkips(unittest.TestCase):
    def test_resolved_avg_up_goes_to_log(self):
        b = base_book(skipped=[{"w": "w1", "t": 5, "reason": "avg_up", "resolved": True,
                                "pnl": 2.0, "notional": 1.0, "qty": 4.0, "val": 1.0,
                                "outcome": "Yes", "title": "Fed rate cut in September?"}],
                      skipped_realized=2.0)
        cash0, realized0 = b["cash"], b["realized"]
        r = ct.materialize_skips(b, per_trade=10.0)
        self.assertEqual(r["materialized"], 1)
        self.assertAlmostEqual(r["pnl_added"], 20.0)                # pnl x (10/1)
        self.assertAlmostEqual(b["realized"], realized0 + 20.0)
        self.assertAlmostEqual(b["cash"], cash0 + 20.0)
        self.assertAlmostEqual(b["skipped_realized"], 0.0)          # тень пуста
        self.assertEqual(b["skipped"], [])
        self.assertEqual(b["log"][-1]["act"], "SETTLE")
        self.assertEqual(b["log"][-1]["mat"], 1)

    def test_unresolved_stays_in_shadow(self):
        b = base_book(skipped=[{"w": "w1", "t": 5, "reason": "avg_up", "resolved": False,
                                "notional": 1.0, "qty": 4.0, "title": "X?"}])
        r = ct.materialize_skips(b, per_trade=10.0)
        self.assertEqual(r["materialized"], 0)
        self.assertEqual(len(b["skipped"]), 1)

    def test_deficit_covered_by_topup(self):
        b = base_book(cash=5.0,
                      skipped=[{"w": "w1", "t": 5, "reason": "avg_up", "resolved": True,
                                "pnl": -2.0, "notional": 1.0, "qty": 0.0, "val": 0.0,
                                "outcome": "No", "title": "X?"}])
        ct.materialize_skips(b, per_trade=10.0)                     # −20 при кэше 5
        self.assertAlmostEqual(b["cash"], 0.0)                      # минус закрыт доливом
        self.assertAlmostEqual(b["topups"], 15.0)


# ----------------------------- атомарная запись и книга на диске -----------------------------
class TestAtomicWrite(unittest.TestCase):
    def test_overwrites_and_cleans_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "book.json"
            p.write_text("old", encoding="utf-8")
            ct.atomic_write_text(p, "новое содержимое ✓")
            self.assertEqual(p.read_text(encoding="utf-8"), "новое содержимое ✓")
            self.assertFalse((Path(d) / "book.json.tmp").exists())

    def test_survives_stale_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "book.json"
            (Path(d) / "book.json.tmp").write_text("мусор от прошлого падения", encoding="utf-8")
            ct.atomic_write_text(p, "ok")
            self.assertEqual(p.read_text(encoding="utf-8"), "ok")

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "b.json")
            b = base_book()
            b["positions"]["w1|t1"]["title"] = "Зеленский ✓ °F"
            ct.save_book(p, b)
            self.assertEqual(ct.load_book(p, 999.0), b)             # bankroll игнорится: файл есть

    def test_load_fresh_book(self):
        with tempfile.TemporaryDirectory() as d:
            b = ct.load_book(str(Path(d) / "nope.json"), 500.0)
            self.assertEqual(b["bankroll"], 500.0)
            self.assertEqual(b["positions"], {})


# ----------------------------- горячие пер-кошельковые фильтры -----------------------------
class TestWalletFilters(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        ct._WFILT["mtime"] = -1                       # сброс кэша модуля
        ct._WFILT["map"] = {}

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()
        ct._WFILT["mtime"] = -1
        ct._WFILT["map"] = {}

    def test_no_file_no_filters_off(self):
        self.assertEqual(ct._wallet_filters("0x" + "1" * 40), set())

    def test_reads_and_reloads(self):
        w = "0x" + "a" * 40
        Path("wallet_filters.json").write_text(json.dumps({w: ["band", "sport"]}), encoding="utf-8")
        self.assertEqual(ct._wallet_filters(w.upper()), {"band", "sport"})   # регистр не важен

    def test_legacy_no_filter_wallet(self):
        w = next(iter(ct.NO_FILTER_WALLETS))
        self.assertTrue({"band", "adverse", "avg_up"} <= ct._wallet_filters(w))


# ----------------------------- исполнитель (live_executor) -----------------------------
class TestExecutorHelpers(unittest.TestCase):
    def test_resp_ok_variants(self):
        self.assertTrue(le.resp_ok(SimpleNamespace(success=True)))
        self.assertFalse(le.resp_ok(SimpleNamespace(success=False)))
        self.assertTrue(le.resp_ok(SimpleNamespace(status="matched")))
        self.assertFalse(le.resp_ok(SimpleNamespace(status="rejected")))
        self.assertTrue(le.resp_ok(SimpleNamespace(order_id="x")))
        self.assertTrue(le.resp_ok(SimpleNamespace()))              # нет сигналов -> принят

    def test_sig_key(self):
        self.assertEqual(le.sig_key({"t": 1, "tok": "abc"}), "1|abc")


# ----------------------------- токен ленты сигналов (/api/signals) -----------------------------
class TestSignalsToken(unittest.TestCase):
    def setUp(self):
        try:
            import copy_dashboard as cd
        except ImportError:                            # нет flask — пропускаем блок
            self.skipTest("flask не установлен")
        self.cd = cd
        self._tok = cd.SIGNALS_TOKEN
        self.client = cd.app.test_client()

    def tearDown(self):
        self.cd.SIGNALS_TOKEN = self._tok

    def test_open_when_no_token(self):
        self.cd.SIGNALS_TOKEN = ""
        self.assertEqual(self.client.get("/api/signals").status_code, 200)

    def test_locked_when_token_set(self):
        self.cd.SIGNALS_TOKEN = "s3cret"
        self.assertEqual(self.client.get("/api/signals").status_code, 401)
        ok = self.client.get("/api/signals", headers={"X-Signals-Token": "s3cret"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.get("/api/signals?token=s3cret").status_code, 200)
        self.assertEqual(self.client.get("/api/signals?token=wrong").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
