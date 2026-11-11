"""Rebuild the local database from the raw files.

Run from the repo root:  python -m backend.pipeline.build

Cleaning rules:
- facilities are joined on facility_id only, never on the typed name;
- demand is what was requested, kept per facility, component and blood group;
- a donation becomes supply on its tested date, and only if non-reactive;
- fridge readings of -99.0, 99.9 and 0.0 are sensor faults and are dropped;
- a transfer with no arrival time is kept but marked "not confirmed".
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

import pandas as pd

from backend import config
from backend.pipeline.calendar import build_calendar
from backend.pipeline.load import load

EMERGENCY_TYPES = {"emergency_request", "emergency_cross_region"}


def _kind(facility_id: pd.Series) -> pd.Series:
    return facility_id.str.split("-").str[0].map(
        {"RBTC": "regional_centre", "SAT": "satellite", "HSP": "hospital"})


def build_facility(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    out["kind"] = _kind(out["facility_id"])
    # A facility's region is the regional centre that supplies it (a centre is its own region).
    out["region"] = out["supply_centre_id"].fillna(out["facility_id"])
    centre_names = out.set_index("facility_id")["name"].str.replace(
        " Regional Blood Transfusion Centre", "", regex=False)
    out["region_name"] = out["region"].map(centre_names)
    for column in ("makes_platelets", "separates_components", "has_blood_fridge_backup_power"):
        out[column] = out[column].astype(bool).astype(int)
    return out


def build_demand_daily(requests: pd.DataFrame) -> pd.DataFrame:
    """Requests added up per facility, component, blood group and day.

    Only days with at least one request are stored; missing days mean zero.
    The patient group "unknown" (some emergencies) is kept as its own group so
    facility totals stay right; it is left out of per-group forecasts.
    """
    r = requests.copy()
    r["date"] = r["requested_at"].dt.normalize()
    r["unmet"] = r["units_requested"] - r["units_issued"]
    r["is_emergency"] = (r["urgency"] == "emergency").astype(int)
    r["not_fully_met"] = (r["outcome"] != "fulfilled").astype(int)
    r["needed_emergency"] = r["emergency_supplied_from_id"].notna().astype(int)
    # The event we warn about: the hospital's own shelf could not fill the request.
    r["shortage"] = ((r["not_fully_met"] == 1) | (r["needed_emergency"] == 1)).astype(int)
    out = (
        r.groupby(["facility_id", "component_requested", "patient_blood_group", "date"], as_index=False)
        .agg(units_requested=("units_requested", "sum"),
             units_issued=("units_issued", "sum"),
             requests=("request_id", "count"),
             unmet_units=("unmet", "sum"),
             emergency_requests=("is_emergency", "sum"),
             requests_not_fully_met=("not_fully_met", "sum"),
             requests_needing_emergency_delivery=("needed_emergency", "sum"),
             shortage_requests=("shortage", "sum"),
             units_substituted=("units_issued_group_substituted", "sum"),
             units_from_local_stock=("units_from_local_stock", "sum"))
        .rename(columns={"component_requested": "component", "patient_blood_group": "blood_group"})
    )
    return out


def build_emergency_resupply(requests: pd.DataFrame) -> pd.DataFrame:
    """One row per request that needed an emergency delivery: how long it took."""
    e = requests[requests["emergency_supplied_from_id"].notna()
                 & requests["last_unit_available_at"].notna()].copy()
    e["hours"] = (e["last_unit_available_at"] - e["requested_at"]).dt.total_seconds() / 3600
    e["date"] = e["requested_at"].dt.normalize()
    return e[["facility_id", "date", "component_requested", "patient_blood_group",
              "emergency_supplied_from_id", "hours"]].rename(columns={
                  "component_requested": "component", "patient_blood_group": "blood_group",
                  "emergency_supplied_from_id": "from_facility_id"})


def build_unit(units: pd.DataFrame, donations: pd.DataFrame, requests: pd.DataFrame) -> pd.DataFrame:
    """Units with the date they became usable stock (the donation's tested date)
    and, if issued, which component the request had asked for."""
    cleared = donations.loc[donations["screening_result"] == "non_reactive",
                            ["donation_id", "tested_date", "collecting_site_id"]]
    out = units.merge(cleared, on="donation_id", how="inner")
    out = out.rename(columns={"tested_date": "available_date"})
    asked = requests[["request_id", "component_requested"]].rename(columns={
        "request_id": "issued_request_id", "component_requested": "issued_for_component"})
    out = out.merge(asked, on="issued_request_id", how="left")
    out["temp_excursion_exposed"] = out["temp_excursion_exposed"].astype(bool).astype(int)
    out["opening_stock"] = out["opening_stock"].astype(bool).astype(int)
    # The donation and request references are dropped: nothing in the database can be
    # traced back to a donation, a donor, a request or a patient.
    return out.drop(columns=["donation_id", "issued_request_id"])


def pseudonymise_unit_ids(unit: pd.DataFrame, unit_move: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Replace unit labels with random-salted hashes.

    A unit label stands in for a donation identification number, so it could
    link a unit to a donation. The salt is made fresh on every build and never
    stored, so the hashes cannot be turned back into labels, yet a unit still
    matches its own movements inside one build.
    """
    salt = secrets.token_bytes(16)

    def scramble(values: pd.Series) -> pd.Series:
        return values.map(lambda v: "u_" + hashlib.sha256(salt + str(v).encode()).hexdigest()[:16])

    unit, unit_move = unit.copy(), unit_move.copy()
    unit["unit_id"] = scramble(unit["unit_id"])
    unit_move["unit_id"] = scramble(unit_move["unit_id"])
    return unit, unit_move.drop(columns=["transfer_id"])


def build_unit_move(transfers: pd.DataFrame) -> pd.DataFrame:
    out = transfers[["transfer_id", "unit_id", "from_facility_id", "to_facility_id", "transfer_type",
                     "dispatched_at", "received_at", "coolbox_max_temp_c"]].copy()
    out["confirmed"] = out["received_at"].notna().astype(int)
    return out


def build_delivery(unit_move: pd.DataFrame, unit: pd.DataFrame) -> pd.DataFrame:
    """Arrivals per receiving facility, day, component and blood group.

    The day is the arrival day when the receiver confirmed it, otherwise the
    dispatch day, with confirmed = 0 ("not confirmed arrived").
    """
    m = unit_move.merge(unit[["unit_id", "component", "blood_group"]], on="unit_id", how="inner")
    m["date"] = m["received_at"].fillna(m["dispatched_at"]).dt.normalize()
    m["kind"] = m["transfer_type"].map(
        lambda t: "emergency" if t in EMERGENCY_TYPES else ("routine" if t == "routine" else "rebalancing"))
    out = (
        m.groupby(["to_facility_id", "from_facility_id", "date", "kind", "transfer_type",
                   "confirmed", "component", "blood_group"], as_index=False)
        .agg(units=("unit_id", "count"))
        .rename(columns={"to_facility_id": "facility_id"})
    )
    return out


def build_supply(donations: pd.DataFrame, unit: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Collections per site and day, and cleared units per processing centre and available day."""
    d = donations.copy()
    d["collected"] = (d["outcome"] == "collected").astype(int)
    d["deferred"] = (d["outcome"] == "deferred").astype(int)
    d["reactive"] = (d["screening_result"] == "reactive").astype(int)
    d["cleared"] = (d["screening_result"] == "non_reactive").astype(int)
    d["wait_days"] = (d["tested_date"] - d["collection_date"]).dt.days
    daily = (
        d.groupby(["collecting_site_id", "collection_date"], as_index=False)
        .agg(attempts=("donation_id", "count"), collected=("collected", "sum"),
             deferred=("deferred", "sum"), reactive=("reactive", "sum"),
             cleared_donations=("cleared", "sum"), mean_testing_wait_days=("wait_days", "mean"))
        .rename(columns={"collection_date": "date"})
    )
    cleared = (
        unit.groupby(["processed_at_id", "collecting_site_id", "available_date", "component", "blood_group"],
                     as_index=False)
        .agg(units=("unit_id", "count"))
        .rename(columns={"processed_at_id": "centre_id", "available_date": "date"})
    )
    return daily, cleared


def build_temperature_daily(logs: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop sensor faults, then keep one row per facility and day."""
    blood = config.load_settings("blood")
    glitches = set(blood["temperature_glitch_values"])
    low, high = blood["safe_fridge_range_c"]
    is_glitch = logs["temp_c"].isin(glitches)
    clean = logs[~is_glitch].copy()
    clean["date"] = clean["recorded_at"].dt.normalize()
    clean["out_of_range"] = ((clean["temp_c"] < low) | (clean["temp_c"] > high)).astype(int)
    daily = clean.groupby(["facility_id", "date"], as_index=False).agg(
        readings=("temp_c", "count"), min_temp_c=("temp_c", "min"), max_temp_c=("temp_c", "max"),
        hours_out_of_range=("out_of_range", "sum"))
    return daily, int(is_glitch.sum())


INDEXES = [
    "CREATE INDEX ix_demand ON demand_daily (facility_id, component, blood_group, date)",
    "CREATE INDEX ix_stock ON stock_daily (facility_id, date)",
    "CREATE INDEX ix_delivery ON delivery (facility_id, date)",
    "CREATE INDEX ix_unit_move ON unit_move (unit_id)",
    "CREATE INDEX ix_calendar ON calendar (date, region)",
]


def fingerprint(raw_dir: Path | None = None) -> str:
    """A short code for "the raw data, the settings and the forecasting code as they are now"."""
    digest = hashlib.sha256()
    # Only what can change the results: the data, the settings, and the pipeline and engine code.
    # Changes to the web server or the central sync do not need a rebuild.
    code = [path for folder in ("pipeline", "engine", "db") for path in (config.ROOT / "backend" / folder).rglob("*.py")]
    files = sorted((raw_dir or config.RAW_DIR).glob("*.csv")) + sorted(config.CONFIG_DIR.glob("*.yaml")) + sorted(code)
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:20]


def up_to_date(db_path: Path, mark: str) -> bool:
    """True when the database was fully built from exactly this data, these settings and this code."""
    if not db_path.exists():
        return False
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"app_setting", "forecast", "alert_metric", "supply_metric", "allocation_replay"} <= tables:
            return False
        row = conn.execute("SELECT value FROM app_setting WHERE key = 'build_fingerprint'").fetchone()
        return bool(row) and row[0] == mark
    finally:
        conn.close()


