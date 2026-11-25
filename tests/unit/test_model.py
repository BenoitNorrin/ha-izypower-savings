"""Business rules run with the standard library, including in GitHub Actions."""
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("savings_model", ROOT / "custom_components/izypower_savings/model.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
TZ = ZoneInfo("Europe/Paris")


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def config(**overrides):
    return {"base_rate": .30, "export_mode": "none", "export_rate": 0.,
            "currency": "EUR", "timezone": "Europe/Paris", "max_gap_seconds": 900, **overrides}


def engine(start, **overrides):
    e = m.Engine()
    cfg = config(**overrides)
    e.apply({"type": "start", "at": start.isoformat(), "timezone": cfg["timezone"],
             "currency": cfg["currency"], "max_gap_seconds": cfg["max_gap_seconds"], "plan": m.build_plan(cfg)})
    return e


def sample(when, consumption=1000, grid=400, exported=None, error=None):
    r = {"type": "sample", "received_at": when.isoformat(), "measured_at": when.isoformat(),
         "consumption_w": consumption, "grid_w": grid, "export_w": exported}
    if error:
        r["error"] = error
    return r


def feed(e, begin, end, consumption=1000, grid=400, exported=None):
    for seconds in range(0, int(end.timestamp() - begin.timestamp()) + 1, 300):
        e.apply(sample(datetime.fromtimestamp(begin.timestamp() + seconds, TZ), consumption, grid, exported))


class TariffTests(unittest.TestCase):
    def test_single_tariff_without_off_peak(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start)
        feed(e, start, start + timedelta(minutes=10))
        r = e.rows["2026-01-10"]
        self.assertAlmostEqual(r["without_cost"], .30 / 6)
        self.assertAlmostEqual(r["import_cost"], .4 * .30 / 6)
        self.assertAlmostEqual(r["benefit"], .6 * .30 / 6)

    def test_zero_rate_is_valid(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start, base_rate=0)
        feed(e, start, start + timedelta(minutes=5))
        self.assertEqual(e.rows["2026-01-10"]["benefit"], 0)

    def test_multiple_and_overnight_windows(self):
        plan = m.build_plan(config(off_peak_enabled=True, off_peak_rate=.15,
                                   off_peak_windows="22:00-06:00, 13:00-15:00"))
        for hour, expected in (("23:00", .15), ("05:59", .15), ("06:00", .30), ("14:00", .15), ("15:00", .30)):
            self.assertEqual(m.rate_at(plan, at("2026-01-10T"+hour+":00").timestamp(), TZ), expected)

    def test_super_off_peak_takes_priority(self):
        plan = m.build_plan(config(off_peak_enabled=True, off_peak_rate=.15, off_peak_windows="22:00-06:00",
            super_off_peak_enabled=True, super_off_peak_rate=.08, super_off_peak_windows="02:00-04:00"))
        for hour, expected in (("01:00", .15), ("03:00", .08), ("04:00", .15), ("10:00", .30)):
            self.assertEqual(m.rate_at(plan, at("2026-01-10T"+hour+":00").timestamp(), TZ), expected)

    def test_super_off_peak_without_regular_off_peak(self):
        plan = m.build_plan(config(super_off_peak_enabled=True, super_off_peak_rate=.08, super_off_peak_windows="02:00-04:00"))
        self.assertEqual(m.rate_at(plan, at("2026-01-10T03:00:00").timestamp(), TZ), .08)
        self.assertEqual(m.rate_at(plan, at("2026-01-10T05:00:00").timestamp(), TZ), .30)

    def test_hour_boundary_is_split(self):
        start = at("2026-01-10T05:55:00")
        e = engine(start, off_peak_enabled=True, off_peak_rate=.15, off_peak_windows="22:00-06:00")
        feed(e, start, start + timedelta(minutes=10), grid=0)
        self.assertAlmostEqual(e.rows["2026-01-10"]["without_cost"], (.15 + .30)*5/60)

    def test_invalid_windows(self):
        for text in ("6:00-08:00", "24:00-06:00", "22:70-06:00", "02:00-02:00", "23:00-24:01", "bad", "22:00-06:00,"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                m.parse_windows(text)
        self.assertEqual(m.parse_windows("00:00-24:00"), [[0, 1440]])

    def test_enabled_band_requires_windows(self):
        with self.assertRaises(ValueError):
            m.build_plan(config(off_peak_enabled=True, off_peak_rate=.1, off_peak_windows=""))

    def test_nonfinite_or_negative_rate_is_rejected(self):
        for value in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                m.build_plan(config(base_rate=value))


class EnergyTests(unittest.TestCase):
    def test_unpaid_injection_is_recorded_without_revenue(self):
        start = at("2026-01-10T12:00:00")
        for mode in ("none", "unpaid"):
            e = engine(start, export_mode=mode, export_rate=999)
            feed(e, start, start + timedelta(minutes=10), grid=-2000)
            r = e.rows["2026-01-10"]
            self.assertAlmostEqual(r["export_kwh"], 2/6)
            self.assertEqual(r["import_cost"], 0)
            self.assertEqual(r["export_revenue"], 0)
            self.assertAlmostEqual(r["benefit"], r["without_cost"])

    def test_paid_injection_separates_revenue_and_savings(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start, export_mode="paid", export_rate=.1)
        feed(e, start, start + timedelta(minutes=10), grid=-2000)
        r = e.rows["2026-01-10"]
        self.assertAlmostEqual(r["export_revenue"], 2/6*.1)
        self.assertAlmostEqual(r["consumption_savings"], 1/6*.30)
        self.assertAlmostEqual(r["benefit"], 1/6*.30+2/6*.1)
        self.assertAlmostEqual(r["net_cost"], -r["export_revenue"])

    def test_signed_meter_crossing_zero_is_not_double_counted(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start, export_mode="paid", export_rate=.1)
        e.apply(sample(start, grid=200)); e.apply(sample(start+timedelta(minutes=10), grid=-200))
        r = e.rows["2026-01-10"]
        self.assertAlmostEqual(r["import_kwh"], .2*5/60/2)
        self.assertAlmostEqual(r["export_kwh"], .2*5/60/2)

    def test_separate_import_export_sensors(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start, export_mode="paid", export_rate=.1)
        feed(e, start, start+timedelta(minutes=10), grid=400, exported=200)
        r = e.rows["2026-01-10"]
        self.assertAlmostEqual(r["import_kwh"], .4/6)
        self.assertAlmostEqual(r["export_revenue"], .2/6*.1)

    def test_negative_benefit_is_preserved(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start); feed(e, start, start+timedelta(minutes=10), grid=1500)
        self.assertLess(e.rows["2026-01-10"]["benefit"], 0)
        self.assertLess(e.statistics("benefit")[0]["sum"], 0)

    def test_partial_day_and_no_data_are_distinct(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start)
        self.assertIsNone(e.summary("jour", start)["benefit"])
        feed(e, start, start+timedelta(minutes=10), grid=1000)
        self.assertEqual(e.summary("jour", start)["benefit"], 0)
        self.assertEqual(e.summary("jour", start)["measured_hours"], .17)

    def test_missing_points_do_not_block_the_rest_of_the_day(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start)
        for minute in (0, 5, 60, 65):
            e.apply(sample(start+timedelta(minutes=minute)))
        self.assertEqual(e.rows["2026-01-10"]["seconds"], 600)

    def test_unknown_is_not_interpolated_as_zero(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start)
        e.apply(sample(start)); e.apply(sample(start+timedelta(minutes=5), error="unavailable"))
        e.apply(sample(start+timedelta(minutes=10)))
        self.assertEqual(e.rows, {})

    def test_source_change_breaks_interpolation(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start); e.apply(sample(start))
        e.apply({"type":"sources", "at":(start+timedelta(minutes=1)).isoformat(), "key":"other", "max_gap_seconds":900})
        e.apply(sample(start+timedelta(minutes=5)))
        self.assertEqual(e.rows, {})

    def test_duplicates_and_start_cutoff(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start); e.apply(sample(start-timedelta(minutes=5)))
        e.apply(sample(start+timedelta(minutes=5)))
        before = json.dumps(e.rows, sort_keys=True)
        e.apply(sample(start+timedelta(minutes=5)))
        self.assertEqual(json.dumps(e.rows, sort_keys=True), before)
        self.assertEqual(e.rows["2026-01-10"]["seconds"], 300)


class HistoryTests(unittest.TestCase):
    def test_source_switch_clips_delayed_measurements_on_replay(self):
        start = at("2026-11-18T11:50:00")
        with tempfile.TemporaryDirectory() as directory:
            journal = m.Journal(directory)
            journal.load(start, config())
            journal.append(sample(start))
            journal.append(sample(start + timedelta(minutes=10)))
            switched = start + timedelta(minutes=12)
            journal.append({"type": "sources", "key": "replacement", "at": switched.isoformat(), "max_gap_seconds": 900})
            delayed = sample(start + timedelta(minutes=5), consumption=0, grid=0)
            journal.append(dict(delayed, received_at=switched.isoformat()))
            journal.append(sample(start + timedelta(minutes=15), consumption=1000, grid=0))
            row = journal.engine.rows["2026-11-18"]
            self.assertEqual(row["seconds"], 13 * 60)
            # From minute 12 to 15 the replacement ramps from 700 W to 1000 W.
            self.assertAlmostEqual(row["consumption_kwh"], 10 / 60 + .85 * 3 / 60)
            restored = m.Journal(directory)
            restored.load(start + timedelta(minutes=16), config())
            self.assertEqual(restored.engine.rows, journal.engine.rows)

    def test_midnight_statistics_in_fractional_time_zones(self):
        for zone in ("Asia/Kolkata", "Asia/Kathmandu", "America/St_Johns"):
            with self.subTest(zone=zone):
                tz = ZoneInfo(zone)
                midnight = datetime(2027, 1, 1, tzinfo=tz)
                e = engine(midnight - timedelta(minutes=5), timezone=zone)
                for n in (-5, 0, 5):
                    e.apply(sample(midnight + timedelta(minutes=n), grid=0))
                stats = e.statistics("benefit")
                self.assertEqual([s["start"].astimezone(tz).date().isoformat() for s in stats],
                                 ["2026-12-31", "2027-01-01"])
                self.assertTrue(all(s["start"].minute == 0 for s in stats))
                self.assertAlmostEqual(stats[1]["sum"] - stats[0]["sum"], .30 * 5 / 60)

    def test_recovers_only_an_interrupted_tail_and_preserves_its_bytes(self):
        start = at("2026-11-18T12:00:00")
        with tempfile.TemporaryDirectory() as directory:
            journal = m.Journal(directory)
            journal.load(start, config())
            journal.append(sample(start))
            path = next(Path(directory).glob("*.jsonl"))
            good = path.read_bytes()
            fragment = b'{"incomplete":"\xc3'
            path.write_bytes(good + fragment)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                journal.append(sample(start + timedelta(minutes=1)))
            restored = m.Journal(directory)
            restored.load(start, config())
            self.assertEqual(path.read_bytes(), good)
            self.assertEqual(next(Path(directory).glob("*.partial")).read_bytes(), fragment)
            restored.append(sample(start + timedelta(minutes=5)))
            restored.load(start, config())
            self.assertEqual(restored.engine.rows["2026-11-18"]["seconds"], 300)

    def test_repairs_missing_newline_without_losing_a_valid_event(self):
        start = at("2026-11-18T12:00:00")
        with tempfile.TemporaryDirectory() as directory:
            journal = m.Journal(directory)
            journal.load(start, config())
            journal.append(sample(start))
            path = next(Path(directory).glob("*.jsonl"))
            good = path.read_bytes()
            path.write_bytes(good[:-1])
            journal.load(start, config())
            self.assertEqual(journal.engine.last["t"], start.timestamp())
            self.assertEqual(path.read_bytes(), good)
            self.assertEqual(list(Path(directory).glob("*.partial")), [])

    def test_corruption_before_the_end_of_history_is_preserved(self):
        start = at("2026-11-18T12:00:00")
        for suffix, later_month in [(b"broken\n", False), (b"broken", True)]:
            with self.subTest(later_month=later_month), tempfile.TemporaryDirectory() as directory:
                journal = m.Journal(directory)
                journal.load(start, config())
                path = next(Path(directory).glob("*.jsonl"))
                damaged = path.read_bytes() + suffix
                path.write_bytes(damaged)
                if later_month:
                    (Path(directory) / "history-2026-12.jsonl").touch()
                with self.assertRaises(ValueError):
                    journal.load(start, config())
                self.assertEqual(path.read_bytes(), damaged)

    def test_changed_windows_do_not_reprice_earlier_intervals(self):
        start = at("2026-01-10T01:55:00")
        e = engine(start, off_peak_enabled=True, off_peak_rate=.10, off_peak_windows="00:00-02:00")
        e.apply(sample(start, grid=0))
        plan = m.build_plan(config(off_peak_enabled=True, off_peak_rate=.08, off_peak_windows="00:00-03:00"))
        e.apply({"type": "tariff", "at": (start+timedelta(minutes=5)).isoformat(), "plan": plan})
        e.apply(sample(start+timedelta(minutes=10), grid=0))
        self.assertAlmostEqual(e.rows["2026-01-10"]["without_cost"], (.10+.08)*5/60)

    def test_price_schedule_and_export_changes_are_prospective(self):
        start = at("2026-01-10T12:00:00")
        e = engine(start, base_rate=.2, export_mode="unpaid")
        e.apply(sample(start, grid=-1000))
        next_plan = m.build_plan(config(base_rate=.4, export_mode="paid", export_rate=.1))
        e.apply({"type":"tariff", "at":(start+timedelta(minutes=5)).isoformat(), "plan":next_plan})
        e.apply(sample(start+timedelta(minutes=10), grid=-1000))
        r = e.rows["2026-01-10"]
        self.assertAlmostEqual(r["without_cost"], (.2+.4)*5/60)
        self.assertAlmostEqual(r["export_revenue"], .1*5/60)

    def test_restart_replays_all_old_rules_before_appending_new_rules(self):
        start = at("2026-01-10T12:00:00")
        with tempfile.TemporaryDirectory() as directory:
            j = m.Journal(directory); j.load(start, config(base_rate=.2))
            feed_points = [sample(start), sample(start+timedelta(minutes=5))]
            for p in feed_points:
                j.append(p)
            before = json.dumps(j.engine.rows, sort_keys=True)
            other = m.Journal(directory); other.load(start+timedelta(minutes=10), config(base_rate=.9, export_mode="paid", export_rate=.1))
            self.assertEqual(json.dumps(other.engine.rows, sort_keys=True), before)
            events = [json.loads(line) for p in Path(directory).glob("*.jsonl") for line in p.read_text().splitlines()]
            self.assertEqual([e["type"] for e in events], ["start", "sample", "sample", "tariff"])
            self.assertEqual(events[0]["plan"]["base_rate"], .2)
            self.assertEqual(events[-1]["plan"]["base_rate"], .9)

    def test_invalid_new_tariff_is_not_written(self):
        start = at("2026-01-10T12:00:00")
        with tempfile.TemporaryDirectory() as directory:
            j = m.Journal(directory); j.load(start, config())
            path = next(Path(directory).glob("*.jsonl")); before = path.read_bytes()
            with self.assertRaises(ValueError):
                j.append({"type":"tariff", "at":(start-timedelta(days=1)).isoformat(), "plan":m.build_plan(config())})
            self.assertEqual(path.read_bytes(), before)

    def test_timezone_currency_mismatch_does_not_reinterpret_history(self):
        start = at("2026-01-10T12:00:00")
        with tempfile.TemporaryDirectory() as directory:
            m.Journal(directory).load(start, config())
            with self.assertRaises(ValueError):
                m.Journal(directory).load(start, config(currency="USD"))

    def test_day_month_year_boundaries_and_signed_statistics(self):
        start = at("2026-12-31T23:55:00")
        e = engine(start); feed(e, start, start+timedelta(minutes=10), grid=1500)
        stats = e.statistics("benefit")
        self.assertEqual([s["start"].astimezone(TZ).date().isoformat() for s in stats], ["2026-12-31", "2027-01-01"])
        self.assertAlmostEqual(stats[1]["sum"]-stats[0]["sum"], e.rows["2027-01-01"]["benefit"])
        self.assertAlmostEqual(e.summary("annee", start+timedelta(minutes=10))["benefit"], e.rows["2027-01-01"]["benefit"])

    def test_first_hour_creates_no_fake_previous_day(self):
        start = at("2026-01-10T00:03:00")
        e = engine(start); feed(e, start, start+timedelta(minutes=5))
        self.assertEqual(e.statistics("benefit")[0]["start"].astimezone(TZ).date(), start.date())

    def test_units_and_nonfinite_values(self):
        self.assertEqual(m.watts("1.5", "kW"), 1500)
        for value, unit in (("nan", "W"), ("unavailable", "W"), ("1", "kWh")):
            with self.assertRaises(ValueError):
                m.watts(value, unit)


class DaylightSavingTests(unittest.TestCase):
    def test_actual_23_and_25_hour_days(self):
        for day, hours in (("2026-03-29", 23), ("2026-10-25", 25)):
            start = at(day+"T00:00:00"); e = engine(start)
            feed(e, start, start+timedelta(days=1), grid=0)
            self.assertEqual(e.rows[day]["seconds"], hours*3600)
            self.assertAlmostEqual(e.rows[day]["without_cost"], hours*.30)

    def test_repeated_off_peak_window_counts_both_occurrences(self):
        start = at("2026-10-25T00:00:00")
        e = engine(start, off_peak_enabled=True, off_peak_rate=.1, off_peak_windows="02:15-02:45")
        feed(e, start, start+timedelta(days=1), grid=0)
        self.assertAlmostEqual(e.rows["2026-10-25"]["without_cost"], 24*.30+1*.1)

    def test_nonexistent_boundary_starts_at_clock_transition(self):
        start = at("2026-03-29T00:00:00")
        e = engine(start, off_peak_enabled=True, off_peak_rate=.1, off_peak_windows="02:30-03:30")
        feed(e, start, start+timedelta(days=1), grid=0)
        self.assertAlmostEqual(e.rows["2026-03-29"]["without_cost"], 22.5*.30+.5*.1)


if __name__ == "__main__":
    unittest.main()
