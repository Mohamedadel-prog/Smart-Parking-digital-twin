"""
Occupancy forecasting (hourly).

Methods
  ma        : moving average of the last W hours (flat line)            -> baseline
  seasonal  : seasonal moving average = mean of the same hour on the last
              N days of the same type (weekday / weekend)               -> captures daily pattern
  hybrid    : seasonal + exponentially-decaying level correction from the
              last 3 hours' residuals (default, best accuracy)
  prophet   : Facebook/Meta Prophet (optional: pip install prophet)

Also produces a rolling 1-hour-ahead backtest over the last 24 hours so the dashboard can
plot "forecast vs actual" and report MAE / RMSE / MAPE.
"""
import math
import time

from common.model import WEEKEND_DAYS

HOUR = 3600
DAY = 86400


def _is_weekend(ts):
    return time.localtime(ts).tm_wday in WEEKEND_DAYS


def seasonal_estimate(series: dict, target: int, days: int = 7):
    same, anyday = [], []
    for k in range(1, 15):
        v = series.get(target - k * DAY)
        if v is None:
            continue
        anyday.append(v)
        if _is_weekend(target - k * DAY) == _is_weekend(target):
            same.append(v)
    pool = same[:days] if same else anyday[:days]
    return sum(pool) / len(pool) if pool else None


def ma_estimate(series: dict, before: int, window: int = 3):
    vals = [series[before - k * HOUR] for k in range(1, window + 1) if (before - k * HOUR) in series]
    return sum(vals) / len(vals) if vals else None


def predict(series: dict, origin: int, horizon: int, method: str, capacity: int, partial: float | None = None):
    """Forecast hours origin, origin+1h, ... using only data strictly before `origin`
    (plus, for hybrid, the running average of the current incomplete hour if given)."""
    out = []
    if method == "ma":
        level = ma_estimate(series, origin)
        level = 0.0 if level is None else level
        return [min(capacity, max(0.0, level)) for _ in range(horizon)]

    # residual of the recent hours w.r.t. the seasonal profile (for hybrid)
    corr = 0.0
    if method == "hybrid":
        res = []
        for k in range(1, 4):
            h = origin - k * HOUR
            s = seasonal_estimate(series, h)
            if h in series and s is not None:
                res.append(series[h] - s)
        corr = sum(res) / len(res) if res else 0.0
        s_now = seasonal_estimate(series, origin)
        if partial is not None and s_now is not None:
            corr = 0.6 * (partial - s_now) + 0.4 * corr

    for i in range(horizon):
        target = origin + i * HOUR
        s = seasonal_estimate(series, target)
        if s is None:
            s = ma_estimate(series, origin) or 0.0
        v = s + corr * (0.7 ** i) if method == "hybrid" else s
        out.append(min(capacity, max(0.0, v)))
    return out


def _prophet_forecast(rows, origin, horizon, capacity):
    try:
        import pandas as pd
        from prophet import Prophet
    except Exception:
        return None
    df = pd.DataFrame({"ds": [pd.to_datetime(r["hour_ts"], unit="s") for r in rows],
                       "y": [r["avg_occupied"] for r in rows]})
    m = Prophet(daily_seasonality=True, weekly_seasonality=True, yearly_seasonality=False)
    m.fit(df)
    fut = pd.DataFrame({"ds": [pd.to_datetime(origin + i * HOUR, unit="s") for i in range(horizon)]})
    yhat = m.predict(fut)["yhat"].tolist()
    return [min(capacity, max(0.0, v)) for v in yhat]


def build_forecast(rows: list, now_ts: float, capacity: int, horizon: int = 12,
                   method: str = "hybrid", live_partial: float | None = None):
    series = {int(r["hour_ts"]): float(r["avg_occupied"]) for r in rows}
    origin = int(now_ts // HOUR * HOUR)          # current (incomplete) hour

    # --- rolling 1-step-ahead backtest over the last 24 completed hours
    bt_method = "seasonal" if method == "prophet" else method
    backtest, errs = [], []
    for k in range(24, 0, -1):
        h = origin - k * HOUR
        if h not in series:
            continue
        p = predict(series, h, 1, bt_method, capacity)[0]
        backtest.append({"ts": h, "predicted": round(p, 2)})
        errs.append(series[h] - p)

    mae = sum(abs(e) for e in errs) / len(errs) if errs else None
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs)) if errs else None
    mape = (sum(abs(e) for e in errs) / len(errs) / capacity * 100) if errs else None  # % of capacity
    std = math.sqrt(sum(e * e for e in errs) / len(errs)) if errs else 1.0

    # --- forward forecast from the current hour
    preds = None
    used = method
    if method == "prophet":
        preds = _prophet_forecast(rows, origin, horizon, capacity)
        if preds is None:
            used = "hybrid (prophet not installed)"
            preds = predict(series, origin, horizon, "hybrid", capacity)
    else:
        preds = predict(series, origin, horizon, method, capacity, live_partial)

    forecast = []
    for i, p in enumerate(preds):
        band = 1.28 * std * math.sqrt(1 + i * 0.25)
        forecast.append({"ts": origin + i * HOUR, "predicted": round(p, 2),
                         "lo": round(max(0.0, p - band), 2), "hi": round(min(capacity, p + band), 2)})

    history = [{"ts": h, "actual": round(series[h], 2)} for h in sorted(series) if origin - 24 * HOUR <= h < origin]
    if live_partial is not None:
        history.append({"ts": origin, "actual": round(live_partial, 2), "partial": True})

    peak = max(forecast, key=lambda f: f["predicted"]) if forecast else None
    full_soon = next((f for f in forecast[:3] if f["predicted"] >= 0.9 * capacity), None)
    return {
        "method": used, "capacity": capacity, "origin": origin,
        "history": history, "backtest": backtest, "forecast": forecast,
        "metrics": {"mae": None if mae is None else round(mae, 2),
                    "rmse": None if rmse is None else round(rmse, 2),
                    "mape_pct_capacity": None if mape is None else round(mape, 1),
                    "samples": len(errs)},
        "peak": None if peak is None else {"ts": peak["ts"], "value": peak["predicted"],
                                           "pct": round(100 * peak["predicted"] / capacity, 1)},
        "full_soon": full_soon,
    }
