# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Fuel Efficiency: did the diesel a truck paid for go into that truck?

One report is up to three .xlsx files, uploaded together on the Fuel
Efficiency dashboard. Each file is recognised by what is in it, not by the box
it was dropped into:

  * the fuel-card export (required): one row per fill -- plate, liters, cost,
    the odometer the driver typed, the station and its coordinates;
  * IM's Fill-Drain report: per truck, over the report's dates, what the
    tank's fuel sensor saw poured in, drained out and burned;
  * IM's Fuel Consumption report: per truck, IM's distance, working hours and
    idle hours over its dates.

Only IM is used. Plates are matched to the IM fleet, and every matched truck's
GPS track is read for the fill period.

Every truck ends with one of three statuses, the worst of its findings. The
status is fuel against distance: where a truck was when it filled up is not
used at all (the owner's rule, Sep 2026 -- a station's map pin or a card's
clock is too often wrong for a fill's location to decide anything).

  Theft likely
    * burns more per GPS or IM km than a truck of its class can;
    * a run of its fills bought more than it could burn over the GPS km between
      them at that limit, plus all its tanks hold (by the typed odometer: Suspicious);
    * above its class's normal consumption, and the tank sensor saw much less
      fuel arrive than the card paid for, or saw fuel drained out.
  Suspicious
    * above its class's normal consumption by GPS or IM km;
    * by the odometer the driver typed, more than its class can burn at all;
    * the tank sensor saw less fuel arrive than paid for, or fuel drained out,
      while the consumption is normal or cannot be judged;
    * the typed odometer disagrees with IM's distance, or with the GPS of a
      tracker whose own IM distance is impossible.
  OK
    * none of the above.

The tank sensor backs up the fuel per km, and is set aside where it cannot see
all the fuel: one that saw nothing while the truck drove, or one whose fuel
would mean a consumption too low for the class (one of two tanks measured, or
a wrong calibration). With the tracker off for large parts of the period, its
GPS distance is not used, nor IM's (the same tracker) unless the typed
odometer backs it up.

Each truck's page also shows every source worked out on its own -- the typed
odometer, the GPS track and each IM report -- each taking what it lacks from
the other files: IM's reports carry no real fuel figures, so they take the
card's liters inside their own dates, and every source takes IM's idle hours.
The most exact one that holds up decides the status: GPS, then an IM report,
then the typed odometer.

Notes are shown with the reasons but never change the status: refuelling
after almost no driving or twice within hours, fills larger than usual or than
the tank, an odometer typed backwards, a typed-odometer consumption above
normal but below the theft limit, consumption well above similar trucks, a
slow drain at the sensor, a sensor set aside or seeing more fuel than the card
bought, an IM distance too big to be driven (set aside, not used), and a GPS
track missing large parts of the period. Whether GPS was on at a fill's moment
is used only for that, and never shown.

Consumption limits are truck limits (CLASSES). This fleet's heavy trucks in
the June 2026 GPS data: median about 45 L/100 km, nine in ten under 60.
Pickups and vans are held to their own, lower limits, so a pickup burning like
a tractor unit is not waved through.

A truck IM does not know gets only the checks the card supports on its own.
Its consumption then comes from the typed odometer, which is routinely wrong
(one June file had 1,133 fills under 1 km/L), so it can make that truck
Suspicious -- and only past the class's theft limit -- never Theft likely.

IM's reports are totals over their own dates, so they are compared only with
the card fills inside those dates. A report for other dates is kept but not
used, and the dashboard says so.

Languages. Findings, flags and notes are stored as a code and its numbers and
put into words when read (translations/ar.csv). The dashboard has its own
Arabic / English button, independent of the desk language: its read calls
take `lang`, and get_translations hands it the page's own words.
"""

import bisect
import csv
import inspect
import json
import os
import math
import re
import statistics
import time
from datetime import datetime, timedelta
from datetime import time as time_of_day
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt, get_system_timezone, get_url, now_datetime

IMPORT_DT = "App Apis Fuel Import"
FILL_DT = "App Apis Fuel Fill"
VEHICLE_DT = "App Apis Fuel Vehicle"
PROGRESS_EVENT = "fuel_efficiency_progress"
TITLE = "Fuel Efficiency"

READ_ROLES = ["System Manager", "Technical", "Support Team"]
RUN_ROLES = ["System Manager", "Technical"]

THEFT, SUSPICIOUS, NOTE = 2, 1, 0
STATUS = {THEFT: "Theft likely", SUSPICIOUS: "Suspicious", NOTE: "OK"}

# Cancelling. Every queued run carries a run id, and the report's current run
# id sits in the cache: a newer run replaces it, Cancel overwrites it. A job
# whose id is no longer current stops at the next truck -- and since a run only
# replaces the stored results on its very last step, the last finished
# results stay as they were.
RUN_KEY = "fuel_efficiency_run::"
CANCELLED = "cancelled"


class Cancelled(Exception):
	"""The run was cancelled from the dashboard."""

# What counts, in one place.
COVERAGE_MIN = 20         # a GPS point this close to the invoice time: the tracker was on at the fill
# The tracker off. With no GPS around this share of a truck's fills (and at
# least this many), its GPS distance is missing whole trips and is not used
# for consumption or the odometer check. (June 2026: one truck's GPS read 856
# km where its odometer read 9,233, which made a normal 41 L/100 km look like 448.)
GPS_GAP_SHARE = 0.25
GPS_GAP_MIN_FILLS = 3
# Driving between fills.
NO_MOVE_KM = 20           # less GPS distance than this since the last fill, yet a real fill
QUICK_REFILL_H = 3        # a second real fill this soon after the last
BIG_FILL_FACTOR = 1.8     # this many times the truck's own median fill (and at least its class's `big`)
MIN_KM_FOR_RATE = 300     # km needed before L/100 km is judged
SHOW_KM_FOR_RATE = 50     # ...and before it is shown at all
PEER_EXCESS = 35          # % above the median of the same brand and model
ODOMETER_GAP = 25         # % between the typed odometer and IM's distance
# The tank's fuel sensor, from IM's Fill-Drain report.
SENSOR_MIN_CARD_L = 50    # card liters inside the report's dates before the sensor is judged
MISSING_SUSPICIOUS = (60, 10)   # paid for but never seen in the tank: liters, and % of the card
MISSING_THEFT = (150, 25)       # ...Theft likely only with the consumption above normal too
DRAIN_THEFT_L = 30        # drained out: this much is Suspicious, less a note; Theft likely / Suspicious with consumption above normal
SLOW_DRAIN_L = 20         # siphoned slowly: a note only -- IM's Aug 2026 exports show the same 21.2 L on nearly every truck
# The sensor backs up the fuel per km, and counts only where it sees all the
# fuel. If burning just the fuel it saw would be under this share of the
# class's normal L/100 km, it sees part of it -- one of two tanks, or a wrong
# calibration (June 2026: ten trucks' sensors saw 33-51% of the fuel bought,
# while that fuel matched their distance).
SENSOR_FLOOR = 0.5
SENSOR_DEAD_SHARE = 0.05  # no fill seen and under this share of the card burned over a real distance: not working
MAX_KM_PER_DAY = 1500     # more IM distance than this a day is a GPS glitch (Aug 2026: one truck "drove" 18,563,796 km in a month)
REPORT_TAIL_MIN = 30      # a card fill this close to a report's end may not be in it yet
LONG_REPORT_DAYS = 20     # an IM report this long can stand in for GPS km on its own
# Reading GPS.
MIN_STEP_M = 30           # GPS jitter below this is not movement
MAX_SPEED_KMH = 150       # faster than this between two points is a GPS jump
CHUNK_DAYS = 31           # track logs are read this many days at a time (a month is one ~4s call)

# Vehicle classes. `normal` and `theft` are L/100 km: above `normal` is
# Suspicious, above `theft` is Theft likely. `idle` is liters an hour allowed
# for idling (air-conditioning, reefer units) out of IM's idle hours. `tank` is
# the most one fill can hold, `big` the size from which a fill is large, and
# `min_l` the smallest fill worth questioning.
CLASSES = {
	"Heavy truck": {"normal": 55, "theft": 70, "idle": 3.0, "tank": 1200, "big": 400, "min_l": 100},
	"Medium truck": {"normal": 30, "theft": 42, "idle": 2.0, "tank": 400, "big": 150, "min_l": 50},
	"Light vehicle": {"normal": 16, "theft": 24, "idle": 1.0, "tank": 150, "big": 70, "min_l": 30},
}
DEFAULT_CLASS = "Heavy truck"   # this report is about trucks
# Brand + model words that make a vehicle lighter than a heavy truck.
CLASS_WORDS = (
	("Light vehicle", re.compile(
		r"(?<![a-z])(d-?max|hilux|pick-? ?up|navara|ranger|l200|fortuner|forteshner|staria|starex|hiace|urvan|"
		r"sprinter|van|master|amigo|land ?cruiser|prado|sedan|corolla|camry|yaris|accent|elantra|sunny|alliance)(?![a-z])")),
	("Medium truck", re.compile(
		r"(?<![a-z])(canter|dyna|dynna|npr|nqr|nkr|elf|frizer|freezer|hino(?![ -]?7\d\d)|coaster)(?![a-z])")),
)

CARD, FILL_DRAIN, CONSUMPTION = "card", "fill_drain", "consumption"
KIND_LABEL = {CARD: "fuel-card export", FILL_DRAIN: "IM Fill-Drain report", CONSUMPTION: "IM Fuel Consumption report"}

# Every sentence the analysis can say. Stored as [code, *args] and put into
# words when read, so each reader gets their own language.
MESSAGES = {
	# one fill
	"odo_back_fill": "Odometer went backwards",
	"quick_fill": "Refilled {0} h after the last fill",
	"over_tank_fill": "{0} L in one fill is more than the tanks hold ({1}: up to {2} L)",
	"big_fill": "{0} L is {1}x this truck's usual fill",
	"no_move_fill": "Refuelled after only {0} km",
	"trip_fill": "One of {0} fills that bought more than the truck could burn and hold",
	# one truck
	"over_tank": "{0} fill(s) bigger than the tanks ({1}: up to {2} L)",
	"missing": "Paid for {0} L, the tank sensor saw {1} L arrive: {2} L missing",
	"drain": "Tank sensor saw {0} L drained out ({1} time(s))",
	"small_drain": "Tank sensor saw {0} L drained out",
	"slow_drain": "Tank sensor saw {0} L slowly siphoned",
	"sensor_dead": "The tank sensor showed no fuel coming in or going out while the truck drove, so it is not working and was not used",
	"sensor_low": "The tank sensor saw only {0} of the {1} L paid for, which would mean {2} L/100 km, too little for a {3}: it sees part of the fuel (one of two tanks, or its calibration), so it was not used",
	"sensor_more": "The tank sensor saw {0} L arrive but the card paid for only {1} L: the truck also fills up elsewhere",
	"burn_theft": "Burns {0} L/100 km by {1}; the most a {2} can burn is {3}",
	"burn_high": "Burns {0} L/100 km by {1}; normal for a {2} is up to {3}",
	"peers": "{0}% more fuel per km than similar trucks",
	"odo_gap": "Typed odometer is {0}% off IM's distance",
	"odo_back": "Odometer went backwards {0} time(s)",
	"no_move": "{0} fill(s) after almost no driving",
	"quick": "{0} refill(s) within {1} h",
	"big": "{0} unusually large fill(s)",
	"gps_off": "The GPS track is missing large parts of the period, so its distance was not used",
	"gps_failed": "GPS read failed: {0}",
	"im_km_bad": "IM's distance for this truck is impossible ({0} km in {1} days), so it was not used",
	"gps_doubt": "The tracker looks faulty (IM's own distance for it is impossible): its GPS reads {0} km but the typed odometer reads {1} km, so the odometer was used",
	"trip_over": "{0} fills from {1} to {2} bought {3} L over {4} km by {5}: a {6} burns at most {7} L on that and its tanks hold {8} L, so at least {9} L did not go into it",
	# one source's own L/100 km on the truck's page: why it did not decide the status
	"src_one": "Only one fill, so no distance between fills",
	"src_no_odo": "No odometer typed at the pump",
	"src_back": "The odometer went backwards {0} time(s), so it was not used",
	"src_short": "Under {0} km: too short to judge",
	"src_no_gps": "No GPS data for these dates",
	"src_gaps": "The tracker was off at {0} of {1} fills, so this distance misses trips",
	"src_doubt": "The tracker looks faulty and the typed odometer disagrees with it",
	"src_gaps_im": "The tracker was off at {0} of {1} fills and the typed odometer does not back this distance",
	"src_bad": "IM's distance ({0} km) is impossible, so it was set aside",
	"src_missing": "This truck is not in this report",
	"src_zero": "This report shows no distance for this truck",
	"src_none_im": "Not on IM",
	"src_none_report": "This report was not uploaded",
	"src_stale": "Re-analyse the report to see this",
	"src_few": "Fewer than two card fills in its dates",
	"src_days": "Covers under {0} days, too short to use on its own",
	# the report
	"fleet_failed": "IM fleet could not be read: {0}",
	"report_unused": "{0} covers {1}, but the fuel card has no fills in those dates, so it was not used. Export it for {2}.",
	"report_later": "{0} covers {1}, but the fuel card has no fills in those dates, so it will not be used. Export it for {2}.",
	"report_used": "{0} {1}: {2} of its {3} vehicles are trucks in the fuel card; {4} card fills fall in its dates.",
}
WARN_NOTES = {"fleet_failed", "report_unused"}
# Arguments that are words to translate rather than numbers or names.
WORDS = {"Heavy truck", "Medium truck", "Light vehicle", "GPS km", "IM km", "the typed odometer",
         "fuel-card export", "IM Fill-Drain report", "IM Fuel Consumption report"}


def _fmt(value) -> str:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		return str(value)
	v = float(value)
	if abs(v) >= 100:
		return f"{round(v):,}"
	return str(int(v)) if v.is_integer() else f"{v:.1f}"


def _say(item, translate: bool = True) -> str:
	"""One stored finding, flag or note -- [code, *args] -- as a sentence."""
	code, args = item[0], list(item[1:])
	template = MESSAGES.get(code)
	if not template:
		return ""
	tr = _ if translate else (lambda text: text)
	return tr(template).format(*[tr(a) if isinstance(a, str) and a in WORDS else _fmt(a) for a in args])


LANGS = ("en", "ar")


def _use_lang(lang: str) -> None:
	"""Answer in the dashboard's language for this request only."""
	if lang in LANGS:
		frappe.local.lang = lang


def _load(text) -> list:
	try:
		value = json.loads(text or "[]")
	except ValueError:
		return []
	return value if isinstance(value, list) else []


FILL_FIELDS = [
	"fuel_import", "fill_time", "plate", "plate_key", "brand", "model", "branch", "driver",
	"flags", "flag_data", "invoice_no", "liters", "price", "cost", "odometer",
	"provider_kmpl", "station", "station_branch", "station_area", "station_lat", "station_lng",
	"source", "imei", "gps_status", "hours_since_prev", "gps_km_since_prev", "odometer_km_since_prev",
]
VEHICLE_FIELDS = [
	"fuel_import", "plate", "plate_key", "verdict", "score", "brand", "model", "vehicle_class", "branch", "drivers",
	"source", "imei", "platform_name", "checked_with", "fills", "liters", "cost", "first_fill", "last_fill",
	"gps_km", "im_km", "lp100", "lp100_basis", "normal_lp100", "theft_lp100", "peer_lp100", "excess_pct",
	"idle_hours", "idle_allowance_l", "odometer_km", "odometer_gap_pct", "gps_points", "window_liters",
	"has_sensor", "sensor_card_l", "sensor_fill_l", "sensor_fills", "missing_l", "drain_l", "drains",
	"slow_drain_l", "sensor_consumed_l", "sensor_lp100", "start_level", "last_level",
	"no_gps_fills", "no_move_fills", "quick_refills", "big_fills", "over_tank_fills",
	"liters_at_risk", "cost_at_risk", "issues", "finding_data", "by_report",
]


def vehicle_class(brand, model) -> str:
	text = f"{brand or ''} {model or ''}".lower()
	for name, pattern in CLASS_WORDS:
		if pattern.search(text):
			return name
	return DEFAULT_CLASS


# --------------------------------------------------------------------------
# Reading the files
# --------------------------------------------------------------------------

# Header text (lowercased, spaces collapsed) -> field. Exact matches only, so
# "Cost" never picks up "cost_before_vat".
HEADERS = {
	"invoice_no": ("invoice number", "invoice no", "invoice"),
	"branch": ("branch",),
	"plate": ("vehicle", "plate", "plate number", "vehicle number", "vehicle no"),
	"brand": ("vehicle brand", "brand"),
	"model": ("vehicle model", "model"),
	"price": ("fuel price", "price"),
	"cost": ("cost", "total cost", "amount"),
	"provider_kmpl": ("km/l",),
	"liters": ("number of liters", "liters", "litres", "quantity"),
	"odometer": ("odometer",),
	"driver": ("delegate", "driver"),
	"station": ("the station", "station"),
	"station_branch": ("station branch",),
	"station_area": ("governorate - city - distric", "governorate - city - district", "city"),
	"station_location": ("station location",),
	"fill_time": ("date", "date and time", "transaction date"),
}
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %I:%M:%S %p", "%Y-%m-%d")


def _header(value) -> str:
	return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _num(value):
	if value is None:
		return None
	if isinstance(value, (int, float)):
		return float(value)
	text = re.sub(r"[^\d.\-]", "", str(value))
	try:
		return float(text) if text not in ("", "-", ".") else None
	except ValueError:
		return None


def _when(value):
	if isinstance(value, datetime):
		return value.replace(tzinfo=None)
	text = str(value or "").strip()
	for fmt in DATE_FORMATS:
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			continue
	return None


def _latlng(value):
	parts = re.findall(r"-?\d+(?:\.\d+)?", str(value or ""))
	if len(parts) < 2:
		return None, None
	lat, lng = float(parts[0]), float(parts[1])
	if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (not lat and not lng):
		return None, None
	return lat, lng


def _sheet_rows(path: str, limit: int | None = None) -> list:
	"""The first sheet's rows. Read-only with the stored dimensions ignored:
	IM's exports carry a wrong one, which would cut the sheet short."""
	import openpyxl

	wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
	try:
		ws = wb.worksheets[0]
		ws.reset_dimensions()
		rows = []
		for i, row in enumerate(ws.iter_rows(values_only=True)):
			if limit and i >= limit:
				break
			rows.append(tuple(row))
		return rows
	finally:
		wb.close()


def _card_columns(row) -> dict | None:
	names = [_header(c) for c in row]
	found = {}
	for key, options in HEADERS.items():
		for option in options:
			if option in names:
				found[key] = names.index(option)
				break
	return found if {"plate", "liters", "fill_time"} <= set(found) else None


def _kind_of(rows: list) -> str:
	cells = {_header(c) for r in rows[:25] for c in r if isinstance(c, str) and c.strip()}
	if {"vehicle", "fill", "drain"} <= cells:
		return FILL_DRAIN
	if "vehicle" in cells and "distance" in cells and ("working duration" in cells or "fuel consumption" in cells):
		return CONSUMPTION
	if any(_card_columns(r) for r in rows[:21]):
		return CARD
	return ""


def classify_file(path: str) -> str:
	"""'card', 'fill_drain', 'consumption', or '' for anything else."""
	return _kind_of(_sheet_rows(path, limit=25))


def parse_workbook(path: str) -> tuple[list[dict], int]:
	"""The fuel-card export: (fills, rows skipped). Finds the header row itself,
	so a title line or two above it do not matter; stops at the totals rows at
	the bottom because those have no plate or no date."""
	col, fills, skipped = None, [], 0
	for i, row in enumerate(_sheet_rows(path)):
		if col is None:
			if i > 20:
				break
			col = _card_columns(row)
			continue

		def cell(key):
			j = col.get(key)
			return row[j] if j is not None and j < len(row) else None

		plate = str(cell("plate") or "").strip()
		when = _when(cell("fill_time"))
		liters = flt(_num(cell("liters")))
		if not plate or not when or liters <= 0:
			skipped += 1
			continue
		lat, lng = _latlng(cell("station_location"))
		fills.append({
			"invoice_no": str(cell("invoice_no") or "").strip()[:140],
			"branch": str(cell("branch") or "").strip()[:140],
			"plate": plate[:140],
			"plate_key": plate_key(plate),
			"brand": str(cell("brand") or "").strip()[:140],
			"model": str(cell("model") or "").strip()[:140],
			"driver": str(cell("driver") or "").strip()[:140],
			"liters": liters,
			"price": flt(_num(cell("price"))),
			"cost": flt(_num(cell("cost"))),
			"odometer": cint(_num(cell("odometer")) or 0),
			"provider_kmpl": flt(_num(cell("provider_kmpl"))),
			"station": str(cell("station") or "").strip()[:140],
			"station_branch": str(cell("station_branch") or "").strip()[:140],
			"station_area": str(cell("station_area") or "").strip()[:140],
			"station_lat": lat,
			"station_lng": lng,
			"fill_time": when,
		})

	if col is None:
		frappe.throw(
			_("This does not look like a fuel-card export: no header row with Vehicle, Number of liters and Date."),
			title=_(TITLE),
		)
	return fills, skipped


# IM report header (top header, then " / " and the sub-header under a merged
# group) -> field.
IM_COLUMNS = {
	"vehicle": ("vehicle",),
	"company": ("company",),
	"brand": ("vehicle brand",),
	"model": ("vehicle model",),
	"km": ("distance",),
	"running_h": ("running",),
	"idle_h": ("idle",),
	"working_h": ("working duration",),
	"consumed_l": ("fuel consumed / total",),
	"sensor_lp100": ("avg consumption / 100",),
	"fill": ("fill",),
	"drain": ("drain",),
	"slow_drain": ("slow drain",),
	"start_level": ("start fuel level",),
	"last_level": ("last fuel level",),
	"sensor_brand": ("sensor brand",),
}
IM_PERIOD = re.compile(
	r"(\d{1,2}-\d{1,2}-\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)\s+to\s+(\d{1,2}-\d{1,2}-\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)",
	re.I,
)
KM_UNITS = ("km", "kms", "kilometer", "kilometers", "kilometre", "kilometres")
LITER_UNITS = ("l", "ltr", "ltrs", "liter", "liters", "litre", "litres")


def _amount(value) -> tuple[float, int]:
	"""IM's '154.00(1)': 154 liters over 1 event. A bare number has no count."""
	m = re.match(r"\s*(-?\d[\d,]*(?:\.\d+)?)\s*(?:\((\d+)\))?", str(value if value is not None else ""))
	if not m:
		return 0.0, 0
	return flt(m.group(1).replace(",", "")), cint(m.group(2) or 0)


def _hours(value) -> float:
	"""IM durations -- '04:36', '856:15:53' -- as hours. IM writes '0.0' for none."""
	if isinstance(value, timedelta):
		return value.total_seconds() / 3600
	if isinstance(value, time_of_day):
		return value.hour + value.minute / 60 + value.second / 3600
	text = str(value or "").strip()
	if ":" in text:
		parts = [cint(p) for p in text.split(":")[:3]]
		parts += [0] * (3 - len(parts))
		return parts[0] + parts[1] / 60 + parts[2] / 3600
	return flt(_num(text))


def _im_time(text: str):
	text = re.sub(r"\s+", " ", text).strip().upper()
	text = re.sub(r"(\d)([AP]M)$", r"\1 \2", text)
	for fmt in ("%d-%m-%Y %I:%M %p", "%d-%m-%Y %I:%M:%S %p"):
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			continue
	return None


def _im_period(rows: list):
	"""The 'dd-mm-yyyy hh:mm AM to dd-mm-yyyy hh:mm PM' line under the title."""
	for r in rows[:6]:
		for c in r:
			m = IM_PERIOD.search(str(c or ""))
			if not m:
				continue
			start, end = _im_time(m.group(1)), _im_time(m.group(2))
			if start and end:
				# "to 11:59 PM" means to the end of that minute.
				return start, (end + timedelta(seconds=59) if end.second == 0 else end)
	return None, None


def _im_units(rows: list) -> None:
	for r in rows[:30]:
		if not r:
			continue
		label = _header(r[0])
		value = next((_header(c) for c in r[1:3] if str(c or "").strip()), "")
		if label == "distance unit" and value and value not in KM_UNITS:
			frappe.throw(_("This IM report measures distance in {0}. Export it in km.").format(value), title=_(TITLE))
		if label == "fuel consumption unit" and value and value not in LITER_UNITS:
			frappe.throw(_("This IM report measures fuel in {0}. Export it in liters.").format(value), title=_(TITLE))


def parse_im_report(path: str, expect: str = "") -> dict:
	"""An IM Fill-Drain or Fuel Consumption export: its dates and one row per vehicle."""
	rows = _sheet_rows(path)
	kind = _kind_of(rows)
	if kind not in (FILL_DRAIN, CONSUMPTION) or (expect and kind != expect):
		frappe.throw(_("This is not an {0}.").format(_(KIND_LABEL[expect or FILL_DRAIN])), title=_(TITLE))
	start, end = _im_period(rows)
	if not start:
		frappe.throw(_("This {0} has no dates line ('… to …') under its title.").format(_(KIND_LABEL[kind])), title=_(TITLE))
	_im_units(rows)

	head_i = next((i for i, r in enumerate(rows[:25]) if "vehicle" in [_header(c) for c in r]), None)
	top = [_header(c) for c in rows[head_i]]
	v_col = top.index("vehicle")
	# The block of totals on the left repeats names like "Distance"; the
	# vehicle table starts at Company.
	first_col = top.index("company") if "company" in top else max(0, v_col - 2)
	below = rows[head_i + 1] if head_i + 1 < len(rows) else ()
	has_sub = v_col >= len(below) or not str(below[v_col] or "").strip()
	sub = [_header(c) for c in below] if has_sub else []

	names, group = [], ""
	for j in range(max(len(top), len(sub))):
		t = top[j] if j < len(top) else ""
		s = sub[j] if j < len(sub) else ""
		if t:
			group = t
		if j < first_col:
			names.append("")
			continue
		names.append(f"{group} / {s}" if s and group else (s or t))

	col = {}
	for key, options in IM_COLUMNS.items():
		for option in options:
			if option in names:
				col[key] = names.index(option)
				break
	if "km" not in col:
		frappe.throw(_("This {0} has no Distance column.").format(_(KIND_LABEL[kind])), title=_(TITLE))

	days = max((end - start).total_seconds() / 86400, 1)
	out = []
	for r in rows[head_i + (2 if has_sub else 1):]:
		def cell(key):
			j = col.get(key)
			return r[j] if j is not None and j < len(r) else None

		vehicle = re.sub(r"\s+", " ", str(cell("vehicle") or "")).strip()
		if not vehicle:
			continue
		km = flt(_num(cell("km")))
		# IM's own distance is sometimes a GPS glitch no truck could drive;
		# such a row keeps its other numbers but its distance is set aside.
		km_bad = round(km) if km > days * MAX_KM_PER_DAY else None
		fill_l, fills = _amount(cell("fill"))
		drain_l, drains = _amount(cell("drain"))
		slow_l, slows = _amount(cell("slow_drain"))
		consumed = flt(_num(cell("consumed_l")))
		start_level, last_level = flt(_num(cell("start_level"))), flt(_num(cell("last_level")))
		running, idle = _hours(cell("running_h")), _hours(cell("idle_h"))
		sensor_brand = str(cell("sensor_brand") or "").strip()
		out.append({
			"vehicle": vehicle,
			"company": str(cell("company") or "").strip(),
			"brand": str(cell("brand") or "").strip(),
			"model": str(cell("model") or "").strip(),
			"km": 0.0 if km_bad else km,
			"km_bad": km_bad,
			"running_h": running,
			"idle_h": idle,
			"working_h": _hours(cell("working_h")) if "working_h" in col else running + idle,
			"fill_l": fill_l,
			"fills": fills,
			"drain_l": drain_l,
			"drains": drains,
			"slow_drain_l": slow_l,
			"slow_drains": slows,
			"consumed_l": consumed,
			"sensor_lp100": flt(_num(cell("sensor_lp100"))),
			"start_level": start_level,
			"last_level": last_level,
			# No sensor shows as '--' and zeros everywhere; a sensor that is
			# there reads a level even on a day the truck stood still. Drains
			# alone prove nothing: IM's Aug 2026 exports put the same 21.2 L
			# slow drain on trucks with no sensor at all.
			"has_sensor": kind == FILL_DRAIN and bool(
				sensor_brand not in ("", "--") or fill_l or consumed or start_level or last_level),
		})
	return {"kind": kind, "start": start, "end": end, "days": days, "rows": out}


def _inside(fills: list, report: dict) -> list:
	"""The card fills an IM report's totals include."""
	end = report["end"] - timedelta(minutes=REPORT_TAIL_MIN)
	return [f for f in fills if report["start"] <= f["fill_time"] <= end]


def _period_text(a: datetime, b: datetime, dates_only: bool = False) -> str:
	"""Dates the way IM writes them (dd-mm-yyyy), the same in every language."""
	if dates_only:
		return f"{a:%d-%m-%Y} – {b:%d-%m-%Y}"
	if a.date() == b.date():
		return f"{a:%d-%m-%Y} {a:%H:%M}–{b:%H:%M}"
	return f"{a:%d-%m-%Y %H:%M} – {b:%d-%m-%Y %H:%M}"


def _path(file_url: str) -> str:
	return frappe.get_doc("File", {"file_url": file_url}).get_full_path()


# --------------------------------------------------------------------------
# Plates and the IM fleet
# --------------------------------------------------------------------------

# Saudi plate letters, Arabic -> the Latin letter printed beside it.
AR2EN = {
	"ا": "A", "أ": "A", "إ": "A", "آ": "A", "ب": "B", "ح": "J", "د": "D", "ر": "R", "س": "S",
	"ص": "X", "ط": "T", "ع": "E", "ق": "G", "ك": "K", "ل": "L", "م": "Z", "ن": "N", "ه": "H",
	"و": "U", "ى": "V", "ي": "V",
}
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
PLATE_SHAPE = re.compile(r"\d{1,4}-[A-Z]{1,3}")


def plate_key(text, reverse: bool = False) -> str:
	"""'N R A-3 9 6 1', 'أ ن ر 3961' and '3961 NRA' -> '3961-NRA'.

	`reverse` flips the letters: platforms disagree on whether Arabic letters
	are written in reading order or in the order they are painted."""
	s = str(text or "").translate(AR_DIGITS).upper()
	digits = "".join(re.findall(r"\d", s))
	letters = "".join(AR2EN.get(ch, ch) for ch in re.findall(r"[A-Z؀-ۿ]", s))
	letters = "".join(ch for ch in letters if "A" <= ch <= "Z")
	if not digits or not letters:
		return ""
	return f"{digits}-{letters[::-1] if reverse else letters}"


def _norm(text) -> str:
	return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


class Fleet:
	"""The IM fleet, findable by plate and by the vehicle names IM prints on
	its reports ('7983 JEA - 7983 JEA' is the vehicle number, a dash, the name)."""

	def __init__(self):
		from app_apis import im_connector as im

		self.fwd, self.rev, self.names, self.notes = {}, {}, {}, []
		res = im.fetch_fleet()
		if cint(res.get("code")) == -429:
			# IM allows about one fleet read a minute. A run started right after
			# another waits its turn rather than losing every IM match.
			time.sleep(65)
			res = im.fetch_fleet()
		if cint(res.get("code")) != 0:
			self.notes.append(["fleet_failed", str(res.get("msg") or "")])
			return
		for v in res.get("rows") or []:
			vehicle_no = str(v.get("vehicle_no") or v.get("vehicle_name") or "").strip()
			imei = str(v.get("imei") or "").strip()
			if not vehicle_no:
				continue
			entry = {"source": "IM", "imei": imei, "vehicle_no": vehicle_no,
			         "name": str(v.get("vehicle_name") or vehicle_no), "company": _norm(v.get("company"))}
			for field in ("vehicle_no", "vehicle_name"):
				k_f, k_r = plate_key(v.get(field)), plate_key(v.get(field), True)
				if k_f:
					self.fwd.setdefault(k_f, entry)
				if k_r:
					self.rev.setdefault(k_r, entry)
			no, name = _norm(v.get("vehicle_no")), _norm(v.get("vehicle_name"))
			for text in {no, name, f"{no} - {name}" if no and name else ""}:
				if text:
					self.names.setdefault(text, []).append(entry)

	def by_plate(self, key: str) -> dict:
		return self.fwd.get(key) or self.rev.get(key) or {}

	def by_report_name(self, text: str, company: str = "") -> dict:
		"""The fleet entry an IM report row is about, or {} when unsure."""
		text, company = _norm(text), _norm(company)
		cuts = [m.start() for m in re.finditer(" - ", text)]
		candidates = [text] + [text[:i] for i in cuts] + [text[i + 3:] for i in cuts]
		for candidate in candidates:
			entries = self.names.get(candidate.strip()) or []
			if company:
				entries = [e for e in entries if e["company"] == company] or entries
			found = {e["imei"]: e for e in entries}
			if len(found) == 1:
				return next(iter(found.values()))
		return {}


def _index_report(report: dict, fleet: Fleet) -> dict:
	"""IM report rows by 'imei:<imei>' and by 'plate:<key>' -- the plate the
	name spells, for when the fleet list and the report disagree on the name."""
	index = {}
	for row in report["rows"]:
		entry = fleet.by_report_name(row["vehicle"], row["company"])
		if entry.get("imei"):
			index.setdefault("imei:" + entry["imei"], row)
		for part in [row["vehicle"]] + row["vehicle"].split(" - "):
			keys = {k for k in (plate_key(part), plate_key(part, True)) if PLATE_SHAPE.fullmatch(k or "")}
			if keys:
				for k in keys:
					index.setdefault("plate:" + k, row)
				break
	return index


def _report_row(index: dict | None, entry: dict, key: str) -> dict | None:
	if not index:
		return None
	return (index.get("imei:" + entry["imei"]) if entry.get("imei") else None) or index.get("plate:" + key)


# --------------------------------------------------------------------------
# GPS
# --------------------------------------------------------------------------

TRACK_FORMATS = ("%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                 "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S")


def _track_time(text, tz) -> float | None:
	raw = str(text or "").strip()
	for fmt in TRACK_FORMATS:
		try:
			return datetime.strptime(raw, fmt).replace(tzinfo=tz).timestamp()
		except ValueError:
			continue
	return None


def _first_rows(node, depth: int = 0):
	if depth > 4:
		return None
	if isinstance(node, list):
		return node if node and isinstance(node[0], dict) else None
	if isinstance(node, dict):
		for value in node.values():
			found = _first_rows(value, depth + 1)
			if found:
				return found
	return None


def _im_track(im, settings: dict, vehicle_no: str, start: datetime, stop: datetime, attempt: int = 0):
	"""(rows, error). IM answers "nothing in this window" as an error string,
	which is treated as no points; only transport failures are errors."""
	token, _source, tmeta = im._get_token(settings, force=attempt == 1)
	if not token:
		return None, str(tmeta.get("error", (0, "IM sign-in failed"))[1])

	body = {"vehicle_no": vehicle_no, "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
	        "end_date": stop.strftime("%Y-%m-%d %H:%M:%S")}
	decoded, meta = im._post(im.CMD_TRACK, settings, body, headers={"auth-code": token})
	if decoded is None:
		code, msg = meta.get("error", (-500, "IM call failed"))
		if code == -401 and attempt == 0:
			return _im_track(im, settings, vehicle_no, start, stop, 1)
		if code == -429 and attempt < 2:
			time.sleep(65)
			return _im_track(im, settings, vehicle_no, start, stop, 2)
		return None, str(msg)

	root = decoded.get("root") if isinstance(decoded.get("root"), dict) else decoded
	if isinstance(root, dict) and root.get("error"):
		if "limit" in str(root["error"]).lower() and attempt < 2:
			time.sleep(65)
			return _im_track(im, settings, vehicle_no, start, stop, 2)
		return [], ""
	return _first_rows(root) or [], ""


def _im_points(vehicle_no: str, start: datetime, end: datetime, tz):
	"""[(epoch, lat, lng, ignition_on)] sorted, or (None, error)."""
	from app_apis import im_connector as im

	settings = im._settings()
	points, cursor = [], start
	while cursor < end:
		stop = min(end, cursor + timedelta(days=CHUNK_DAYS))
		rows, error = _im_track(im, settings, vehicle_no, cursor, stop)
		if rows is None:
			return None, error
		for r in rows:
			ts = _track_time(r.get("timestamp"), tz)
			lat, lng = flt(r.get("latitude")), flt(r.get("longitude"))
			if ts and lat and lng:
				points.append((ts, lat, lng, str(r.get("ignition_status") or "").strip().lower() == "on"))
		cursor = stop
	points.sort()
	unique = [p for i, p in enumerate(points) if i == 0 or p[0] != points[i - 1][0]]
	return unique, ""


def _meters(a_lat, a_lng, b_lat, b_lng) -> float:
	p1, p2 = math.radians(a_lat), math.radians(b_lat)
	h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b_lng - a_lng) / 2) ** 2
	return 2 * 6371000.0 * math.asin(math.sqrt(h))


