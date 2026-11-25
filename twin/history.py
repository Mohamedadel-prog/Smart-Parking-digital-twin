"""
Bootstrap history: a brand-new lot has no data, so the twin synthesises HISTORY_DAYS of
hourly occupancy with the same stochastic model the simulator uses. Live data then
overwrites/extends it hour by hour (source='live').

Run standalone:  python -m twin.history  [--days 14] [--reset]
"""
import argparse
import random

import config
from common.model import LotModel
from twin.storage import Storage


def generate(storage: Storage, end_epoch: float, days: int, capacity: int, seed: int = 11) -> int:
    end_hour = int(end_epoch // 3600 * 3600)
    start = end_hour - days * 24 * 3600
    # warm-up one day so the first stored hour does not start from an empty lot
    model = LotModel(capacity, random.Random(seed))
    model.run(start - 24 * 3600, 24)
    rows = model.run(start, days * 24)
    for r in rows:
        storage.upsert_hour(r.hour_ts, r.avg_occupied, r.peak_occupied, r.entries, r.exits, r.rejected,
                            capacity, source="synthetic")
    return len(rows)


def ensure_history(storage: Storage) -> int:
    if storage.hourly_count() > 0:
        return 0
    return generate(storage, config.sim_start_epoch(), config.HISTORY_DAYS, config.CAPACITY)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=config.HISTORY_DAYS)
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    st = Storage(config.DB_PATH)
    if a.reset:
        with st.lock:
            st.conn.execute("DELETE FROM hourly")
            st.conn.execute("DELETE FROM events")
            st.conn.commit()
    n = generate(st, config.sim_start_epoch(), a.days, config.CAPACITY)
    print(f"generated {n} hourly rows")
