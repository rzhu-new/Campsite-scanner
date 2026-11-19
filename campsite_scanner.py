#!/usr/bin/env python3
"""
Campsite Scanner — watches Recreation.gov campgrounds and alerts you
(email, SMS via Twilio, and/or phone push via ntfy) when a site opens up
for the dates you want.

Usage
-----
    python campsite_scanner.py                 # run forever, checking every N minutes
    python campsite_scanner.py --once          # one scan, then exit (for Task Scheduler)
    python campsite_scanner.py --dry-run       # scan + print, never send notifications
    python campsite_scanner.py find "death valley"   # look up campground IDs
    python campsite_scanner.py test-notify     # send a test message on every enabled channel

Everything else lives in config.toml — copy config.example.toml to get started.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import logging.handlers
import os
import random
import smtplib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.8–3.10
    import tomli as tomllib  # type: ignore

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RECGOV = "https://www.recreation.gov"
AVAIL_URL = RECGOV + "/api/camps/availability/campground/{cg}/month"
SEARCH_URL = RECGOV + "/api/search"
CAMPGROUND_PAGE = RECGOV + "/camping/campgrounds/{cg}"
CAMPSITE_PAGE = RECGOV + "/camping/campsites/{site}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CampsiteScanner/1.0",
    "Accept": "application/json",
}
BOOKABLE = "Available"  # other statuses: Reserved, Closed, Not Reservable, NYR, Open (walk-up) ...

log = logging.getLogger("campsite_scanner")


# --------------------------------------------------------------------------- config

@dataclass
class Watch:
    name: str
    campground_id: str
    check_in: date
    check_out: date
    nights: int                      # min consecutive nights needed inside the window
    site_types: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    loops: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{self.campground_id}:{self.check_in}:{self.check_out}:{self.nights}"


class ConfigError(ValueError):
    """Invalid config. The CLI turns this into a clean exit; the GUI shows it in a dialog."""


def _as_date(v, what: str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise ConfigError(f"{what} must be YYYY-MM-DD, got {v!r}")


def load_config(path: Path) -> dict:
    if not path.exists():
        example = path.with_name("config.example.toml")
        hint = f" — copy {example.name} to {path.name} and edit it" if example.exists() else ""
        raise SystemExit(f"Config file not found: {path}{hint}")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    try:
        return parse_config(raw)
    except ConfigError as e:
        raise SystemExit(f"Config error: {e}")


def parse_config(cfg: dict) -> dict:
    """Validate a config dict (as read from TOML) and attach the parsed watches as cfg["_watches"]."""
    cfg = dict(cfg)
    watches = []
    for i, w in enumerate(cfg.get("watch", []), 1):
        label = w.get("name") or f"watch #{i}"
        if "campground_id" not in w:
            raise ConfigError(f"{label} is missing campground_id")
        ci = _as_date(w.get("check_in"), f"{label}.check_in")
        co = _as_date(w.get("check_out"), f"{label}.check_out")
        if co <= ci:
            raise ConfigError(f"{label}: check_out must be after check_in")
        stay = (co - ci).days
        nights = int(w.get("nights") or stay)
        if not 1 <= nights <= stay:
            raise ConfigError(f"{label}: nights must be between 1 and {stay}")
        watches.append(Watch(
            name=label,
            campground_id=str(w["campground_id"]),
            check_in=ci,
            check_out=co,
            nights=nights,
            site_types=[s.upper() for s in w.get("site_types", [])],
            sites=[str(s).upper() for s in w.get("sites", [])],
            loops=[s.upper() for s in w.get("loops", [])],
            enabled=w.get("enabled", True),
        ))
    cfg["_watches"] = watches
    return cfg


# --------------------------------------------------------------------------- recreation.gov

class RateLimited(Exception):
    """Recreation.gov answered 429 (Too Many Requests) or 403 (blocked)."""


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # Retry only transient server errors, quickly. 429/403 are NOT retried here: at a 15 s cycle,
    # hammering a rate-limited API makes things worse, so the main loop backs off instead.
    retry = Retry(total=2, backoff_factor=1, status_forcelist=(500, 502, 503, 504),
                  allowed_methods=("GET", "POST"))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


_tls = threading.local()


def thread_session() -> requests.Session:
    """One Session per worker thread (requests.Session isn't guaranteed thread-safe)."""
    s = getattr(_tls, "session", None)
    if s is None:
        s = _tls.session = make_session()
    return s


def month_starts(first: date, last: date):
    d = first.replace(day=1)
    while d <= last:
        yield d
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def fetch_month(cg_id: str, month: date) -> dict:
    """Fetch one campground-month: {campsite_id: site_dict}."""
    r = thread_session().get(AVAIL_URL.format(cg=cg_id),
                             params={"start_date": f"{month.isoformat()}T00:00:00.000Z"}, timeout=20)
    if r.status_code in (429, 403):
        raise RateLimited(f"HTTP {r.status_code}")
    r.raise_for_status()
    return r.json().get("campsites") or {}


def merge_months(parts: list[dict]) -> dict:
    """Merge several fetch_month() results into one {campsite_id: site} with all availabilities."""
    merged: dict[str, dict] = {}
    for campsites in parts:
        for sid, site in campsites.items():
            if sid in merged:
                merged[sid]["availabilities"].update(site.get("availabilities") or {})
            else:
                site = dict(site)
                site["availabilities"] = dict(site.get("availabilities") or {})
                merged[sid] = site
    return merged


def search_campgrounds(query: str, size: int = 20) -> list[dict]:
    """Search Recreation.gov campgrounds by name. Returns [{"id", "name", "park", "state"}]."""
    r = thread_session().get(SEARCH_URL, params={"q": query, "fq": "entity_type:campground", "size": size},
                             timeout=20)
    r.raise_for_status()
    return [{"id": str(x.get("entity_id", "")), "name": x.get("name", ""),
             "park": x.get("parent_name", ""), "state": x.get("state_code", "")}
            for x in r.json().get("results", []) if x.get("entity_id")]


def campground_name(session: requests.Session, cg_id: str) -> str | None:
    try:
        r = session.get(SEARCH_URL, params={"q": cg_id, "fq": "entity_type:campground", "size": 5}, timeout=20)
        for res in r.json().get("results", []):
            if str(res.get("entity_id")) == cg_id:
                return res.get("name")
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- matching

@dataclass
class Opening:
    campsite_id: str
    site: str
    loop: str
    site_type: str
    start: date   # first night (check-in)
    end: date     # check-out

    @property
    def nights(self) -> int:
        return (self.end - self.start).days

    @property
    def url(self) -> str:
        return CAMPSITE_PAGE.format(site=self.campsite_id)


def site_matches(site: dict, w: Watch) -> bool:
    if w.sites and str(site.get("site", "")).upper() not in w.sites:
        return False
    if w.loops and str(site.get("loop", "")).upper() not in w.loops:
        return False
    if w.site_types:
        st = str(site.get("campsite_type", "")).upper()
        if not any(t in st for t in w.site_types):
            return False
    return True


def find_openings(campsites: dict, w: Watch, today: date | None = None) -> list[Opening]:
    """Find every maximal run of bookable nights (>= w.nights) inside [check_in, check_out)."""
    today = today or date.today()
    first = max(w.check_in, today)
    if first >= w.check_out:
        return []
    nights = [first + timedelta(days=i) for i in range((w.check_out - first).days)]
    out: list[Opening] = []
    for sid, site in campsites.items():
        if not site_matches(site, w):
            continue
        avail = site.get("availabilities") or {}
        run_start = None
        for n in nights + [None]:  # sentinel closes the last run
            ok = n is not None and avail.get(f"{n.isoformat()}T00:00:00Z") == BOOKABLE
            if ok and run_start is None:
                run_start = n
            elif not ok and run_start is not None:
                end = n if n is not None else w.check_out
                if (end - run_start).days >= w.nights:
                    out.append(Opening(sid, str(site.get("site", sid)), str(site.get("loop", "")),
                                       str(site.get("campsite_type", "")), run_start, end))
                run_start = None
    out.sort(key=lambda o: (o.start, -o.nights, o.site))
    return out


# --------------------------------------------------------------------------- state (avoid repeat alerts)

class State:
    """Remembers which openings we already alerted on. If an opening disappears and later
    comes back, you get alerted again."""

    # data = {watch_key: {opening_id: {"since": iso_timestamp, "label": "site 042 11/26-11/28"}}}

    def __init__(self, path: Path):
        self.path = path
        try:
            raw = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        now = datetime.now().isoformat(timespec="seconds")
        self.data: dict[str, dict[str, dict]] = {
            k: (v if isinstance(v, dict) else {i: {"since": now, "label": i} for i in v})  # old list format
            for k, v in raw.items()}
        self._dirty = False

    @staticmethod
    def _id(o: Opening) -> str:
        return f"{o.campsite_id}|{o.start}|{o.end}"

    @staticmethod
    def label(o: Opening) -> str:
        return f"site {o.site} {o.start:%m/%d}-{o.end:%m/%d}"

    def new_openings(self, watch_key: str, openings: list[Opening]) -> list[Opening]:
        seen = self.data.get(watch_key, {})
        return [o for o in openings if self._id(o) not in seen]

    def update(self, watch_key: str, openings: list[Opening]) -> list[tuple[str, float]]:
        """Record current openings. Returns [(label, seconds_it_was_open)] for openings that vanished."""
        old = self.data.get(watch_key, {})
        now = datetime.now()
        cur = {self._id(o): old.get(self._id(o)) or {"since": now.isoformat(timespec="seconds"),
                                                     "label": self.label(o)} for o in openings}
        gone = []
        for oid, info in old.items():
            if oid not in cur:
                try:
                    secs = (now - datetime.fromisoformat(info["since"])).total_seconds()
                except (KeyError, ValueError):
                    secs = float("nan")
                gone.append((info.get("label", oid), secs))
        if cur != old:
            self._dirty = True
        self.data[watch_key] = cur
        return gone

    def save(self) -> None:
        if self._dirty:  # at a 15 s cycle, only touch the disk when something changed
            self.path.write_text(json.dumps(self.data, indent=1))
            self._dirty = False


# --------------------------------------------------------------------------- notifications

def fmt_range(o: Opening) -> str:
    return f"{o.start:%a %b %d} → {o.end:%a %b %d} ({o.nights} night{'s' * (o.nights != 1)})"


def build_messages(w: Watch, cg_name: str, opens: list[Opening]) -> tuple[str, str, str]:
    """Returns (subject, long_body, short_text)."""
    cg_url = CAMPGROUND_PAGE.format(cg=w.campground_id)
    subject = f"⛺ {len(opens)} campsite opening(s): {cg_name}"
    lines = [f"New availability at {cg_name} for {w.name}",
             f"Your window: {w.check_in:%b %d} → {w.check_out:%b %d}, need {w.nights}+ night(s)", ""]
    for o in opens[:15]:
        loop = f" ({o.loop})" if o.loop else ""
        lines.append(f"• Site {o.site}{loop} — {o.site_type.title()}\n  {fmt_range(o)}\n  Book: {o.url}")
    if len(opens) > 15:
        lines.append(f"…and {len(opens) - 15} more.")
    lines += ["", f"Campground page: {cg_url}", "", "Book fast — these usually go within minutes."]
    body = "\n".join(lines)

    top = "; ".join(f"#{o.site} {o.start:%m/%d}-{o.end:%m/%d}" for o in opens[:3])
    more = f" +{len(opens) - 3} more" if len(opens) > 3 else ""
    short = f"Campsite open! {cg_name}: {top}{more}. {opens[0].url if len(opens) == 1 else cg_url}"
    return subject, body, short


def _secret(section: dict, key: str, env: str) -> str:
    return os.environ.get(env) or section.get(key, "")


def send_email(cfg: dict, subject: str, body: str) -> None:
    c = cfg["notify"]["email"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, c.get("from") or c["username"], ", ".join(c["to"])
    msg.set_content(body)
    host, port = c.get("smtp_host", "smtp.gmail.com"), int(c.get("smtp_port", 587))
    password = _secret(c, "password", "CAMPSITE_SMTP_PASSWORD").replace(" ", "")
    if not password:
        raise RuntimeError("no SMTP password set (config.toml [notify.email] password or CAMPSITE_SMTP_PASSWORD)")
    if "gmail.com" in host and len(password) != 16:
        raise RuntimeError(f"Gmail App Passwords are 16 letters, but the configured one has {len(password)}. "
                           "Re-copy it from https://myaccount.google.com/apppasswords")

    def _login(s):
        try:
            s.login(c["username"], password)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as e:
            raise RuntimeError(f"SMTP login rejected for {c['username']} ({e}). Check the username and "
                               "App Password (Gmail needs 2-Step Verification + an App Password).") from e

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            _login(s)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            _login(s)
            s.send_message(msg)


def send_sms(cfg: dict, text: str) -> None:
    c = cfg["notify"]["sms"]
    sid = _secret(c, "account_sid", "TWILIO_ACCOUNT_SID")
    token = _secret(c, "auth_token", "TWILIO_AUTH_TOKEN")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    for to in c["to"]:
        r = requests.post(url, auth=(sid, token), data={"From": c["from"], "To": to, "Body": text[:600]}, timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"Twilio error {r.status_code}: {r.text[:300]}")


def send_ntfy(cfg: dict, title: str, text: str, click: str) -> None:
    c = cfg["notify"]["ntfy"]
    server = c.get("server", "https://ntfy.sh").rstrip("/")
    title = title.replace("⛺", "").strip()
    if not title.isascii():  # HTTP headers must be ASCII; ntfy understands RFC 2047 encoding
        title = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode() + "?="
    headers = {"Title": title, "Priority": str(c.get("priority", "high")),
               "Tags": "tent", "Click": click}
    token = _secret(c, "token", "NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.post(f"{server}/{c['topic']}", data=text.encode("utf-8"), headers=headers, timeout=30)
    r.raise_for_status()


def notify(cfg: dict, subject: str, body: str, short: str, click: str, dry_run: bool = False) -> dict:
    """Send on every enabled channel. Returns {channel: None if sent, else error message}."""
    n = cfg.get("notify", {})
    channels = [k for k in ("email", "sms", "ntfy") if n.get(k, {}).get("enabled")]
    results: dict[str, str | None] = {}
    if dry_run:
        log.info("[dry-run] would notify via %s:\n%s", channels or "nothing", body)
        return results
    if not channels:
        log.warning("No notification channel enabled — printing only:\n%s", body)
        return results
    for ch in channels:
        try:
            if ch == "email":
                send_email(cfg, subject, body)
            elif ch == "sms":
                send_sms(cfg, short)
            elif ch == "ntfy":
                send_ntfy(cfg, subject, short, click)
            log.info("Sent %s notification", ch)
            results[ch] = None
        except Exception as e:  # one broken channel shouldn't block the others
            log.error("Failed to send %s notification: %s", ch, e)
            results[ch] = str(e)
    return results


# --------------------------------------------------------------------------- main loop

MIN_INTERVAL_S = 10       # hard floor, whatever the config says
MAX_BACKOFF_S = 600       # slowest we'll go while being rate-limited
WORKERS = 4               # parallel requests per scan


def active_watches(cfg: dict, today: date) -> list[Watch]:
    return [w for w in cfg["_watches"] if w.enabled and max(w.check_in, today) < w.check_out]


def watch_months(w: Watch, today: date) -> list[date]:
    return list(month_starts(max(w.check_in, today), w.check_out - timedelta(days=1)))


def scan(cfg: dict, state: State, names: dict, dry_run: bool, pool: ThreadPoolExecutor,
         on_fresh=None) -> dict:
    """One pass over every watch. Fetches each campground-month once, in parallel.
    Returns {"openings": n, "rate_limited": bool, "errors": n}."""
    today = date.today()
    watches = active_watches(cfg, today)
    jobs = sorted({(w.campground_id, m) for w in watches for m in watch_months(w, today)})
    futures = {job: pool.submit(fetch_month, *job) for job in jobs}

    results, rate_limited, errors = {}, False, 0
    for job, fut in futures.items():
        try:
            results[job] = fut.result()
        except RateLimited as e:
            rate_limited = True
            log.warning("Campground %s: rate-limited by Recreation.gov (%s)", job[0], e)
        except Exception as e:
            errors += 1
            log.error("Campground %s: could not fetch availability (%s)", job[0], e)

    total = 0
    for w in watches:
        months = watch_months(w, today)
        if any((w.campground_id, m) not in results for m in months):
            continue  # fetch failed this cycle; keep previous state, try again next cycle
        sites = merge_months([results[(w.campground_id, m)] for m in months])
        if not sites:
            log.warning("%s: campground %s returned no campsites — check the ID", w.name, w.campground_id)
            continue
        cg_name = names.get(w.campground_id) or f"Campground {w.campground_id}"

        opens = find_openings(sites, w, today)
        total += len(opens)
        fresh = state.new_openings(w.key, opens)
        for o in fresh:
            log.info("OPEN  %s (%s): %s — %s", w.name, cg_name, State.label(o), o.url)
        if fresh:
            subject, body, short = build_messages(w, cg_name, fresh)
            click = fresh[0].url if len(fresh) == 1 else CAMPGROUND_PAGE.format(cg=w.campground_id)
            notify(cfg, subject, body, short, click, dry_run)
            if on_fresh:
                try:
                    on_fresh(w, cg_name, fresh)
                except Exception:
                    log.exception("on_fresh callback failed")
        if not dry_run:
            for label, secs in state.update(w.key, opens):
                log.info("GONE  %s (%s): %s — was open for ~%.0f s", w.name, cg_name, label, secs)
    if not dry_run:
        state.save()
    return {"openings": total, "rate_limited": rate_limited, "errors": errors}


def interval_seconds(cfg: dict) -> float:
    if "check_interval_seconds" in cfg:
        s = float(cfg["check_interval_seconds"])
    else:
        s = float(cfg.get("check_interval_minutes", 5)) * 60
    if s < MIN_INTERVAL_S:
        log.warning("check interval %.0f s is below the %d s floor; using %d s", s, MIN_INTERVAL_S, MIN_INTERVAL_S)
        s = MIN_INTERVAL_S
    return s


def run_loop(cfg: dict, state: State, *, dry_run: bool = False, once: bool = False,
             stop: threading.Event | None = None, on_scan=None, on_fresh=None) -> None:
    """Scan forever (or once) until `stop` is set. Used by both the CLI and the desktop app.

    on_scan(result: dict, took_s: float, next_in_s: float | None) is called after every scan;
    on_fresh(watch, campground_name, openings) is called when new openings are found."""
    stop = stop or threading.Event()
    base = interval_seconds(cfg)
    cur = base
    warned_blocked = False
    pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="fetch")
    try:
        # Look up campground names once, in parallel.
        ids = sorted({w.campground_id for w in active_watches(cfg, date.today())})
        names = {}
        for cg, name in zip(ids, pool.map(lambda cg: campground_name(thread_session(), cg), ids)):
            names[cg] = name or f"Campground {cg}"

        log.info("Watching %d campground(s) every %.0f s.", len(ids), base)
        n, last_summary = 0, time.monotonic()
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                res = scan(cfg, state, names, dry_run, pool, on_fresh=on_fresh)
            except Exception:
                log.exception("Scan failed; will retry next cycle")
                res = {"openings": 0, "rate_limited": False, "errors": 1}
            n += 1
            took = time.monotonic() - t0

            if res["rate_limited"]:
                cur = min(cur * 2, MAX_BACKOFF_S)
                log.warning("Backing off: next scan in %.0f s", cur)
                if cur >= MAX_BACKOFF_S and not warned_blocked and not dry_run:
                    warned_blocked = True
                    notify(cfg, "⚠ Campsite Scanner is being rate-limited",
                           "Recreation.gov is refusing requests (HTTP 429/403). The scanner has slowed to "
                           f"one check every {MAX_BACKOFF_S // 60} min and will speed back up on its own. "
                           "If this persists, raise the check interval.",
                           "Campsite Scanner is being rate-limited by Recreation.gov; checks slowed to every 10 min.",
                           RECGOV)
            elif cur > base:
                cur = max(base, cur * 0.7)  # ease back toward the normal pace
                if cur == base:
                    log.info("Rate limit cleared; back to every %.0f s", base)
                    warned_blocked = False

            if once:
                log.info("Scan done: %d opening(s) in %.1f s", res["openings"], took)
                if on_scan:
                    on_scan(res, took, None)
                return
            # One summary line per ~5 min instead of per scan, so the log stays readable.
            if time.monotonic() - last_summary >= 300:
                log.info("Still watching: %d scans in last ~5 min, last scan %.1f s, %d current opening(s)",
                         n, took, res["openings"])
                n, last_summary = 0, time.monotonic()

            # Fixed-rate schedule: subtract the time the scan took, add ±20 % jitter.
            wait = max(1.0, cur * random.uniform(0.8, 1.2) - took)
            if on_scan:
                on_scan(res, took, wait)
            stop.wait(wait)  # returns early the moment stop is set
    finally:
        pool.shutdown(wait=False)


def cmd_run(args) -> None:
    cfg = load_config(args.config)
    if not cfg["_watches"]:
        raise SystemExit("No [[watch]] entries in config.toml — add at least one.")
    state = State(args.config.with_name("state.json"))
    log.info("Starting. Ctrl+C to stop.")
    run_loop(cfg, state, dry_run=args.dry_run, once=args.once)


def cmd_find(args) -> None:
    results = search_campgrounds(" ".join(args.query))
    if not results:
        print("No campgrounds found.")
        return
    print(f"{'ID':<10}  {'Campground':<45}  Park / area")
    for r in results:
        print(f"{r['id']:<10}  {r['name'][:45]:<45}  {r['park']} ({r['state']})")


def cmd_test_notify(args) -> None:
    cfg = load_config(args.config)
    notify(cfg, "⛺ Campsite Scanner test",
           "This is a test from Campsite Scanner. If you got this, alerts will reach you.",
           "Campsite Scanner test — alerts will reach you.", RECGOV)


def main() -> None:
    p = argparse.ArgumentParser(description="Watch Recreation.gov campgrounds for openings.")
    p.add_argument("--config", type=Path, default=HERE / "config.toml")
    p.add_argument("--once", action="store_true", help="scan once and exit (for Task Scheduler / cron)")
    p.add_argument("--dry-run", action="store_true", help="don't send notifications or update state")
    sub = p.add_subparsers(dest="cmd")
    f = sub.add_parser("find", help="search campground IDs by name")
    f.add_argument("query", nargs="+")
    sub.add_parser("test-notify", help="send a test notification on all enabled channels")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.handlers.RotatingFileHandler(HERE / "scanner.log", maxBytes=5_000_000,
                                                       backupCount=3, encoding="utf-8")])
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console + emoji
    except Exception:
        pass

    try:
        {"find": cmd_find, "test-notify": cmd_test_notify}.get(args.cmd, cmd_run)(args)
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