class Track:
	"""A truck's GPS points, with cumulative driven distance for fast lookups."""

	def __init__(self, points: list):
		self.points = points
		self.ts = [p[0] for p in points]
		self.cum, total = [0.0], 0.0
		for i in range(1, len(points)):
			t0, la0, lo0, ig0 = points[i - 1]
			t1, la1, lo1, ig1 = points[i]
			dt = t1 - t0
			if dt > 0:
				d = _meters(la0, lo0, la1, lo1)
				speed = d / dt * 3.6
				if d >= MIN_STEP_M and speed <= MAX_SPEED_KMH and (ig0 or ig1 or speed >= 8):
					total += d
			self.cum.append(total)

	def km_between(self, a: float, b: float) -> float:
		if not self.ts or b <= a:
			return 0.0
		i = max(bisect.bisect_right(self.ts, a) - 1, 0)
		j = bisect.bisect_right(self.ts, b) - 1
		return (self.cum[j] - self.cum[i]) / 1000.0 if j > i else 0.0

	def points_between(self, a: float, b: float) -> int:
		return bisect.bisect_right(self.ts, b) - bisect.bisect_left(self.ts, a)

	def covered(self, t: float) -> bool:
		"""Whether the tracker sent anything within COVERAGE_MIN of time t --
		only to know if its distance can be trusted; where it was is not used."""
		i = bisect.bisect_left(self.ts, t - COVERAGE_MIN * 60)
		return i < len(self.ts) and self.ts[i] <= t + COVERAGE_MIN * 60


