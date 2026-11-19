#!/usr/bin/env python3
"""
Campsite Scanner — desktop app.

A window for the same scanner as campsite_scanner.py: add campgrounds and dates, set up alerts,
press Start. Settings, state and the log live in %APPDATA%\\CampsiteScanner (Windows) or
~/.campsite-scanner (elsewhere), never next to the program.

Run from source:   python campsite_gui.py
Build the .exe:    pyinstaller --onefile --windowed --name CampsiteScanner campsite_gui.py
"""
from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import queue
import secrets
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import date, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import campsite_scanner as core

APP_NAME = "Campsite Scanner"
__version__ = "1.0.0"

if os.name == "nt" and os.environ.get("APPDATA"):
    APP_DIR = Path(os.environ["APPDATA"]) / "CampsiteScanner"
else:
    APP_DIR = Path.home() / ".campsite-scanner"
CONFIG_PATH = APP_DIR / "config.toml"
STATE_PATH = APP_DIR / "state.json"
LOG_PATH = APP_DIR / "scanner.log"

log = logging.getLogger("campsite_scanner")


# --------------------------------------------------------------------------- config file I/O

def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))  # JSON string escapes are valid TOML basic strings


def dump_toml(cfg: dict) -> str:
    """Minimal TOML writer for this app's config shape (top-level scalars, [[watch]], [notify.*])."""
    out = ["# Campsite Scanner settings, written by the desktop app.",
           "# Safe to edit by hand while the app is closed.", ""]
    for k, v in cfg.items():
        if k.startswith("_") or isinstance(v, dict) or k == "watch":
            continue
        out.append(f"{k} = {_toml_value(v)}")
    for w in cfg.get("watch", []):
        out += ["", "[[watch]]"] + [f"{k} = {_toml_value(v)}" for k, v in w.items()]
    for section, body in cfg.get("notify", {}).items():
        out += ["", f"[notify.{section}]"] + [f"{k} = {_toml_value(v)}" for k, v in body.items()]
    return "\n".join(out) + "\n"


def default_config() -> dict:
    return {
        "check_interval_seconds": 45,
        "keep_awake": True,
        "open_browser": True,
        "watch": [],
        "notify": {
            "ntfy": {"enabled": False, "topic": "campsites-" + secrets.token_hex(6),
                     "server": "https://ntfy.sh", "priority": "high"},
            "email": {"enabled": False, "to": [], "username": "", "password": "",
                      "smtp_host": "smtp.gmail.com", "smtp_port": 587},
            "sms": {"enabled": False, "to": [], "from": "", "account_sid": "", "auth_token": ""},
        },
    }


def load_app_config() -> dict:
    cfg = default_config()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            saved = core.tomllib.load(f)
        for k, v in saved.items():
            if k == "notify":
                for sec, body in v.items():
                    cfg["notify"].setdefault(sec, {}).update(body)
            else:
                cfg[k] = v
    return cfg


def save_app_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(dump_toml(cfg), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)  # atomic: never leaves a half-written config


def parse_date(text: str) -> date:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"'{text}' isn't a date — use YYYY-MM-DD or MM/DD/YYYY")


def split_list(text: str) -> list[str]:
    return [x.strip() for x in text.replace(";", ",").split(",") if x.strip()]


# --------------------------------------------------------------------------- keep the PC awake (Windows)

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def set_keep_awake(on: bool) -> None:
    """Ask Windows not to sleep while scanning. The screen can still turn off."""
    if os.name != "nt":
        return
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        log.warning("Couldn't change the Windows sleep setting")


# --------------------------------------------------------------------------- logging into the window

class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


# --------------------------------------------------------------------------- add / edit watch dialog

