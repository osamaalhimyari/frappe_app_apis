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

Every truck ends with one of three statuses, the worst of its findings:

  Theft likely
    * a fill paid for while GPS put the truck away from the pump;
    * the tank sensor saw much less fuel arrive than the card paid for;
    * the tank sensor saw fuel drained out;
    * one fill bigger than that kind of vehicle's tanks hold;
    * burned more per GPS or IM km than a truck of its class can.
  Suspicious
    * above its class's normal consumption by GPS or IM km, or well above
      trucks of the same brand and model;
    * by the odometer the driver typed, more than its class can burn at all;
    * a smaller shortfall at the sensor, a small or slow drain;
    * the typed odometer disagrees with IM's distance;
    * refuelling after almost no driving, or twice within hours;
    * the tracker sent no GPS around many of its fills (its GPS distance is
      then not used at all: it would be missing whole trips).
  OK
    * none of the above. Large fills, an odometer typed backwards and a high
      typed-odometer consumption below the theft limit are shown as notes: on
      their own they are far more often typing than theft. (June 2026: as
      statuses they flagged 162 extra trucks, almost none of them credibly.)

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
from frappe.utils import cint, flt, get_system_timezone, now_datetime

IMPORT_DT = "App Apis Fuel Import"
FILL_DT = "App Apis Fuel Fill"
VEHICLE_DT = "App Apis Fuel Vehicle"
PROGRESS_EVENT = "fuel_efficiency_progress"
TITLE = "Fuel Efficiency"

READ_ROLES = ["System Manager", "Technical", "Support Team"]
RUN_ROLES = ["System Manager", "Technical"]

THEFT, SUSPICIOUS, NOTE = 2, 1, 0
STATUS = {THEFT: "Theft likely", SUSPICIOUS: "Suspicious", NOTE: "OK"}

# What counts, in one place.
# Where the truck was when the card was swiped.
FILL_WINDOW_MIN = 30      # GPS points this close to the invoice time are looked at
COVERAGE_MIN = 20         # at least one must be this close, else "No GPS at fill time"
AT_STATION_M = 700        # truck within this of the pump: at the station
AWAY_M = 2500             # never closer than this: away from the station. Not less: in June 2026
                          # one station's fills all put the truck at one spot 1.6 km off its pin.