# --------------------------------------------------------------------------
# Checking one truck
# --------------------------------------------------------------------------


def _check_truck(fills: list, track: "Track | None", tz, cls: str) -> dict:
	"""Fill-level findings written onto each fill dict, truck-level numbers returned."""
	limits = CLASSES[cls]
	fills.sort(key=lambda f: f["fill_time"])
	median_fill = statistics.median([f["liters"] for f in fills]) if fills else 0
	counts = {"no_gps": 0, "no_move": 0, "quick": 0, "big": 0, "over_tank": 0, "odo_back": 0}
	prev = None

	# Every fill-level finding is a note: the truck's status comes from its
	# fuel against its distance over all its fills (_verdict).
	for f in fills:
		t = f["fill_time"].replace(tzinfo=tz).timestamp()
		f["_t"] = t
		flags = []
		f.update({"hours_since_prev": None, "gps_km_since_prev": None, "odometer_km_since_prev": None,
		          "gps_status": "Not on IM"})

		if prev:
			hours = (t - prev["_t"]) / 3600
			f["hours_since_prev"] = round(hours, 1)
			if f["odometer"] and prev["odometer"]:
				f["odometer_km_since_prev"] = f["odometer"] - prev["odometer"]
				if f["odometer_km_since_prev"] < 0:
					flags.append(["odo_back_fill"])
					counts["odo_back"] += 1
			if hours < QUICK_REFILL_H and f["liters"] >= limits["min_l"]:
				flags.append(["quick_fill", round(hours, 1)])
				counts["quick"] += 1

		if f["liters"] > limits["tank"]:
			flags.append(["over_tank_fill", round(f["liters"]), cls, limits["tank"]])
			counts["over_tank"] += 1
		elif median_fill and f["liters"] > BIG_FILL_FACTOR * median_fill and f["liters"] >= limits["big"]:
			flags.append(["big_fill", round(f["liters"]), round(f["liters"] / median_fill, 1)])
			counts["big"] += 1

		if track is not None:
			if track.covered(t):
				f["gps_status"] = "GPS at fill time"
			else:
				f["gps_status"] = "No GPS at fill time"
				counts["no_gps"] += 1
			if prev:
				km = track.km_between(prev["_t"], t)
				f["gps_km_since_prev"] = round(km, 1)
				covered = track.points_between(prev["_t"], t) > 0
				f["_gps_leg_ok"] = covered
				if covered and km < NO_MOVE_KM and f["liters"] >= limits["min_l"]:
					flags.append(["no_move_fill", round(km, 1)])
					counts["no_move"] += 1

		f["flag_data"] = json.dumps(flags)
		f["flags"] = "; ".join(_say(x, translate=False) for x in flags)
		prev = f

	truck = {"counts": counts, "gps_km": None, "consumed": sum(f["liters"] for f in fills[1:]),
	         "odometer_km": None, "first": fills[0]["fill_time"], "last": fills[-1]["fill_time"],
	         "gps_gaps": track is not None and counts["no_gps"] >= GPS_GAP_MIN_FILLS
	                     and counts["no_gps"] / len(fills) >= GPS_GAP_SHARE}
	if track is not None and len(fills) >= 2:
		truck["gps_km"] = round(track.km_between(fills[0]["_t"], fills[-1]["_t"]), 1)
	# The typed odometer, from the first fill with a reading to the last, and
	# the liters bought after the first of them.
	typed = [i for i, f in enumerate(fills) if f["odometer"]]
	if len(typed) >= 2 and fills[typed[-1]]["odometer"] > fills[typed[0]]["odometer"]:
		a, b = fills[typed[0]], fills[typed[-1]]
		truck["odometer_km"] = b["odometer"] - a["odometer"]
		truck["odo"] = {"from": a["odometer"], "to": b["odometer"], "first": a["fill_time"], "last": b["fill_time"],
		                "liters": sum(f["liters"] for f in fills[typed[0] + 1:typed[-1] + 1]),
		                "fills": typed[-1] - typed[0]}
	return truck


