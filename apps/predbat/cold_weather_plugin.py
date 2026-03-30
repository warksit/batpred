# -----------------------------------------------------------------------------
# Cold Weather Plugin for Predbat
# Predicts morning GSHP heating load from weather data and adjusts
# best_soc_keep to ensure sufficient battery for the overnight/morning period.
#
# Self-improving: fits a 3-variable linear regression model from historical
# GSHP consumption vs temperature, wind speed, and overnight temperature drop.
# Pre-populate cold_weather_history.json for instant model training,
# or let the plugin cold-start with a conservative default and learn over ~7 days.
# -----------------------------------------------------------------------------

import json
import os

import requests

from plugin_system import PredBatPlugin

# HA entity IDs
HA_GSHP_HEATING = "input_boolean.cold_weather_gshp_heating"
HA_GSHP_ENERGY = "sensor.heat_pump_energy_meter_energy"
HA_WEATHER = "weather.forecast_home"

# Open-Meteo API for historical hourly weather
OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"

# Time windows for feature extraction (hours relative to midnight of heating day)
# These were determined by exhaustive sweep over 23 nights of data (R²=0.71)
TEMP_WINDOW = (-4, 3)  # 20:00 to 03:00 — heat loss after heating off
WIND_WINDOW = (-12, 3)  # 12:00 to 03:00 — infiltration while heating on
DROP_EVENING = (-2, 0)  # 22:00 to 00:00 — evening reference
DROP_DAWN = (4, 7)  # 04:00 to 07:00 — dawn reference

# GSHP measurement window
GSHP_START_HOUR = 4  # 04:00
GSHP_END_HOUR = 8  # 08:00
CHEAP_RATE_END_HOUR = 7  # 07:00 — only inject alert_keep during cheap rate

HISTORY_FILE = "cold_weather_history.json"
MARGIN_KWH = 0.5  # Safety margin added to prediction
DEFAULT_KEEP_KWH = 6.0  # Conservative default before model is trained
MIN_TRAINING_DAYS = 7  # Minimum history for model to be used
BOOTSTRAP_DAYS = 60  # Days of history to fetch on first run
MIN_KEEP_KWH = 3.0  # Don't inject alert_keep below this threshold