# The tracker off. With no GPS around this share of a truck's fills (and at
# least this many), its GPS distance is missing whole trips and is not used
# for consumption or the odometer check; from GPS_OFF_SUSPICIOUS the tracker
# being off is itself a finding. (June 2026: one truck's GPS read 856 km where
# its odometer read 9,233, which made a normal 41 L/100 km look like 448.)
GPS_GAP_SHARE = 0.25
GPS_GAP_MIN_FILLS = 3
GPS_OFF_SUSPICIOUS = 0.4
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
MISSING_THEFT = (150, 25)
DRAIN_THEFT_L = 30        # drained out: this much is Theft likely, less is Suspicious
SLOW_DRAIN_L = 20         # siphoned slowly: Suspicious
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
	"away_fill": "Truck was {0} km from the station",
	"no_move_fill": "Refuelled after only {0} km",
	# one truck
	"away": "{0} fill(s) paid for while the truck was away from the station",
	"over_tank": "{0} fill(s) bigger than the tanks ({1}: up to {2} L)",
	"missing": "Paid for {0} L, the tank sensor saw {1} L arrive: {2} L missing",
	"drain": "Tank sensor saw {0} L drained out ({1} time(s))",
	"small_drain": "Tank sensor saw {0} L drained out",
	"slow_drain": "Tank sensor saw {0} L slowly siphoned",
	"burn_theft": "Burns {0} L/100 km by {1}; the most a {2} can burn is {3}",
	"burn_high": "Burns {0} L/100 km by {1}; normal for a {2} is up to {3}",
	"peers": "{0}% more fuel per km than similar trucks",
	"odo_gap": "Typed odometer is {0}% off IM's distance",
	"odo_back": "Odometer went backwards {0} time(s)",
	"no_move": "{0} fill(s) after almost no driving",
	"quick": "{0} refill(s) within {1} h",
	"big": "{0} unusually large fill(s)",
	"no_gps": "No GPS around {0} fill(s)",
	"gps_off": "The tracker sent no GPS around {0} of {1} fills, so its GPS distance was not used",
	"gps_failed": "GPS read failed: {0}",
	# the report
	"fleet_failed": "IM fleet could not be read: {0}",
	"report_unused": "{0} covers {1}, but the fuel card has no fills in those dates, so it was not used. Export it for {2}.",
	"report_later": "{0} covers {1}, but the fuel card has no fills in those dates, so it will not be used. Export it for {2}.",
	"report_used": "{0} {1}: {2} of its {3} vehicles are trucks in the fuel card; {4} card fills fall in its dates.",
}
WARN_NOTES = {"fleet_failed", "report_unused"}
# Fill flags that are worth showing but are, alone, usually typing rather than theft.
NOTE_FLAGS = {"big_fill", "odo_back_fill"}
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
	"severity", "flags", "flag_data", "liters_at_risk", "invoice_no", "liters", "price", "cost", "odometer",
	"provider_kmpl", "station", "station_branch", "station_area", "station_lat", "station_lng",
	"source", "imei", "gps_status", "distance_to_station_m", "nearest_point_min", "truck_lat",
	"truck_lng", "hours_since_prev", "gps_km_since_prev", "odometer_km_since_prev",
]
VEHICLE_FIELDS = [
	"fuel_import", "plate", "plate_key", "verdict", "score", "brand", "model", "vehicle_class", "branch", "drivers",
	"source", "imei", "platform_name", "checked_with", "fills", "liters", "cost", "first_fill", "last_fill",
	"gps_km", "im_km", "lp100", "lp100_basis", "normal_lp100", "theft_lp100", "peer_lp100", "excess_pct",
	"idle_hours", "idle_allowance_l", "odometer_km", "odometer_gap_pct", "gps_points", "window_liters",
	"has_sensor", "sensor_card_l", "sensor_fill_l", "sensor_fills", "missing_l", "drain_l", "drains",
	"slow_drain_l", "sensor_consumed_l", "sensor_lp100", "start_level", "last_level",
	"away_fills", "no_gps_fills", "no_move_fills", "quick_refills", "big_fills", "over_tank_fills",
	"liters_at_risk", "cost_at_risk", "issues", "finding_data",
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

	out = []
	for r in rows[head_i + (2 if has_sub else 1):]:
		def cell(key):
			j = col.get(key)
			return r[j] if j is not None and j < len(r) else None

		vehicle = re.sub(r"\s+", " ", str(cell("vehicle") or "")).strip()
		if not vehicle:
			continue
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
			"km": flt(_num(cell("km"))),
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
			# there reads a level even on a day the truck stood still.
			"has_sensor": kind == FILL_DRAIN and bool(
				sensor_brand not in ("", "--") or fill_l or drain_l or slow_l or consumed or start_level or last_level),
		})
	return {"kind": kind, "start": start, "end": end, "rows": out}


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

	def at_fill(self, t: float, lat, lng) -> dict:
		"""Where the truck was around time t, relative to the pump at lat/lng."""
		lo = bisect.bisect_left(self.ts, t - FILL_WINDOW_MIN * 60)
		hi = bisect.bisect_right(self.ts, t + FILL_WINDOW_MIN * 60)
		if lo >= hi:
			return {"status": "No GPS at fill time"}
		gap_min = min(abs(self.ts[k] - t) for k in range(lo, hi)) / 60
		if gap_min > COVERAGE_MIN:
			return {"status": "No GPS at fill time", "gap_min": gap_min}
		if not (lat and lng):
			return {"status": "Station location missing", "gap_min": gap_min}
		best = min(range(lo, hi), key=lambda k: _meters(self.points[k][1], self.points[k][2], lat, lng))
		dist = _meters(self.points[best][1], self.points[best][2], lat, lng)
		status = "At station" if dist <= AT_STATION_M else "Away from station" if dist > AWAY_M else "Near station"
		return {"status": status, "dist": dist, "gap_min": gap_min,
		        "truck_lat": self.points[best][1], "truck_lng": self.points[best][2]}


