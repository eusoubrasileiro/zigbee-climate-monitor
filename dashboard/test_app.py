"""Tests for the recorder and web API. See README.md for how to run them."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

_tmp = tempfile.TemporaryDirectory()
os.environ["DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ["SENSOR_TOPIC"] = "test/sensor"

import app  # noqa: E402


def message(payload):
    return SimpleNamespace(payload=payload)


def stored_rows():
    with closing(app.connect()) as db:
        return db.execute(
            "SELECT temperature, humidity, battery, linkquality FROM readings ORDER BY ts"
        ).fetchall()


class AppTestCase(unittest.TestCase):
    def setUp(self):
        with closing(app.connect()) as db, db:
            db.executescript(app.SCHEMA)
            db.execute("DELETE FROM readings")
        app.last_row = None


class ParseReadingTests(unittest.TestCase):
    def test_keeps_numeric_fields(self):
        payload = b'{"temperature":27.35,"humidity":70.65,"battery":65,"linkquality":188,"x":1}'
        self.assertEqual(app.parse_reading(payload), (27.35, 70.65, 65, 188))

    def test_accepts_a_partial_reading(self):
        self.assertEqual(app.parse_reading(b'{"humidity":50}'), (None, 50, None, None))

    def test_rejects_messages_that_are_not_readings(self):
        for payload in (b"not json", b"[1,2]", b"\xff\xfe", b'{"battery":50}',
                        b'{"temperature":"hot","humidity":true}'):
            with self.subTest(payload=payload):
                self.assertIsNone(app.parse_reading(payload))


class ParseHoursTests(unittest.TestCase):
    def test_bad_values_fall_back_to_the_default(self):
        for value in ("abc", "nan", "inf", "1e999"):
            with self.subTest(value=value):
                self.assertEqual(app.parse_hours({"hours": [value]}), app.DEFAULT_HOURS)

    def test_values_are_clamped(self):
        self.assertEqual(app.parse_hours({"hours": ["-5"]}), 0.25)
        self.assertEqual(app.parse_hours({"hours": ["99999999"]}), app.MAX_HOURS)
        self.assertEqual(app.parse_hours({}), app.DEFAULT_HOURS)


class RecorderTests(AppTestCase):
    def test_stores_a_reading(self):
        app.on_message(None, None, message(b'{"temperature":25.5,"humidity":60}'))
        self.assertEqual(stored_rows(), [(25.5, 60.0, None, None)])

    def test_drops_an_identical_repeat(self):
        for _ in range(2):
            app.on_message(None, None, message(b'{"temperature":25.5,"humidity":60}'))
        self.assertEqual(len(stored_rows()), 1)

    def test_stores_a_changed_reading(self):
        app.on_message(None, None, message(b'{"temperature":25.5,"humidity":60}'))
        app.on_message(None, None, message(b'{"temperature":25.6,"humidity":60}'))
        self.assertEqual(len(stored_rows()), 2)

    def test_ignores_junk(self):
        app.on_message(None, None, message(b"not json"))
        self.assertEqual(stored_rows(), [])


class QueryTests(AppTestCase):
    def insert(self, count):
        now = int(time.time() * 1000)
        with closing(app.connect()) as db, db:
            db.executemany(
                "INSERT INTO readings (ts, temperature, humidity) VALUES (?, ?, ?)",
                [(now - i * 60_000, 25.0, 60.0) for i in range(count)],
            )

    def test_short_ranges_return_every_reading(self):
        self.insert(10)
        data = app.query_readings(24, with_rows=False)
        self.assertEqual(data["count"], 10)
        self.assertEqual(len(data["points"]), 10)
        self.assertNotIn("rows", data)

    def test_long_ranges_are_averaged_down(self):
        self.insert(app.MAX_POINTS * 2)
        data = app.query_readings(24, with_rows=False)
        self.assertLessEqual(len(data["points"]), app.MAX_POINTS + 1)
        self.assertEqual(sum(point[3] for point in data["points"]), data["count"])

    def test_table_rows_are_newest_first_and_capped(self):
        self.insert(app.MAX_TABLE_ROWS + 5)
        rows = app.query_readings(24, with_rows=True)["rows"]
        self.assertEqual(len(rows), app.MAX_TABLE_ROWS)
        self.assertGreater(rows[0]["t"], rows[1]["t"])

    def test_empty_database(self):
        data = app.query_readings(24, with_rows=False)
        self.assertIsNone(data["latest"])
        self.assertEqual(data["points"], [])


class HttpTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def test_page_and_api(self):
        self.assertEqual(self.get("/").headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("points", json.load(self.get("/api/readings?hours=1")))

    def test_csv_has_a_header_and_a_utc_time(self):
        app.on_message(None, None, message(b'{"temperature":25.5,"humidity":60}'))
        lines = self.get("/readings.csv").read().decode().splitlines()
        self.assertTrue(lines[0].startswith("time_utc,temperature_c"))
        self.assertIn("+00:00,25.5,60.0", lines[1])

    def test_unknown_paths_and_writes_are_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/data/readings.db")
        self.assertEqual(caught.exception.code, 404)
        request = urllib.request.Request(self.base + "/api/readings", method="POST", data=b"")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 501)


if __name__ == "__main__":
    unittest.main()
