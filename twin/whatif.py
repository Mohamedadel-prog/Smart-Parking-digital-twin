"""
What-if simulation: Monte-Carlo runs of the demand model started from the twin's *current*
state (occupied slots, time of day), comparing a baseline against a user scenario.

Scenario knobs
  demand       : multiplier on arrivals       (e.g. 1.5 = event day, +50 %)
  closed_slots : slots taken out of service   (maintenance)
  stay_mult    : multiplier on parking time   (e.g. 0.7 = enforced 2-hour limit)
  hours        : horizon
"""
import random
import statistics

from common.model import LotModel, sample_stay_min


def _runs(now_ts, occupied, capacity, demand, stay_mult, hours, n_runs, seed):
    hour_start = int(now_ts // 3600 * 3600)
    per_hour = [[] for _ in range(hours)]
    rejected, full_hours = [], []
    for r in range(n_runs):
        rng = random.Random(seed + r)
        m = LotModel(capacity, rng, demand=demand, stay_mult=stay_mult)
        # cars already parked: remaining time ~ uniform fraction of a typical stay
        for _ in range(min(occupied, capacity)):
            m.parked.append(now_ts + rng.random() * sample_stay_min(now_ts, rng) * 60)
        rows = m.run(hour_start, hours)
        for i, row in enumerate(rows):
            per_hour[i].append(row.avg_occupied)
        rejected.append(sum(row.rejected for row in rows))
        full_hours.append(sum(1 for row in rows if row.avg_occupied >= capacity - 0.5))

    def q(vals, p):
        s = sorted(vals)
        return s[min(len(s) - 1, int(p * len(s)))]

    curve = [{"ts": hour_start + i * 3600,
              "mean": round(statistics.mean(v), 2), "p10": round(q(v, 0.1), 2), "p90": round(q(v, 0.9), 2),
              "pct": round(100 * statistics.mean(v) / capacity, 1) if capacity else 0}
             for i, v in enumerate(per_hour)]
    return {
        "capacity": capacity,
        "curve": curve,
        "expected_rejected": round(statistics.mean(rejected), 1),
        "expected_full_hours": round(statistics.mean(full_hours), 1),
        "peak_pct": max((c["pct"] for c in curve), default=0),
    }


def run_whatif(now_ts, occupied, capacity, demand=1.0, closed_slots=0, stay_mult=1.0, hours=12, n_runs=40):
    hours = max(1, min(48, int(hours)))
    seed = int(now_ts) % 100000
    baseline = _runs(now_ts, occupied, capacity, 1.0, 1.0, hours, n_runs, seed)
    cap2 = max(1, capacity - int(closed_slots))
    scenario = _runs(now_ts, min(occupied, cap2), cap2, demand, stay_mult, hours, n_runs, seed)
    return {
        "params": {"demand": demand, "closed_slots": int(closed_slots), "stay_mult": stay_mult,
                   "hours": hours, "runs": n_runs},
        "baseline": baseline,
        "scenario": scenario,
        "delta": {
            "rejected": round(scenario["expected_rejected"] - baseline["expected_rejected"], 1),
            "full_hours": round(scenario["expected_full_hours"] - baseline["expected_full_hours"], 1),
        },
    }