# --------------------------------------------------------------------------
# Checking one truck
# --------------------------------------------------------------------------


def _check_truck(fills: list, track: "Track | None", tz, cls: str) -> dict:
	"""Fill-level findings written onto each fill dict, truck-level numbers returned."""
	limits = CLASSES[cls]
	fills.sort(key=lambda f: f["fill_time"])
	median_fill = statistics.median([f["liters"] for f in fills]) if fills else 0
	counts = {"away": 0, "no_gps": 0, "no_move": 0, "quick": 0, "big": 0, "over_tank": 0, "odo_back": 0}
	prev = None

	for f in fills:
		t = f["fill_time"].replace(tzinfo=tz).timestamp()
		f["_t"] = t
		flags, level, at_risk = [], NOTE, 0.0
		f.update({"hours_since_prev": None, "gps_km_since_prev": None, "odometer_km_since_prev": None,
		          "gps_status": "Not on IM", "distance_to_station_m": None, "nearest_point_min": None,
		          "truck_lat": None, "truck_lng": None})

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
			level, at_risk = THEFT, f["liters"] - limits["tank"]
			counts["over_tank"] += 1
		elif median_fill and f["liters"] > BIG_FILL_FACTOR * median_fill and f["liters"] >= limits["big"]:
			flags.append(["big_fill", round(f["liters"]), round(f["liters"] / median_fill, 1)])
			counts["big"] += 1

		if track is not None:
			where = track.at_fill(t, f["station_lat"], f["station_lng"])
			f["gps_status"] = where["status"]
			f["nearest_point_min"] = round(where["gap_min"], 1) if where.get("gap_min") is not None else None
			if "dist" in where:
				f["distance_to_station_m"] = int(where["dist"])
				f["truck_lat"], f["truck_lng"] = where["truck_lat"], where["truck_lng"]
			if where["status"] == "Away from station":
				flags.append(["away_fill", round(where["dist"] / 1000, 1)])
				level, at_risk = THEFT, f["liters"]
				counts["away"] += 1
			elif where["status"] == "No GPS at fill time":
				counts["no_gps"] += 1

			if prev:
				km = track.km_between(prev["_t"], t)
				f["gps_km_since_prev"] = round(km, 1)
				covered = track.points_between(prev["_t"], t) > 0
				if covered and km < NO_MOVE_KM and f["liters"] >= limits["min_l"]:
					flags.append(["no_move_fill", round(km, 1)])
					counts["no_move"] += 1
					at_risk = max(at_risk, f["liters"])

		# A large fill or a backwards odometer is shown, but on its own does
		# not put the fill on the flagged list.
		if level == NOTE and any(x[0] not in NOTE_FLAGS for x in flags):
			level = SUSPICIOUS
		f["flag_data"] = json.dumps(flags)
		f["flags"] = "; ".join(_say(x, translate=False) for x in flags)
		f["severity"] = STATUS[level]
		f["liters_at_risk"] = round(at_risk, 1)
		prev = f

	truck = {"counts": counts, "gps_km": None, "consumed": sum(f["liters"] for f in fills[1:]),
	         "odometer_km": None, "first": fills[0]["fill_time"], "last": fills[-1]["fill_time"],
	         "gps_gaps": track is not None and counts["no_gps"] >= GPS_GAP_MIN_FILLS
	                     and counts["no_gps"] / len(fills) >= GPS_GAP_SHARE}
	if track is not None and len(fills) >= 2:
		truck["gps_km"] = round(track.km_between(fills[0]["_t"], fills[-1]["_t"]), 1)
	odos = [f["odometer"] for f in fills if f["odometer"]]
	if len(odos) >= 2 and odos[-1] > odos[0]:
		truck["odometer_km"] = odos[-1] - odos[0]
	return truck


