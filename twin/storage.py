"""SQLite persistence: event log (entries/exits/...) and hourly occupancy series."""
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,          -- simulated epoch seconds
    type      TEXT NOT NULL,          -- entry | exit | denied | rejected | slot_occupied | slot_freed | command | alert
    slot      TEXT,
    card      TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS hourly (
    hour_ts        INTEGER PRIMARY KEY,   -- start of the hour (epoch)
    avg_occupied   REAL NOT NULL,         -- time-weighted mean occupied slots
    peak_occupied  INTEGER NOT NULL,
    entries        INTEGER NOT NULL,
    exits          INTEGER NOT NULL,
    rejected       INTEGER NOT NULL,
    capacity       INTEGER NOT NULL,
    source         TEXT NOT NULL          -- 'synthetic' (bootstrap history) | 'live'
);
"""


class Storage:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------- events
    def add_event(self, ts, etype, slot=None, card=None, detail=None):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO events(ts,type,slot,card,detail) VALUES (?,?,?,?,?)",
                (ts, etype, slot, card, detail))
            self.conn.commit()
            return cur.lastrowid

    def events(self, limit=50, etype=None, since=None):
        q, args = "SELECT * FROM events WHERE 1=1", []
        if etype:
            q += " AND type=?"
            args.append(etype)
        if since is not None:
            q += " AND ts>=?"
            args.append(since)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [dict(r) for r in self.conn.execute(q, args)]

    def count_events(self, etype, since, until=None):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM events WHERE type=? AND ts>=? AND ts<=?",
                                     (etype, since, until if until is not None else 9e18)).fetchone()[0]

    # ------------------------------------------------------------- hourly
    def upsert_hour(self, hour_ts, avg, peak, entries, exits, rejected, capacity, source="live"):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO hourly VALUES (?,?,?,?,?,?,?,?)",
                (int(hour_ts), float(avg), int(peak), int(entries), int(exits), int(rejected), int(capacity), source))
            self.conn.commit()

    def hourly(self, since=None, until=None):
        q, args = "SELECT * FROM hourly WHERE 1=1", []
        if since is not None:
            q += " AND hour_ts>=?"
            args.append(int(since))
        if until is not None:
            q += " AND hour_ts<?"
            args.append(int(until))
        q += " ORDER BY hour_ts"
        with self.lock:
            return [dict(r) for r in self.conn.execute(q, args)]

    def hourly_count(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM hourly").fetchone()[0]
