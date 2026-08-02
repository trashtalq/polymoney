#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""
polymoney_app.pyw — ПУЛЬТ (десктоп-приложение) для реального копи-бота.

Двойной клик по этому файлу открывает окно. Ни PyCharm, ни консоли не нужно.
Что умеет по-человечески (без правки .env руками):
  • Старт/Стоп бота, выбор режима (dry / smoke / live) большой кнопкой.
  • Лимиты и размер ставки — поля с подписями (пишутся в polymarket.env).
  • Кошельки в копи (Core-реал) — добавить, убрать, поставить на паузу/включить.
  • Живой лог сделок с иконками вместо «матрицы цифр».

Приватный ключ приложение НЕ трогает и НЕ показывает — он живёт только в
polymarket.env и читается самим ботом (live_executor.py) при запуске.
Управление кошельками ходит на наш сервер (пароль действий спрашивается один раз).
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None

HERE = Path(__file__).resolve().parent
ENV = HERE / "polymarket.env"
ENV_EXAMPLE = HERE / "polymarket.env.example"
STATE = HERE / "live_exec_state.json"
PAPER = HERE / "paper15k_state.json"           # состояние теневого бота (equity/pnl/pos)
UPTIME_FILE = HERE / "paper_uptime.json"       # накопленное время работы теневого (переживает рестарты)
HOLD_FILE = HERE / "hold_only.json"            # реал: режим «держим» (стоп входов, выходы работают)
HOLD_FILE_PAPER = HERE / "hold_only_paper.json"   # теневой: то же
# ── контур «Лучшие»: свой allowlist + своё состояние (тестируем отобранных изолированно) ──
BEST_ALLOW = HERE / "best_allowlist.json"
BEST_STATE = HERE / "best15k_state.json"
BEST_EXEC_STATE = HERE / "best_exec_state.json"
SETTINGS = HERE / "app_settings.json"          # {server, pw} — локально, в gitignore
BOT = HERE / "live_executor.py"
ALLOW_FILE = HERE / "copy_allowlist.json"      # белый список бота — держим в синхроне с копи

# ── палитра (неон-терминал, как дашборд) ──────────────────────────────────────
BG = "#0b0f1a"; PANEL = "#0f1826"; CARD = "#122034"
TXT = "#e6f0ff"; MUT = "#7d8ca8"; ACC = "#06e5ff"
GRN = "#39d98a"; RED = "#ff5c7a"; AMB = "#ffcf5c"; BORD = "#1d2b44"

# ── лимиты: (ключ_в_env, подпись, дефолт). MAX_AGE_MIN — особый (в минутах). ──
LIMITS = [
    ("PER_TRADE_USD",       "Размер ставки в позицию, $",         "1"),
    ("DEPOSIT",             "Лимит денег в открытых позах, $",    "50"),
    ("DAILY_MAX_USD",       "Дневной лимит трат, $",              "20"),
    ("MAX_PER_WALLET_DAY",  "Лимит на кошелёк (в позах), $",      "5"),
    ("MAX_PRICE",           "Макс. цена входа (0–1)",             "0.92"),
    ("MIN_PRICE",           "Мин. цена входа (не лонгшоты)",      "0.12"),
    ("MAX_ENTRIES_PER_POS", "Макс. входов в одну позицию",        "2"),
    ("MAX_AGE_MIN",         "Не копировать сигналы старше, мин",  "5"),
    ("MAX_RESOLVE_DAYS",    "Горизонт резолва рынка, дн (0=выкл)", "0"),
    ("EXIT_MIN_FRAC",       "Выход, когда цель продала долю",     "0.1"),
    ("POLL_SEC",            "Опрос сервера каждые, сек",          "30"),
    ("CHASE_MATCH",         "Догон под задержку (0=выкл, 1=вкл)", "0"),
    ("CHASE_MAX_MULT",      "Потолок множителя догона",          "2"),
]
# ── те же лимиты для ТЕНЕВОГО бота (свои PAPER_*-ключи, независимо от реала) ──
PAPER_LIMITS = [
    ("PAPER_BANKROLL",           "Банкролл (стартовый), $",           "15000"),
    ("PAPER_PER_TRADE",          "Размер ставки в позицию, $",         "10"),
    ("PAPER_MAX_PER_WALLET",     "Лимит на кошелёк (в позах), $",      "300"),
    ("PAPER_MAX_PRICE",          "Макс. цена входа (0–1)",             "0.92"),
    ("PAPER_MIN_PRICE",          "Мин. цена входа (не лонгшоты)",      "0.12"),
    ("PAPER_MAX_ENTRIES_PER_POS", "Макс. входов в одну позицию",       "3"),
    ("PAPER_MAX_AGE_MIN",        "Не копировать сигналы старше, мин",  "5"),
    ("PAPER_MAX_RESOLVE_DAYS",   "Горизонт резолва рынка, дн (0=выкл)", "0"),
    ("PAPER_EXIT_MIN_FRAC",      "Выход, когда цель продала долю",     "0.1"),
    ("PAPER_POLL_SEC",           "Опрос сервера каждые, сек",          "30"),
    ("PAPER_CHASE_MATCH",        "Догон под задержку (0=выкл, 1=вкл)", "1"),
    ("PAPER_CHASE_MAX_MULT",     "Потолок множителя догона",          "2"),
]
DEFAULT_SERVER = "http://144.31.197.121:5000"
CORE_LABEL = "core-реал"


# ───────────────────────────── env / настройки ───────────────────────────────
def read_env() -> dict:
    src = ENV if ENV.exists() else (ENV_EXAMPLE if ENV_EXAMPLE.exists() else None)
    cfg = {}
    if src:
        for line in src.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def write_env(updates: dict):
    """Обновляем только заданные ключи, сохраняя комментарии, порядок и PRIVATE_KEY/FUNDER."""
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else (
        ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() if ENV_EXAMPLE.exists() else [])
    seen, out = set(), []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")