def _apply_reports(t: dict, fills: list, reports: dict, rows: dict) -> None:
	"""What IM's reports add to a truck: distance and idling, and the tank sensor."""
	kind = CONSUMPTION if rows.get(CONSUMPTION) else FILL_DRAIN if rows.get(FILL_DRAIN) else None
	if kind:
		report, row = reports[kind], rows[kind]
		inside = _inside(fills, report)
		t["im_km"] = round(row["km"], 1)
		t["idle_hours"] = round(row["idle_h"], 1)
		t["window_liters"] = round(sum(f["liters"] for f in inside), 1)
		t["_im"] = {"report": report, "row": row, "inside": inside}

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


def _consumption(t: dict, info: dict) -> None:
	"""L/100 km from the best distance there is, net of an idling allowance,
	and the typed odometer against IM's distance."""
	cls = CLASSES[t["vehicle_class"]]
	im = t.get("_im")
	odo_km = info["odometer_km"] or 0
	# A track with the tracker off around many fills is missing whole trips.
	gps_km = 0 if info.get("gps_gaps") else (info["gps_km"] or 0)
	basis = km = liters = None
	allowance = 0.0

	def report_idle(share_from=None, share_to=None) -> float:
		report, row = im["report"], im["row"]
		if share_from is None:
			return row["idle_h"] * cls["idle"]
		whole = (report["end"] - report["start"]).total_seconds()
		shared = (min(report["end"], share_to) - max(report["start"], share_from)).total_seconds()
		return row["idle_h"] * shared / whole * cls["idle"] if whole > 0 and shared > 0 else 0.0

	if gps_km >= MIN_KM_FOR_RATE:
		basis, km, liters = "GPS", gps_km, info["consumed"]
		allowance = report_idle(info["first"], info["last"]) if im else 0.0
	elif (im and im["row"]["km"] >= MIN_KM_FOR_RATE and len(im["inside"]) >= 2
	      and (im["report"]["end"] - im["report"]["start"]).days >= LONG_REPORT_DAYS):
		# Liters after the first fill inside the report, over all of IM's km in
		# it: never more than the truck burned, so a flag here is earned.
		basis, km = "IM report", im["row"]["km"]
		liters = sum(f["liters"] for f in im["inside"][1:])
		allowance = report_idle()
	elif odo_km >= MIN_KM_FOR_RATE and not info["counts"]["odo_back"]:
		basis, km, liters = "Odometer (card)", odo_km, info["consumed"]
	elif gps_km >= SHOW_KM_FOR_RATE:
		basis, km, liters = "GPS", gps_km, info["consumed"]

	t["normal_lp100"], t["theft_lp100"] = cls["normal"], cls["theft"]
	t["lp100_basis"] = basis or ""
	t["_judged"] = bool(basis) and km >= MIN_KM_FOR_RATE
	if basis:
		allowance = min(allowance, liters)
		t["lp100"] = round((liters - allowance) / km * 100, 1)
		t["idle_allowance_l"] = round(allowance) if allowance else None
		t["_excess_l"] = max(0.0, liters - allowance - km * cls["normal"] / 100)

	gap = None
	if odo_km and gps_km >= MIN_KM_FOR_RATE:
		gap = (odo_km - gps_km) / gps_km * 100
	elif im and im["row"]["km"] >= MIN_KM_FOR_RATE:
		odos = [f["odometer"] for f in im["inside"] if f["odometer"]]
		if len(odos) >= 2 and odos[-1] > odos[0]:
			typed = odos[-1] - odos[0]
			# The typed span lies inside the report's dates, so it can only be
			# shorter than IM's distance; longer is the finding.
			if typed > im["row"]["km"]:
				gap = (typed - im["row"]["km"]) / im["row"]["km"] * 100
	t["odometer_gap_pct"] = round(gap) if gap is not None else None


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


