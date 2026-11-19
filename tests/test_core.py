"""Tests for campsite_scanner.py (no network: Recreation.gov calls are mocked)."""
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campsite_scanner as cs  # noqa: E402

A, R = "Available", "Reserved"
TODAY = date(2026, 11, 14)


def site(sid, num, statuses, start, typ="STANDARD NONELECTRIC"):
    return {"campsite_id": sid, "site": num, "loop": "Loop A", "campsite_type": typ,
            "availabilities": {f"{(start + timedelta(days=i)).isoformat()}T00:00:00Z": s
                               for i, s in enumerate(statuses)}}


D0 = date(2026, 12, 18)
SITES = {
    "1": site("1", "001", [A, A, A, A, A], D0),
    "2": site("2", "002", [A, R, A, R, A], D0),
    "3": site("3", "003", [R, A, A, R, A], D0),
    "4": site("4", "004", [R, R, R, A, A], D0, typ="TENT ONLY NONELECTRIC"),
    "5": {"campsite_id": "5", "site": "C1", "availabilities": {}},
}


class FindOpenings(unittest.TestCase):
    def runs(self, w, today=TODAY):
        return [(o.site, o.start.isoformat(), o.end.isoformat()) for o in cs.find_openings(SITES, w, today)]

    def test_flexible_runs(self):
        w = cs.Watch("t", "1", D0, D0 + timedelta(days=5), nights=2)
        self.assertEqual(self.runs(w), [("001", "2026-12-18", "2026-12-23"),
                                        ("003", "2026-12-19", "2026-12-21"),
                                        ("004", "2026-12-21", "2026-12-23")])

    def test_whole_stay(self):
        w = cs.Watch("t", "1", D0, D0 + timedelta(days=5), nights=5)
        self.assertEqual([r[0] for r in self.runs(w)], ["001"])

    def test_filters(self):
        w = cs.Watch("t", "1", D0, D0 + timedelta(days=5), nights=2, site_types=["TENT"])
        self.assertEqual([r[0] for r in self.runs(w)], ["004"])
        w = cs.Watch("t", "1", D0, D0 + timedelta(days=5), nights=2, sites=["003"])
        self.assertEqual([r[0] for r in self.runs(w)], ["003"])

    def test_today_inside_window(self):
        w = cs.Watch("t", "1", D0, D0 + timedelta(days=5), nights=2)
        self.assertEqual([(r[0], r[1]) for r in self.runs(w, today=date(2026, 12, 20))],
                         [("001", "2026-12-20"), ("004", "2026-12-21")])


class Fetching(unittest.TestCase):
    def test_month_starts_across_year(self):
        self.assertEqual(list(cs.month_starts(date(2026, 12, 30), date(2027, 1, 2))),
                         [date(2026, 12, 1), date(2027, 1, 1)])

    def test_merge_months(self):
        dec = {"9": site("9", "009", [A, A], date(2026, 12, 30))}
        jan = {"9": site("9", "009", [A, A], date(2027, 1, 1))}
        merged = cs.merge_months([dec, jan])
        self.assertEqual(len(merged["9"]["availabilities"]), 4)

    def test_rate_limit_raises(self):
        fake = mock.Mock()
        fake.get.return_value = mock.Mock(status_code=429)
        with mock.patch.object(cs, "thread_session", return_value=fake):
            with self.assertRaises(cs.RateLimited):
                cs.fetch_month("1", date(2026, 11, 1))

    def test_search_campgrounds(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"results": [
            {"entity_id": "272300", "name": "JUMBO ROCKS CAMPGROUND", "parent_name": "Joshua Tree National Park",
             "state_code": "California"}, {"name": "no id"}]}
        fake = mock.Mock()
        fake.get.return_value = resp
        with mock.patch.object(cs, "thread_session", return_value=fake):
            res = cs.search_campgrounds("jumbo")
        self.assertEqual(res, [{"id": "272300", "name": "JUMBO ROCKS CAMPGROUND",
                                "park": "Joshua Tree National Park", "state": "California"}])


class Config(unittest.TestCase):
    def test_parse_ok(self):
        cfg = cs.parse_config({"watch": [{"campground_id": 1, "check_in": "2026-11-26",
                                          "check_out": date(2026, 11, 28)}]})
        w = cfg["_watches"][0]
        self.assertEqual((w.campground_id, w.nights), ("1", 2))

    def test_parse_errors(self):
        bad = [{"check_in": "2026-11-26", "check_out": "2026-11-28"},
               {"campground_id": "1", "check_in": "2026-11-28", "check_out": "2026-11-26"},
               {"campground_id": "1", "check_in": "2026-11-26", "check_out": "2026-11-28", "nights": 3},
               {"campground_id": "1", "check_in": "nope", "check_out": "2026-11-28"}]
        for w in bad:
            with self.assertRaises(cs.ConfigError):
                cs.parse_config({"watch": [w]})

    def test_example_config_loads(self):
        cfg = cs.load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.assertTrue(cfg["_watches"])
        self.assertEqual(cs.interval_seconds(cfg), 45)

    def test_interval_floor(self):
        self.assertEqual(cs.interval_seconds({"check_interval_seconds": 3}), cs.MIN_INTERVAL_S)
        self.assertEqual(cs.interval_seconds({"check_interval_minutes": 3}), 180)


class StateAndScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pool = ThreadPoolExecutor(4)

    def tearDown(self):
        self.pool.shutdown()
        self.tmp.cleanup()

    def test_old_state_format_migrates(self):
        p = self.dir / "s.json"
        p.write_text(json.dumps({"k": ["5|2026-11-26|2026-11-28"]}))
        self.assertIn("5|2026-11-26|2026-11-28", cs.State(p).data["k"])

    def test_scan_alerts_once_then_logs_gone(self):
        cfg = cs.parse_config({"watch": [
            {"name": "JT", "campground_id": "272300", "check_in": "2026-11-26", "check_out": "2026-11-28"},
            {"name": "JT2", "campground_id": "272300", "check_in": "2026-11-20", "check_out": "2026-11-30",
             "nights": 2},
            {"name": "Other", "campground_id": "232472", "check_in": "2026-11-26", "check_out": "2026-11-28"}]})
        is_open = {"v": True}
        calls = []

        def fake_fetch(cg, m):
            calls.append((cg, m))
            time.sleep(0.2)
            if cg == "272300" and is_open["v"]:
                return {"42": site("42", "042", [A, A], date(2026, 11, 26))}
            return {"1": site("1", "001", [R, R], date(2026, 11, 26))}

        notes, fresh = [], []
        state = cs.State(self.dir / "state.json")
        with mock.patch.object(cs, "fetch_month", side_effect=fake_fetch), \
                mock.patch.object(cs, "notify", side_effect=lambda *a, **k: notes.append(a[1])), \
                mock.patch.object(cs, "date") as md:
            md.today.return_value = TODAY
            t = time.monotonic()
            r = cs.scan(cfg, state, {}, False, self.pool, on_fresh=lambda w, n, o: fresh.append(w.name))
            self.assertLess(time.monotonic() - t, 0.6)          # parallel, not 2 × 0.2 s sequential + extra
            self.assertEqual(len(calls), 2)                      # same campground-month fetched once
            self.assertEqual(r["openings"], 2)                   # both JT watches match
            self.assertEqual(len(notes), 2)
            self.assertEqual(sorted(fresh), ["JT", "JT2"])
            cs.scan(cfg, state, {}, False, self.pool)
            self.assertEqual(len(notes), 2)                      # no repeat alert
            is_open["v"] = False
            with self.assertLogs("campsite_scanner", "INFO") as logs:
                cs.scan(cfg, state, {}, False, self.pool)
            self.assertTrue(any("GONE" in line for line in logs.output))

    def test_rate_limited_flag(self):
        cfg = cs.parse_config({"watch": [{"campground_id": "1", "check_in": "2026-11-26",
                                          "check_out": "2026-11-28"}]})
        with mock.patch.object(cs, "fetch_month", side_effect=cs.RateLimited("HTTP 429")), \
                mock.patch.object(cs, "date") as md:
            md.today.return_value = TODAY
            self.assertTrue(cs.scan(cfg, cs.State(self.dir / "s.json"), {}, True, self.pool)["rate_limited"])


class RunLoop(unittest.TestCase):
    def cfg(self):
        return cs.parse_config({"check_interval_seconds": 10, "notify": {},
                                "watch": [{"campground_id": "1", "check_in": "2026-11-26",
                                           "check_out": "2026-11-28"}]})

    def test_stop_event_ends_loop_quickly(self):
        stop = threading.Event()
        scans = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(cs, "scan", return_value={"openings": 0, "rate_limited": False, "errors": 0}), \
                mock.patch.object(cs, "campground_name", return_value="X"):
            t = threading.Thread(target=cs.run_loop, args=(self.cfg(), cs.State(Path(d) / "s.json")),
                                 kwargs={"stop": stop, "on_scan": lambda *a: scans.append(a)})
            t.start()
            time.sleep(0.5)
            stop.set()
            t.join(timeout=3)
            self.assertFalse(t.is_alive())
            self.assertEqual(len(scans), 1)
            self.assertGreater(scans[0][2], 1)  # next_in reported

    def test_backoff_and_recovery(self):
        seq = [{"openings": 0, "rate_limited": True, "errors": 0}] * 6 + \
              [{"openings": 0, "rate_limited": False, "errors": 0}] * 3
        waits, warned = [], []
        stop = threading.Event()

        class FakeEvent:
            def is_set(self):
                return stop.is_set()

            def wait(self, s):
                waits.append(s)
                if len(waits) == len(seq):
                    stop.set()

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(cs, "scan", side_effect=seq), \
                mock.patch.object(cs, "campground_name", return_value="X"), \
                mock.patch.object(cs.random, "uniform", return_value=1.0), \
                mock.patch.object(cs, "notify", side_effect=lambda *a, **k: warned.append(a[1])):
            cs.run_loop(self.cfg(), cs.State(Path(d) / "s.json"), stop=FakeEvent())
        self.assertEqual([round(w) for w in waits[:6]], [20, 40, 80, 160, 320, 600])
        self.assertLess(waits[6], 600)
        self.assertEqual(len(warned), 1)


class Notify(unittest.TestCase):
    def test_results_per_channel(self):
        cfg = {"notify": {"email": {"enabled": True, "to": ["a@x.com"], "username": "me@gmail.com",
                                    "password": "abcdabcdabcdabcd"},
                          "ntfy": {"enabled": True, "topic": "abc"}}}
        with mock.patch.object(cs.smtplib, "SMTP") as smtp, mock.patch.object(cs.requests, "post") as post:
            post.return_value = mock.Mock(status_code=200)
            smtp.return_value.__enter__.return_value.send_message.side_effect = RuntimeError("boom")
            res = cs.notify(cfg, "s", "b", "short", "https://x")
        self.assertIsNone(res["ntfy"])
        self.assertIn("boom", res["email"])


if __name__ == "__main__":
    unittest.main()
