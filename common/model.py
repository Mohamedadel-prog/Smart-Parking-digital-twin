"""
Stochastic demand model of the parking lot.

Shared by:
  * simulator/physical_sim.py  -> generates the cars that drive into the virtual lot
  * twin/history.py            -> synthesises past days so the forecaster has data on day 1
  * twin/whatif.py             -> Monte-Carlo "what-if" runs started from the twin's live state
"""
import math
import random
import time
from dataclasses import dataclass, field

# Mean arrivals per hour for a 12-slot lot (index = hour of day). Morning commuter peak,
# lunch bump, evening shopping bump.
HOURLY_ARRIVALS = [
    0.2, 0.1, 0.1, 0.1, 0.2, 0.6,   # 00-05
    2.0, 5.0, 6.0, 4.5, 3.0, 3.0,   # 06-11
    3.8, 3.5, 2.8, 2.8, 3.2, 4.2,   # 12-17
    4.0, 3.0, 2.0, 1.2, 0.6, 0.3,   # 18-23
]
# Egyptian weekend = Friday (4) and Saturday (5)
WEEKEND_DAYS = {4, 5}
WEEKEND_FACTOR = 0.55


def hour_and_weekday(epoch: float):
    t = time.localtime(epoch)
    return t.tm_hour, t.tm_wday


def arrival_rate(epoch: float, capacity: int = 12, demand: float = 1.0) -> float:
    """Expected cars per hour at a given (simulated) time."""
    hour, wday = hour_and_weekday(epoch)
    rate = HOURLY_ARRIVALS[hour] * capacity / 12.0
    if wday in WEEKEND_DAYS:
        rate *= WEEKEND_FACTOR
    return rate * demand


def sample_stay_min(epoch: float, rng: random.Random, stay_mult: float = 1.0) -> float:
    """Parking duration in minutes (log-normal, depends on arrival hour)."""
    hour, _ = hour_and_weekday(epoch)
    if 6 <= hour < 10:
        mean = 240      # commuters
    elif 10 <= hour < 15:
        mean = 100      # errands / lunch
    elif 15 <= hour < 21:
        mean = 110      # shopping
    else:
        mean = 60
    sigma = 0.5
    mu = math.log(mean) - sigma ** 2 / 2
    return max(10.0, rng.lognormvariate(mu, sigma)) * stay_mult


def sample_patience_min(rng: random.Random) -> float:
    """How long a driver waits at a full gate before giving up."""
    return rng.uniform(3, 12)


def poisson(lam: float, rng: random.Random) -> int:
    if lam <= 0:
        return 0
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


@dataclass
class HourRow:
    hour_ts: int
    avg_occupied: float
    peak_occupied: int
    entries: int
    exits: int
    rejected: int


@dataclass
class LotModel:
    """Aggregate minute-step model (no geometry) used for history and what-if."""
    capacity: int
    rng: random.Random
    demand: float = 1.0
    stay_mult: float = 1.0
    parked: list = field(default_factory=list)   # departure epochs
    queue: list = field(default_factory=list)    # give-up epochs

    def step(self, t: float, dt_s: float = 60.0):
        entries = exits = rejected = 0
        # departures
        still = [d for d in self.parked if d > t]
        exits = len(self.parked) - len(still)
        self.parked = still
        # arrivals join the gate queue
        n = poisson(arrival_rate(t, self.capacity, self.demand) * dt_s / 3600.0, self.rng)
        for _ in range(n):
            self.queue.append(t + sample_patience_min(self.rng) * 60)
        # admit while there is room
        while self.queue and len(self.parked) < self.capacity:
            self.queue.pop(0)
            self.parked.append(t + sample_stay_min(t, self.rng, self.stay_mult) * 60)
            entries += 1
        # impatient drivers leave
        keep = [g for g in self.queue if g > t]
        rejected = len(self.queue) - len(keep)
        self.queue = keep
        return len(self.parked), entries, exits, rejected

    def run(self, start: float, hours: int, dt_s: float = 60.0):
        """Simulate `hours` from `start` (start should be on an hour boundary). Returns HourRow list."""
        rows = []
        t = start
        steps_per_hour = int(3600 / dt_s)
        for h in range(hours):
            occ_sum, peak, en, ex, rj = 0.0, 0, 0, 0, 0
            for _ in range(steps_per_hour):
                occ, e, x, r = self.step(t, dt_s)
                occ_sum += occ
                peak = max(peak, occ)
                en, ex, rj = en + e, ex + x, rj + r
                t += dt_s
            rows.append(HourRow(int(start + h * 3600), occ_sum / steps_per_hour, peak, en, ex, rj))
        return rows