def _verdict(t: dict) -> None:
	"""The truck's status is the worst of its findings; each finding also adds
	to a 0-100 score used only for sorting."""
	c, cls, name = t["counts"], CLASSES[t["vehicle_class"]], t["vehicle_class"]
	found = []

	def add(level: int, weight: int, *item) -> None:
		found.append((level, weight, list(item)))

	if c["away"]:
		add(THEFT, 35 * min(c["away"], 2), "away", c["away"])
	if c["over_tank"]:
		add(THEFT, 30, "over_tank", c["over_tank"], name, cls["tank"])

	if t.get("has_sensor"):
		card, missing = flt(t["sensor_card_l"]), flt(t["missing_l"])
		pct = missing / card * 100 if card else 0
		shortfall = ("missing", round(card), round(flt(t["sensor_fill_l"])), round(missing))
		if card >= SENSOR_MIN_CARD_L and missing >= MISSING_THEFT[0] and pct >= MISSING_THEFT[1]:
			add(THEFT, 40, *shortfall)
			t["_missing_flag"] = True
		elif card >= SENSOR_MIN_CARD_L and missing >= MISSING_SUSPICIOUS[0] and pct >= MISSING_SUSPICIOUS[1]:
			add(SUSPICIOUS, 15, *shortfall)
			t["_missing_flag"] = True
		drained = flt(t["drain_l"])
		if drained >= DRAIN_THEFT_L:
			add(THEFT, 40, "drain", round(drained), t["drains"])
		elif drained > 0:
			add(SUSPICIOUS, 10, "small_drain", round(drained, 1))
		if flt(t["slow_drain_l"]) >= SLOW_DRAIN_L:
			add(SUSPICIOUS, 10, "slow_drain", round(flt(t["slow_drain_l"])))

	lp, basis = t.get("lp100"), t.get("lp100_basis")
	if lp is not None and t.get("_judged"):
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
			t["_excess_flag"] = True
		elif lp > cls["normal"]:
			add(SUSPICIOUS, 12, "burn_high", lp, by, name, cls["normal"])
			t["_excess_flag"] = True

	excess = None
	if basis == "GPS" and t.get("_judged") and lp is not None and t.get("peer_lp100"):
		excess = (lp - t["peer_lp100"]) / t["peer_lp100"] * 100
		if excess >= PEER_EXCESS:
			add(SUSPICIOUS, 8, "peers", round(excess))
	t["excess_pct"] = round(excess) if excess is not None else None

	gap = t.get("odometer_gap_pct")
	if gap is not None and abs(gap) >= ODOMETER_GAP:
		add(SUSPICIOUS, 10, "odo_gap", f"+{gap}" if gap > 0 else str(gap))
	if c["no_move"]:
		add(SUSPICIOUS, 15 * min(c["no_move"], 2), "no_move", c["no_move"])
	if c["quick"]:
		add(SUSPICIOUS if c["quick"] >= 2 else NOTE, 5 * min(c["quick"], 4), "quick", c["quick"], QUICK_REFILL_H)
	if c["odo_back"]:
		add(NOTE, 0, "odo_back", c["odo_back"])
	if c["big"]:
		add(NOTE, 0, "big", c["big"])
	if c["no_gps"] and t.get("_gps_gaps"):
		off = c["no_gps"] / t["fills"] >= GPS_OFF_SUSPICIOUS
		add(SUSPICIOUS if off else NOTE, 10 if off else 0, "gps_off", c["no_gps"], t["fills"])
	elif c["no_gps"]:
		add(NOTE, 0, "no_gps", c["no_gps"])
	if t.get("_error"):
		add(NOTE, 0, "gps_failed", t["_error"])

	found.sort(key=lambda x: (-x[0], -x[1]))
	t["verdict"] = STATUS[found[0][0] if found else NOTE]
	t["finding_data"] = json.dumps([[level] + item for level, _weight, item in found])
	t["issues"] = "; ".join(_say(item, translate=False) for _level, _weight, item in found)
	t["score"] = min(100, sum(x[1] for x in found))

	# Liters at risk: the biggest single estimate, never a sum -- a fill away
	# from the station is also part of the excess consumption.
	risk = [t["fill_risk"]]
	if t.get("_missing_flag"):
		risk.append(flt(t["missing_l"]))
	if t.get("has_sensor"):
		risk.append(flt(t["drain_l"]) + flt(t["slow_drain_l"]))
	if t.get("_excess_flag"):
		risk.append(t["_excess_l"])
	t["liters_at_risk"] = round(max(risk), 1)
	t["cost_at_risk"] = round(t["liters_at_risk"] * (t["cost"] / t["liters"] if t["liters"] else 0), 2)


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