def _apply_reports(t: dict, fills: list, reports: dict, rows: dict) -> None:
	"""What IM's reports add to a truck: distance and idling, and the tank sensor."""
	# Distance from the Fuel Consumption report first, else the Fill-Drain
	# one -- whichever has a distance a truck could actually have driven.
	candidates = [k for k in (CONSUMPTION, FILL_DRAIN) if rows.get(k)]
	good = [k for k in candidates if not rows[k].get("km_bad")]
	kind = good[0] if good else None
	if candidates and not good:
		k = candidates[0]
		t["_im_km_bad"] = [rows[k]["km_bad"], round(reports[k]["days"])]
	if kind:
		report, row = reports[kind], rows[kind]
		inside = _inside(fills, report)
		t["im_km"] = round(row["km"], 1)
		t["idle_hours"] = round(row["idle_h"], 1)
		t["window_liters"] = round(sum(f["liters"] for f in inside), 1)
		t["_im"] = {"kind": kind, "report": report, "row": row, "inside": inside}
	# Each report on its own as well, for the truck's page.
	t["_reports"] = {k: {"kind": k, "report": reports[k], "row": rows.get(k), "inside": _inside(fills, reports[k])}
	                 for k in (FILL_DRAIN, CONSUMPTION) if k in reports}

	fd = rows.get(FILL_DRAIN)
	if fd and fd["has_sensor"]:
		card_l = sum(f["liters"] for f in _inside(fills, reports[FILL_DRAIN]))
		t.update({
			"has_sensor": 1,
			"sensor_card_l": round(card_l, 1),
			"sensor_fill_l": round(fd["fill_l"], 1),
			"sensor_fills": fd["fills"],
			"missing_l": round(card_l - fd["fill_l"], 1),
			"drain_l": round(fd["drain_l"], 1),
			"drains": fd["drains"],
			"slow_drain_l": round(fd["slow_drain_l"], 1),
			"sensor_consumed_l": round(fd["consumed_l"], 1),
			"sensor_lp100": round(fd["sensor_lp100"], 1) if fd["sensor_lp100"] and fd["km"] >= SHOW_KM_FOR_RATE else None,
			"start_level": round(fd["start_level"], 1),
			"last_level": round(fd["last_level"], 1),
		})


def _typed_km(fills: list) -> float:
	"""The typed odometer's distance from the first of these fills to the last."""
	odos = [f["odometer"] for f in fills if f["odometer"]]
	return odos[-1] - odos[0] if len(odos) >= 2 and odos[-1] > odos[0] else 0


def _agree(typed_km: float, km: float) -> bool:
	"""The typed odometer backs up a distance: within ODOMETER_GAP of it."""
	return bool(typed_km and km) and abs(typed_km - km) / km * 100 < ODOMETER_GAP


def _own_limits(doc) -> dict | None:
	"""A report's own normal and theft limits for heavy trucks, when it has them."""
	normal, theft = flt(doc.get("normal_limit")), flt(doc.get("theft_limit"))
	return {"normal": normal, "theft": theft} if 0 < normal < theft else None


def _limits_from(normal, theft) -> dict:
	"""The limits typed in the upload dialog, checked; {} when none were given."""
	if normal in (None, "") and theft in (None, ""):
		return {}
	normal, theft = flt(normal), flt(theft)
	if not 0 < normal < theft:
		frappe.throw(_("The theft limit must be above the normal limit."), title=_(TITLE))
	return {"normal_limit": normal, "theft_limit": theft}


def _cls(t: dict) -> dict:
	"""The truck's class limits -- with the report's own normal and theft limits
	for heavy trucks, when it was given them."""
	cls = CLASSES[t["vehicle_class"]]
	own = t.get("_limits")
	return {**cls, **own} if own and t["vehicle_class"] == DEFAULT_CLASS else cls


def _idle_l(im: dict | None, cls: dict, a: datetime | None = None, b: datetime | None = None) -> float:
	"""Liters allowed for idling out of an IM report's idle hours: all of them,
	or the share of the report's dates that falls between a and b."""
	if not im or not im.get("row"):
		return 0.0
	report, row = im["report"], im["row"]
	if a is None:
		return row["idle_h"] * cls["idle"]
	whole = (report["end"] - report["start"]).total_seconds()
	shared = (min(report["end"], b) - max(report["start"], a)).total_seconds()
	return row["idle_h"] * shared / whole * cls["idle"] if whole > 0 and shared > 0 else 0.0


def _consumption(t: dict, info: dict) -> None:
	"""Every source's own L/100 km -- the typed odometer, the GPS track and each
	IM report, each taking what it lacks from the other files -- for the
	truck's page; the status's from the most exact one that holds up. Also the
	typed odometer against IM's distance."""
	cls = _cls(t)
	c = info["counts"]
	im = t.get("_im")
	if im and "kind" not in im:
		im = {**im, "kind": CONSUMPTION}
	reports = t.get("_reports") or ({im["kind"]: im} if im else {})
	on_im = bool(t.get("imei") or t.get("source"))
	fills = cint(t.get("fills"))
	odo = info.get("odo") or {}
	odo_km = info["odometer_km"] or 0
	gaps = bool(info.get("gps_gaps"))
	# A track with the tracker off around many fills is missing whole trips.
	gps_km = 0 if gaps else (info["gps_km"] or 0)
	# A tracker whose own distance is impossible can have its GPS wrong too, so
	# its GPS km stands only where the typed odometer does not contradict it.
	if (gps_km >= MIN_KM_FOR_RATE and t.get("_im_km_bad") and odo_km and not c["odo_back"]
	        and not _agree(odo_km, gps_km)):
		t["_gps_doubt"] = [round(gps_km), round(odo_km)]
		gps_km = 0

	def misses_trips(entry) -> bool:
		# IM's distance comes from the same tracker: with the tracker off for
		# whole trips it is short too, unless the typed odometer backs it up.
		return gaps and not _agree(_typed_km(entry["inside"]), entry["row"]["km"])

	ways = {}

	def way(src, a, b, km, liters, counted, allowance, why=None) -> dict:
		"""One source: the liters bought after its first fill, less idling, per 100 of its km."""
		allowance = min(allowance, liters)
		ways[src] = {
			"src": src, "dates": _period_text(a, b, dates_only=True) if a and b else "",
			"km": round(km, 1) if km else None, "liters": round(liters, 1), "fills": counted,
			"idle_l": round(allowance), "idle_h": round(allowance / cls["idle"], 1),
			"lp": round((liters - allowance) / km * 100, 1) if km and km >= SHOW_KM_FOR_RATE and counted else None,
			"used": 0, "why": why, "_raw": (km, liters, allowance),
		}
		return ways[src]

	# The typed odometer, over its own span, with IM's idle hours for it.
	a, b = odo.get("first", info["first"]), odo.get("last", info["last"])
	w = way("card", a, b, odo_km, odo.get("liters", info["consumed"]) if odo_km else 0,
	        odo.get("fills", max(fills - 1, 0)) if odo_km else 0, _idle_l(im, cls, a, b))
	if odo:
		w["odo"] = [odo["from"], odo["to"]]
	w["why"] = (["src_one"] if fills < 2 else ["src_back", c["odo_back"]] if c["odo_back"] else ["src_no_odo"] if not odo_km
	            else ["src_short", MIN_KM_FOR_RATE] if odo_km < MIN_KM_FOR_RATE else None)

	# The GPS track between the first fill and the last, with the card's liters.
	if info["gps_km"] is not None or on_im:
		w = way("gps", info["first"], info["last"], info["gps_km"] or 0, info["consumed"], max(fills - 1, 0),
		        _idle_l(im, cls, info["first"], info["last"]))
		w["why"] = (["src_one"] if fills < 2 else ["src_no_gps"] if info["gps_km"] is None
		            else ["src_gaps", c["no_gps"], fills] if gaps else ["src_doubt"] if t.get("_gps_doubt")
		            else ["src_short", MIN_KM_FOR_RATE] if info["gps_km"] < MIN_KM_FOR_RATE else None)

	# Each IM report over its own dates: its distance and idle hours, and the
	# card's liters inside those dates (its own fuel figures are not real).
	for kind in (FILL_DRAIN, CONSUMPTION):
		entry = reports.get(kind)
		if not entry or not (entry.get("row") or on_im):
			continue
		report, row, inside = entry["report"], entry.get("row"), entry["inside"]
		if not row:
			way(kind, report["start"], report["end"], None, 0, 0, 0, ["src_missing"])
			continue
		km = 0 if row.get("km_bad") else row["km"]
		w = way(kind, report["start"], report["end"], km, sum(f["liters"] for f in inside[1:]), max(len(inside) - 1, 0),
		        _idle_l(entry, cls))
		typed = [f["odometer"] for f in inside if f.get("odometer")]
		if len(typed) >= 2 and typed[-1] > typed[0]:
			w["odo"] = [typed[0], typed[-1]]
		if kind == FILL_DRAIN and row.get("has_sensor"):
			w["sensor_l"] = round(row["consumed_l"], 1)
			w["sensor_lp"] = round(row["sensor_lp100"], 1) if row["sensor_lp100"] and km >= SHOW_KM_FOR_RATE else None
		w["why"] = (["src_bad", row["km_bad"]] if row.get("km_bad") else ["src_zero"] if not km else ["src_few"] if len(inside) < 2
		            else ["src_gaps_im", c["no_gps"], fills] if misses_trips(entry)
		            else ["src_short", MIN_KM_FOR_RATE] if km < MIN_KM_FOR_RATE
		            else ["src_days", LONG_REPORT_DAYS] if (report["end"] - report["start"]).days < LONG_REPORT_DAYS else None)

	# The status: GPS, else the IM report, else the typed odometer.
	im_ok = im if im and not misses_trips(im) else None
	chosen, basis = None, ""
	if gps_km >= MIN_KM_FOR_RATE:
		chosen, basis = ways["gps"], "GPS"
	elif (im_ok and im_ok["row"]["km"] >= MIN_KM_FOR_RATE and len(im_ok["inside"]) >= 2
	      and (im_ok["report"]["end"] - im_ok["report"]["start"]).days >= LONG_REPORT_DAYS):
		# Liters after the first fill inside the report, over all of IM's km in
		# it: never more than the truck burned, so a flag here is earned.
		chosen, basis = ways[im_ok["kind"]], "IM report"
	elif odo_km >= MIN_KM_FOR_RATE and not c["odo_back"]:
		chosen, basis = ways["card"], "Odometer (card)"
	elif gps_km >= SHOW_KM_FOR_RATE:
		chosen, basis = ways["gps"], "GPS"

	t["normal_lp100"], t["theft_lp100"] = cls["normal"], cls["theft"]
	t["lp100_basis"] = basis
	t["_judged"] = bool(chosen) and chosen["_raw"][0] >= MIN_KM_FOR_RATE
	if chosen:
		km, liters, allowance = chosen["_raw"]
		t["lp100"] = chosen["lp"]
		t["idle_allowance_l"] = round(allowance) if allowance else None
		t["_excess_l"] = max(0.0, liters - allowance - km * cls["normal"] / 100)
		chosen["used"] = int(t["_judged"])

	gap = None
	if odo_km and gps_km >= MIN_KM_FOR_RATE:
		gap = (odo_km - gps_km) / gps_km * 100
	elif im_ok and im_ok["row"]["km"] >= MIN_KM_FOR_RATE:
		typed = _typed_km(im_ok["inside"])
		# The typed span lies inside the report's dates, so it can only be
		# shorter than IM's distance; longer is the finding.
		if typed > im_ok["row"]["km"]:
			gap = (typed - im_ok["row"]["km"]) / im_ok["row"]["km"] * 100
	t["odometer_gap_pct"] = round(gap) if gap is not None else None
	# The typed odometer's row says so when it is far off the distance used.
	card = ways["card"]
	if gap is not None and abs(gap) >= ODOMETER_GAP and not card["used"] and not card["why"]:
		card["why"] = ["odo_gap", f"+{round(gap)}" if gap > 0 else str(round(gap))]
	order = ("card", "gps", FILL_DRAIN, CONSUMPTION)
	t["by_report"] = json.dumps([{k: v for k, v in ways[s].items() if k != "_raw"} for s in order if s in ways])
	t["_ways"] = ways  # each source's own verdict is added once the status is known (_views)


