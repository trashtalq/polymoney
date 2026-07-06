#!/usr/bin/env python3
"""
trade_feed.py — ГЛОБАЛЬНЫЙ ПОЛЛЕР ленты сделок Polymarket -> event store (SQLite).

Фундамент онлайн-аналитики: ОДИН поток тянет /trades (все рынки сразу) раз в
FEED_INTERVAL сек и складывает в append-only базу. Одна лента питает всё:
first-K индексы ранних покупателей, счётчики пар лидер-копир, EWMA-фичи,
триггеры — без пер-кошелькового опроса (и без потолка offset=3500 вперёд от
даты включения).

Схема:
  trades(ts, wallet, side, asset, cid, px, sz, outcome, tx)  + индексы
  markets(cid PK, title, first_seen_ts)   -- title один раз, не в каждой строке;
                                             first_seen_ts = прокси возраста рынка
Дедуп: UNIQUE(tx, wallet, asset, px, sz) + INSERT OR IGNORE.

Запуск: python trade_feed.py            # демон (под run_all-супервизором на сервере)
        python trade_feed.py --once     # один цикл (тест)
Служебное: env TRADES_DB (путь базы), FEED_INTERVAL (сек, деф. 60).
"""
import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"
DB_PATH = os.environ.get("TRADES_DB", "trades.db")
INTERVAL = int(os.environ.get("FEED_INTERVAL", "60"))
PAGE = 500
MAX_PAGES = 6            # потолок offset у API = 3000 -> 6 страниц максимум
STATE_KEY = "last_max_ts"
# Лента ~1М сделок/день. Пыль (< $ нотионала) — в основном бот-спам, режем на входе;
# сырьё старше RETENTION_DAYS чистим раз в сутки (иначе база растёт на ~100МБ/день).
MIN_NOTIONAL = float(os.environ.get("FEED_MIN_NOTIONAL", "2"))
RETENTION_DAYS = int(os.environ.get("FEED_RETENTION_DAYS", "45"))


def log(m):
    print(f"[{datetime.now(timezone.utc):%m-%d %H:%M:%S}Z] {m}", flush=True)


def db_open(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""CREATE TABLE IF NOT EXISTS trades(
        ts INTEGER NOT NULL, wallet TEXT NOT NULL, side TEXT NOT NULL,
        asset TEXT NOT NULL, cid TEXT NOT NULL, px REAL NOT NULL, sz REAL NOT NULL,
        outcome TEXT, tx TEXT)""")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_trade ON trades(tx, wallet, asset, px, sz)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ts ON trades(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_wallet ON trades(wallet, ts)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_cid ON trades(cid, ts)")
    con.execute("""CREATE TABLE IF NOT EXISTS markets(
        cid TEXT PRIMARY KEY, title TEXT, first_seen_ts INTEGER)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return con


def get_meta(con, k, default=0):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v)))


def poll_once(con: sqlite3.Connection, s: requests.Session) -> dict:
    """Один цикл: страницы новейших сделок до перекрытия с last_max_ts."""
    last_max = get_meta(con, STATE_KEY, 0)
    new_max = last_max
    inserted = seen = 0
    mk_new = 0
    for page in range(MAX_PAGES):
        try:
            r = s.get(f"{DATA_API}/trades", params={"limit": PAGE, "offset": page * PAGE},
                      timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 10)))
                continue
            rows = r.json() or []
        except (requests.RequestException, ValueError) as ex:
            log(f"страница {page}: {ex}")
            break
        if isinstance(rows, dict):                    # {"error": ...} от Cloudflare/API
            log(f"страница {page}: ответ-ошибка {str(rows)[:120]}")
            break
        if not rows:
            break
        page_min = None
        batch, mkts = [], {}
        for t in rows:
            ts = int(t.get("timestamp") or 0)
            w = (t.get("proxyWallet") or "").lower()
            asset = str(t.get("asset") or "")
            cid = t.get("conditionId") or ""
            px, sz = t.get("price"), t.get("size")
            if not (ts and w and asset and cid and px and sz):
                continue
            seen += 1
            page_min = ts if page_min is None else min(page_min, ts)
            new_max = max(new_max, ts)
            if ts <= last_max:                       # уже видели в прошлые циклы
                continue
            if float(px) * float(sz) < MIN_NOTIONAL:  # пыль/бот-спам — не храним
                continue
            batch.append((ts, w, (t.get("side") or "").upper(), asset, cid,
                          float(px), float(sz), t.get("outcome") or "",
                          t.get("transactionHash") or ""))
            if cid not in mkts:
                mkts[cid] = ((t.get("title") or "")[:80], ts)
        if batch:
            cur = con.executemany(
                "INSERT OR IGNORE INTO trades(ts,wallet,side,asset,cid,px,sz,outcome,tx) "
                "VALUES(?,?,?,?,?,?,?,?,?)", batch)
            inserted += cur.rowcount
            for cid, (title, ts) in mkts.items():
                c = con.execute("INSERT OR IGNORE INTO markets(cid,title,first_seen_ts) "
                                "VALUES(?,?,?)", (cid, title, ts))
                mk_new += c.rowcount
        if page_min is not None and page_min <= last_max:
            break                                     # дошли до уже виденного — стоп
        time.sleep(0.15)
    set_meta(con, STATE_KEY, new_max)
    con.commit()
    return {"inserted": inserted, "seen": seen, "new_markets": mk_new}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один цикл и выход (тест)")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    con = db_open(args.db)
    s = requests.Session()
    # Mozilla-UA обязателен: на «странные» UA Cloudflare у data-api отвечает ошибкой-словарём
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    n_tr = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    log(f"старт: база {args.db}, сделок в базе {n_tr}, интервал {INTERVAL}s")

    last_prune = 0.0
    while True:
        try:
            st = poll_once(con, s)
            if st["inserted"]:
                log(f"+{st['inserted']} сделок (видел {st['seen']}, новых рынков {st['new_markets']})")
            if time.time() - last_prune > 86400:      # суточный ретеншн сырья
                cut = int(time.time()) - RETENTION_DAYS * 86400
                n = con.execute("DELETE FROM trades WHERE ts < ?", (cut,)).rowcount
                con.commit()
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if n:
                    log(f"ретеншн: удалено {n} сделок старше {RETENTION_DAYS}д")
                last_prune = time.time()
        except Exception as ex:  # noqa: BLE001
            log(f"цикл упал: {ex}")
        if args.once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