def build(db_path: Path | None = None, raw_dir: Path | None = None, verbose: bool = True,
          write_docs: bool = True, run_backtests: bool = True, force: bool = False) -> dict[str, int]:
    """Rebuild every pipeline table. Tables written by the app (users, decisions,
    audit log) are left untouched if they already exist."""
    db_path = db_path or config.DB_PATH
    started = time.time()
    mark = fingerprint(raw_dir)
    if run_backtests and not force and up_to_date(db_path, mark):
        if verbose:
            print("Already up to date: the data, settings and code have not changed since the last build.")
            print("Nothing to do. To rebuild anyway, run: python -m backend.pipeline.build --force")
        return {}

    def say(message: str) -> None:
        if verbose:
            print(message, flush=True)

    say("Reading raw files...")
    facility = build_facility(load("facilities", raw_dir))
    requests = load("requests", raw_dir)
    donations = load("donations", raw_dir)
    unit = build_unit(load("units", raw_dir), donations, requests)
    unit_move = build_unit_move(load("transfers", raw_dir))
    unit, unit_move = pseudonymise_unit_ids(unit, unit_move)
    supply_daily, supply_cleared = build_supply(donations, unit)
    temperature_daily, glitches_dropped = build_temperature_daily(load("temperature_logs", raw_dir))
    stock = load("daily_stock", raw_dir).rename(columns={"units_in_stock_end_of_day": "units"})
    regions = sorted(facility.loc[facility["kind"] == "regional_centre", "facility_id"])

    tables: dict[str, pd.DataFrame] = {
        "facility": facility,
        "route": load("routes", raw_dir),
        "demand_daily": build_demand_daily(requests),
        "emergency_resupply": build_emergency_resupply(requests),
        "stock_daily": stock,
        "unit": unit,
        "unit_move": unit_move,
        "delivery": build_delivery(unit_move, unit),
        "drive": load("drives", raw_dir),
        "supply_daily": supply_daily,
        "supply_cleared_daily": supply_cleared,
        "calendar": build_calendar(regions),
        "temperature_daily": temperature_daily,
        "power_outage": load("power_outages", raw_dir),
        "pipeline_note": pd.DataFrame([
            {"key": "temperature_glitches_dropped", "value": str(glitches_dropped)},
            {"key": "built_at", "value": pd.Timestamp.now().isoformat(timespec="seconds")},
            {"key": "data_first_date", "value": str(stock["date"].min().date())},
            {"key": "data_last_date", "value": str(stock["date"].max().date())},
        ]),
    }

    say(f"Writing {len(tables)} tables to {db_path} ...")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        for name, frame in tables.items():
            frame = frame.copy()
            for column in frame.columns:
                if pd.api.types.is_datetime64_any_dtype(frame[column]):
                    has_time = (frame[column].dropna().dt.normalize() != frame[column].dropna()).any()
                    fmt = "%Y-%m-%d %H:%M:%S" if has_time else "%Y-%m-%d"
                    frame[column] = frame[column].dt.strftime(fmt)
            frame.to_sql(name, conn, if_exists="replace", index=False)
        for statement in INDEXES:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()

    counts = {name: len(frame) for name, frame in tables.items()}
    for name, count in counts.items():
        say(f"  {name:<22}{count:>9,} rows")

    from backend.pipeline.facts import write_data_facts
    text = write_data_facts(db_path, write_docs=write_docs)
    say("")
    say(text)
    say("Data facts written to docs/DATA_FACTS.md")

    from backend.db.app_tables import ensure_app_tables
    ensure_app_tables(db_path)

    if run_backtests:
        from backend.engine import alert_backtest, backtest, models, supply_backtest
        say("Backtesting the supply forecast...")
        supply_backtest.run(db_path=db_path, write_docs=write_docs, verbose=False)
        for name in models.REGISTRY:
            say(f"Backtesting the '{name}' forecast...")
            backtest.run(name, db_path=db_path, write_docs=write_docs, verbose=False)
            alert_backtest.run(name, db_path=db_path, write_docs=write_docs, verbose=False)
        from backend.engine import allocation
        say("Replaying the fair-share allocation...")
        allocation.run(db_path=db_path, write_docs=write_docs, verbose=False)
        say("Results written to docs/RESULTS.md")
    if run_backtests:
        from backend.db.app_tables import set_setting
        set_setting("build_fingerprint", mark, db_path)
    say(f"Done in {time.time() - started:.0f} seconds.")
    return counts


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild the local database from the raw files.")
    parser.add_argument("--data-only", action="store_true", help="skip the forecast backtests")
    parser.add_argument("--force", action="store_true", help="rebuild even if nothing has changed")
    options = parser.parse_args()
    build(run_backtests=not options.data_only, force=options.force)