def _trip_check(t: dict, fills: list, info: dict) -> None:
	"""The run of fills that bought more than the truck could burn over the
	distance between them -- even at its class's theft limit -- plus all its
	tanks hold: that much cannot all have gone into it, full fills or partial.
	One trip's own L/100 km is not judged: a partial fill before it makes it
	look high (June 2026: 26% of healthy trucks' trips looked like theft alone)."""
	cls = _cls(t)
	gps = info.get("gps_km") is not None and not info.get("gps_gaps") and not t.get("_gps_doubt")
	best = run = None
	for i in range(1, len(fills)):
		f, prev = fills[i], fills[i - 1]
		if gps:
			km = f.get("gps_km_since_prev") if f.get("_gps_leg_ok", True) else None
		else:
			km = f.get("odometer_km_since_prev")
		if km is None or km < 0:
			run = None  # distance unknown here: a run cannot span it
			continue
		burn = km * cls["theft"] / 100 + _idle_l(t.get("_im"), cls, prev["fill_time"], f["fill_time"])
		gain = f["liters"] - burn
		if run is None or run["over"] <= 0:
			run = {"over": gain, "first": i, "liters": f["liters"], "km": km, "burn": burn}
		else:
			run = {"over": run["over"] + gain, "first": run["first"], "liters": run["liters"] + f["liters"],
			       "km": run["km"] + km, "burn": run["burn"] + burn}
		if best is None or run["over"] > best["over"]:
			best = {**run, "last": i}
	if not best or best["over"] <= cls["tank"]:
		return
	n, excess = best["last"] - best["first"] + 1, best["over"] - cls["tank"]
	t["_trip"] = [n, f"{fills[best['first']]['fill_time']:%d-%m-%Y}", f"{fills[best['last']]['fill_time']:%d-%m-%Y}",
	              round(best["liters"]), round(best["km"]), "GPS km" if gps else "the typed odometer", t["vehicle_class"],
	              round(best["burn"]), cls["tank"], round(excess)]
	for f in fills[best["first"]:best["last"] + 1]:
		flags = _load(f.get("flag_data")) + [["trip_fill", n]]
		f["flag_data"] = json.dumps(flags)
		f["flags"] = "; ".join(_say(x, translate=False) for x in flags)


def _peer_rates(trucks: list) -> None:
	"""Median GPS L/100 km of the same brand+model (then brand, then everyone)."""
	judged = [t for t in trucks if t.get("lp100_basis") == "GPS" and t.get("_judged")]

	def median_of(items):
		return statistics.median([i["lp100"] for i in items]) if len(items) >= 3 else None

	by_model, by_brand = {}, {}
	for t in judged:
		by_model.setdefault((t["brand"].lower(), t["model"].lower()), []).append(t)
		by_brand.setdefault(t["brand"].lower(), []).append(t)
	fleet = median_of(judged)
	for t in trucks:
		peer = (median_of(by_model.get((t["brand"].lower(), t["model"].lower()), []))
		        or median_of(by_brand.get(t["brand"].lower(), [])) or fleet)
		t["peer_lp100"] = round(peer, 1) if peer else None


def _sensor_findings(t: dict, add, lp: float | None) -> None:
	"""The tank sensor, from IM's Fill-Drain report. It backs up the fuel per km
	rather than replacing it: with the consumption above normal its findings are
	Theft likely, otherwise Suspicious, and a sensor that cannot see all the
	fuel is set aside. `lp` is the judged L/100 km, None when not judged."""
	cls, name = _cls(t), t["vehicle_class"]
	high = lp is not None and lp > cls["normal"]
	card, seen, missing = flt(t["sensor_card_l"]), flt(t["sensor_fill_l"]), flt(t["missing_l"])
	drove = max(flt(t.get("gps_km")), flt(t.get("im_km")), flt(t.get("odometer_km")))
	if (card >= SENSOR_MIN_CARD_L and seen < 1 and flt(t.get("sensor_consumed_l")) < card * SENSOR_DEAD_SHARE
	        and drove >= MIN_KM_FOR_RATE):
		add(NOTE, 0, "sensor_dead")
		return
	pct = missing / card * 100 if card else 0
	if card >= SENSOR_MIN_CARD_L and missing >= MISSING_SUSPICIOUS[0] and pct >= MISSING_SUSPICIOUS[1]:
		# The truck's L/100 km if it had burned only the fuel the sensor saw.
		seen_lp = round(lp * seen / card, 1) if lp is not None else None
		if seen_lp is not None and seen_lp < cls["normal"] * SENSOR_FLOOR:
			add(NOTE, 0, "sensor_low", round(seen), round(card), seen_lp, name)
		else:
			big = missing >= MISSING_THEFT[0] and pct >= MISSING_THEFT[1] and high
			add(THEFT if big else SUSPICIOUS, 40 if big else 15, "missing", round(card), round(seen), round(missing))
	elif card >= SENSOR_MIN_CARD_L and -missing >= MISSING_SUSPICIOUS[0] and -pct >= MISSING_SUSPICIOUS[1]:
		add(NOTE, 0, "sensor_more", round(seen), round(card))
	drained = flt(t["drain_l"])
	if drained >= DRAIN_THEFT_L:
		add(THEFT if high else SUSPICIOUS, 40 if high else 15, "drain", round(drained), t["drains"])
	elif drained > 0:
		add(SUSPICIOUS if high else NOTE, 10 if high else 0, "small_drain", round(drained, 1))
	if flt(t["slow_drain_l"]) >= SLOW_DRAIN_L:
		add(NOTE, 0, "slow_drain", round(flt(t["slow_drain_l"])))


def _verdict(t: dict) -> None:
	"""The truck's status is the worst of its findings; each finding also adds
	to a 0-100 score used only for sorting."""
	c, cls, name = t["counts"], _cls(t), t["vehicle_class"]
	found = []

	def add(level: int, weight: int, *item) -> None:
		found.append((level, weight, list(item)))

	lp, basis = t.get("lp100"), t.get("lp100_basis")
	judged = lp is not None and bool(t.get("_judged"))
	if judged:
		by = {"GPS": "GPS km", "IM report": "IM km", "Odometer (card)": "the typed odometer"}[basis]
		if basis == "Odometer (card)":
			# The driver types the odometer and often gets it wrong, so only the
			# impossible counts; merely high is a note.
			if lp > cls["theft"]:
				add(SUSPICIOUS, 12, "burn_theft", lp, by, name, cls["theft"])
			elif lp > cls["normal"]:
				add(NOTE, 0, "burn_high", lp, by, name, cls["normal"])
		elif lp > cls["theft"]:
			add(THEFT, 30, "burn_theft", lp, by, name, cls["theft"])
		elif lp > cls["normal"]:
			add(SUSPICIOUS, 12, "burn_high", lp, by, name, cls["normal"])
	if t.get("has_sensor"):
		_sensor_findings(t, add, lp if judged else None)

	excess = None
	if basis == "GPS" and t.get("_judged") and lp is not None and t.get("peer_lp100"):
		excess = (lp - t["peer_lp100"]) / t["peer_lp100"] * 100
		if excess >= PEER_EXCESS:
			add(NOTE, 0, "peers", round(excess))
	t["excess_pct"] = round(excess) if excess is not None else None

	gap = t.get("odometer_gap_pct")
	if gap is not None and abs(gap) >= ODOMETER_GAP:
		add(SUSPICIOUS, 10, "odo_gap", f"+{gap}" if gap > 0 else str(gap))
	# A faulty tracker the typed odometer contradicts: judged by the odometer,
	# and worth a look.
	if t.get("_gps_doubt"):
		add(SUSPICIOUS, 10, "gps_doubt", *t["_gps_doubt"])
	# Fills that bought more than the truck could burn and hold; the typed
	# odometer alone never makes it Theft likely.
	if t.get("_trip"):
		gps_based = t["_trip"][5] == "GPS km"
		add(THEFT if gps_based else SUSPICIOUS, 35 if gps_based else 12, "trip_over", *t["_trip"])
	# Notes: shown with the reasons, never the status.
	if c["no_move"]:
		add(NOTE, 0, "no_move", c["no_move"])
	if c["quick"]:
		add(NOTE, 0, "quick", c["quick"], QUICK_REFILL_H)
	if c["over_tank"]:
		add(NOTE, 0, "over_tank", c["over_tank"], name, cls["tank"])
	if c["big"]:
		add(NOTE, 0, "big", c["big"])
	if c["odo_back"]:
		add(NOTE, 0, "odo_back", c["odo_back"])
	if t.get("_gps_gaps"):
		add(NOTE, 0, "gps_off")
	if t.get("_im_km_bad") and not t.get("_gps_doubt"):
		add(NOTE, 0, "im_km_bad", *t["_im_km_bad"])
	if t.get("_error"):
		add(NOTE, 0, "gps_failed", t["_error"])

	found.sort(key=lambda x: (-x[0], -x[1]))
	t["verdict"] = STATUS[found[0][0] if found else NOTE]
	t["finding_data"] = json.dumps([[level] + item for level, _weight, item in found])
	t["issues"] = "; ".join(_say(item, translate=False) for _level, _weight, item in found)
	t["score"] = min(100, sum(x[1] for x in found))

	# Stolen fuel (the owner's rule, Sep 2026): whatever the truck burned above
	# its fuel limit, by the distance its consumption was judged on -- any basis,
	# the typed odometer too. The tank sensor's missing and drained liters, and
	# a run of fills beyond the tanks, stay reasons for the status; they are not
	# counted as liters here.
	t["liters_at_risk"] = round(t.get("_excess_l", 0.0), 1) if judged and lp > cls["normal"] else 0.0
	t["cost_at_risk"] = round(t["liters_at_risk"] * (t["cost"] / t["liters"] if t["liters"] else 0), 2)


# The rules' name for each source, when the whole report is shown from it.
VIEW_BASIS = {"card": "Odometer (card)", "gps": "GPS", FILL_DRAIN: "IM report", CONSUMPTION: "IM report"}
VIEW_SOURCES = tuple(VIEW_BASIS)