class WatchDialog(tk.Toplevel):
    def __init__(self, parent, watch: dict | None = None):
        super().__init__(parent)
        self.title("Edit campground" if watch else "Add campground")
        self.transient(parent)
        self.resizable(False, False)
        self.result: dict | None = None
        self._results: list[dict] = []
        self._q: queue.Queue = queue.Queue()  # search results come back here (Tk isn't thread-safe)
        w = watch or {}

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")

        # search
        ttk.Label(frm, text="Find a campground on Recreation.gov:").grid(row=0, column=0, columnspan=3, sticky="w")
        self.q = tk.StringVar()
        qe = ttk.Entry(frm, textvariable=self.q, width=40)
        qe.grid(row=1, column=0, columnspan=2, sticky="ew", **pad)
        qe.bind("<Return>", lambda e: self.search())
        self.search_btn = ttk.Button(frm, text="Search", command=self.search)
        self.search_btn.grid(row=1, column=2, sticky="ew", **pad)
        self.listbox = tk.Listbox(frm, height=7, width=70, activestyle="dotbox")
        self.listbox.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        self.listbox.bind("<<ListboxSelect>>", self.pick)
        self.search_status = ttk.Label(frm, text="Type a name (e.g. \"jumbo rocks\") and press Search.",
                                       foreground="gray")
        self.search_status.grid(row=3, column=0, columnspan=3, sticky="w", padx=8)

        ttk.Separator(frm).grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)

        self.v = {
            "campground_id": tk.StringVar(value=w.get("campground_id", "")),
            "name": tk.StringVar(value=w.get("name", "")),
            "check_in": tk.StringVar(value=str(w.get("check_in", ""))),
            "check_out": tk.StringVar(value=str(w.get("check_out", ""))),
            "nights": tk.StringVar(value=str(w.get("nights", ""))),
            "site_types": tk.StringVar(value=", ".join(w.get("site_types", []))),
            "sites": tk.StringVar(value=", ".join(w.get("sites", []))),
        }
        rows = [("Campground ID", "campground_id", "filled in when you pick a search result"),
                ("Label", "name", "any name you like"),
                ("Check-in", "check_in", "first night, e.g. 2027-05-21"),
                ("Check-out", "check_out", "the morning you leave"),
                ("Nights needed", "nights", "blank = the whole stay; fewer = flexible dates"),
                ("Site type contains", "site_types", "optional, e.g. TENT  (comma-separated)"),
                ("Only these sites", "sites", "optional site numbers, e.g. 017, 042")]
        for i, (label, key, hint) in enumerate(rows, start=5):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", **pad)
            ttk.Entry(frm, textvariable=self.v[key], width=42).grid(row=i, column=1, sticky="w", **pad)
            ttk.Label(frm, text=hint, foreground="gray").grid(row=i, column=2, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=20, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="OK", command=self.ok).pack(side="right", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())

        qe.focus_set()
        self.grab_set()

    def search(self):
        text = self.q.get().strip()
        if not text:
            return
        self.search_btn.config(state="disabled")
        self.search_status.config(text="Searching…")

        def work():
            try:
                res, err = core.search_campgrounds(text), None
            except Exception as e:
                res, err = [], str(e)
            self._q.put((res, err))
        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_search)

    def _poll_search(self):
        if not self.winfo_exists():
            return
        try:
            res, err = self._q.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_search)
            return
        self.show_results(res, err)

    def show_results(self, res, err):
        if not self.winfo_exists():
            return
        self.search_btn.config(state="normal")
        self._results = res
        self.listbox.delete(0, "end")
        for r in res:
            self.listbox.insert("end", f"{r['name']}  —  {r['park']} ({r['state']})   [ID {r['id']}]")
        if err:
            self.search_status.config(text=f"Search failed: {err}")
        else:
            self.search_status.config(text=f"{len(res)} result(s). Click one to select it." if res
                                      else "No campgrounds found. Try a shorter name.")

    def pick(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        r = self._results[sel[0]]
        self.v["campground_id"].set(r["id"])
        name = r["name"].title() if r["name"].isupper() else r["name"]
        self.v["name"].set(f"{r['park']} - {name}" if r["park"] else name)

    def ok(self):
        try:
            cg = self.v["campground_id"].get().strip()
            if not cg.isdigit():
                raise ValueError("Pick a campground from the search results (or enter its numeric ID).")
            ci, co = parse_date(self.v["check_in"].get()), parse_date(self.v["check_out"].get())
            if co <= ci:
                raise ValueError("Check-out must be after check-in.")
            stay = (co - ci).days
            nights_txt = self.v["nights"].get().strip()
            nights = int(nights_txt) if nights_txt else stay
            if not 1 <= nights <= stay:
                raise ValueError(f"Nights needed must be between 1 and {stay}.")
        except ValueError as e:
            messagebox.showerror("Check this campground", str(e), parent=self)
            return
        self.result = {
            "name": self.v["name"].get().strip() or f"Campground {cg}",
            "campground_id": cg, "check_in": ci, "check_out": co, "nights": nights,
            "site_types": [s.upper() for s in split_list(self.v["site_types"].get())],
            "sites": [s.upper() for s in split_list(self.v["sites"].get())],
            "enabled": True,
        }
        self.destroy()


# --------------------------------------------------------------------------- main window

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("920x640")
        self.minsize(760, 560)
        self.q: queue.Queue = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.dirty_while_running = False

        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.setup_logging()
        try:
            self.cfg = load_app_config()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't read {CONFIG_PATH}:\n{e}\n\nStarting with default settings.")
            self.cfg = default_config()

        self.build_ui()
        self.refresh_watches()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_job = self.after(150, self.poll_queue)

    # ---- setup
    def setup_logging(self):
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        qh = QueueLogHandler(self.q)
        qh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        log.setLevel(logging.INFO)
        log.addHandler(fh)
        log.addHandler(qh)

    def build_ui(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        # Pack the bottom bar FIRST so the tabs can never squeeze it out of the window.
        bar = ttk.Frame(self, padding=8)
        bar.pack(side="bottom", fill="x")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.build_watches_tab()
        self.build_alerts_tab()
        self.build_settings_tab()
        self.build_activity_tab()

        self.start_btn = ttk.Button(bar, text="▶  Start scanning", command=self.toggle, width=20)
        self.start_btn.pack(side="left")
        self.status = ttk.Label(bar, text="Not running.")
        self.status.pack(side="left", padx=12)

    def build_watches_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Campgrounds")
        cols = ("on", "name", "cg", "in", "out", "nights")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        for c, label, width in [("on", "On", 40), ("name", "Label", 380), ("cg", "ID", 90),
                                ("in", "Check-in", 100), ("out", "Check-out", 100), ("nights", "Nights", 60)]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=width, anchor="w" if c == "name" else "center", stretch=(c == "name"))
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_watch())

        side = ttk.Frame(tab, padding=(8, 0))
        side.pack(side="left", fill="y")
        for text, cmd in [("Add…", self.add_watch), ("Edit…", self.edit_watch),
                          ("Turn on / off", self.toggle_watch), ("Remove", self.remove_watch)]:
            ttk.Button(side, text=text, command=cmd, width=14).pack(pady=3)
        self.empty_hint = ttk.Label(side, text="Click Add… to watch\nyour first campground.",
                                    foreground="gray", justify="left")
        self.empty_hint.pack(pady=12)

    def build_alerts_tab(self):
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="Alerts")
        n = self.cfg["notify"]
        self.av = {
            "ntfy_on": tk.BooleanVar(value=n["ntfy"].get("enabled", False)),
            "ntfy_topic": tk.StringVar(value=n["ntfy"].get("topic", "")),
            "email_on": tk.BooleanVar(value=n["email"].get("enabled", False)),
            "email_to": tk.StringVar(value=", ".join(n["email"].get("to", []))),
            "email_user": tk.StringVar(value=n["email"].get("username", "")),
            "email_pw": tk.StringVar(value=n["email"].get("password", "")),
            "sms_on": tk.BooleanVar(value=n["sms"].get("enabled", False)),
            "sms_to": tk.StringVar(value=", ".join(n["sms"].get("to", []))),
            "sms_from": tk.StringVar(value=n["sms"].get("from", "")),
            "sms_sid": tk.StringVar(value=n["sms"].get("account_sid", "")),
            "sms_token": tk.StringVar(value=n["sms"].get("auth_token", "")),
        }
        pad = {"padx": 6, "pady": 3}

        def link(parent, text, url):
            lbl = ttk.Label(parent, text=text, foreground="#0a58ca", cursor="hand2")
            lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
            return lbl

        f = ttk.LabelFrame(tab, text="Phone push (ntfy) — free, recommended", padding=8)
        f.pack(fill="x", pady=4)
        ttk.Checkbutton(f, text="Send push notifications", variable=self.av["ntfy_on"]).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Topic").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.av["ntfy_topic"], width=36).grid(row=1, column=1, sticky="w", **pad)
        ttk.Button(f, text="New random topic", command=lambda: self.av["ntfy_topic"].set(
            "campsites-" + secrets.token_hex(6))).grid(row=1, column=2, **pad)
        ttk.Label(f, text="Install the ntfy app on your phone, tap +, and subscribe to exactly this topic. "
                          "Keep it private — anyone who knows it can read your alerts.",
                  foreground="gray", wraplength=640).grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        link(f, "Get the ntfy app →", "https://ntfy.sh/").grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        f = ttk.LabelFrame(tab, text="Email (sent from a Gmail account)", padding=8)
        f.pack(fill="x", pady=4)
        ttk.Checkbutton(f, text="Send email alerts", variable=self.av["email_on"]).grid(row=0, column=0, columnspan=3, sticky="w")
        for i, (label, key, kw) in enumerate([("Send alerts to", "email_to", {}),
                                              ("From Gmail address", "email_user", {}),
                                              ("Gmail App Password", "email_pw", {"show": "•"})], start=1):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", **pad)
            ttk.Entry(f, textvariable=self.av[key], width=36, **kw).grid(row=i, column=1, sticky="w", **pad)
        ttk.Label(f, text="comma-separate several addresses", foreground="gray").grid(row=1, column=2, sticky="w", **pad)
        link(f, "Create an App Password →", "https://myaccount.google.com/apppasswords").grid(row=3, column=2, sticky="w", **pad)

        f = ttk.LabelFrame(tab, text="Text message (Twilio) — paid, optional", padding=8)
        f.pack(fill="x", pady=4)
        ttk.Checkbutton(f, text="Send SMS", variable=self.av["sms_on"]).grid(row=0, column=0, sticky="w")
        for i, (label, key, kw) in enumerate([("To (+1…)", "sms_to", {}), ("From (Twilio no.)", "sms_from", {}),
                                              ("Account SID", "sms_sid", {}),
                                              ("Auth token", "sms_token", {"show": "•"})]):
            r, c = divmod(i, 2)
            ttk.Label(f, text=label).grid(row=r + 1, column=c * 2, sticky="w", **pad)
            ttk.Entry(f, textvariable=self.av[key], width=24, **kw).grid(row=r + 1, column=c * 2 + 1, sticky="w", **pad)

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=8)
        self.test_btn = ttk.Button(row, text="Send a test alert", command=self.send_test)
        self.test_btn.pack(side="left")
        ttk.Label(row, text="Settings are saved automatically.", foreground="gray").pack(side="left", padx=10)

    def build_settings_tab(self):
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="Settings")
        self.sv = {
            "interval": tk.StringVar(value=str(self.cfg.get("check_interval_seconds", 45))),
            "keep_awake": tk.BooleanVar(value=self.cfg.get("keep_awake", True)),
            "open_browser": tk.BooleanVar(value=self.cfg.get("open_browser", True)),
        }
        r = ttk.Frame(tab)
        r.pack(anchor="w", pady=4)
        ttk.Label(r, text="Check every").pack(side="left")
        ttk.Spinbox(r, from_=core.MIN_INTERVAL_S, to=3600, increment=5, width=6,
                    textvariable=self.sv["interval"]).pack(side="left", padx=6)
        ttk.Label(r, text="seconds   (45–60 is polite; minimum 10)").pack(side="left")
        ttk.Checkbutton(tab, text="Keep this computer awake while scanning (the screen can still turn off)",
                        variable=self.sv["keep_awake"]).pack(anchor="w", pady=4)
        ttk.Checkbutton(tab, text="Open the booking page in my browser when a site opens up",
                        variable=self.sv["open_browser"]).pack(anchor="w", pady=4)
        ttk.Separator(tab).pack(fill="x", pady=12)
        ttk.Label(tab, text=f"Settings, history and log are stored in:\n{APP_DIR}", foreground="gray").pack(anchor="w")
        ttk.Button(tab, text="Open that folder", command=self.open_data_dir).pack(anchor="w", pady=6)
        ttk.Label(tab, text="This app only alerts you — you still book the site yourself on Recreation.gov.",
                  foreground="gray").pack(anchor="w", pady=(12, 0))

    def build_activity_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Activity")
        self.logbox = ScrolledText(tab, height=10, state="disabled", wrap="word", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)

    # ---- watches
    def refresh_watches(self):
        self.tree.delete(*self.tree.get_children())
        for i, w in enumerate(self.cfg["watch"]):
            self.tree.insert("", "end", iid=str(i), values=(
                "✔" if w.get("enabled", True) else "—", w.get("name", ""), w.get("campground_id", ""),
                str(w.get("check_in", "")), str(w.get("check_out", "")), w.get("nights", "")))
        if self.cfg["watch"]:
            self.empty_hint.pack_forget()
        else:
            self.empty_hint.pack(pady=12)

    def selected_index(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a campground in the list first.")
            return None
        return int(sel[0])

    def add_watch(self):
        d = WatchDialog(self)
        self.wait_window(d)
        if d.result:
            self.cfg["watch"].append(d.result)
            self.after_edit()

    def edit_watch(self):
        i = self.selected_index()
        if i is None:
            return
        d = WatchDialog(self, self.cfg["watch"][i])
        self.wait_window(d)
        if d.result:
            d.result["enabled"] = self.cfg["watch"][i].get("enabled", True)
            self.cfg["watch"][i] = d.result
            self.after_edit()

    def toggle_watch(self):
        i = self.selected_index()
        if i is not None:
            self.cfg["watch"][i]["enabled"] = not self.cfg["watch"][i].get("enabled", True)
            self.after_edit()

    def remove_watch(self):
        i = self.selected_index()
        if i is not None and messagebox.askyesno(APP_NAME, f"Remove \"{self.cfg['watch'][i].get('name')}\"?"):
            del self.cfg["watch"][i]
            self.after_edit()

    def after_edit(self):
        self.refresh_watches()
        self.save()

    # ---- settings <-> config
    def collect(self):
        """Copy the form fields into self.cfg."""
        n = self.cfg["notify"]
        a = self.av
        n["ntfy"].update(enabled=a["ntfy_on"].get(), topic=a["ntfy_topic"].get().strip())
        n["email"].update(enabled=a["email_on"].get(), to=split_list(a["email_to"].get()),
                          username=a["email_user"].get().strip(), password=a["email_pw"].get().strip())
        n["sms"].update(enabled=a["sms_on"].get(), to=split_list(a["sms_to"].get()),
                        account_sid=a["sms_sid"].get().strip(), auth_token=a["sms_token"].get().strip())
        n["sms"]["from"] = a["sms_from"].get().strip()
        try:
            self.cfg["check_interval_seconds"] = max(core.MIN_INTERVAL_S, int(float(self.sv["interval"].get())))
        except ValueError:
            pass
        self.cfg["keep_awake"] = self.sv["keep_awake"].get()
        self.cfg["open_browser"] = self.sv["open_browser"].get()

    def save(self):
        self.collect()
        try:
            save_app_config(self.cfg)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't save settings:\n{e}")
            return
        if self.running:
            self.dirty_while_running = True
            self.set_status("Changes saved — stop and start scanning to apply them.")

    def validate_alerts(self) -> str | None:
        n = self.cfg["notify"]
        if n["ntfy"]["enabled"] and len(n["ntfy"]["topic"]) < 6:
            return "Enter an ntfy topic (or click New random topic)."
        if n["email"]["enabled"]:
            if not n["email"]["to"] or not n["email"]["username"] or not n["email"]["password"]:
                return "Email alerts need a To address, the Gmail address to send from, and its App Password."
        if n["sms"]["enabled"] and not all([n["sms"]["to"], n["sms"]["from"], n["sms"]["account_sid"],
                                           n["sms"]["auth_token"]]):
            return "Text alerts need all four Twilio fields."
        return None

    # ---- actions
    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def send_test(self):
        self.save()
        problem = self.validate_alerts()
        if problem:
            messagebox.showwarning(APP_NAME, problem)
            return
        if not any(self.cfg["notify"][k]["enabled"] for k in ("ntfy", "email", "sms")):
            messagebox.showwarning(APP_NAME, "Turn on at least one alert type first.")
            return
        self.test_btn.config(state="disabled", text="Sending…")
        cfg = copy.deepcopy(self.cfg)

        def work():
            res = core.notify(cfg, "⛺ Campsite Scanner test",
                              "This is a test from Campsite Scanner. If you got this, alerts will reach you.",
                              "Campsite Scanner test — alerts will reach you.", core.RECGOV)
            self.q.put(("test_done", res))
        threading.Thread(target=work, daemon=True).start()

    def toggle(self):
        if self.running:
            self.stop_scanning()
        else:
            self.start_scanning()

    def start_scanning(self):
        self.save()
        try:
            cfg = core.parse_config(copy.deepcopy(self.cfg))  # snapshot: edits apply on next Start
        except core.ConfigError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        today = date.today()
        if not core.active_watches(cfg, today):
            messagebox.showinfo(APP_NAME, "Add at least one campground with future dates (and make sure it's turned on).")
            self.nb.select(0)
            return
        problem = self.validate_alerts()
        if problem:
            messagebox.showwarning(APP_NAME, problem)
            self.nb.select(1)
            return
        if not any(cfg["notify"][k]["enabled"] for k in ("ntfy", "email", "sms")):
            if not messagebox.askyesno(APP_NAME, "No alerts are turned on, so openings will only show in this "
                                                 "window. Start anyway?"):
                self.nb.select(1)
                return

        self.stop_event = threading.Event()
        state = core.State(STATE_PATH)
        open_browser = cfg.get("open_browser", True)

        def on_scan(res, took, next_in):
            self.q.put(("scan", res, took, next_in))

        def on_fresh(w, cg_name, opens):
            self.q.put(("fresh", w.name, cg_name, opens[0].url if len(opens) == 1
                        else core.CAMPGROUND_PAGE.format(cg=w.campground_id), open_browser))

        def work():
            try:
                core.run_loop(cfg, state, stop=self.stop_event, on_scan=on_scan, on_fresh=on_fresh)
            except Exception:
                log.exception("Scanner stopped because of an error")
            finally:
                self.q.put(("stopped",))

        self.dirty_while_running = False
        self.worker = threading.Thread(target=work, daemon=True, name="scanner")
        self.worker.start()
        if cfg.get("keep_awake", True):
            set_keep_awake(True)
        n = len({w.campground_id for w in core.active_watches(cfg, today)})
        self.start_btn.config(text="■  Stop scanning")
        self.set_status(f"Starting — watching {n} campground(s)…")
        self.nb.select(3)

    def stop_scanning(self):
        if self.stop_event:
            self.stop_event.set()
        self.start_btn.config(state="disabled", text="Stopping…")

    def open_data_dir(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(APP_DIR)  # type: ignore[attr-defined]
        else:
            webbrowser.open(APP_DIR.as_uri())

    def set_status(self, text: str):
        self.status.config(text=text)

    # ---- events from worker threads
    def poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.append_log(msg[1])
                elif kind == "scan":
                    _, res, took, next_in = msg
                    if not self.dirty_while_running:
                        extra = " — slowed down (rate-limited)" if res.get("rate_limited") else ""
                        nxt = f", next in {next_in:.0f} s" if next_in else ""
                        self.set_status(f"Scanning. Last check {datetime.now():%H:%M:%S}: "
                                        f"{res['openings']} opening(s){nxt}{extra}")
                elif kind == "fresh":
                    _, wname, cg_name, url, open_browser = msg
                    if open_browser:
                        webbrowser.open(url)
                    self.bell()
                    self.deiconify()
                    self.lift()
                    self.attributes("-topmost", True)
                    self.after(1500, lambda: self.attributes("-topmost", False))
                elif kind == "test_done":
                    self.test_btn.config(state="normal", text="Send a test alert")
                    res = msg[1]
                    lines = [f"{ch}: " + ("sent ✓" if err is None else f"FAILED — {err}") for ch, err in res.items()]
                    (messagebox.showinfo if all(e is None for e in res.values()) else messagebox.showwarning)(
                        APP_NAME, "\n".join(lines) or "Nothing was sent.")
                elif kind == "stopped":
                    set_keep_awake(False)
                    self.worker = None
                    self.start_btn.config(state="normal", text="▶  Start scanning")
                    self.set_status("Not running.")
        except queue.Empty:
            pass
        self._poll_job = self.after(150, self.poll_queue)

    def destroy(self):
        try:
            self.after_cancel(self._poll_job)
        except (AttributeError, tk.TclError):
            pass
        super().destroy()

    def append_log(self, line: str):
        self.logbox.config(state="normal")
        self.logbox.insert("end", line + "\n")
        n = int(self.logbox.index("end-1c").split(".")[0])
        if n > 1500:
            self.logbox.delete("1.0", f"{n - 1000}.0")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def on_close(self):
        if self.running and not messagebox.askyesno(
                APP_NAME, "Scanning is running. Quit and stop scanning?\n\n"
                          "(To keep scanning, minimize the window instead.)"):
            return
        self.save()
        if self.stop_event:
            self.stop_event.set()
        set_keep_awake(False)
        self.destroy()


def main():
    if os.name == "nt":  # crisp text on high-DPI screens
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
