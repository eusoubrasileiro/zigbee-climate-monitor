#!/usr/bin/env python3
"""Record one Zigbee2MQTT sensor to SQLite and serve a read-only chart page."""
import csv
import io
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import paho.mqtt.client as mqtt

DB_PATH = os.environ.get("DB_PATH", "/data/readings.db")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
SENSOR_TOPIC = os.environ["SENSOR_TOPIC"]
SENSOR_NAME = os.environ.get("SENSOR_NAME", "Sensor")
HTTP_PORT = 8090
DEFAULT_HOURS = 24.0
MAX_HOURS = 24.0 * 366
MAX_POINTS = 600
MAX_TABLE_ROWS = 200
FIELDS = ("temperature", "humidity", "battery", "linkquality")
FIELD_COLUMNS = ", ".join(FIELDS)
COLUMNS = f"ts, {FIELD_COLUMNS}"
INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts INTEGER NOT NULL,  -- Unix milliseconds, UTC
    temperature REAL,
    humidity REAL,
    battery REAL,
    linkquality INTEGER
);
CREATE INDEX IF NOT EXISTS readings_ts ON readings (ts);
"""

log = logging.getLogger("dashboard")
mqtt_connected = False
last_row = None


def connect():
    return sqlite3.connect(DB_PATH, timeout=10)


def parse_reading(payload):
    """Return a row of FIELDS, or None if the message has no temperature or humidity."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    row = tuple(data[f] if type(data.get(f)) in (int, float) else None for f in FIELDS)
    return row if row[0] is not None or row[1] is not None else None


def on_connect(client, _userdata, _flags, reason_code, _properties):
    global mqtt_connected
    mqtt_connected = not reason_code.is_failure
    if mqtt_connected:
        client.subscribe(SENSOR_TOPIC)
        log.info("subscribed to %s", SENSOR_TOPIC)
    else:
        log.warning("broker refused connection: %s", reason_code)


def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
    global mqtt_connected
    mqtt_connected = False
    log.warning("disconnected from broker: %s", reason_code)


def on_message(_client, _userdata, msg):
    global last_row
    row = parse_reading(msg.payload)
    # Zigbee2MQTT republishes its cached state on restart; that is not a new reading.
    if row is None or row == last_row:
        return
    try:
        with closing(connect()) as db, db:
            db.execute(
                f"INSERT INTO readings ({COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                (int(time.time() * 1000),) + row,
            )
        last_row = row
    except sqlite3.Error:
        log.exception("could not store reading")


def row_to_dict(row):
    return dict(zip(("t",) + FIELDS, row))


def query_readings(hours, with_rows):
    now = int(time.time() * 1000)
    since = now - int(hours * 3_600_000)
    with closing(connect()) as db:
        count = db.execute("SELECT COUNT(*) FROM readings WHERE ts >= ?", (since,)).fetchone()[0]
        if count <= MAX_POINTS:
            points = db.execute(
                "SELECT ts, temperature, humidity, 1 FROM readings WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        else:
            bucket = math.ceil((now - since) / MAX_POINTS)
            points = db.execute(
                "SELECT CAST(AVG(ts) AS INTEGER), ROUND(AVG(temperature), 2),"
                " ROUND(AVG(humidity), 2), COUNT(*) FROM readings WHERE ts >= ?"
                " GROUP BY ts / ? ORDER BY 1",
                (since, bucket),
            ).fetchall()
        latest = db.execute(f"SELECT {COLUMNS} FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
        result = {
            "name": SENSOR_NAME,
            "now": now,
            "mqtt_connected": mqtt_connected,
            "latest": row_to_dict(latest) if latest else None,
            "count": count,
            "points": points,
        }
        if with_rows:
            rows = db.execute(
                f"SELECT {COLUMNS} FROM readings WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (since, MAX_TABLE_ROWS),
            ).fetchall()
            result["rows"] = [row_to_dict(r) for r in rows]
    return result


def readings_csv():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(("time_utc", "temperature_c", "humidity_pct", "battery_pct", "linkquality"))
    with closing(connect()) as db:
        for ts, *values in db.execute(f"SELECT {COLUMNS} FROM readings ORDER BY ts"):
            stamp = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(timespec="seconds")
            writer.writerow((stamp, *values))
    return out.getvalue().encode()


def parse_hours(query):
    try:
        hours = float(query.get("hours", [DEFAULT_HOURS])[0])
    except ValueError:
        return DEFAULT_HOURS
    return min(max(hours, 0.25), MAX_HOURS) if math.isfinite(hours) else DEFAULT_HOURS


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.reply(INDEX_HTML, "text/html; charset=utf-8")
        elif url.path == "/api/readings":
            query = parse_qs(url.query)
            data = query_readings(parse_hours(query), "table" in query)
            self.reply(json.dumps(data, separators=(",", ":")).encode(), "application/json")
        elif url.path == "/readings.csv":
            self.reply(readings_csv(), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="readings.csv"'})
        else:
            self.reply(b"Not found", "text/plain", status=404)

    def reply(self, body, content_type, headers=None, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # The page polls every 5 s; per-request logs would only be noise.


def main():
    global last_row
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with closing(connect()) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        last = db.execute(
            f"SELECT {FIELD_COLUMNS} FROM readings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        last_row = tuple(last) if last else None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sensor-dashboard")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()

    log.info("serving on port %d", HTTP_PORT)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