def _views(t: dict) -> None:
	"""Each source's own verdict -- the same rules, with that source's distance
	and liters as the basis -- kept with it in by_report, so the page can show
	the whole report worked out from any one source without analysing again. A
	source with no distance for the truck gets none (a dash on the page)."""
	ways = t.get("_ways") or {}
	cls = _cls(t)
	for src, w in ways.items():
		if w.get("lp") is None:
			continue
		km, liters, allowance = w["_raw"]
		v = dict(t)
		v.update({"lp100": w["lp"], "lp100_basis": VIEW_BASIS[src], "_judged": km >= MIN_KM_FOR_RATE,
		          "_excess_l": max(0.0, liters - allowance - km * cls["normal"] / 100)})
		_verdict(v)
		w.update({"status": v["verdict"], "risk": v["liters_at_risk"], "sar": v["cost_at_risk"], "score": v["score"],
		          "found": _load(v["finding_data"])})
	order = ("card", "gps", FILL_DRAIN, CONSUMPTION)
	t["by_report"] = json.dumps([{k: val for k, val in ways[s].items() if k != "_raw"} for s in order if s in ways])


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def _progress(fuel_import: str, text: str, done: int = 0, total: int = 0, stage: str = "running",
              user: str | None = None) -> None:
	try:
		frappe.db.set_value(IMPORT_DT, fuel_import, "progress", text[:140], update_modified=False)
		frappe.db.commit()
		frappe.publish_realtime(PROGRESS_EVENT, {"fuel_import": fuel_import, "stage": stage, "text": text,
		                                         "done": done, "total": total}, user=user)
	except Exception:
		pass


def _set_run(fuel_import: str, value: str) -> None:
	# With an expiry, frappe keeps the value out of its per-process cache, so a
	# running job reads the real one each time and sees a cancel at once.
	frappe.cache.set_value(RUN_KEY + fuel_import, value, expires_in_sec=2 * 86400)


def _wanted(fuel_import: str, run_id: str | None) -> bool:
	"""Whether this run should (still) go ahead. A run queued before run ids
	existed has none, and is stopped only by a cancel."""
	current = frappe.cache.get_value(RUN_KEY + fuel_import, expires=True)
	if current == CANCELLED:
		return False
	return not run_id or not current or current == run_id


def run_analysis(fuel_import: str, user: str | None = None, run_id: str | None = None) -> None:
	"""Background job: read the files, fetch GPS per IM truck, store findings."""
	# Progress is written in English whatever the uploader's language; the
	# dashboard puts it into whichever language it is showing.
	frappe.local.lang = "en"
	if not _wanted(fuel_import, run_id):
		return  # cancelled, or a newer run was queued after this one
	try:
		_run(fuel_import, user, run_id)
	except Cancelled:
		frappe.db.rollback()
		_finish_cancelled(fuel_import, user)
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Failed", "progress": "",
		                                             "error": frappe.get_traceback()[-2000:]})
		frappe.db.commit()
		frappe.log_error(frappe.get_traceback(), f"Fuel Efficiency failed: {fuel_import}")
		_progress(fuel_import, "Failed", stage="failed", user=user)


def _finish_cancelled(fuel_import: str, user: str | None) -> None:
	if frappe.cache.get_value(RUN_KEY + fuel_import, expires=True) != CANCELLED:
		return  # superseded by a newer run, which owns the status now
	finished_before = frappe.db.get_value(IMPORT_DT, fuel_import, "analysed_on")
	frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Done" if finished_before else "Cancelled", "progress": "Cancelled"})
	frappe.db.commit()
	_progress(fuel_import, "Cancelled", stage="cancelled", user=user)


def _report_note(kind: str, report: dict, fills: list, matched: int) -> list:
	period = _period_text(report["start"], report["end"])
	inside = len(_inside(fills, report))
	if not inside:
		first, last = min(f["fill_time"] for f in fills), max(f["fill_time"] for f in fills)
		return ["report_unused", KIND_LABEL[kind], period, _period_text(first, last, dates_only=True)]
	return ["report_used", KIND_LABEL[kind], period, matched, len(report["rows"]), inside]


def _run(fuel_import: str, user: str | None, run_id: str | None = None) -> None:
	doc = frappe.get_doc(IMPORT_DT, fuel_import)
	own = _own_limits(doc)
	frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Running", "error": ""})
	_progress(fuel_import, _("Reading the files…"), user=user)

	fills, _skipped = parse_workbook(_path(doc.source_file))
	parsed = {}
	for kind, field in ((FILL_DRAIN, "fill_drain_file"), (CONSUMPTION, "consumption_file")):
		if doc.get(field):
			parsed[kind] = parse_im_report(_path(doc.get(field)), expect=kind)
	# A report for other dates says nothing about these fills.
	reports = {kind: report for kind, report in parsed.items() if _inside(fills, report)}
	tz = ZoneInfo(get_system_timezone() or "Asia/Riyadh")

	_progress(fuel_import, _("Matching plates to the IM fleet…"), user=user)
	fleet = Fleet()
	notes = list(fleet.notes)
	index ={kind: _index_report(report, fleet) for kind, report in reports.items()}

	trucks = {}
	for f in fills:
		trucks.setdefault(f["plate_key"] or f"raw:{f['plate']}", []).append(f)

	on_im = [k for k in trucks if fleet.by_plate(k)]
	done, results = 0, []
	used = {kind: set() for kind in reports}
	for key, truck_fills in trucks.items():
		if not _wanted(fuel_import, run_id):
			raise Cancelled()
		entry = fleet.by_plate(key)
		track, points, error = None, 0, ""
		if entry:
			done += 1
			_progress(fuel_import, _("Reading GPS {0} of {1}: {2}").format(done, len(on_im), truck_fills[0]["plate"]),
			          done, len(on_im), user=user)
			first = min(f["fill_time"] for f in truck_fills) - timedelta(hours=6)
			last = max(f["fill_time"] for f in truck_fills) + timedelta(hours=1)
			pts, error = _im_points(entry["vehicle_no"], first, last, tz)
			if pts:
				track, points = Track(pts), len(pts)

		cls = vehicle_class(truck_fills[0]["brand"], truck_fills[0]["model"])
		info = _check_truck(truck_fills, track, tz, cls)
		base = truck_fills[0]
		t = {
			"plate": base["plate"], "plate_key": base["plate_key"], "brand": base["brand"], "model": base["model"],
			"vehicle_class": cls, "branch": base["branch"],
			"drivers": ", ".join(sorted({f["driver"] for f in truck_fills if f["driver"]}))[:1000],
			"source": entry.get("source", ""), "imei": entry.get("imei", ""), "platform_name": entry.get("name", ""),
			"fills": len(truck_fills), "liters": round(sum(f["liters"] for f in truck_fills), 1),
			"cost": round(sum(f["cost"] for f in truck_fills), 2),
			"first_fill": info["first"], "last_fill": info["last"], "gps_km": info["gps_km"],
			"odometer_km": info["odometer_km"], "gps_points": points, "counts": info["counts"],
			"_error": error, "_gps_gaps": info["gps_gaps"], "_limits": own,
		}
		rows = {kind: _report_row(index[kind], entry, key) for kind in reports}
		for kind, row in rows.items():
			if row:
				used[kind].add(id(row))
		_apply_reports(t, truck_fills, reports, rows)
		_consumption(t, info)
		_trip_check(t, truck_fills, info)
		c = info["counts"]
		t.update({"no_gps_fills": c["no_gps"], "no_move_fills": c["no_move"],
		          "quick_refills": c["quick"], "big_fills": c["big"], "over_tank_fills": c["over_tank"]})
		checked = [x for x, on in (("GPS", points), ("Fuel sensor", t.get("has_sensor")),
		                           ("IM report", t.get("im_km") is not None)) if on]
		t["checked_with"] = " + ".join(checked) if checked else ("IM, no GPS" if entry else "Fuel card only")
		results.append(t)
		for f in truck_fills:
			f["source"], f["imei"] = entry.get("source", ""), entry.get("imei", "")
			if entry and track is None:
				f["gps_status"] = "No GPS data"

	for kind, report in parsed.items():
		notes.append(_report_note(kind, report, fills, len(used.get(kind, ()))))

	_peer_rates(results)
	for t in results:
		_verdict(t)
		_views(t)

	if not _wanted(fuel_import, run_id):
		raise Cancelled()
	_progress(fuel_import, _("Saving…"), user=user)
	_store(fuel_import, fills, results)

	flagged = sum(1 for t in results if t["verdict"] != "OK")
	frappe.db.set_value(IMPORT_DT, fuel_import, {
		"status": "Done",
		"progress": "",
		"notes": "\n".join(_say(n, translate=False) for n in notes),
		"note_data": json.dumps(notes),
		"analysed_on": now_datetime(),
		"fills": len(fills),
		"vehicles": len(results),
		"tracked_vehicles": sum(1 for t in results if t["gps_points"]),
		"sensor_vehicles": sum(1 for t in results if t.get("has_sensor")),
		"liters": round(sum(f["liters"] for f in fills), 1),
		"cost": round(sum(f["cost"] for f in fills), 2),
		"liters_at_risk": round(sum(t["liters_at_risk"] for t in results), 1),
		"cost_at_risk": round(sum(t["cost_at_risk"] for t in results), 2),
	})
	frappe.db.commit()
	_progress(fuel_import, _("Done: {0} trucks flagged").format(flagged), stage="done", user=user)


# Numbers that can honestly be unknown -- no previous fill, no GPS, too few km
# to judge, no sensor. Frappe keeps Int/Float columns NOT NULL (default 0), and
# a 0 here would read as a measurement ("0 km since the last fill"), so these
# are text columns holding the number or nothing.
MAYBE_UNKNOWN = {
	"hours_since_prev", "gps_km_since_prev",
	"odometer_km_since_prev", "gps_km", "im_km", "lp100", "peer_lp100", "excess_pct", "idle_hours",
	"idle_allowance_l", "odometer_km", "odometer_gap_pct", "window_liters", "sensor_card_l", "sensor_fill_l",
	"sensor_fills", "missing_l", "drain_l", "drains", "slow_drain_l", "sensor_consumed_l", "sensor_lp100",
	"start_level", "last_level",
}
NUMBERS = {
	"liters_at_risk", "liters", "price", "cost", "odometer", "provider_kmpl", "station_lat", "station_lng",
	"score", "fills", "gps_points", "no_gps_fills", "no_move_fills",
	"quick_refills", "big_fills", "over_tank_fills", "cost_at_risk", "normal_lp100", "theft_lp100", "has_sensor",
}


def _cell(field: str, value):
	if field in MAYBE_UNKNOWN:
		if value is None or value == "":
			return ""
		v = round(float(value), 1)
		return str(int(v)) if v.is_integer() else f"{v:.1f}"
	if field in NUMBERS:
		return value or 0
	return value


def _store(fuel_import: str, fills: list, trucks: list) -> None:
	frappe.db.delete(FILL_DT, {"fuel_import": fuel_import})
	frappe.db.delete(VEHICLE_DT, {"fuel_import": fuel_import})
	now, user = now_datetime(), frappe.session.user
	meta_cols = ["name", "creation", "modified", "modified_by", "owner", "docstatus", "idx"]

	def rows(items, fields):
		for i, item in enumerate(items):
			item["fuel_import"] = fuel_import
			yield [frappe.generate_hash(length=12), now, now, user, user, 0, i] + [_cell(f, item.get(f)) for f in fields]

	frappe.db.bulk_insert(FILL_DT, fields=meta_cols + FILL_FIELDS, values=list(rows(fills, FILL_FIELDS)), chunk_size=2000)
	frappe.db.bulk_insert(VEHICLE_DT, fields=meta_cols + VEHICLE_FIELDS, values=list(rows(trucks, VEHICLE_FIELDS)), chunk_size=2000)
	frappe.db.commit()


# --------------------------------------------------------------------------
# Public surface -- the dashboard
# --------------------------------------------------------------------------


def _enqueue(fuel_import: str) -> None:
	run_id = frappe.generate_hash(length=10)
	_set_run(fuel_import, run_id)  # the newest run wins; also lifts an earlier cancel
	frappe.enqueue("app_apis.fuel_efficiency.run_analysis", queue="long", timeout=7200,
	               fuel_import=fuel_import, user=frappe.session.user, run_id=run_id,
	               job_name=f"fuel_efficiency::{fuel_import}", enqueue_after_commit=True)


@frappe.whitelist()
def get_translations(lang: str = "ar") -> dict:
	"""The dashboard's own words in `lang` ({english or "english:context": text}),
	so the page can switch language without changing the user's desk language."""
	_read_check()
	path = os.path.join(frappe.get_app_path("app_apis"), "translations", f"{lang}.csv")
	if lang not in LANGS or lang == "en" or not os.path.exists(path):
		return {}
	words = {}
	with open(path, encoding="utf-8") as fh:
		for row in csv.reader(fh):
			if len(row) >= 2 and row[0]:
				words[row[0] + (":" + row[2] if len(row) > 2 and row[2] else "")] = row[1]
	return words