class ColdWeatherPlugin(PredBatPlugin):
    """
    Predicts morning GSHP heating load and adjusts best_soc_keep.

    Model: heat_kWh = b0 + b1*avg_temp + b2*avg_wind + b3*temp_drop
    - avg_temp: outdoor temp 20:00-03:00 (heat loss after heating off)
    - avg_wind: wind speed 12:00-03:00 (infiltration losses)
    - temp_drop: evening temp minus dawn temp (overnight cooling)

    Self-improving: records actual consumption daily, refits model.
    Bootstrap: queries Open-Meteo + HA statistics on first run.
    """

    priority = 50  # Run before curtailment_plugin (priority 200)

    def __init__(self, base):
        super().__init__(base)
        self.history = []
        self.coefficients = None  # (b0, b1, b2, b3) or None
        self.model_r2 = 0.0
        self._prediction = None  # Cached prediction
        self._recorded_today = False
        self._last_record_date = None
        self._gshp_energy_at_start = None
        self._morning_min_soc = 999.0  # Track lowest SOC 04:00-08:00
        self._keep_pct = None  # Last injected alert_keep start %
        self._keep_kwh = None  # Last injected keep kWh
        self._keep_weight = None  # Last applied soc_keep weight
        self._history_path = os.path.join(getattr(base, "config_root", "./"), HISTORY_FILE)
        self._load_history()

    def register_hooks(self, plugin_system):
        plugin_system.register_hook("on_before_plan", self.on_before_plan, plugin=self)
        plugin_system.register_hook("on_update", self.on_update, plugin=self)

    # =========================================================================
    # History persistence
    # =========================================================================

    def _load_history(self):
        """Load history from JSON file."""
        try:
            if os.path.exists(self._history_path):
                with open(self._history_path) as f:
                    self.history = json.load(f)
                self.log("Cold weather: loaded {} history entries".format(len(self.history)))
                if len(self.history) >= MIN_TRAINING_DAYS:
                    self._fit_model()
        except Exception as e:
            self.log("Cold weather: failed to load history: {}".format(e))
            self.history = []

    def _save_history(self):
        """Save history to JSON file."""
        try:
            with open(self._history_path, "w") as f:
                json.dump(self.history, f, indent=2)
        except Exception as e:
            self.log("Cold weather: failed to save history: {}".format(e))

    # =========================================================================
    # Linear regression (pure Python, no numpy)
    # =========================================================================

    def _fit_model(self):
        """Fit 3-variable linear regression via normal equations."""
        n = len(self.history)
        if n < MIN_TRAINING_DAYS:
            self.coefficients = None
            return

        # Extract arrays
        y = [h["heat_kwh"] for h in self.history]
        x1 = [h["avg_temp"] for h in self.history]
        x2 = [h["avg_wind"] for h in self.history]
        x3 = [h["t_drop"] for h in self.history]

        # Normal equations: (X^T X) b = X^T y
        # X = [1, x1, x2, x3] for each row
        # Build X^T X (4x4) and X^T y (4x1)
        xtx = [[0.0] * 4 for _ in range(4)]
        xty = [0.0] * 4
        cols = [
            [1.0] * n,
            x1,
            x2,
            x3,
        ]

        for i in range(4):
            for j in range(4):
                xtx[i][j] = sum(cols[i][k] * cols[j][k] for k in range(n))
            xty[i] = sum(cols[i][k] * y[k] for k in range(n))

        # Solve via Gaussian elimination
        b = self._solve_4x4(xtx, xty)
        if b is None:
            self.coefficients = None
            return

        self.coefficients = tuple(b)

        # Compute R²
        y_mean = sum(y) / n
        ss_tot = sum((yi - y_mean) ** 2 for yi in y)
        ss_res = sum((y[k] - (b[0] + b[1] * x1[k] + b[2] * x2[k] + b[3] * x3[k])) ** 2 for k in range(n))
        self.model_r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        self.log("Cold weather: model fitted (n={}, R²={:.3f}) " "b0={:.2f} temp={:+.4f} wind={:+.4f} drop={:+.4f}".format(n, self.model_r2, *self.coefficients))

    @staticmethod
    def _solve_4x4(a, b):
        """Solve 4x4 linear system via Gaussian elimination with partial pivoting."""
        n = 4
        # Augmented matrix
        m = [a[i][:] + [b[i]] for i in range(n)]

        for col in range(n):
            # Partial pivoting
            max_row = col
            for row in range(col + 1, n):
                if abs(m[row][col]) > abs(m[max_row][col]):
                    max_row = row
            m[col], m[max_row] = m[max_row], m[col]

            if abs(m[col][col]) < 1e-10:
                return None  # Singular

            # Eliminate below
            for row in range(col + 1, n):
                factor = m[row][col] / m[col][col]
                for j in range(col, n + 1):
                    m[row][j] -= factor * m[col][j]

        # Back substitution
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            x[i] = m[i][n]
            for j in range(i + 1, n):
                x[i] -= m[i][j] * x[j]
            x[i] /= m[i][i]

        return x

    # =========================================================================
    # Feature extraction
    # =========================================================================

    @staticmethod
    def _extract_features_from_hourly(hourly_temp, hourly_wind, day_offset_hours):
        """Extract model features from hourly weather data.

        Args:
            hourly_temp: dict {hour_offset: temp_C} where 0 = midnight of heating day
            hourly_wind: dict {hour_offset: wind_kmh}
            day_offset_hours: not used, data already offset

        Returns:
            (avg_temp, avg_wind, t_drop) or None if insufficient data
        """
        # Avg temp 20:00-03:00 (offsets -4 to 3)
        temps = [hourly_temp[h] for h in range(TEMP_WINDOW[0], TEMP_WINDOW[1]) if h in hourly_temp]
        if len(temps) < 4:
            return None

        # Avg wind 12:00-03:00 (offsets -12 to 3)
        winds = [hourly_wind[h] for h in range(WIND_WINDOW[0], WIND_WINDOW[1]) if h in hourly_wind]
        if len(winds) < 8:
            return None

        # Temp drop: evening (22:00-00:00) minus dawn (04:00-07:00)
        eve_temps = [hourly_temp[h] for h in range(DROP_EVENING[0], DROP_EVENING[1]) if h in hourly_temp]
        dawn_temps = [hourly_temp[h] for h in range(DROP_DAWN[0], DROP_DAWN[1]) if h in hourly_temp]
        if not eve_temps or not dawn_temps:
            return None

        avg_temp = sum(temps) / len(temps)
        avg_wind = sum(winds) / len(winds)
        t_drop = sum(eve_temps) / len(eve_temps) - sum(dawn_temps) / len(dawn_temps)

        return (round(avg_temp, 2), round(avg_wind, 2), round(t_drop, 2))

    def _predict(self, avg_temp, avg_wind, t_drop):
        """Predict GSHP morning consumption from features."""
        if self.coefficients is None:
            return DEFAULT_KEEP_KWH
        b = self.coefficients
        return max(0.0, b[0] + b[1] * avg_temp + b[2] * avg_wind + b[3] * t_drop)

    # =========================================================================
    # Bootstrap from historical data
    # =========================================================================

    # =========================================================================
    # Daily recording
    # =========================================================================

    def _record_daily(self):
        """Record today's GSHP consumption and weather features at ~08:00."""
        now = self.base.now_utc
        today_str = now.strftime("%Y-%m-%d")

        if self._last_record_date == today_str:
            return  # Already recorded today

        minutes_now = getattr(self.base, "minutes_now", 0)
        if minutes_now < GSHP_END_HOUR * 60 + 10:
            return  # Too early, wait until after 08:10

        # Snapshot GSHP energy at start of window if not done
        if self._gshp_energy_at_start is None:
            # Can't compute delta without start snapshot — skip today
            # Will catch it next day via bootstrap/history
            self._last_record_date = today_str
            return

        # Get current GSHP energy
        try:
            energy_now = float(self.base.get_state_wrapper(HA_GSHP_ENERGY, default=0))
        except (ValueError, TypeError):
            return

        heat_kwh = energy_now - self._gshp_energy_at_start
        if heat_kwh < 0 or heat_kwh > 20:
            self._last_record_date = today_str
            return

        # Get weather features for today from Open-Meteo recent data or HA history
        # Use forecast_home current attributes as approximation for recent hours
        # (the on_update cycle builds up hourly observations)
        # For now, use the weather data we've been collecting
        try:
            zone_lat = float(self.base.get_state_wrapper("zone.home", attribute="latitude"))
            zone_lon = float(self.base.get_state_wrapper("zone.home", attribute="longitude"))

            from datetime import timedelta

            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            resp = requests.get(
                OPEN_METEO_URL,
                params={
                    "latitude": zone_lat,
                    "longitude": zone_lon,
                    "start_date": yesterday,
                    "end_date": today_str,
                    "hourly": "temperature_2m,wind_speed_10m",
                    "timezone": "UTC",
                },
                timeout=15,
            )
            resp.raise_for_status()
            wx = resp.json()

            hourly_temp = {}
            hourly_wind = {}
            times = wx.get("hourly", {}).get("time", [])
            temps = wx.get("hourly", {}).get("temperature_2m", [])
            winds = wx.get("hourly", {}).get("wind_speed_10m", [])

            today_parts = today_str.split("-")
            today_tuple = (int(today_parts[0]), int(today_parts[1]), int(today_parts[2]))

            for i, t_str in enumerate(times):
                try:
                    dp, tp = t_str.split("T")
                    y, mo, d = dp.split("-")
                    h = int(tp.split(":")[0])
                    # Convert to offset from midnight of today
                    if (int(y), int(mo), int(d)) == today_tuple:
                        h_off = h
                    else:
                        h_off = h - 24  # Yesterday
                    if temps[i] is not None and winds[i] is not None:
                        hourly_temp[h_off] = temps[i]
                        hourly_wind[h_off] = winds[i]
                except (ValueError, IndexError):
                    continue

            features = self._extract_features_from_hourly(hourly_temp, hourly_wind, 0)
        except Exception as e:
            self.log("Cold weather: failed to get weather for recording: {}".format(e))
            features = None

        if features is None:
            self._last_record_date = today_str
            return

        avg_temp, avg_wind, t_drop = features

        # Check for duplicate date
        existing_dates = {h["date"] for h in self.history}
        if today_str not in existing_dates:
            rolling_avg = self._rolling_avg()
            boost = max(0.0, (self._prediction or 0) - rolling_avg)
            min_soc = round(self._morning_min_soc, 1) if self._morning_min_soc < 999 else None
            self.history.append(
                {
                    "date": today_str,
                    "heat_kwh": round(heat_kwh, 2),
                    "avg_temp": avg_temp,
                    "avg_wind": avg_wind,
                    "t_drop": t_drop,
                    "prediction": round(self._prediction, 2) if self._prediction else None,
                    "boost": round(boost, 2),
                    "morning_min_soc_pct": min_soc,
                }
            )
            self._save_history()
            self._fit_model()
            self.log(
                "Cold weather: recorded day {} heat={:.1f}kWh temp={:.1f} wind={:.1f} drop={:.1f} "
                "prediction={} boost={:.2f} min_soc={}%".format(today_str, heat_kwh, avg_temp, avg_wind, t_drop, round(self._prediction, 1) if self._prediction else "?", boost, min_soc)
            )

        self._last_record_date = today_str
        self._gshp_energy_at_start = None  # Reset for next day

    # =========================================================================
    # Hooks
    # =========================================================================

    def on_update(self):
        """Called every Predbat cycle. Handles GSHP energy snapshots and daily recording."""
        enabled = str(self.base.get_state_wrapper(HA_GSHP_HEATING, default="off")).lower() in ("on", "true")
        if not enabled:
            return

        minutes_now = getattr(self.base, "minutes_now", 0)

        # Snapshot GSHP energy at start of measurement window
        if GSHP_START_HOUR * 60 <= minutes_now < GSHP_START_HOUR * 60 + 10:
            if self._gshp_energy_at_start is None:
                self._morning_min_soc = 999.0  # Reset for new day
                try:
                    self._gshp_energy_at_start = float(self.base.get_state_wrapper(HA_GSHP_ENERGY, default=0))
                    self.log("Cold weather: GSHP energy snapshot at 04:00 = {:.2f} kWh".format(self._gshp_energy_at_start))
                except (ValueError, TypeError):
                    pass

        # Track min SOC during morning window (04:00-08:00)
        if GSHP_START_HOUR * 60 <= minutes_now < GSHP_END_HOUR * 60:
            soc_kw = getattr(self.base, "soc_kw", 999)
            soc_max = getattr(self.base, "soc_max", 18.08)
            soc_pct = (soc_kw / soc_max * 100) if soc_max > 0 else 0
            self._morning_min_soc = min(self._morning_min_soc, soc_pct)

        # Record daily consumption after 08:10
        if minutes_now >= GSHP_END_HOUR * 60 + 10:
            self._record_daily()

        # Publish sensor
        self._publish()

    def on_before_plan(self, context):
        """Inject alert_keep for GSHP morning window so optimizer respects it.

        The old approach boosted best_soc_keep (soft penalty, weight=0.5) which
        the optimizer ignored during cheap rate — the penalty was less than the
        cost of charging. Now we inject into all_active_keep which triggers the
        alert_keep path in prediction.py: keep_minute_scaling is forced to 10.0,
        bypassing the four-hour ramp and making the penalty 20x stronger.
        """
        enabled = str(self.base.get_state_wrapper(HA_GSHP_HEATING, default="off")).lower() in ("on", "true")
        if not enabled:
            return context

        if self.coefficients is None:
            prediction = DEFAULT_KEEP_KWH
        else:
            prediction = self._get_tonight_prediction()
            if prediction is None:
                return context

        if prediction < MIN_KEEP_KWH:
            return context

        # Convert predicted load to SOC percentage for alert_keep
        soc_max = getattr(self.base, "soc_max", 18.08)
        keep_kwh = prediction + MARGIN_KWH
        keep_pct = min(keep_kwh / soc_max * 100.0, 100.0)

        # Determine next GSHP morning window in absolute minutes (from midnight today)
        minutes_now = getattr(self.base, "minutes_now", 0)
        if minutes_now < 12 * 60:
            gshp_start = GSHP_START_HOUR * 60
            gshp_end = GSHP_END_HOUR * 60
            inject_end = CHEAP_RATE_END_HOUR * 60
        else:
            gshp_start = (24 + GSHP_START_HOUR) * 60
            gshp_end = (24 + GSHP_END_HOUR) * 60
            inject_end = (24 + CHEAP_RATE_END_HOUR) * 60

        # Inject alert_keep only during cheap rate (04:00-07:00). The taper
        # references the full GSHP window (04:00-08:00) so remaining fraction
        # represents how much GSHP load is still to come. At 07:00 (end of
        # cheap rate) ~25% of the prediction remains — enough for the last
        # hour. After 07:00: no penalty, battery drains naturally for load.
        all_active_keep = getattr(self.base, "all_active_keep", {})
        window_len = gshp_end - gshp_start
        end_pct = 0.0
        for minute in range(gshp_start, inject_end):
            remaining = (gshp_end - minute) / window_len
            tapered_pct = keep_pct * remaining
            if minute not in all_active_keep or all_active_keep[minute] < tapered_pct:
                all_active_keep[minute] = tapered_pct
            end_pct = tapered_pct

        # Boost best_soc_keep — covers GSHP uncertainty beyond LoadML's average.
        # best_soc_keep is a planning floor (buffer for forecast error), not the
        # load itself. Predbat already accounts for load in charge targets.
        base_keep = context.get("best_soc_keep", 0)
        keep_boost = prediction * 0.5
        context["best_soc_keep"] = base_keep + keep_boost

        # Boost weight so penalty exceeds cheap rate cost, forcing the optimizer
        # to actually charge enough during cheap windows.
        base_weight = context.get("best_soc_keep_weight", 0.5)
        weight_boost = prediction / DEFAULT_KEEP_KWH * 2.0
        new_weight = min(base_weight + weight_boost, 5.0)
        context["best_soc_keep_weight"] = new_weight

        self._keep_pct = round(keep_pct, 1)
        self._keep_kwh = round(keep_kwh, 1)
        self._keep_weight = round(new_weight, 1)

        # Publish adjustment for visibility
        self.base.dashboard_item(
            "sensor.{}_cold_weather_soc_keep_boost".format(self.base.prefix),
            round(keep_boost, 2),
            {
                "friendly_name": "Cold Weather SOC Keep Boost",
                "unit_of_measurement": "kWh",
                "icon": "mdi:snowflake-thermometer",
                "base_keep": round(base_keep, 2),
                "boosted_keep": round(context["best_soc_keep"], 2),
                "weight": round(new_weight, 1),
                "prediction": round(prediction, 2),
            },
        )

        self.log(
            "Cold weather: alert_keep={:.1f}%->{:.1f}% for {:02d}:00-{:02d}:00, "
            "soc_keep={:.1f}->{:.1f}kWh, weight={:.1f}->{:.1f} "
            "(predicted={:.1f}kWh)".format(
                keep_pct,
                end_pct,
                GSHP_START_HOUR,
                CHEAP_RATE_END_HOUR,
                base_keep,
                context["best_soc_keep"],
                base_weight,
                new_weight,
                prediction,
            )
        )

        return context

    def _get_tonight_prediction(self):
        """Predict tonight's GSHP consumption from weather forecast.

        The forecast only contains future hours. For past hours we use the
        current weather state as a stand-in. The model needs:
        - avg_temp: 20:00-03:00 (offsets -4 to 3 from midnight)
        - avg_wind: 12:00-03:00 (offsets -12 to 3 from midnight)
        - temp_drop: evening (22:00-00:00) minus dawn (04:00-07:00)

        At evening time most of the temp window is available from forecast
        but the wind window reaches back into the afternoon. We fill past
        hours with the current weather conditions.
        """
        if self.coefficients is None:
            return None

        try:
            result = self.base.call_service_wrapper("weather/get_forecasts", type="hourly", entity_id=HA_WEATHER, return_response=True)
            if not result:
                self.log("Cold weather: weather forecast returned no result")
                return None

            forecasts = result.get(HA_WEATHER, {}).get("forecast", [])
            if not forecasts:
                self.log("Cold weather: no forecast entries in result")
                return None

            from datetime import datetime, timedelta

            now = self.base.now_utc
            today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

            # Target heating morning: today if before noon, tomorrow if after
            if now.hour < 12:
                target_midnight = today_midnight
            else:
                target_midnight = today_midnight + timedelta(days=1)

            # Fill from forecast (future hours)
            hourly_temp = {}
            hourly_wind = {}
            for f in forecasts:
                try:
                    dt_str = f.get("datetime", "")
                    dt = datetime.fromisoformat(dt_str)
                    h_off = int((dt - target_midnight).total_seconds() / 3600)
                    if -12 <= h_off < 8:
                        temp = f.get("temperature", f.get("temp"))
                        wind = f.get("wind_speed")
                        if temp is not None:
                            hourly_temp[h_off] = temp
                        if wind is not None:
                            hourly_wind[h_off] = wind
                except (ValueError, TypeError, KeyError):
                    continue

            # Fill past hours with current weather state (for the wind window
            # which reaches back to 12:00 — mostly past by evening)
            current_temp = self.base.get_state_wrapper(HA_WEATHER, attribute="temperature")
            current_wind = self.base.get_state_wrapper(HA_WEATHER, attribute="wind_speed")
            if current_temp is not None and current_wind is not None:
                try:
                    current_temp = float(current_temp)
                    current_wind = float(current_wind)
                    for h_off in range(-12, 8):
                        if h_off not in hourly_temp:
                            hourly_temp[h_off] = current_temp
                        if h_off not in hourly_wind:
                            hourly_wind[h_off] = current_wind
                except (ValueError, TypeError):
                    pass

            features = self._extract_features_from_hourly(hourly_temp, hourly_wind, 0)
            if features is None:
                self.log("Cold weather: insufficient data for features (temp={}, wind={})".format(len(hourly_temp), len(hourly_wind)))
                return None

            avg_temp, avg_wind, t_drop = features
            prediction = self._predict(avg_temp, avg_wind, t_drop)
            self._prediction = prediction
            self.log("Cold weather: prediction={:.2f}kWh (temp={:.1f}, wind={:.1f}, drop={:.1f})".format(prediction, avg_temp, avg_wind, t_drop))
            return prediction

        except Exception as e:
            self.log("Cold weather: forecast prediction failed: {}".format(e))
            return None

    def _rolling_avg(self):
        """Rolling average of recent GSHP consumption (approximates what LoadML has learned)."""
        if not self.history:
            return 0.0
        recent = self.history[-14:]  # Last 14 days
        return sum(h["heat_kwh"] for h in recent) / len(recent)

    def _publish(self):
        """Publish cold weather sensor."""
        rolling_avg = self._rolling_avg()
        boost = max(0.0, self._prediction - rolling_avg) if self._prediction is not None else 0.0

        attrs = {
            "friendly_name": "Cold Weather GSHP Prediction",
            "unit_of_measurement": "kWh",
            "icon": "mdi:snowflake-thermometer",
            "boost": round(boost, 2),
            "rolling_avg": round(rolling_avg, 2),
            "alert_keep_pct": self._keep_pct,
            "alert_keep_kwh": self._keep_kwh,
            "keep_weight": self._keep_weight,
            "model_r2": round(self.model_r2, 3) if self.coefficients else None,
            "data_points": len(self.history),
            "coefficients": [round(c, 4) for c in self.coefficients] if self.coefficients else None,
        }

        value = round(self._prediction, 2) if self._prediction is not None else "unknown"
        self.base.dashboard_item(
            "sensor.{}_cold_weather_prediction".format(self.base.prefix),
            value,
            attrs,
        )
