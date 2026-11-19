"""Tests for the desktop app. Config round-trip runs anywhere; the window test needs a display
(skipped automatically when there isn't one)."""
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campsite_scanner as core  # noqa: E402

try:
    import tkinter as tk
    import campsite_gui as gui
except ImportError:  # tkinter not installed
    gui = None


@unittest.skipIf(gui is None, "tkinter not available")
class ConfigFile(unittest.TestCase):
    def test_round_trip(self):
        cfg = gui.default_config()
        cfg["watch"].append({"name": 'Joshua Tree - "Jumbo" Rocks', "campground_id": "272300",
                             "check_in": date(2026, 11, 26), "check_out": date(2026, 11, 28), "nights": 2,
                             "site_types": ["TENT"], "sites": [], "enabled": False})
        cfg["notify"]["email"].update(enabled=True, to=["a@b.com", "c@d.com"], password="p\\w é")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.multiple(gui, APP_DIR=Path(d), CONFIG_PATH=Path(d) / "config.toml"):
            gui.save_app_config(cfg)
            back = gui.load_app_config()
        self.assertEqual(back["watch"], cfg["watch"])
        self.assertEqual(back["notify"], cfg["notify"])
        self.assertEqual(back["check_interval_seconds"], 45)
        core.parse_config(back)  # the scanner accepts what the app writes

    def test_parse_date_formats(self):
        self.assertEqual(gui.parse_date("2026-11-26"), date(2026, 11, 26))
        self.assertEqual(gui.parse_date("11/26/2026"), date(2026, 11, 26))
        with self.assertRaises(ValueError):
            gui.parse_date("next friday")


def _have_display():
    if gui is None:
        return False
    try:
        r = tk.Tk()
        r.destroy()
        return True
    except tk.TclError:
        return False


@unittest.skipUnless(_have_display(), "no display")
class Window(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patch = mock.patch.multiple(gui, APP_DIR=d, CONFIG_PATH=d / "config.toml",
                                         STATE_PATH=d / "state.json", LOG_PATH=d / "scanner.log")
        self.patch.start()
        self.dialogs = mock.patch.multiple(gui.messagebox, showinfo=mock.DEFAULT, showwarning=mock.DEFAULT,
                                           showerror=mock.DEFAULT, askyesno=mock.DEFAULT)
        self.mb = self.dialogs.start()
        self.mb["askyesno"].return_value = True
        self.app = gui.App()
        self.app.withdraw()

    def tearDown(self):
        try:
            if self.app.stop_event:
                self.app.stop_event.set()
            self.app.destroy()
        except tk.TclError:
            pass
        for h in list(gui.log.handlers):
            gui.log.removeHandler(h)
            h.close()
        self.dialogs.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def pump(self, seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not until():
            self.app.update()
            time.sleep(0.02)

    def test_add_dialog_search_and_ok(self):
        results = [{"id": "272300", "name": "JUMBO ROCKS CAMPGROUND", "park": "Joshua Tree National Park",
                    "state": "California"}]
        with mock.patch.object(core, "search_campgrounds", return_value=results):
            dlg = gui.WatchDialog(self.app)
            dlg.q.set("jumbo")
            dlg.search()
            self.pump(2, until=lambda: dlg.listbox.size() > 0)
            self.assertEqual(dlg.listbox.size(), 1)
            dlg.listbox.selection_set(0)
            dlg.pick()
            self.assertEqual(dlg.v["campground_id"].get(), "272300")
            self.assertEqual(dlg.v["name"].get(), "Joshua Tree National Park - Jumbo Rocks Campground")
            dlg.v["check_in"].set("11/26/2026")
            dlg.v["check_out"].set("2026-11-28")
            dlg.ok()
        self.assertEqual(dlg.result["nights"], 2)
        self.assertEqual(dlg.result["check_in"], date(2026, 11, 26))

    def test_dialog_rejects_bad_dates(self):
        dlg = gui.WatchDialog(self.app)
        dlg.v["campground_id"].set("1")
        dlg.v["check_in"].set("2026-11-28")
        dlg.v["check_out"].set("2026-11-26")
        dlg.ok()
        self.assertIsNone(dlg.result)
        self.mb["showerror"].assert_called()
        dlg.destroy()

    def test_start_find_opening_stop(self):
        start = date.today() + timedelta(days=10)
        self.app.cfg["watch"].append({"name": "Test CG", "campground_id": "272300", "check_in": start,
                                      "check_out": start + timedelta(days=2), "nights": 2, "enabled": True})
        self.app.refresh_watches()
        self.app.av["ntfy_on"].set(True)
        self.app.sv["interval"].set("10")

        def fake_fetch(cg, m):
            return {"42": {"campsite_id": "42", "site": "042", "campsite_type": "STANDARD",
                           "availabilities": {f"{(start + timedelta(days=i)).isoformat()}T00:00:00Z": "Available"
                                              for i in range(2)}}}

        with mock.patch.object(core, "fetch_month", side_effect=fake_fetch), \
                mock.patch.object(core, "campground_name", return_value="Jumbo Rocks"), \
                mock.patch.object(core, "send_ntfy") as ntfy, \
                mock.patch.object(gui.webbrowser, "open") as browser:
            self.app.start_scanning()
            self.assertTrue(self.app.running)
            self.pump(5, until=lambda: browser.called and "Last check" in self.app.status.cget("text"))
            browser.assert_called_once_with("https://www.recreation.gov/camping/campsites/42")
            ntfy.assert_called_once()
            self.assertIn("1 opening", self.app.status.cget("text"))
            self.assertIn("OPEN", self.app.logbox.get("1.0", "end"))
            self.app.stop_scanning()
            self.pump(5, until=lambda: not self.app.running and "Not running" in self.app.status.cget("text"))
        self.assertFalse(self.app.running)
        self.assertEqual(self.app.start_btn.cget("text"), "▶  Start scanning")
        saved = gui.load_app_config()
        self.assertEqual(saved["check_interval_seconds"], 10)
        self.assertTrue(saved["notify"]["ntfy"]["enabled"])
        self.assertTrue(gui.STATE_PATH.exists())

    def test_start_refuses_without_watches(self):
        self.app.start_scanning()
        self.assertFalse(self.app.running)
        self.mb["showinfo"].assert_called()

    def test_email_validation(self):
        self.app.cfg["watch"].append({"name": "x", "campground_id": "1", "check_in": date.today() + timedelta(days=3),
                                      "check_out": date.today() + timedelta(days=5), "enabled": True})
        self.app.av["email_on"].set(True)
        self.app.start_scanning()
        self.assertFalse(self.app.running)
        self.mb["showwarning"].assert_called()


if __name__ == "__main__":
    unittest.main()