@frappe.whitelist()
def upload_files(files, fuel_import: str = "", lang: str = "", normal_limit=None, theft_limit=None) -> dict:
	"""Register uploaded .xlsx files (already saved by Frappe's uploader) and
	queue the analysis. Each file is recognised by its content.

	Without `fuel_import` this starts a new report, and the fuel-card export
	is required. With it, the files are added to that report -- an IM report
	replaces the one of its kind -- and the report is analysed again; with no
	files it only takes the new limits. `normal_limit` and `theft_limit` are
	the report's own L/100 km limits for heavy trucks."""
	frappe.only_for(RUN_ROLES)
	_use_lang(lang)
	limits = _limits_from(normal_limit, theft_limit)
	urls = frappe.parse_json(files) if isinstance(files, str) else files
	urls = [str(u or "").strip() for u in (urls or []) if str(u or "").strip()]
	if not urls and not (fuel_import and limits):
		frappe.throw(_("Choose a file."), title=_(TITLE))

	found = {}
	for url in urls:
		file_doc = frappe.get_doc("File", {"file_url": url})
		path = file_doc.get_full_path()
		if not path.lower().endswith(".xlsx"):
			frappe.throw(_("{0} is not an .xlsx file. Save it as Excel Workbook (.xlsx) and upload it again.").format(
				file_doc.file_name), title=_(TITLE))
		kind = classify_file(path)
		if not kind:
			frappe.throw(_("{0} is not a fuel-card export, an IM Fill-Drain report or an IM Fuel Consumption report.").format(
				file_doc.file_name), title=_(TITLE))
		if kind in found:
			frappe.throw(_("{0} and {1} are both an {2}. Upload one of each kind.").format(
				found[kind][0].file_name, file_doc.file_name, _(KIND_LABEL[kind])), title=_(TITLE))
		found[kind] = (file_doc, path)
	if not fuel_import and CARD not in found:
		frappe.throw(_("Include the fuel-card export: the IM reports are checked against it."), title=_(TITLE))

	values, warnings, skipped = {}, [], 0
	if CARD in found:
		file_doc, path = found[CARD]
		fills, skipped = parse_workbook(path)
		if not fills:
			frappe.throw(_("No usable fills in {0}.").format(file_doc.file_name), title=_(TITLE))
		card_from, card_to = min(f["fill_time"] for f in fills), max(f["fill_time"] for f in fills)
		values.update({
			"title": f"{file_doc.file_name} · {card_from:%d-%m-%Y} – {card_to:%d-%m-%Y}",
			"period_from": card_from.date(),
			"period_to": card_to.date(),
			"source_file": file_doc.file_url,
			"file_name": file_doc.file_name,
			"fills": len(fills),
		})
	else:
		fills, skipped = parse_workbook(_path(frappe.db.get_value(IMPORT_DT, fuel_import, "source_file")))

	for kind, field in ((FILL_DRAIN, "fill_drain"), (CONSUMPTION, "consumption")):
		if kind not in found:
			continue
		file_doc, path = found[kind]
		report = parse_im_report(path, expect=kind)
		values[field + "_file"] = file_doc.file_url
		values[field + "_period"] = _period_text(report["start"], report["end"])
		if not _inside(fills, report):
			first, last = min(f["fill_time"] for f in fills), max(f["fill_time"] for f in fills)
			warnings.append(_say(["report_later", KIND_LABEL[kind], values[field + "_period"],
			                      _period_text(first, last, dates_only=True)]))

	values.update(limits)
	values.update({"status": "Queued", "progress": "Queued", "error": ""})
	if fuel_import:
		frappe.db.set_value(IMPORT_DT, fuel_import, values)
		name, title = fuel_import, values.get("title") or frappe.db.get_value(IMPORT_DT, fuel_import, "title")
	else:
		doc = frappe.get_doc({"doctype": IMPORT_DT, **values}).insert(ignore_permissions=True)
		name, title = doc.name, doc.title
	_enqueue(name)
	return {"name": name, "title": title, "fills": len(fills), "skipped": skipped,
	        "kinds": [_(KIND_LABEL[k]) for k in found], "warnings": warnings}


@frappe.whitelist()
def upload_report(file_url: str) -> dict:
	"""One fuel-card export on its own; kept for callers of the first version."""
	return upload_files([file_url])


@frappe.whitelist()
def reanalyse(fuel_import: str) -> dict:
	frappe.only_for(RUN_ROLES)
	frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Queued", "progress": "Queued", "error": ""})
	_enqueue(fuel_import)
	return {"queued": True}


@frappe.whitelist()
def set_limits(fuel_import: str, normal_limit=None, theft_limit=None) -> dict:
	"""The report's own limits for heavy trucks, changed on their own: saved,
	and the report analysed again on the files it already has."""
	frappe.only_for(RUN_ROLES)
	limits = _limits_from(normal_limit, theft_limit)
	if not limits:
		frappe.throw(_("The theft limit must be above the normal limit."), title=_(TITLE))
	frappe.db.set_value(IMPORT_DT, fuel_import, {**limits, "status": "Queued", "progress": "Queued", "error": ""})
	_enqueue(fuel_import)
	return {"queued": True, **limits}


@frappe.whitelist()
def cancel_analysis(fuel_import: str) -> dict:
	"""Stop a queued or running analysis. A running one stops at its next
	truck; the results of the last finished run stay as they were."""
	frappe.only_for(RUN_ROLES)
	_set_run(fuel_import, CANCELLED)
	if frappe.db.get_value(IMPORT_DT, fuel_import, "status") in ("Queued", "Running"):
		finished_before = frappe.db.get_value(IMPORT_DT, fuel_import, "analysed_on")
		frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Done" if finished_before else "Cancelled", "progress": "Cancelled"})
	return {"cancelled": True}


@frappe.whitelist()
def list_imports() -> list:
	_read_check()
	fields = ["name", "title", "status", "period_from", "period_to", "fills", "analysed_on"]
	shared = frappe.flags.get("fuel_shared")
	if shared:  # a share link lists its own report only
		return frappe.get_all(IMPORT_DT, filters={"name": shared}, fields=fields)
	return frappe.get_all(IMPORT_DT, fields=fields, order_by="creation desc", limit_page_length=50)


@frappe.whitelist()
def get_progress(fuel_import: str) -> dict:
	_read_check(fuel_import)
	return frappe.db.get_value(IMPORT_DT, fuel_import, ["status", "progress", "error"], as_dict=True) or {}


@frappe.whitelist()
def get_summary(fuel_import: str, lang: str = "", by: str = "") -> dict:
	"""The report's head and status cards; with `by`, the cards as that source
	works the trucks out (see _views)."""
	_read_check(fuel_import)
	_use_lang(lang)
	doc =frappe.get_doc(IMPORT_DT, fuel_import)
	verdicts = dict(frappe.db.sql(f"select verdict, count(*) from `tab{VEHICLE_DT}` where fuel_import=%s group by verdict", fuel_import))
	# Only fuel flagged as missing: a sensor that sees one of two tanks would
	# count half the fleet's fuel. Slow siphoning is a note, so not counted.
	sensor = frappe.db.sql(
		f"""select count(*), sum(if(finding_data like %s, greatest(missing_l + 0, 0), 0)), sum(drain_l + 0)
		from `tab{VEHICLE_DT}` where fuel_import=%s and has_sensor=1""", ('%"missing"%', fuel_import))[0]

	def distinct(field):
		return [r[0] for r in frappe.db.sql(
			f"select distinct {field} from `tab{VEHICLE_DT}` where fuel_import=%s and ifnull({field},'')!='' order by {field}",
			fuel_import)]

	out = {
		"import": {k: v for k, v in doc.as_dict(no_default_fields=True).items() if k != "share_key"} | {"name": doc.name},
		"notes": [{"warn": n[0] in WARN_NOTES, "text": _say(n)} for n in _load(doc.note_data) if n and n[0] in MESSAGES],
		"verdicts": verdicts,
		"sensor": {"trucks": cint(sensor[0]), "missing_l": flt(sensor[1], 1), "drained_l": flt(sensor[2], 1)},
		"brands": distinct("brand"),
		"branches": distinct("branch"),
		"classes": distinct("vehicle_class"),
		"limits": [{"class": k, "normal": v["normal"], "theft": v["theft"], "idle": v["idle"], "tank": v["tank"]}
		           for k, v in ((k, _cls({"vehicle_class": k, "_limits": _own_limits(doc)})) for k in CLASSES)],
		"can_run": bool(set(frappe.get_roles()) & set(RUN_ROLES)),
		"shared": bool(doc.get("share_key")),
	}
	if by in VIEW_SOURCES:
		out["view"] = _summary_by(fuel_import, by)
	return out


def _summary_by(fuel_import: str, src: str) -> dict:
	"""The status cards when every truck is worked out from `src`; `missing`
	counts the trucks it cannot work out (dashes on the page). `ready` is False
	for a report analysed before the sources had verdicts of their own."""
	counts, missing, liters, sar, ready = {}, 0, 0.0, 0.0, False
	for (raw,) in frappe.db.sql(f"select by_report from `tab{VEHICLE_DT}` where fuel_import=%s", fuel_import):
		ways = [x for x in _load(raw) if isinstance(x, dict)]
		ready = ready or any("status" in x for x in ways)
		w = next((x for x in ways if x.get("src") == src), None)
		if w and w.get("status"):
			counts[w["status"]] = counts.get(w["status"], 0) + 1
			liters += flt(w.get("risk"))
			sar += flt(w.get("sar"))
		else:
			missing += 1
	return {"verdicts": counts, "missing": missing, "liters_at_risk": round(liters, 1), "cost_at_risk": round(sar, 2),
	        "ready": ready}


# --------------------------------------------------------------------------
# The share link: one report, read-only, for someone without an account
# --------------------------------------------------------------------------

SHARE_KEY_LEN = 32
# What a share link may call: the dashboard's reads, for its own report only.
PUBLIC_READS = ("get_translations", "list_imports", "get_progress", "get_summary", "get_vehicles", "get_fills")


def _read_check(fuel_import: str | None = None) -> None:
	"""Staff with a reading role -- or a visitor on a report's share link, for
	the one report that link opens (see public_call)."""
	shared = frappe.flags.get("fuel_shared")
	if shared and fuel_import in (None, shared):
		return
	frappe.only_for(READ_ROLES)


def _shared_import(key) -> str:
	"""The report a share key opens. One answer for a wrong key and a stopped
	one, so keys cannot be probed."""
	key = str(key or "")
	name = (frappe.db.get_value(IMPORT_DT, {"share_key": key}, "name")
	        if len(key) == SHARE_KEY_LEN and key.isalnum() else None)
	if not name:
		raise frappe.PermissionError(_("This link is not valid or was stopped."))
	return name


@frappe.whitelist()
def share_link(fuel_import: str, stop: int = 0) -> dict:
	"""The report's link for someone without an account -- made on first use,
	the same link after that -- or, with `stop`, no link at all."""
	frappe.only_for(RUN_ROLES)
	if cint(stop):
		frappe.db.set_value(IMPORT_DT, fuel_import, "share_key", "")
		return {"url": ""}
	key = frappe.db.get_value(IMPORT_DT, fuel_import, "share_key")
	if not key:
		key = frappe.generate_hash(length=SHARE_KEY_LEN)
		frappe.db.set_value(IMPORT_DT, fuel_import, "share_key", key)
	return {"url": get_url(f"/fuel_report?k={key}")}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="k", limit=1200, seconds=60 * 60)
def public_call(k: str, method: str, args=None):
	"""The public report page's one door (www/fuel_report): the key opens its
	report, and only the reads in PUBLIC_READS run -- with that report forced
	in, whatever else was asked."""
	name = _shared_import(k)
	if method not in PUBLIC_READS:
		raise frappe.PermissionError(_("This link is not valid or was stopped."))
	fn = globals()[method]
	kwargs = frappe.parse_json(args) if isinstance(args, str) else args
	accepted = inspect.signature(fn).parameters
	kwargs = {key: v for key, v in (kwargs if isinstance(kwargs, dict) else {}).items() if key in accepted}
	if "fuel_import" in accepted:
		kwargs["fuel_import"] = name
	frappe.flags.fuel_shared = name
	try:
		return fn(**kwargs)
	finally:
		frappe.flags.fuel_shared = None


def _like(value: str) -> str:
	return "%" + str(value).replace("%", "").replace("_", "\\_") + "%"


# The dashboard's "Main reason" and "Reason" column filters -> the stored codes.
FINDING_GROUPS = {
	"missing": ("missing",), "drain": ("drain", "small_drain", "slow_drain"),
	"burn": ("burn_theft", "burn_high", "peers"), "odometer": ("odo_gap", "odo_back"), "no_move": ("no_move",),
	"quick": ("quick",), "big": ("big", "over_tank"), "no_gps": ("no_gps", "gps_failed", "gps_off", "gps_doubt"),
	"sensor": ("sensor_dead", "sensor_low"), "trip": ("trip_over",),
}
FLAG_GROUPS = {
	"no_move": ("no_move_fill",), "quick": ("quick_fill",), "big": ("big_fill",),
	"over_tank": ("over_tank_fill",), "odometer": ("odo_back_fill",), "trip": ("trip_fill",),
}


# Sorting from the column headings: each key is its column's SQL, with the
# direction applied or not (empty values always last). Anything else keeps the
# default order, so no request text ever reaches the SQL.
_KM_USED = ("(case lp100_basis when 'IM report' then im_km when 'Odometer (card)' then odometer_km "
            "else (case when ifnull(gps_km, '') != '' then gps_km else im_km end) end)")
# The source the status came from: GPS, an IM report, the typed odometer, none.
USED_RANK = (f"(case when lp100_basis = 'GPS' and gps_km + 0 >= {MIN_KM_FOR_RATE} then 1 when lp100_basis = 'IM report' then 2 "
             "when lp100_basis = 'Odometer (card)' then 3 else 4 end)")