def run_analysis(fuel_import: str, user: str | None = None) -> None:
	"""Background job: read the files, fetch GPS per IM truck, store findings."""
	# Progress is written in English whatever the uploader's language; the
	# dashboard puts it into whichever language it is showing.
	frappe.local.lang = "en"
	try:
		_run(fuel_import, user)
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Failed", "progress": "",
		                                             "error": frappe.get_traceback()[-2000:]})
		frappe.db.commit()
		frappe.log_error(frappe.get_traceback(), f"Fuel Efficiency failed: {fuel_import}")
		_progress(fuel_import, "Failed", stage="failed", user=user)


def _report_note(kind: str, report: dict, fills: list, matched: int) -> list:
	period = _period_text(report["start"], report["end"])
	inside = len(_inside(fills, report))
	if not inside:
		first, last = min(f["fill_time"] for f in fills), max(f["fill_time"] for f in fills)
		return ["report_unused", KIND_LABEL[kind], period, _period_text(first, last, dates_only=True)]
	return ["report_used", KIND_LABEL[kind], period, matched, len(report["rows"]), inside]


def _run(fuel_import: str, user: str | None) -> None:
	doc = frappe.get_doc(IMPORT_DT, fuel_import)
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
			"fill_risk": sum(f["liters_at_risk"] for f in truck_fills), "_error": error, "_gps_gaps": info["gps_gaps"],
		}
		rows = {kind: _report_row(index[kind], entry, key) for kind in reports}
		for kind, row in rows.items():
			if row:
				used[kind].add(id(row))
		_apply_reports(t, truck_fills, reports, rows)
		_consumption(t, info)
		c = info["counts"]
		t.update({"away_fills": c["away"], "no_gps_fills": c["no_gps"], "no_move_fills": c["no_move"],
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
	"distance_to_station_m", "nearest_point_min", "hours_since_prev", "gps_km_since_prev",
	"odometer_km_since_prev", "gps_km", "im_km", "lp100", "peer_lp100", "excess_pct", "idle_hours",
	"idle_allowance_l", "odometer_km", "odometer_gap_pct", "window_liters", "sensor_card_l", "sensor_fill_l",
	"sensor_fills", "missing_l", "drain_l", "drains", "slow_drain_l", "sensor_consumed_l", "sensor_lp100",
	"start_level", "last_level",
}
NUMBERS = {
	"liters_at_risk", "liters", "price", "cost", "odometer", "provider_kmpl", "station_lat", "station_lng",
	"truck_lat", "truck_lng", "score", "fills", "gps_points", "away_fills", "no_gps_fills", "no_move_fills",
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
	frappe.enqueue("app_apis.fuel_efficiency.run_analysis", queue="long", timeout=7200,
	               fuel_import=fuel_import, user=frappe.session.user, job_name=f"fuel_efficiency::{fuel_import}",
	               enqueue_after_commit=True)


@frappe.whitelist()
def get_translations(lang: str = "ar") -> dict:
	"""The dashboard's own words in `lang` ({english or "english:context": text}),
	so the page can switch language without changing the user's desk language."""
	frappe.only_for(READ_ROLES)
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
def upload_files(files, fuel_import: str = "", lang: str = "") -> dict:
	"""Register uploaded .xlsx files (already saved by Frappe's uploader) and
	queue the analysis. Each file is recognised by its content.

	Without `fuel_import` this starts a new report, and the fuel-card export
	is required. With it, the files are added to that report -- an IM report
	replaces the one of its kind -- and the report is analysed again."""
	frappe.only_for(RUN_ROLES)
	_use_lang(lang)
	urls = frappe.parse_json(files) if isinstance(files, str) else files
	urls = [str(u or "").strip() for u in (urls or []) if str(u or "").strip()]
	if not urls:
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
def list_imports() -> list:
	frappe.only_for(READ_ROLES)
	return frappe.get_all(IMPORT_DT, fields=["name", "title", "status", "period_from", "period_to", "fills", "analysed_on"],
	                      order_by="creation desc", limit_page_length=50)


@frappe.whitelist()
def get_progress(fuel_import: str) -> dict:
	frappe.only_for(READ_ROLES)
	return frappe.db.get_value(IMPORT_DT, fuel_import, ["status", "progress", "error"], as_dict=True) or {}


@frappe.whitelist()
def get_summary(fuel_import: str, lang: str = "") -> dict:
	frappe.only_for(READ_ROLES)
	_use_lang(lang)
	doc =frappe.get_doc(IMPORT_DT, fuel_import)
	verdicts = dict(frappe.db.sql(f"select verdict, count(*) from `tab{VEHICLE_DT}` where fuel_import=%s group by verdict", fuel_import))
	severity = dict(frappe.db.sql(f"select severity, count(*) from `tab{FILL_DT}` where fuel_import=%s group by severity", fuel_import))
	away = frappe.db.count(FILL_DT, {"fuel_import": fuel_import, "gps_status": "Away from station"})
	sensor = frappe.db.sql(
		f"""select count(*), sum(greatest(missing_l + 0, 0)), sum(drain_l + 0) + sum(slow_drain_l + 0)
		from `tab{VEHICLE_DT}` where fuel_import=%s and has_sensor=1""", fuel_import)[0]

	def distinct(field):
		return [r[0] for r in frappe.db.sql(
			f"select distinct {field} from `tab{VEHICLE_DT}` where fuel_import=%s and ifnull({field},'')!='' order by {field}",
			fuel_import)]

	return {
		"import": doc.as_dict(no_default_fields=True) | {"name": doc.name},
		"notes": [{"warn": n[0] in WARN_NOTES, "text": _say(n)} for n in _load(doc.note_data) if n],
		"verdicts": verdicts,
		"severity": severity,
		"away_fills": away,
		"sensor": {"trucks": cint(sensor[0]), "missing_l": flt(sensor[1], 1), "drained_l": flt(sensor[2], 1)},
		"brands": distinct("brand"),
		"branches": distinct("branch"),
		"classes": distinct("vehicle_class"),
		"limits": [{"class": k, "normal": v["normal"], "theft": v["theft"], "idle": v["idle"], "tank": v["tank"]}
		           for k, v in CLASSES.items()],
		"can_run": bool(set(frappe.get_roles()) & set(RUN_ROLES)),
	}


def _like(value: str) -> str:
	return "%" + str(value).replace("%", "").replace("_", "\\_") + "%"


# The dashboard's "Main reason" and "Reason" column filters -> the stored codes.
FINDING_GROUPS = {
	"away": ("away",), "missing": ("missing",), "drain": ("drain", "small_drain", "slow_drain"),
	"burn": ("burn_theft", "burn_high", "peers"), "odometer": ("odo_gap", "odo_back"), "no_move": ("no_move",),
	"quick": ("quick",), "big": ("big", "over_tank"), "no_gps": ("no_gps", "gps_failed", "gps_off"),
}
FLAG_GROUPS = {
	"away": ("away_fill",), "no_move": ("no_move_fill",), "quick": ("quick_fill",), "big": ("big_fill",),
	"over_tank": ("over_tank_fill",), "odometer": ("odo_back_fill",),
}


def _has_code(field: str, codes: tuple, where: list, vals: list) -> None:
	# Codes sit quoted in the stored JSON ('[2, "away", 1]'), so '"drain"'
	# never matches "small_drain".
	where.append("(" + " or ".join(f"{field} like %s" for _code in codes) + ")")
	vals += [f'%"{code}"%' for code in codes]


@frappe.whitelist()
def get_vehicles(fuel_import: str, verdict: str = "", brand: str = "", branch: str = "", source: str = "",
                 search: str = "", start: int = 0, limit: int = 100, checked: str = "", vehicle_class: str = "",
                 plate: str = "", vehicle: str = "", rate: str = "", finding: str = "", risk: str = "",
                 lang: str = "") -> dict:
	"""Trucks of one report. The filters are the dashboard's column headings:
	`vehicle` is 'c:<class>', 'b:<brand>' or 'r:<branch>'; `rate` is
	above_normal, above_theft or unjudged; `finding` a FINDING_GROUPS key."""
	frappe.only_for(READ_ROLES)
	_use_lang(lang)
	where, vals = ["fuel_import=%s"], [fuel_import]
	kind, _sep, picked = str(vehicle or "").partition(":")
	fields = {"verdict": verdict, "brand": brand, "branch": branch, "vehicle_class": vehicle_class}
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
	cond = " and ".join(where)
	order = "field(verdict, 'Theft likely', 'Suspicious', 'OK'), liters_at_risk desc, score desc, liters desc"
	total = frappe.db.sql(f"select count(*) from `tab{VEHICLE_DT}` where {cond}", vals)[0][0]
	rows = frappe.db.sql(
		f"select {', '.join(VEHICLE_FIELDS)} from `tab{VEHICLE_DT}` where {cond} order by {order} limit %s offset %s",
		vals + [cint(limit) or 100, cint(start)], as_dict=True)
	for r in rows:
		items = _load(r.pop("finding_data", None))
		r["findings"] = [{"level": item[0], "text": _say(item[1:])} for item in items if len(item) > 1]
		if items:
			r["issues"] = "; ".join(x["text"] for x in r["findings"])
	return {"total": total, "rows": rows}


@frappe.whitelist()
def get_fills(fuel_import: str, plate_key: str = "", plate: str = "", flagged: int = 0, severity: str = "",
              search: str = "", start: int = 0, limit: int = 500, plate_like: str = "", station: str = "",
              driver: str = "", gps_status: str = "", flag: str = "", lang: str = "") -> dict:
	"""Fills of one report: one truck's (`plate_key`/`plate`, for its dialog)
	or the flagged ones, filtered by the dashboard's column headings."""
	frappe.only_for(READ_ROLES)
	_use_lang(lang)
	where, vals = ["fuel_import=%s"], [fuel_import]
	if plate_key:
		where.append("plate_key=%s")
		vals.append(plate_key)
	elif plate:
		where.append("plate=%s")
		vals.append(plate)
	if severity:
		where.append("severity=%s")
		vals.append(severity)
	elif cint(flagged):
		where.append("severity in ('Suspicious', 'Theft likely')")
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
	order = "fill_time asc" if (plate_key or plate) else "field(severity, 'Theft likely', 'Suspicious', 'OK'), liters_at_risk desc, fill_time desc"
	total = frappe.db.sql(f"select count(*) from `tab{FILL_DT}` where {cond}", vals)[0][0]
	rows = frappe.db.sql(
		f"select name, {', '.join(FILL_FIELDS)} from `tab{FILL_DT}` where {cond} order by {order} limit %s offset %s",
		vals + [cint(limit) or 500, cint(start)], as_dict=True)
	for r in rows:
		items = _load(r.pop("flag_data", None))
		if items:
			r["flags"] = "; ".join(_say(item) for item in items)
	return {"total": total, "rows": rows}