def load_settings() -> dict:
    if SETTINGS.exists():
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_settings(d: dict):
    SETTINGS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def allow_update(add=None, remove=None):
    """Держим локальный белый список бота в синхроне: добавили кошелёк в копи -> он и в allowlist,
    убрали -> и оттуда. Бот копирует только адреса из этого файла (защита от инъекции сигналов)."""
    try:
        cur = set()
        if ALLOW_FILE.exists():
            cur = {str(w).lower() for w in json.loads(ALLOW_FILE.read_text(encoding="utf-8"))}
        if add:
            cur.add(add.lower())
        if remove:
            cur.discard(remove.lower())
        ALLOW_FILE.write_text(json.dumps(sorted(cur), ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def positions_value(funder: str):
    """Стоимость открытых позиций ($) с Polymarket — data-api /value, БЕЗ приватного ключа."""
    if not funder or requests is None:
        return None
    try:
        v = requests.get(f"https://data-api.polymarket.com/value?user={funder}", timeout=12).json()
        return float(v[0]["value"]) if isinstance(v, list) and v else 0.0
    except Exception:  # noqa: BLE001
        return None


# ───────────────────────────────── приложение ────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("POLYMONEY · Пульт")
        self.configure(bg=BG)
        self.geometry("1000x760")
        self.minsize(880, 640)
        self.proc = None
        self.paper_proc = None     # теневой (paper) бот — отдельный процесс
        self.best_proc = None      # контур «Лучшие» — свой allowlist/банк/состояние
        self.paper_started = None   # старт ТЕКУЩЕЙ сессии теневого (None = стоит)
        self.paper_uptime = self._load_uptime()   # накопленное время работы за всё время (сек)
        self._uptime_save_t = 0
        self.q = queue.Queue()
        self.wallet_rows = {}      # iid -> {"addr":..., "paused":bool}
        self._client = None        # SDK-клиент для баланса (создаётся лениво по ключу, локально)
        self._attr = {}            # (название,сторона)->кошелёк из истории копирования (кэш)
        self._attr_ts = 0
        self._tok_wallet = {}      # токен->кошелёк из локальных тегов бота (дополнение к привязке)
        self._style()
        self._build()
        self._enable_clipboard()       # Ctrl+V/C/X/A при ЛЮБОЙ раскладке (в т.ч. русской)
        self._load_into_fields()
        self.after(200, self._pump)
        self.after(500, self._refresh_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # первая загрузка кошельков + баланса (не блокируя старт окна)
        self.after(300, lambda: self.refresh_wallets(quiet=True))
        self.after(700, self._poll_account)
        self.after(900, self._refresh_paper_stats)
        self.after(950, self._refresh_best_stats)
        self.after(1000, self._tick_paper_timer)
        self._paint_hold(False)                     # начальное состояние кнопок «держим»
        self._paint_hold(True)
        self.after(1100, self._poll_positions)

    # ── стили ──
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        st.configure(".", background=BG, foreground=TXT, fieldbackground=CARD,
                     bordercolor=BORD, lightcolor=BORD, darkcolor=BORD)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=TXT, font=("Segoe UI", 10))
        st.configure("Card.TLabel", background=PANEL, foreground=TXT, font=("Segoe UI", 10))
        st.configure("Field.TLabel", background=PANEL, foreground=MUT, font=("Segoe UI", 9))
        st.configure("Title.TLabel", background=BG, foreground=ACC,
                     font=("Consolas", 16, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUT, font=("Segoe UI", 9))
        st.configure("Stat.TLabel", background=PANEL, foreground=TXT, font=("Consolas", 11))
        st.configure("Money.TLabel", background=PANEL, foreground=GRN, font=("Consolas", 14, "bold"))
        st.configure("Cash.TLabel", background=PANEL, foreground=ACC, font=("Consolas", 14, "bold"))
        st.configure("TButton", background=CARD, foreground=TXT, borderwidth=0,
                     padding=(12, 8), font=("Segoe UI", 10, "bold"))
        st.map("TButton", background=[("active", BORD)])
        st.configure("Accent.TButton", background=ACC, foreground="#00121a")
        st.map("Accent.TButton", background=[("active", "#38ecff")])
        st.configure("Stop.TButton", background="#2a1622", foreground=RED)
        st.map("Stop.TButton", background=[("active", "#3a1e2e")])
        st.configure("Small.TButton", padding=(8, 5), font=("Segoe UI", 9, "bold"))
        st.configure("TEntry", fieldbackground=CARD, foreground=TXT, insertcolor=ACC,
                     borderwidth=1, padding=4)
        st.configure("TCombobox", fieldbackground=CARD, foreground=TXT, background=CARD,
                     arrowcolor=ACC, borderwidth=1, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", CARD)],
               foreground=[("readonly", TXT)])
        st.configure("TLabelframe", background=PANEL, bordercolor=BORD, borderwidth=1)
        st.configure("TLabelframe.Label", background=PANEL, foreground=ACC,
                     font=("Segoe UI", 10, "bold"))
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=CARD, foreground=MUT, borderwidth=0,
                     padding=(18, 9), font=("Segoe UI", 10, "bold"))
        st.map("TNotebook.Tab", background=[("selected", PANEL)],
               foreground=[("selected", ACC)])
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TXT,
                     borderwidth=0, rowheight=26, font=("Segoe UI", 9))
        st.configure("Treeview.Heading", background=PANEL, foreground=MUT,
                     borderwidth=0, font=("Segoe UI", 9, "bold"))
        st.map("Treeview", background=[("selected", "#173049")],
               foreground=[("selected", ACC)])

    # ── буфер обмена по КЕЙКОДУ (работает при русской раскладке, где Ctrl+V = Ctrl+«м») ──
    def _enable_clipboard(self):
        def clip(event):
            kc = event.keycode
            w = event.widget
            try:
                if kc == 86:                      # V — вставка
                    w.event_generate("<<Paste>>"); return "break"
                if kc == 67:                      # C — копировать
                    w.event_generate("<<Copy>>"); return "break"
                if kc == 88:                      # X — вырезать
                    w.event_generate("<<Cut>>"); return "break"
                if kc == 65:                      # A — выделить всё
                    w.select_range(0, "end"); w.icursor("end"); return "break"
            except Exception:  # noqa: BLE001
                pass
        for cls in ("TEntry", "Entry", "Text"):
            self.bind_class(cls, "<Control-KeyPress>", clip)

    # ── разметка ──
    def _build(self):
        outer = ttk.Frame(self, padding=(12, 10))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="◉ POLYMONEY", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)
        real = ttk.Frame(self.nb, padding=10)
        paper = ttk.Frame(self.nb, padding=10)
        positions = ttk.Frame(self.nb, padding=10)
        best = ttk.Frame(self.nb, padding=10)
        self.nb.add(real, text="  💰 Реал  ")
        self.nb.add(paper, text="  🌗 Теневой $15k  ")
        self.nb.add(best, text="  ⭐ Лучшие  ")
        self.nb.add(positions, text="  📋 Позиции теневого  ")
        self._build_best(best)
        self._build_real(real)
        self._build_paper(paper)
        self._build_positions(positions)

    def _build_real(self, root):
        root.columnconfigure(0, weight=1, uniform="c")
        root.columnconfigure(1, weight=1, uniform="c")
        root.rowconfigure(3, weight=1)
        ttk.Label(root, text="реальный счёт — живые деньги", style="Sub.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # ── панель управления ботом ──
        bar = ttk.Frame(root, style="Card.TFrame", padding=12)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(bar, text="Режим:", style="Card.TLabel").pack(side="left")
        self.mode_var = tk.StringVar(value="dry")
        self.mode_cb = ttk.Combobox(bar, textvariable=self.mode_var, width=8, state="readonly",
                                    values=["dry", "smoke", "live"])
        self.mode_cb.pack(side="left", padx=(6, 14))
        self.start_btn = ttk.Button(bar, text="▶  Запустить", style="Accent.TButton",
                                    command=self.toggle_bot)
        self.start_btn.pack(side="left")
        self.dot = tk.Canvas(bar, width=14, height=14, bg=PANEL, highlightthickness=0)
        self.dot.pack(side="left", padx=(16, 6))
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=MUT, outline="")
        self.stat_lbl = ttk.Label(bar, text="остановлен", style="Stat.TLabel")
        self.stat_lbl.pack(side="left")
        ttk.Button(bar, text="⚙ Лимиты", style="Small.TButton",
                   command=self.open_settings).pack(side="left", padx=(16, 0))
        self.hold_btn = ttk.Button(bar, text="⏸ Стоп входов (держим)", style="Small.TButton",
                                   command=lambda: self._toggle_hold(False))
        self.hold_btn.pack(side="left", padx=(8, 0))
        ttk.Label(bar, text="", style="Card.TLabel").pack(side="left", expand=True, fill="x")
        # ── баланс Polymarket: портфель (позиции+наличные) и «Доступно» (кэш, по ключу локально) ──
        money = ttk.Frame(bar, style="Card.TFrame")
        money.pack(side="right")
        for c, cap in ((0, "Портфель"), (1, "Доступно"), (2, "Сегодня")):
            ttk.Label(money, text=cap, style="Field.TLabel").grid(row=0, column=c, padx=(20, 0), sticky="w")
        self.portf_lbl = ttk.Label(money, text="—", style="Money.TLabel")
        self.portf_lbl.grid(row=1, column=0, padx=(20, 0), sticky="w")
        self.cash_lbl = ttk.Label(money, text="—", style="Cash.TLabel")
        self.cash_lbl.grid(row=1, column=1, padx=(20, 0), sticky="w")
        self.spend_lbl = ttk.Label(money, text="$0.00", style="Stat.TLabel")
        self.spend_lbl.grid(row=1, column=2, padx=(20, 0), sticky="w")

        # лимиты живут в переменных; сами поля — в окне «⚙ Лимиты» (open_settings)
        self.fields = {key: tk.StringVar(value=default) for key, _l, default in LIMITS}

        # ── кошельки (на всю ширину; лимиты уехали в попап, лог получил место) ──
        wf = ttk.Labelframe(root, text="  КОШЕЛЬКИ В КОПИ (Core-реал)  ", padding=12)
        wf.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        wf.columnconfigure(0, weight=1)
        wf.rowconfigure(1, weight=1)
        add = ttk.Frame(wf, style="Card.TFrame")
        add.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        add.columnconfigure(0, weight=1)
        self.addr_var = tk.StringVar()
        ttk.Entry(add, textvariable=self.addr_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(add, text="+ Добавить", style="Small.TButton",
                   command=self.add_wallet).grid(row=0, column=1, padx=(6, 0))

        cols = ("addr", "src", "copy", "pnl")
        self.tree = ttk.Treeview(wf, columns=cols, show="headings", height=8, selectmode="browse")
        for c, txt, w in (("addr", "Кошелёк", 120), ("src", "Список", 90),
                          ("copy", "Копи", 60), ("pnl", "Мой PnL $", 80)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w" if c in ("addr", "src") else "center")
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.tag_configure("on", foreground=TXT)
        self.tree.tag_configure("off", foreground=MUT)
        wbtn = ttk.Frame(wf, style="Card.TFrame")
        wbtn.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(wbtn, text="⏸ / ▶  Копи вкл-выкл", style="Small.TButton",
                   command=self.toggle_pause).pack(side="left")
        ttk.Button(wbtn, text="✕ Убрать", style="Small.TButton",
                   command=self.remove_wallet).pack(side="left", padx=6)
        ttk.Button(wbtn, text="🔄 Обновить", style="Small.TButton",
                   command=lambda: self.refresh_wallets()).pack(side="right")

        # ── лог ──
        logf = ttk.Labelframe(root, text="  ЖИВОЙ ЛОГ  ", padding=(10, 8))
        logf.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, bg=CARD, fg=TXT, insertbackground=TXT, relief="flat",
                           font=("Consolas", 10), padx=10, pady=8, wrap="word",
                           highlightthickness=0, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log["yscrollcommand"] = sb.set
        for tag, col in (("green", GRN), ("red", RED), ("amber", AMB),
                         ("cyan", ACC), ("muted", MUT), ("text", TXT)):
            self.log.tag_configure(tag, foreground=col)
        ttk.Button(logf, text="Очистить", style="Small.TButton",
                   command=self._clear_log).grid(row=1, column=0, sticky="e", pady=(6, 0))
        self._log_raw("Пульт готов. Выбери режим и нажми «Запустить».", "cyan")
        self._log_raw("dry = ничего не тратит (тест) · smoke = 1 живая сделка · "
                      "live = реальные деньги.", "muted")

    # ── окно настроек (общее для Реала и Теневого): поля в попапе ──
    def _open_limits(self, fields, spec, title, saver):
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=PANEL)
        win.transient(self)
        win.resizable(False, False)
        lf = ttk.Labelframe(win, text=f"  {title}  ", padding=14)
        lf.pack(fill="both", expand=True, padx=12, pady=12)
        lf.columnconfigure(0, weight=1)
        for i, (key, label, _d) in enumerate(spec):
            row = ttk.Frame(lf, style="Card.TFrame")
            row.grid(row=i, column=0, sticky="ew", pady=3)
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text=label, style="Field.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Entry(row, textvariable=fields[key], width=8, justify="right").grid(
                row=0, column=1, sticky="e", padx=(12, 0))
        btns = ttk.Frame(lf, style="Card.TFrame")
        btns.grid(row=len(spec), column=0, sticky="ew", pady=(12, 0))

        def do_save():
            if saver():
                try:
                    win.destroy()
                except Exception:  # noqa: BLE001
                    pass
        ttk.Button(btns, text="💾  Сохранить", style="Accent.TButton",
                   command=do_save).pack(side="left")
        ttk.Button(btns, text="↺ сбросить", style="Small.TButton",
                   command=self._load_into_fields).pack(side="left", padx=6)

    def open_settings(self):
        self._open_limits(self.fields, LIMITS, "Лимиты — Реал", self.save_limits)

    def open_paper_settings(self):
        self._open_limits(self.pfields, PAPER_LIMITS, "Настройки теневого $15k", self.save_paper_limits)

    # ── вкладка «Теневой $15k»: тот же бот на виртуальный банк, реальные кэфы ──
    def _build_paper(self, root):
        self.pfields = {key: tk.StringVar(value=default) for key, _l, default in PAPER_LIMITS}
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        bar = ttk.Frame(root, style="Card.TFrame", padding=12)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.pstart_btn = ttk.Button(bar, text="▶  Запустить теневой", style="Accent.TButton",
                                     command=self.toggle_paper)
        self.pstart_btn.pack(side="left")
        self.pdot = tk.Canvas(bar, width=14, height=14, bg=PANEL, highlightthickness=0)
        self.pdot.pack(side="left", padx=(16, 6))
        self._pdot_id = self.pdot.create_oval(2, 2, 12, 12, fill=MUT, outline="")
        self.pstat_lbl = ttk.Label(bar, text="остановлен", style="Stat.TLabel")
        self.pstat_lbl.pack(side="left")
        ttk.Button(bar, text="⚙ Настройки теневого", style="Small.TButton",
                   command=self.open_paper_settings).pack(side="left", padx=(16, 0))
        self.phold_btn = ttk.Button(bar, text="⏸ Стоп входов (держим)", style="Small.TButton",
                                    command=lambda: self._toggle_hold(True))
        self.phold_btn.pack(side="left", padx=(8, 0))
        self.puptime_lbl = ttk.Label(bar, text="⏱ 00:00:00", style="Money.TLabel")
        self.puptime_lbl.pack(side="right")

        sc = ttk.Labelframe(root, text="  ТЕНЕВОЙ СЧЁТ $15k  ", padding=14)
        sc.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for i in range(6):
            sc.columnconfigure(i, weight=1)
        self.pcards = {}
        for i, (key, cap) in enumerate([("equity", "Банк $"), ("pnl", "PnL $"), ("roi", "ROI %"),
                                        ("realized", "Реализ $"), ("pos", "Позиций"), ("trades", "Сделок")]):
            ttk.Label(sc, text=cap, style="Field.TLabel").grid(row=0, column=i, sticky="w", padx=(0, 12))
            lbl = ttk.Label(sc, text="—", style="Money.TLabel")
            lbl.grid(row=1, column=i, sticky="w", padx=(0, 12))
            self.pcards[key] = lbl
        self.pupd_lbl = ttk.Label(sc, text="ещё не запускался", style="Field.TLabel")
        self.pupd_lbl.grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))

        logf = ttk.Labelframe(root, text="  ЛОГ ТЕНЕВОГО  ", padding=(10, 8))
        logf.grid(row=2, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.plog = tk.Text(logf, bg=CARD, fg=TXT, insertbackground=TXT, relief="flat",
                            font=("Consolas", 10), padx=10, pady=8, wrap="word",
                            highlightthickness=0, state="disabled")
        self.plog.grid(row=0, column=0, sticky="nsew")
        psb = ttk.Scrollbar(logf, command=self.plog.yview)
        psb.grid(row=0, column=1, sticky="ns")
        self.plog["yscrollcommand"] = psb.set
        for tag, col in (("green", GRN), ("red", RED), ("amber", AMB),
                         ("cyan", ACC), ("muted", MUT), ("text", TXT)):
            self.plog.tag_configure(tag, foreground=col)
        self._log_raw("Теневой бот: те же кошельки на виртуальный $15k. Жми «Запустить».",
                      "cyan", self.plog)

    # ── вкладка «⭐ Лучшие»: отобранный состав, свой контур (allowlist+банк+статистика) ──
    def _build_best(self, root):
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)
        bar = ttk.Frame(root, style="Card.TFrame", padding=12)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.bstart_btn = ttk.Button(bar, text="▶  Запустить тест", style="Accent.TButton",
                                     command=self.toggle_best)
        self.bstart_btn.pack(side="left")
        self.bdot = tk.Canvas(bar, width=14, height=14, bg=PANEL, highlightthickness=0)
        self.bdot.pack(side="left", padx=(16, 6))
        self._bdot_id = self.bdot.create_oval(2, 2, 12, 12, fill=MUT, outline="")
        self.bstat_lbl = ttk.Label(bar, text="остановлен", style="Stat.TLabel")
        self.bstat_lbl.pack(side="left")
        ttk.Label(bar, text="  · отобранные кошельки, без флипперов · виртуальный банк",
                  style="Card.TLabel").pack(side="left")

        sc = ttk.Labelframe(root, text="  СЧЁТ ТЕСТА  ", padding=14)
        sc.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for i in range(6):
            sc.columnconfigure(i, weight=1)
        self.bcards = {}
        for i, (key, cap) in enumerate([("equity", "Банк $"), ("pnl", "PnL $"), ("roi", "ROI %"),
                                        ("realized", "Реализ $"), ("pos", "Позиций"), ("trades", "Сделок")]):
            ttk.Label(sc, text=cap, style="Field.TLabel").grid(row=0, column=i, sticky="w", padx=(0, 12))
            lbl = ttk.Label(sc, text="—", style="Money.TLabel")
            lbl.grid(row=1, column=i, sticky="w", padx=(0, 12))
            self.bcards[key] = lbl
        self.bupd_lbl = ttk.Label(sc, text="ещё не запускался", style="Field.TLabel")
        self.bupd_lbl.grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))

        wf = ttk.Labelframe(root, text="  СОСТАВ (лучшие) — можно править  ", padding=10)
        wf.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        wf.columnconfigure(0, weight=1)
        add = ttk.Frame(wf, style="Card.TFrame")
        add.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        add.columnconfigure(0, weight=1)
        self.baddr_var = tk.StringVar()
        ttk.Entry(add, textvariable=self.baddr_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(add, text="+ Добавить", style="Small.TButton",
                   command=self.best_add).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(add, text="✕ Убрать", style="Small.TButton",
                   command=self.best_remove).grid(row=0, column=2, padx=(6, 0))
        self.blist = tk.Listbox(wf, height=6, bg=CARD, fg=TXT, relief="flat",
                                highlightthickness=0, selectbackground="#173049",
                                selectforeground=ACC, font=("Consolas", 9))
        self.blist.grid(row=1, column=0, sticky="ew")

        logf = ttk.Labelframe(root, text="  ЛОГ ТЕСТА  ", padding=(10, 8))
        logf.grid(row=3, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.blog = tk.Text(logf, bg=CARD, fg=TXT, insertbackground=TXT, relief="flat",
                            font=("Consolas", 10), padx=10, pady=8, wrap="word",
                            highlightthickness=0, state="disabled")
        self.blog.grid(row=0, column=0, sticky="nsew")
        bsb = ttk.Scrollbar(logf, command=self.blog.yview)
        bsb.grid(row=0, column=1, sticky="ns")
        self.blog["yscrollcommand"] = bsb.set
        for tag, col in (("green", GRN), ("red", RED), ("amber", AMB),
                         ("cyan", ACC), ("muted", MUT), ("text", TXT)):
            self.blog.tag_configure(tag, foreground=col)
        self._best_reload_list()
        self._log_raw("Тест лучших: отобранные кошельки (флипперы исключены). Жми «Запустить тест».",
                      "cyan", self.blog)

    # ── вкладка «Позиции теневого»: НАШИ цены входа/выхода + потеря на задержке (сверка с Polymarket) ──
    def _build_positions(self, root):
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        top = ttk.Frame(root, style="Card.TFrame", padding=10)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(top, text="Позиции теневого — НАШИ цены входа/выхода (сверяй с Polymarket)",
                  style="Card.TLabel").pack(side="left")
        self.posdelay_lbl = ttk.Label(top, text="", style="Money.TLabel")
        self.posdelay_lbl.pack(side="left", padx=(16, 0))
        ttk.Button(top, text="🔄 Обновить", style="Small.TButton",
                   command=self.refresh_positions).pack(side="right")
        wrap = ttk.Frame(root)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        cols = ("mkt", "side", "entry", "target", "delay", "exit", "pnl", "st")
        self.postree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("mkt", "Рынок", 320), ("side", "Ставка", 66), ("entry", "Наш вход", 78),
                        ("target", "Цена цели", 78), ("delay", "Задержка $", 90),
                        ("exit", "Наш выход", 78), ("pnl", "Реализ $", 80), ("st", "", 34)):
            self.postree.heading(c, text=t)
            self.postree.column(c, width=w, anchor="w" if c == "mkt" else "center")
        self.postree.grid(row=0, column=0, sticky="nsew")
        psb = ttk.Scrollbar(wrap, command=self.postree.yview)
        psb.grid(row=0, column=1, sticky="ns")
        self.postree["yscrollcommand"] = psb.set
        self.postree.tag_configure("open", foreground=ACC)
        self.postree.tag_configure("win", foreground=GRN)
        self.postree.tag_configure("lose", foreground=RED)

    def refresh_positions(self):
        try:
            p = json.loads(PAPER.read_text(encoding="utf-8")) if PAPER.exists() else {}
        except Exception:  # noqa: BLE001
            p = {}
        self.postree.delete(*self.postree.get_children())

        def dlay(entry, target, shares):             # <0 = переплатили из-за задержки
            return (target - entry) * shares if target else None
        total_delay = 0.0
        for _tok, pos in (p.get("pos") or {}).items():          # открытые
            entry = pos.get("entry") or 0
            target = pos.get("target_px")
            d = dlay(entry, target, pos.get("shares", 0))
            if d is not None:
                total_delay += d
            self.postree.insert("", "end", tags=("open",), values=(
                pos.get("title", "")[:52], pos.get("outcome", "")[:6],
                f"{entry:.3f}", f"{target:.3f}" if target else "—",
                f"{d:+.2f}" if d is not None else "—", "—", "открыта", "🟢"))
        for c in reversed(p.get("closed") or []):               # закрытые, свежие сверху
            entry = c.get("entry") or 0
            target = c.get("target_px")
            d = dlay(entry, target, c.get("shares", 0))
            if d is not None:
                total_delay += d
            real = c.get("realized", 0)
            self.postree.insert("", "end", tags=("win" if real > 0 else "lose",), values=(
                c.get("title", "")[:52], c.get("outcome", "")[:6],
                f"{entry:.3f}", f"{target:.3f}" if target else "—",
                f"{d:+.2f}" if d is not None else "—", f"{c.get('exit_px', 0):.3f}",
                f"{real:+.2f}", "✓"))
        self.posdelay_lbl.config(text=f"потеряно на задержке: {total_delay:+,.2f}$")

    def _poll_positions(self):
        try:
            self.refresh_positions()
        except Exception:  # noqa: BLE001
            pass
        self.after(5000, self._poll_positions)

    # ───────────────────── env поля ─────────────────────
    def _load_fields(self, fields, spec):
        """Заполнить StringVars из polymarket.env. Ключ *AGE_MIN читается из *AGE_SEC (÷60)."""
        cfg = read_env()
        for key, _label, default in spec:
            if key.endswith("AGE_MIN"):
                sec = cfg.get(key.replace("AGE_MIN", "AGE_SEC"))
                try:
                    fields[key].set(str(round(float(sec) / 60)) if sec else default)
                except Exception:  # noqa: BLE001
                    fields[key].set(default)
            else:
                fields[key].set(cfg.get(key, default))

    def _load_into_fields(self):
        self.mode_var.set((read_env().get("MODE") or "dry").lower())
        self._load_fields(self.fields, LIMITS)
        self._load_fields(self.pfields, PAPER_LIMITS)

    def _collect_limits(self, fields, spec):
        """Собрать {env_key: value} из полей. Возвращает (updates, bad_labels)."""
        updates, bad = {}, []
        for key, label, _d in spec:
            raw = fields[key].get().strip().replace(",", ".")
            try:
                val = float(raw)
            except Exception:  # noqa: BLE001
                bad.append(label)
                continue
            if key.endswith("AGE_MIN"):
                updates[key.replace("AGE_MIN", "AGE_SEC")] = str(int(round(val * 60)))
            elif key.endswith("ENTRIES_PER_POS") or key.endswith("POLL_SEC"):
                updates[key] = str(int(round(val)))
            else:
                updates[key] = ("%g" % val)
        return updates, bad

    def save_limits(self, silent=False):
        updates, bad = self._collect_limits(self.fields, LIMITS)
        if bad:
            messagebox.showerror("Проверь числа", "Не числа в полях:\n• " + "\n• ".join(bad))
            return False
        updates["MODE"] = self.mode_var.get()
        write_env(updates)
        if not silent:
            self._log_raw("✔ Лимиты сохранены в polymarket.env" +
                          (" — перезапусти бота, чтобы применить." if self.proc else "."), "green")
        return True

    def save_paper_limits(self, silent=False):
        updates, bad = self._collect_limits(self.pfields, PAPER_LIMITS)
        if bad:
            messagebox.showerror("Проверь числа", "Не числа в полях:\n• " + "\n• ".join(bad))
            return False
        write_env(updates)
        if not silent:
            self._log_raw("✔ Настройки теневого сохранены" +
                          (" — перезапусти теневой, чтобы применить." if self.paper_proc else "."),
                          "green", self.plog)
        return True

    # ───────────────────── бот ─────────────────────
    def toggle_bot(self):
        if self.proc:
            self.stop_bot()
        else:
            self.start_bot()

    def start_bot(self):
        if not BOT.exists():
            messagebox.showerror("Нет бота", f"Не найден {BOT.name} рядом с приложением.")
            return
        mode = self.mode_var.get()
        if mode == "live" and not messagebox.askyesno(
                "РЕАЛЬНЫЕ ДЕНЬГИ",
                "Режим LIVE — бот будет тратить НАСТОЯЩИЕ деньги по текущим лимитам.\n\nЗапустить?"):
            return
        if not self.save_limits(silent=True):
            return
        cfg = read_env()
        if mode in ("smoke", "live") and not (cfg.get("PRIVATE_KEY") and cfg.get("FUNDER")):
            messagebox.showwarning(
                "Нет ключа",
                "Для smoke/live в polymarket.env нужны PRIVATE_KEY и FUNDER.\n"
                "Впиши их в файл (приложение ключ не хранит) и запусти снова.")
            return
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW — без чёрного окна
        try:
            self.proc = subprocess.Popen(
                [sys.executable, str(BOT)], cwd=str(HERE),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Не запустилось", str(ex))
            self.proc = None
            return
        threading.Thread(target=self._reader, args=(self.proc, "real"), daemon=True).start()
        self.start_btn.config(text="■  Остановить", style="Stop.TButton")
        self.mode_cb.config(state="disabled")
        self._set_dot(GRN if mode == "live" else ACC, f"работает · {mode}")
        self._log_raw(f"▶ Бот запущен в режиме {mode.upper()}.", "cyan")

    # ── теневой (paper) бот: тот же процесс с MODE=paper, виртуальный $15k ──
    def toggle_paper(self):
        if self.paper_proc:
            self.stop_paper()
        else:
            self.start_paper()

    def start_paper(self):
        if not BOT.exists():
            return
        self.save_paper_limits(silent=True)          # свои PAPER_*-настройки перед запуском
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["MODE"] = "paper"                        # переопределяет MODE из файла для ЭТОГО процесса
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            self.paper_proc = subprocess.Popen(
                [sys.executable, str(BOT)], cwd=str(HERE),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Не запустилось", str(ex))
            self.paper_proc = None
            return
        threading.Thread(target=self._reader, args=(self.paper_proc, "paper"), daemon=True).start()
        self.paper_started = time.time()             # старт таймера работы
        self.pstart_btn.config(text="■  Остановить теневой", style="Stop.TButton")
        self.pdot.itemconfig(self._pdot_id, fill=ACC)
        self.pstat_lbl.config(text="работает")
        self._log_raw("▶ Теневой бот запущен (виртуальный $15k, реальные кэфы).", "cyan", self.plog)

    def stop_paper(self):
        if self.paper_proc:
            try:
                self.paper_proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.paper_proc = None
        if self.paper_started:                       # КОММИТИМ время сессии в накопленное
            self.paper_uptime += time.time() - self.paper_started
            self.paper_started = None
            self._save_uptime(self.paper_uptime)
            self._log_raw(f"■ Теневой остановлен. Всего наработано {self._fmt_dur(self.paper_uptime)}.",
                          "muted", self.plog)
        else:
            self._log_raw("■ Теневой бот остановлен.", "muted", self.plog)
        self.pstart_btn.config(text="▶  Запустить теневой", style="Accent.TButton")
        self.pdot.itemconfig(self._pdot_id, fill=MUT)
        self.pstat_lbl.config(text="остановлен")

    @staticmethod
    def _fmt_dur(sec):
        sec = int(sec); h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _load_uptime(self):
        try:
            return int(json.loads(UPTIME_FILE.read_text(encoding="utf-8")).get("total", 0))
        except Exception:  # noqa: BLE001
            return 0

    def _save_uptime(self, total):
        try:
            UPTIME_FILE.write_text(json.dumps({"total": int(total)}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _tick_paper_timer(self):
        live = (time.time() - self.paper_started) if self.paper_started else 0
        total = self.paper_uptime + live             # накопленное + текущая сессия
        self.puptime_lbl.config(text=f"⏱ {self._fmt_dur(total)}")
        if self.paper_started and time.time() - self._uptime_save_t > 10:
            self._save_uptime(total)                 # периодически на диск (переживёт краш пульта)
            self._uptime_save_t = time.time()
        self.after(1000, self._tick_paper_timer)

    # ── режим «ДЕРЖИМ»: стоп новых входов, выходы за целью работают (горячий флаг) ──
    def _read_hold(self, paper):
        f = HOLD_FILE_PAPER if paper else HOLD_FILE
        try:
            return bool(json.loads(f.read_text(encoding="utf-8")).get("hold"))
        except Exception:  # noqa: BLE001
            return False

    def _paint_hold(self, paper):
        holding = self._read_hold(paper)
        btn = self.phold_btn if paper else self.hold_btn
        btn.config(text=("▶ Возобновить входы" if holding else "⏸ Стоп входов (держим)"),
                   style="Stop.TButton" if holding else "Small.TButton")

    def _toggle_hold(self, paper):
        new = not self._read_hold(paper)
        f = HOLD_FILE_PAPER if paper else HOLD_FILE
        try:
            f.write_text(json.dumps({"hold": new}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        self._paint_hold(paper)
        w = self.plog if paper else None
        self._log_raw(("⏸ ДЕРЖИМ: новые входы на паузе, позиции доживают и закрываются вслед за целью."
                       if new else "▶ Входы возобновлены."),
                      "amber" if new else "green", w)

    def stop_bot(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.proc = None
        self.start_btn.config(text="▶  Запустить", style="Accent.TButton")
        self.mode_cb.config(state="readonly")
        self._set_dot(MUT, "остановлен")
        self._log_raw("■ Бот остановлен.", "muted")

    def _reader(self, proc, src):
        try:
            for line in proc.stdout:
                self.q.put((src, line))
        except Exception:  # noqa: BLE001
            pass
        self.q.put((src, None))        # сигнал: процесс завершился

    def _pump(self):
        try:
            while True:
                src, line = self.q.get_nowait()
                if line is None:
                    if src == "paper" and self.paper_proc:
                        self._log_raw("⚠ Теневой завершился.", "amber", self.plog)
                        self.stop_paper()
                    elif src == "best" and self.best_proc:
                        self._log_raw("⚠ Тест завершился.", "amber", self.blog)
                        self.stop_best()
                    elif src == "real" and self.proc:
                        self._log_raw("⚠ Бот завершился.", "amber")
                        self.stop_bot()
                else:
                    self._log(line, {"paper": self.plog, "best": self.blog}.get(src))
        except queue.Empty:
            pass
        self.after(150, self._pump)

    # ───────────────────── лог ─────────────────────
    def _log(self, line, w=None):
        line = line.rstrip("\n").rstrip("\r")
        if not line.strip():
            return
        low = line.lower()
        if "связь с биржей моргнула" in low or ("сеть" in low and "повтор" in low):
            tag, icon = "amber", "🌐"                # сетевой блип — не тревога, само повторится
        elif any(x in low for x in ("!!", "стоп", "не прош", "недоступ", "ошиб", "error", "traceback")):
            tag, icon = "red", "⛔"
        elif "выход" in low or "продал" in low or "sell" in low:
            tag, icon = "amber", "🔴"
        elif "куп" in low or "ордер:" in low or "[live]" in low or "[dry]" in low or "[smoke]" in low:
            tag, icon = "green", "🟢"
        elif "skip" in low or "пропуск" in low or "пропущ" in low or "не гонимся" in low:
            tag, icon = "muted", "⏭"
        elif "===" in line or "старт" in low or "сигналы:" in low or "форвард" in low:
            tag, icon = "cyan", "•"
        elif "пауз" in low:
            tag, icon = "amber", "⏸"
        else:
            tag, icon = "text", " "
        self._log_raw(f"{icon} {line.strip()}", tag, w)

    def _log_raw(self, text, tag="text", w=None):
        w = w if w is not None else self.log
        ts = time.strftime("%H:%M:%S")
        w.config(state="normal")
        w.insert("end", f"{ts}  ", "muted")
        w.insert("end", text + "\n", tag)
        if int(w.index("end-1c").split(".")[0]) > 800:   # держим лог компактным
            w.delete("1.0", "200.0")
        w.see("end")
        w.config(state="disabled")

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # ───────────────────── статус (трата за день из state-файла) ─────────────────────
    def _refresh_status(self):
        try:
            if STATE.exists():
                s = json.loads(STATE.read_text(encoding="utf-8"))
                self.spend_lbl.config(text=f"${s.get('spent_day', 0):.2f}")
        except Exception:  # noqa: BLE001
            pass
        self.after(3000, self._refresh_status)

    # ───────────────────── контур «Лучшие» ─────────────────────
    def _best_load(self):
        try:
            return [str(w).lower() for w in json.loads(BEST_ALLOW.read_text(encoding="utf-8"))]
        except Exception:  # noqa: BLE001
            return []

    def _best_save(self, lst):
        BEST_ALLOW.write_text(json.dumps(sorted(set(lst)), indent=1), encoding="utf-8")

    def _best_reload_list(self):
        self.blist.delete(0, "end")
        for w in self._best_load():
            self.blist.insert("end", w)

    def best_add(self):
        a = self.baddr_var.get().strip().lower()
        if not (a.startswith("0x") and len(a) == 42):
            messagebox.showerror("Адрес", "Нужен адрес вида 0x… (42 символа).")
            return
        lst = self._best_load(); lst.append(a); self._best_save(lst)
        self.baddr_var.set("")
        self._best_reload_list()
        self._log_raw(f"➕ В тест добавлен {a[:10]}…", "green", self.blog)

    def best_remove(self):
        sel = self.blist.curselection()
        if not sel:
            messagebox.showinfo("Выбери кошелёк", "Кликни строку в списке состава.")
            return
        a = self.blist.get(sel[0])
        self._best_save([w for w in self._best_load() if w != a])
        self._best_reload_list()
        self._log_raw(f"✕ Из теста убран {a[:10]}…", "red", self.blog)

    def toggle_best(self):
        if self.best_proc:
            self.stop_best()
        else:
            self.start_best()

    def start_best(self):
        if not BOT.exists():
            return
        if not self._best_load():
            messagebox.showwarning("Пустой состав", "Добавь хотя бы один кошелёк в состав теста.")
            return
        self.save_paper_limits(silent=True)          # тест идёт на PAPER_*-лимитах
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["MODE"] = "paper"                        # виртуальные деньги, реальные кэфы
        env["ALLOWLIST_FILE"] = BEST_ALLOW.name      # СВОЙ состав
        env["PAPER_STATE_FILE"] = BEST_STATE.name    # СВОЙ виртуальный счёт
        env["STATE_FILE"] = BEST_EXEC_STATE.name     # СВОЙ журнал исполненного
        env["HOLD_FILE"] = "hold_only_best.json"
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            self.best_proc = subprocess.Popen(
                [sys.executable, str(BOT)], cwd=str(HERE),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Не запустилось", str(ex))
            self.best_proc = None
            return
        threading.Thread(target=self._reader, args=(self.best_proc, "best"), daemon=True).start()
        self.bstart_btn.config(text="■  Остановить тест", style="Stop.TButton")
        self.bdot.itemconfig(self._bdot_id, fill=ACC)
        self.bstat_lbl.config(text="работает")
        self._log_raw(f"▶ Тест запущен: {len(self._best_load())} кошельков, виртуальный банк.",
                      "cyan", self.blog)

    def stop_best(self):
        if self.best_proc:
            try:
                self.best_proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.best_proc = None
        self.bstart_btn.config(text="▶  Запустить тест", style="Accent.TButton")
        self.bdot.itemconfig(self._bdot_id, fill=MUT)
        self.bstat_lbl.config(text="остановлен")
        self._log_raw("■ Тест остановлен.", "muted", self.blog)

    def _refresh_best_stats(self):
        try:
            if BEST_STATE.exists():
                p = json.loads(BEST_STATE.read_text(encoding="utf-8"))
                self.bcards["equity"].config(text=f"{p.get('equity', p.get('bankroll', 0)):,.0f}")
                self.bcards["pnl"].config(text=f"{p.get('pnl', 0):+,.0f}")
                self.bcards["roi"].config(text=f"{p.get('roi', 0):+.1f}")
                self.bcards["realized"].config(text=f"{p.get('realized', 0):+,.0f}")
                self.bcards["pos"].config(text=f"{len(p.get('pos', {}))}")
                self.bcards["trades"].config(text=f"{p.get('buys', 0)}/{p.get('sells', 0)}")
                ts = p.get("ts", 0)
                ago = int(time.time()) - ts if ts else 0
                self.bupd_lbl.config(text=(f"обновлено {ago} сек назад" if ts else "ещё не запускался")
                                     + f"  ·  старт ${p.get('bankroll', 0):,.0f}"
                                     + f"  ·  состав {len(self._best_load())}")
        except Exception:  # noqa: BLE001
            pass
        self.after(3000, self._refresh_best_stats)

    def _refresh_paper_stats(self):
        try:
            if PAPER.exists():
                p = json.loads(PAPER.read_text(encoding="utf-8"))
                self.pcards["equity"].config(text=f"{p.get('equity', p.get('bankroll', 0)):,.0f}")
                self.pcards["pnl"].config(text=f"{p.get('pnl', 0):+,.0f}")
                self.pcards["roi"].config(text=f"{p.get('roi', 0):+.1f}")
                self.pcards["realized"].config(text=f"{p.get('realized', 0):+,.0f}")
                self.pcards["pos"].config(text=f"{len(p.get('pos', {}))}")
                self.pcards["trades"].config(text=f"{p.get('buys', 0)}/{p.get('sells', 0)}")
                ts = p.get("ts", 0)
                ago = int(time.time()) - ts if ts else 0
                self.pupd_lbl.config(text=(f"обновлено {ago} сек назад" if ts else "ещё не запускался")
                                     + f"  ·  старт ${p.get('bankroll', 0):,.0f}")
        except Exception:  # noqa: BLE001
            pass
        self.after(3000, self._refresh_paper_stats)

    def _set_dot(self, color, text):
        self.dot.itemconfig(self._dot_id, fill=color)
        self.stat_lbl.config(text=text)

    # ───────────────────── баланс Polymarket (портфель + наличные) ─────────────────────
    def _poll_account(self):
        threading.Thread(target=self._account_work, daemon=True).start()
        self.after(45000, self._poll_account)      # раз в 45с (баланс меняется небыстро, вежливо к API)

    def _account_work(self):
        cfg = read_env()
        funder = (cfg.get("FUNDER") or "").strip()
        pk = (cfg.get("PRIVATE_KEY") or "").strip()
        pos = positions_value(funder)              # позиции — без ключа (data-api)
        cash = None
        if pk and funder:                          # наличные — по ключу, ЛОКАЛЬНО (как сайт Polymarket)
            try:
                c = self._get_client(pk, funder)
                if c is not None:
                    ba = c.get_balance_allowance(asset_type="COLLATERAL")
                    cash = int(getattr(ba, "balance", 0)) / 1e6
            except Exception:  # noqa: BLE001
                cash = None
        self.after(0, lambda: self._set_account(pos, cash))

    def _get_client(self, pk, funder):
        """Ленивая инициализация SDK-клиента для чтения баланса. Ключ НЕ покидает машину."""
        if self._client is not None:
            return self._client
        try:
            from polymarket import SecureClient
            self._client = SecureClient.create(private_key=pk, wallet=funder)
        except Exception:  # noqa: BLE001
            self._client = None
        return self._client

    def _set_account(self, pos, cash):
        total = (pos or 0) + (cash or 0) if (pos is not None or cash is not None) else None
        self.portf_lbl.config(text=f"${total:.2f}" if total is not None else "—")
        self.cash_lbl.config(text=f"${cash:.2f}" if cash is not None else "—")

    # ───────────────────── сервер / кошельки ─────────────────────
    def _server(self):
        s = load_settings()
        srv = s.get("server") or read_env().get("SERVER") or DEFAULT_SERVER
        return srv.rstrip("/")

    def _pw(self):
        s = load_settings()
        if s.get("pw"):
            return s["pw"]
        pw = simpledialog.askstring("Пароль действий",
                                    "Пароль для управления кошельками на сервере:", show="*")
        if pw:
            s["pw"] = pw
            s.setdefault("server", self._server())
            save_settings(s)
        return pw

    def _api_post(self, path, body):
        if requests is None:
            messagebox.showerror("Нет requests", "pip install requests")
            return None
        try:
            r = requests.post(self._server() + path, json=body, timeout=20)
            if r.status_code == 401:
                s = load_settings(); s.pop("pw", None); save_settings(s)
                messagebox.showerror("Пароль неверный", "Попробуй ещё раз.")
                return None
            return r.json()
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror("Сервер недоступен", str(ex))
            return None

    def refresh_wallets(self, quiet=False):
        if requests is None:
            return
        threading.Thread(target=self._wallets_work, args=(quiet,), daemon=True).start()

    def _wallets_work(self, quiet):
        srv = self._server()
        # КОПИ-НАБОР = ЛОКАЛЬНЫЙ allowlist (источник правды): добавленное видно сразу, без сервера/пароля
        try:
            allow = [str(w).lower() for w in json.loads(ALLOW_FILE.read_text(encoding="utf-8"))] \
                if ALLOW_FILE.exists() else []
        except Exception:  # noqa: BLE001
            allow = []
        try:
            main = requests.get(srv + "/api/state", timeout=20).json().get("per_wallet", []) or []
        except Exception as ex:  # noqa: BLE001
            if not quiet:
                self.after(0, lambda: messagebox.showerror("Сервер недоступен", str(ex)))
            main = []
        srcmap = {(w.get("wallet") or "").lower(): w for w in main}
        if allow:                                          # показываем ровно наш allowlist
            rows = [{"wallet": a, "source": (srcmap.get(a) or {}).get("source", "—"),
                     "copy_paused": False} for a in allow]
        else:                                              # пусто -> что знает сервер (fallback)
            rows = main
        funder = (read_env().get("FUNDER") or "").strip()
        real = self._real_pnl(srv, funder, [w.get("wallet", "") for w in rows])
        self.after(0, lambda: self._render_wallets(rows, real))

    def _render_wallets(self, rows, real):
        self.tree.delete(*self.tree.get_children())
        self.wallet_rows.clear()
        for w in rows:
            addr = w.get("wallet", "")
            paused = bool(w.get("copy_paused"))
            short = addr[:6] + "…" + addr[-4:] if len(addr) > 12 else addr
            rw = real.get(addr.lower())            # ТВОЙ реальный PnL по кошельку (или прочерк)
            pnl_txt = f"{rw['pnl']:+.2f}" if rw else "—"
            iid = self.tree.insert(
                "", "end", tags=("off" if paused else "on"),
                values=(short, w.get("source", "—"),
                        "пауза" if paused else "✓", pnl_txt))
            self.wallet_rows[iid] = {"addr": addr, "paused": paused}

    def _real_pnl(self, srv, funder, wallets):
        """ТВОЙ реальный PnL по кошелькам: позиции счёта с чейна (cashPnl) x привязка «какой кошелёк
        копировали». Привязка = карта (название,сторона)->кошелёк из серверных логов копирования
        (кэш 5 мин) + локальные теги бота. PnL 100% реальный, покрытие ~ вся история копирования."""
        if not funder or requests is None:
            return {}
        now = time.time()
        if not self._attr or now - self._attr_ts > 300:      # строим карту привязки (кэш 5 мин)
            amap = {}
            for a in wallets:
                try:
                    d = requests.get(srv + "/api/wallet", params={"g": "core", "addr": a}, timeout=20).json()
                except Exception:  # noqa: BLE001
                    continue
                for e in d.get("log", []):
                    if e.get("act") != "BUY":
                        continue
                    key = ((e.get("title") or "").strip().lower()[:40], (e.get("out") or "").lower())
                    amap.setdefault(key, a.lower())          # первый закрепляет (неоднозначных ~0)
            # локальные теги токен->кошелёк как дополнение (для рынков без совпадения по названию)
            try:
                stt = json.loads(STATE.read_text(encoding="utf-8"))
                self._tok_wallet = {str(t): (wv or "").lower() for t, wv in (stt.get("tok_wallet") or {}).items()}
            except Exception:  # noqa: BLE001
                self._tok_wallet = {}
            self._attr, self._attr_ts = amap, now
        try:
            pos = requests.get(f"https://data-api.polymarket.com/positions?user={funder}&limit=500",
                               timeout=15).json()
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for p in pos if isinstance(pos, list) else []:
            key = ((p.get("title") or "").strip().lower()[:40], (p.get("outcome") or "").lower())
            w = self._attr.get(key) or self._tok_wallet.get(str(p.get("asset") or ""))
            if not w:
                continue
            o = out.setdefault(w, {"pnl": 0.0, "inv": 0.0, "n": 0})
            o["pnl"] += float(p.get("cashPnl") or 0)
            o["inv"] += float(p.get("initialValue") or 0)
            o["n"] += 1
        return out

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Выбери кошелёк", "Кликни строку в списке.")
            return None
        return sel[0]

    def add_wallet(self):
        addr = self.addr_var.get().strip().lower()
        if not (addr.startswith("0x") and len(addr) == 42):
            messagebox.showerror("Адрес", "Нужен адрес вида 0x… (42 символа).")
            return
        allow_update(add=addr)                     # ЛОКАЛЬНО и МГНОВЕННО — без пароля/сервера
        self.addr_var.set("")
        self._log_raw(f"➕ Добавлен в копи: {addr[:10]}… (бот копирует его новые сделки)", "green")
        self.refresh_wallets()

    def toggle_pause(self):
        # В allowlist-модели «пауза» = убрать из копи (вернуть = добавить адрес заново).
        self.remove_wallet()

    def remove_wallet(self):
        iid = self._selected()
        if not iid:
            return
        row = self.wallet_rows[iid]
        if not messagebox.askyesno("Убрать кошелёк",
                                   f"Убрать {row['addr'][:10]}… из копи?"):
            return
        allow_update(remove=row["addr"])           # ЛОКАЛЬНО и МГНОВЕННО — без пароля/сервера
        self._log_raw(f"✕ Убран из копи: {row['addr'][:10]}…", "red")
        self.refresh_wallets()

    def _on_close(self):
        if (self.proc or self.paper_proc or self.best_proc) and not messagebox.askyesno(
                "Боты работают", "Бот(ы) ещё запущены. Остановить и выйти?"):
            return
        self.stop_bot()
        self.stop_paper()
        self.stop_best()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