VEHICLE_SORT = {
	"verdict": [("field(verdict, 'Theft likely', 'Suspicious', 'OK')", True)],
	"plate": [("plate", True)],
	"vehicle": [("vehicle_class", True), ("brand", True), ("model", True)],
	"liters": [("liters", True)],
	"km": [(f"ifnull({_KM_USED}, '') = ''", False), (f"{_KM_USED} + 0", True)],
	"used": [(USED_RANK, True)],
	"rate": [("ifnull(lp100, '') = ''", False), ("lp100 + 0", True)],
	"finding": [("score", True)],
	"risk": [("liters_at_risk", True)],
}
FILL_SORT = {
	"fplate": [("plate", True)],
	"time": [("fill_time", True)],
	"station": [("station_branch", True), ("station", True)],
	"liters": [("liters", True)],
	"odometer": [("odometer = 0", False), ("odometer", True)],
	"odo_km": [("ifnull(odometer_km_since_prev, '') = ''", False), ("odometer_km_since_prev + 0", True)],
	"gps_km": [("ifnull(gps_km_since_prev, '') = ''", False), ("gps_km_since_prev + 0", True)],
	"driver": [("driver", True)],
	"flag": [("ifnull(flags, '') = ''", False), ("flags", True)],
}


def _order_by(sort: str, order: str, table: dict, default: str) -> str:
	spec = table.get(sort)
	if not spec:
		return default
	direction = "desc" if str(order).lower() == "desc" else "asc"
	return ", ".join([f"{expr} {direction if flips else 'asc'}" for expr, flips in spec] + [default])


def _has_code(field: str, codes: tuple, where: list, vals: list) -> None:
	# Codes sit quoted in the stored JSON ('[2, "drain", 45, 2]'), so '"drain"'
	# never matches "small_drain".
	where.append("(" + " or ".join(f"{field} like %s" for _code in codes) + ")")
	vals += [f'%"{code}"%' for code in codes]


@frappe.whitelist()
def get_vehicles(fuel_import: str, verdict: str = "", brand: str = "", branch: str = "", source: str = "",
                 search: str = "", start: int = 0, limit: int = 100, checked: str = "", vehicle_class: str = "",
                 plate: str = "", vehicle: str = "", rate: str = "", finding: str = "", risk: str = "",
                 used: str = "", sort: str = "", order: str = "", by: str = "", lang: str = "") -> dict:
	"""Trucks of one report. The filters are the dashboard's column headings:
	`vehicle` is 'c:<class>', 'b:<brand>' or 'r:<branch>'; `rate` is
	above_normal, above_theft or unjudged; `finding` a FINDING_GROUPS key;
	`used` the source the status came from (gps, im, card, none). `sort` is a
	VEHICLE_SORT key and `order` asc or desc. `by` (a VIEW_SOURCES key) shows
	every truck worked out from that one source, its status included."""
	_read_check(fuel_import)
	_use_lang(lang)
	where, vals = ["fuel_import=%s"], [fuel_import]
	kind, _sep, picked = str(vehicle or "").partition(":")
	fields = {"brand": brand, "branch": branch, "vehicle_class": vehicle_class}
	if picked and kind in ("c", "b", "r"):
		fields[{"c": "vehicle_class", "b": "brand", "r": "branch"}[kind]] = picked
	for field, value in fields.items():
		if value:
			where.append(f"{field}=%s")
			vals.append(value)
	checked = checked or {"tracked": "gps", "untracked": "card"}.get(source, "")
	if checked == "gps":
		where.append("gps_points > 0")
	elif checked == "sensor":
		where.append("has_sensor = 1")
	elif checked == "card":
		where.append("ifnull(source, '') = ''")
	text = plate or search
	if text:
		where.append("(plate like %s or plate_key like %s or drivers like %s or platform_name like %s)")
		vals += [_like(text)] * 4
	if by in VIEW_SOURCES:
		return _vehicles_by(by, where, vals, verdict, rate, finding, risk, sort, order, start, limit)
	if verdict:
		where.append("verdict=%s")
		vals.append(verdict)
	# Judged the way the page colours it: GPS needs MIN_KM_FOR_RATE; the other
	# two bases only exist above it, and the typed odometer never reaches theft.
	judged = "(lp100_basis != 'GPS' or gps_km + 0 >= %s)"
	if rate == "above_normal":
		where.append(f"ifnull(lp100, '') != '' and lp100 + 0 > normal_lp100 and {judged}")
		vals.append(MIN_KM_FOR_RATE)
	elif rate == "above_theft":
		where.append(f"ifnull(lp100, '') != '' and lp100 + 0 > theft_lp100 and lp100_basis != 'Odometer (card)' and {judged}")
		vals.append(MIN_KM_FOR_RATE)
	elif rate == "unjudged":
		where.append("(ifnull(lp100, '') = '' or (lp100_basis = 'GPS' and gps_km + 0 < %s))")
		vals.append(MIN_KM_FOR_RATE)
	if finding in FINDING_GROUPS:
		_has_code("finding_data", FINDING_GROUPS[finding], where, vals)
	if risk:
		where.append("liters_at_risk > 0")
	if used in ("gps", "im", "card", "none"):
		where.append(f"{USED_RANK} = %s")
		vals.append({"gps": 1, "im": 2, "card": 3, "none": 4}[used])
	cond = " and ".join(where)
	order_by = _order_by(sort, order, VEHICLE_SORT,
	                     "field(verdict, 'Theft likely', 'Suspicious', 'OK'), liters_at_risk desc, score desc, liters desc")
	total = frappe.db.sql(f"select count(*) from `tab{VEHICLE_DT}` where {cond}", vals)[0][0]
	rows = frappe.db.sql(
		f"select {', '.join(VEHICLE_FIELDS)} from `tab{VEHICLE_DT}` where {cond} order by {order_by} limit %s offset %s",
		vals + [cint(limit) or 100, cint(start)], as_dict=True)
	for r in rows:
		_dress(r)
	return {"total": total, "rows": rows}


def _words(items) -> list:
	"""Stored findings as sentences with their level. A code no longer in
	MESSAGES (a retired check, in an older run) is dropped."""
	return [{"level": item[0], "code": item[1], "text": _say(item[1:])} for item in items or []
	        if isinstance(item, list) and len(item) > 1 and item[1] in MESSAGES]


def _dress(r: dict) -> dict:
	"""A stored truck as the page reads it: its findings, and each source's
	reason and findings, in words."""
	items = _load(r.pop("finding_data", None))
	r["findings"] = _words(items)
	if items:
		r["issues"] = "; ".join(x["text"] for x in r["findings"])
	r["by_report"] = [w for w in _load(r.get("by_report")) if isinstance(w, dict)]
	for w in r["by_report"]:
		why = w.get("why")
		w["why"] = _say(why) if isinstance(why, list) and why and why[0] in MESSAGES else ""
		w["findings"] = _words(w.pop("found", None))
	return r


def _vehicles_by(src, where, vals, verdict, rate, finding, risk, sort, order, start, limit) -> dict:
	"""Every truck as one source works it out (see _views): filtered and sorted
	on that source's own numbers and status, with the trucks it cannot work out
	last, as a dash and the reason."""
	rows = frappe.db.sql(f"select {', '.join(VEHICLE_FIELDS)} from `tab{VEHICLE_DT}` where {' and '.join(where)}",
	                     vals, as_dict=True)
	rank = {"Theft likely": 0, "Suspicious": 1, "OK": 2}
	for r in rows:
		r["_w"] = next((x for x in _load(r.get("by_report")) if isinstance(x, dict) and x.get("src") == src), None)
		r["_v"] = r["_w"] if r["_w"] and r["_w"].get("status") else None

	def keep(r) -> bool:
		v = r["_v"] or {}
		judged, lp = bool(v) and flt(v.get("km")) >= MIN_KM_FOR_RATE, flt(v.get("lp"))
		if verdict and v.get("status") != verdict:
			return False
		if rate == "above_normal" and not (judged and lp > flt(r["normal_lp100"])):
			return False
		if rate == "above_theft" and not (judged and src != "card" and lp > flt(r["theft_lp100"])):
			return False
		if rate == "unjudged" and judged:
			return False
		if finding in FINDING_GROUPS and not {f[1] for f in v.get("found") or [] if len(f) > 1} & set(FINDING_GROUPS[finding]):
			return False
		return not risk or flt(v.get("risk")) > 0

	rows = [r for r in rows if keep(r)]
	if sort in ("plate", "vehicle"):
		rows.sort(key=(lambda r: r["plate"] or "") if sort == "plate"
		          else (lambda r: (r["vehicle_class"] or "", r["brand"] or "", r["model"] or "")),
		          reverse=str(order).lower() == "desc")
	elif sort in ("verdict", "liters", "km", "rate", "finding", "risk"):
		value = {"verdict": lambda v: rank.get(v.get("status"), 3), "liters": lambda v: flt(v.get("liters")),
		         "km": lambda v: flt(v.get("km")), "rate": lambda v: flt(v.get("lp")),
		         "finding": lambda v: cint(v.get("score")), "risk": lambda v: flt(v.get("risk"))}[sort]
		rows = (sorted([r for r in rows if r["_v"]], key=lambda r: value(r["_v"]), reverse=str(order).lower() == "desc")
		        + sorted([r for r in rows if not r["_v"]], key=lambda r: r["plate"] or ""))
	else:
		rows.sort(key=lambda r: (not r["_v"], rank.get((r["_v"] or {}).get("status"), 3), -flt((r["_v"] or {}).get("risk")),
		                         -cint((r["_v"] or {}).get("score")), -flt(r["liters"])))
	page = rows[cint(start):cint(start) + (cint(limit) or 100)]
	for r in page:
		w, v = r.pop("_w"), r.pop("_v")
		_dress(r)
		r["view"] = None
		if v:
			r["view"] = {key: v.get(key) for key in ("src", "status", "lp", "km", "liters", "fills", "idle_l", "idle_h", "odo",
			                                          "dates", "risk", "sar")}
			r["view"]["findings"] = _words(v.get("found"))
			# Why the most exact view does not use this source for the truck -- a
			# tracker that was off, an odometer typed wrong -- said beside it.
			why = v.get("why")
			r["view"]["why"] = _say(why) if isinstance(why, list) and why and why[0] in MESSAGES else ""
		elif w and w.get("lp") is not None:
			r["view_why"] = _say(["src_stale"])
		else:
			why = (w or {}).get("why")
			r["view_why"] = (_say(why) if isinstance(why, list) and why and why[0] in MESSAGES
			                 else _say(["src_none_report" if r.get("imei") or r.get("source") else "src_none_im"]))
	return {"total": len(rows), "rows": page, "by": src}


@frappe.whitelist()
def get_fills(fuel_import: str, plate_key: str = "", plate: str = "", flagged: int = 0, severity: str = "",
              search: str = "", start: int = 0, limit: int = 500, plate_like: str = "", station: str = "",
              driver: str = "", gps_status: str = "", flag: str = "", sort: str = "", order: str = "",
              lang: str = "") -> dict:
	"""Fills of one report: one truck's (`plate_key`/`plate`, for its dialog)
	or all of them, filtered by the dashboard's column headings. `flagged`:
	only fills with a note. (`severity` is kept for old callers; fills have
	no status of their own.)"""
	_read_check(fuel_import)
	_use_lang(lang)
	where, vals = ["fuel_import=%s"], [fuel_import]
	if plate_key:
		where.append("plate_key=%s")
		vals.append(plate_key)
	elif plate:
		where.append("plate=%s")
		vals.append(plate)
	if cint(flagged):
		where.append("ifnull(flag_data, '') not in ('', '[]')")
	if search:
		where.append("(plate like %s or driver like %s or station like %s or station_branch like %s)")
		vals += [_like(search)] * 4
	if plate_like:
		where.append("(plate like %s or plate_key like %s)")
		vals += [_like(plate_like)] * 2
	if station:
		where.append("(station like %s or station_branch like %s or station_area like %s)")
		vals += [_like(station)] * 3
	if driver:
		where.append("driver like %s")
		vals.append(_like(driver))
	if gps_status:
		where.append("gps_status=%s")
		vals.append(gps_status)
	if flag in FLAG_GROUPS:
		_has_code("flag_data", FLAG_GROUPS[flag], where, vals)
	cond = " and ".join(where)
	order_by = _order_by(sort, order, FILL_SORT, "fill_time asc" if (plate_key or plate) else "fill_time desc")
	total = frappe.db.sql(f"select count(*) from `tab{FILL_DT}` where {cond}", vals)[0][0]
	rows = frappe.db.sql(
		f"select name, {', '.join(FILL_FIELDS)} from `tab{FILL_DT}` where {cond} order by {order_by} limit %s offset %s",
		vals + [cint(limit) or 500, cint(start)], as_dict=True)
	for r in rows:
		items = _load(r.pop("flag_data", None))
		if items:
			r["flags"] = "; ".join(filter(None, (_say(item) for item in items)))
		# Part of a run of fills that bought more than the truck could burn and hold.
		r["trip"] = int(any(item and item[0] == "trip_fill" for item in items))
	return {"total": total, "rows": rows}
