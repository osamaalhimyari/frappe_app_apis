# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Fuel Efficiency: does the fuel a truck bought match what its GPS says?

A fuel-card export is uploaded on the Fuel Efficiency dashboard -- one row per
fill: plate, liters, cost, the odometer the driver typed, the station and its
coordinates. Every truck we track on IM is then checked against its own GPS
track for the same period:

  * Away from station: at every GPS point within FILL_WINDOW_MIN of the
    invoice, the truck was more than AWAY_M from the pump. The clearest sign
    that the fuel went somewhere other than this tank.
  * Refuelled without driving: less than NO_MOVE_KM of GPS distance since the
    previous fill, yet a real fill.
  * Refilled too soon: within QUICK_REFILL_H of the previous fill.
  * Consumption: liters per 100 GPS km against the median of trucks of the
    same brand and model in the same report.
  * Odometer vs GPS: the odometer on the card is typed by the driver and is
    routinely wrong (one June file had 1,133 fills under 1 km/L), so distance
    always comes from GPS; the gap is reported, never trusted.

Trucks on no platform get only the checks the file supports on its own.

Pilot: its only documented history call (`cmd=gettrack`) needs a track id
from a separate asynchronous `rungeo` job, so a truck found only on Pilot is
reported as "No GPS data" rather than guessed at.
"""

import bisect
import math
import re
import statistics
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, flt, get_system_timezone, now_datetime

IMPORT_DT = "App Apis Fuel Import"
FILL_DT = "App Apis Fuel Fill"
VEHICLE_DT = "App Apis Fuel Vehicle"
PROGRESS_EVENT = "fuel_efficiency_progress"

READ_ROLES = ["System Manager", "Technical", "Support Team"]
RUN_ROLES = ["System Manager", "Technical"]

# What "suspicious" means, in one place.
FILL_WINDOW_MIN = 30      # GPS points this close to the invoice time are looked at
COVERAGE_MIN = 20         # at least one must be this close, else "No GPS at fill time"
AT_STATION_M = 700        # truck within this of the pump: at the station
AWAY_M = 1500             # never closer than this: away from the station
NO_MOVE_KM = 20           # less GPS distance than this since the last fill...
NO_MOVE_MIN_L = 60        # ...yet a fill of at least this many liters
QUICK_REFILL_H = 3        # a second fill this soon after the last
BIG_FILL_FACTOR = 1.8     # this many times the truck's own median fill...
BIG_FILL_MIN_L = 250      # ...and at least this many liters
MIN_KM_FOR_RATE = 300     # GPS km needed before L/100 km is judged
SUSPICIOUS_EXCESS = 30    # % above the peer median
THEFT_EXCESS = 60
ODOMETER_GAP = 25         # % between the typed odometer and GPS
MIN_STEP_M = 30           # GPS jitter below this is not movement
MAX_SPEED_KMH = 150       # faster than this between two points is a GPS jump
CHUNK_DAYS = 16           # track logs are read this many days at a time

VERDICTS = ("Theft likely", "Suspicious", "OK", "No GPS data", "Not tracked")

FILL_FIELDS = [
	"fuel_import", "fill_time", "plate", "plate_key", "brand", "model", "branch", "driver",
	"severity", "flags", "liters_at_risk", "invoice_no", "liters", "price", "cost", "odometer",
	"provider_kmpl", "station", "station_branch", "station_area", "station_lat", "station_lng",
	"source", "imei", "gps_status", "distance_to_station_m", "nearest_point_min", "truck_lat",
	"truck_lng", "hours_since_prev", "gps_km_since_prev", "odometer_km_since_prev",
]
VEHICLE_FIELDS = [
	"fuel_import", "plate", "plate_key", "verdict", "score", "brand", "model", "branch", "drivers",
	"source", "imei", "platform_name", "fills", "liters", "cost", "first_fill", "last_fill",
	"gps_km", "lp100", "peer_lp100", "excess_pct", "odometer_km", "odometer_gap_pct", "gps_points",
	"away_fills", "no_gps_fills", "no_move_fills", "quick_refills", "big_fills", "liters_at_risk",
	"cost_at_risk", "issues",
]


# --------------------------------------------------------------------------
# Reading the fuel-card export
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


def parse_workbook(path: str) -> tuple[list[dict], int]:
	"""(fills, rows skipped). Finds the header row itself, so a title line or
	two above it do not matter; stops at the totals rows at the bottom because
	those have no plate or no date."""
	import openpyxl

	wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
	try:
		col, fills, skipped = None, [], 0
		for i, row in enumerate(wb.worksheets[0].iter_rows(values_only=True)):
			if col is None:
				if i > 20:
					break
				names = [_header(c) for c in row]
				found = {}
				for key, options in HEADERS.items():
					for option in options:
						if option in names:
							found[key] = names.index(option)
							break
				if {"plate", "liters", "fill_time"} <= set(found):
					col = found
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
	finally:
		wb.close()

	if col is None:
		frappe.throw(
			_("This does not look like a fuel-card export: no header row with Vehicle, Number of liters and Date."),
			title=_("Fuel Efficiency"),
		)
	return fills, skipped


# --------------------------------------------------------------------------
# Plates
# --------------------------------------------------------------------------

# Saudi plate letters, Arabic -> the Latin letter printed beside it.
AR2EN = {
	"ا": "A", "أ": "A", "إ": "A", "آ": "A", "ب": "B", "ح": "J", "د": "D", "ر": "R", "س": "S",
	"ص": "X", "ط": "T", "ع": "E", "ق": "G", "ك": "K", "ل": "L", "م": "Z", "ن": "N", "ه": "H",
	"و": "U", "ى": "V", "ي": "V",
}
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


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


def _platforms() -> tuple[dict, dict, list]:
	"""Plate key -> platform entry, (forward, reversed letters), and notes."""
	notes, fwd, rev = [], {}, {}

	def add(key_fwd, key_rev, entry):
		if key_fwd:
			fwd.setdefault(key_fwd, entry)
		if key_rev:
			rev.setdefault(key_rev, entry)

	from app_apis import im_connector as im

	res = im.fetch_fleet()
	if cint(res.get("code")) == 0:
		for v in res.get("rows") or []:
			vehicle_no = str(v.get("vehicle_no") or v.get("vehicle_name") or "").strip()
			if not vehicle_no:
				continue
			entry = {"source": "IM", "imei": str(v.get("imei") or ""), "vehicle_no": vehicle_no,
			         "name": str(v.get("vehicle_name") or vehicle_no)}
			for field in ("vehicle_no", "vehicle_name"):
				add(plate_key(v.get(field)), plate_key(v.get(field), True), entry)
	else:
		notes.append(_("IM fleet could not be read: {0}").format(res.get("msg")))

	# Pilot, from the Fleet Audit snapshot -- identified only; see the module note.
	try:
		for r in frappe.get_all("app_apis_fleet_audit", filters={"on_pilot": 1},
		                        fields=["imei", "pilot_vehicle", "plate"], limit_page_length=0):
			entry = {"source": "Pilot", "imei": str(r.imei or ""), "vehicle_no": "",
			         "name": str(r.pilot_vehicle or r.plate or "")}
			for value in (r.pilot_vehicle, r.plate):
				k_f, k_r = plate_key(value), plate_key(value, True)
				if k_f and k_f not in fwd:
					fwd[k_f] = entry
				if k_r and k_r not in rev:
					rev[k_r] = entry
	except Exception:
		notes.append(_("Pilot vehicles could not be read from the Fleet Audit snapshot."))

	return fwd, rev, notes


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


def _check_truck(fills: list, track: "Track | None", tz) -> dict:
	"""Fill-level findings written onto each fill dict, truck-level numbers returned."""
	fills.sort(key=lambda f: f["fill_time"])
	median_fill = statistics.median([f["liters"] for f in fills]) if fills else 0
	counts = {"away": 0, "no_gps": 0, "no_move": 0, "quick": 0, "big": 0, "odo_back": 0}
	prev = None

	for f in fills:
		t = f["fill_time"].replace(tzinfo=tz).timestamp()
		f["_t"] = t
		flags, severity, at_risk = [], "OK", 0.0
		f.update({"hours_since_prev": None, "gps_km_since_prev": None, "odometer_km_since_prev": None,
		          "gps_status": "Not tracked", "distance_to_station_m": None, "nearest_point_min": None,
		          "truck_lat": None, "truck_lng": None})

		if prev:
			hours = (t - prev["_t"]) / 3600
			f["hours_since_prev"] = round(hours, 1)
			if f["odometer"] and prev["odometer"]:
				f["odometer_km_since_prev"] = f["odometer"] - prev["odometer"]
				if f["odometer_km_since_prev"] < 0:
					flags.append(_("Odometer went backwards"))
					counts["odo_back"] += 1
			if hours < QUICK_REFILL_H and f["liters"] >= NO_MOVE_MIN_L:
				flags.append(_("Refilled {0} h after the last fill").format(round(hours, 1)))
				counts["quick"] += 1

		if median_fill and f["liters"] > BIG_FILL_FACTOR * median_fill and f["liters"] >= BIG_FILL_MIN_L:
			flags.append(_("{0} L is {1}x this truck's usual fill").format(round(f["liters"]), round(f["liters"] / median_fill, 1)))
			counts["big"] += 1

		if track is not None:
			where = track.at_fill(t, f["station_lat"], f["station_lng"])
			f["gps_status"] = where["status"]
			f["nearest_point_min"] = round(where["gap_min"], 1) if where.get("gap_min") is not None else None
			if "dist" in where:
				f["distance_to_station_m"] = int(where["dist"])
				f["truck_lat"], f["truck_lng"] = where["truck_lat"], where["truck_lng"]
			if where["status"] == "Away from station":
				flags.append(_("Truck was {0} km from the station").format(round(where["dist"] / 1000, 1)))
				severity, at_risk = "Theft likely", f["liters"]
				counts["away"] += 1
			elif where["status"] == "No GPS at fill time":
				counts["no_gps"] += 1

			if prev:
				km = track.km_between(prev["_t"], t)
				f["gps_km_since_prev"] = round(km, 1)
				covered = track.points_between(prev["_t"], t) > 0
				if covered and km < NO_MOVE_KM and f["liters"] >= NO_MOVE_MIN_L:
					flags.append(_("Refuelled after only {0} km").format(round(km, 1)))
					counts["no_move"] += 1
					at_risk = max(at_risk, f["liters"])

		if flags and severity == "OK":
			severity = "Suspicious"
		f["flags"] = "; ".join(flags)
		f["severity"] = severity
		f["liters_at_risk"] = round(at_risk, 1)
		prev = f

	truck = {"counts": counts, "gps_km": None, "consumed": sum(f["liters"] for f in fills[1:]),
	         "odometer_km": None}
	if track is not None and len(fills) >= 2:
		truck["gps_km"] = round(track.km_between(fills[0]["_t"], fills[-1]["_t"]), 1)
	odos = [f["odometer"] for f in fills if f["odometer"]]
	if len(odos) >= 2 and odos[-1] > odos[0]:
		truck["odometer_km"] = odos[-1] - odos[0]
	return truck


def _peer_rates(trucks: list) -> None:
	"""Median L/100 km of the same brand+model (then brand, then everyone)."""
	judged = [t for t in trucks if t.get("lp100") and (t.get("gps_km") or 0) >= MIN_KM_FOR_RATE]

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
	c = t["counts"]
	judged = (t.get("gps_km") or 0) >= MIN_KM_FOR_RATE and t.get("lp100") and t.get("peer_lp100")
	excess = ((t["lp100"] - t["peer_lp100"]) / t["peer_lp100"] * 100) if judged else None
	gap = ((t["odometer_km"] - t["gps_km"]) / t["gps_km"] * 100) if (t.get("odometer_km") and (t.get("gps_km") or 0) >= MIN_KM_FOR_RATE) else None
	t["excess_pct"] = round(excess) if excess is not None else None
	t["odometer_gap_pct"] = round(gap) if gap is not None else None

	issues = []
	if c["away"]:
		issues.append(_("{0} fill(s) paid for while the truck was away from the station").format(c["away"]))
	if excess is not None and excess >= SUSPICIOUS_EXCESS:
		issues.append(_("uses {0}% more fuel per km than similar trucks").format(round(excess)))
	if c["no_move"]:
		issues.append(_("{0} fill(s) after almost no driving").format(c["no_move"]))
	if c["quick"]:
		issues.append(_("{0} refill(s) within {1} h").format(c["quick"], QUICK_REFILL_H))
	if c["big"]:
		issues.append(_("{0} unusually large fill(s)").format(c["big"]))
	if c["odo_back"]:
		issues.append(_("odometer went backwards {0} time(s)").format(c["odo_back"]))
	if gap is not None and abs(gap) >= ODOMETER_GAP:
		issues.append(_("typed odometer is {0}% off the GPS distance").format(round(gap)))
	if c["no_gps"]:
		issues.append(_("no GPS around {0} fill(s)").format(c["no_gps"]))

	excess_l = max(0.0, t["consumed"] - t["peer_lp100"] * t["gps_km"] / 100) if judged else 0.0
	t["liters_at_risk"] = round(max(excess_l, t["fill_risk"]), 1)
	t["cost_at_risk"] = round(t["liters_at_risk"] * (t["cost"] / t["liters"] if t["liters"] else 0), 2)

	if not t["source"]:
		verdict = "Not tracked"
	elif not t["gps_points"]:
		verdict = "No GPS data"
	elif c["away"] or (excess is not None and excess >= THEFT_EXCESS):
		verdict = "Theft likely"
	elif ((excess is not None and excess >= SUSPICIOUS_EXCESS) or c["no_move"] or c["quick"] >= 2
	      or c["big"] >= 2 or c["odo_back"] or (gap is not None and abs(gap) >= ODOMETER_GAP)):
		verdict = "Suspicious"
	else:
		verdict = "OK"

	t["verdict"] = verdict
	t["issues"] = "; ".join(issues)
	t["score"] = min(100, 35 * min(c["away"], 2) + 20 * min(c["no_move"], 2) + 5 * min(c["quick"], 4)
	                 + 5 * min(c["big"], 2) + (min(60, max(0, round(excess or 0))) // 2)
	                 + (10 if gap is not None and abs(gap) >= ODOMETER_GAP else 0))


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
	"""Background job: read the file, fetch GPS per tracked truck, store findings."""
	try:
		_run(fuel_import, user)
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Failed", "progress": "",
		                                             "error": frappe.get_traceback()[-2000:]})
		frappe.db.commit()
		frappe.log_error(frappe.get_traceback(), f"Fuel Efficiency failed: {fuel_import}")
		_progress(fuel_import, "Failed", stage="failed", user=user)


def _run(fuel_import: str, user: str | None) -> None:
	doc = frappe.get_doc(IMPORT_DT, fuel_import)
	frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Running", "error": ""})
	_progress(fuel_import, "Reading the file…", user=user)

	path = frappe.get_doc("File", {"file_url": doc.source_file}).get_full_path()
	fills, _skipped = parse_workbook(path)
	tz = ZoneInfo(get_system_timezone() or "Asia/Riyadh")

	_progress(fuel_import, "Matching plates to tracked vehicles…", user=user)
	fwd, rev, notes = _platforms()

	trucks = {}
	for f in fills:
		key = f["plate_key"] or f"raw:{f['plate']}"
		trucks.setdefault(key, []).append(f)

	tracked = [k for k in trucks if (fwd.get(k) or rev.get(k) or {}).get("source") == "IM"]
	done, results = 0, []
	for key, truck_fills in trucks.items():
		entry = fwd.get(key) or rev.get(key) or {}
		track, points, error = None, 0, ""
		if entry.get("source") == "IM":
			done += 1
			_progress(fuel_import, _("Reading GPS {0} of {1}: {2}").format(done, len(tracked), truck_fills[0]["plate"]),
			          done, len(tracked), user=user)
			first = min(f["fill_time"] for f in truck_fills) - timedelta(hours=6)
			last = max(f["fill_time"] for f in truck_fills) + timedelta(hours=1)
			pts, error = _im_points(entry["vehicle_no"], first, last, tz)
			if pts:
				track, points = Track(pts), len(pts)

		info = _check_truck(truck_fills, track, tz)
		liters = sum(f["liters"] for f in truck_fills)
		lp100 = (info["consumed"] / info["gps_km"] * 100) if info["gps_km"] and info["gps_km"] >= 50 else None
		base = truck_fills[0]
		results.append({
			"plate": base["plate"], "plate_key": base["plate_key"], "brand": base["brand"], "model": base["model"],
			"branch": base["branch"],
			"drivers": ", ".join(sorted({f["driver"] for f in truck_fills if f["driver"]}))[:1000],
			"source": entry.get("source", ""), "imei": entry.get("imei", ""), "platform_name": entry.get("name", ""),
			"fills": len(truck_fills), "liters": round(liters, 1), "cost": round(sum(f["cost"] for f in truck_fills), 2),
			"first_fill": truck_fills[0]["fill_time"], "last_fill": truck_fills[-1]["fill_time"],
			"gps_km": info["gps_km"], "lp100": round(lp100, 1) if lp100 else None,
			"odometer_km": info["odometer_km"], "gps_points": points, "counts": info["counts"],
			"consumed": info["consumed"], "fill_risk": sum(f["liters_at_risk"] for f in truck_fills),
			"_error": error,
		})
		for f in truck_fills:
			f["source"], f["imei"] = entry.get("source", ""), entry.get("imei", "")
			if entry.get("source") == "Pilot":
				f["gps_status"] = "Pilot: no GPS history yet"
			elif entry.get("source") == "IM" and track is None:
				f["gps_status"] = "No GPS data"

	_peer_rates(results)
	for t in results:
		_verdict(t)
		if t["_error"]:
			t["issues"] = "; ".join(filter(None, [t["issues"], _("GPS read failed: {0}").format(t["_error"])]))

	_progress(fuel_import, "Saving…", user=user)
	_store(fuel_import, fills, results)

	flagged = sum(1 for f in fills if f["severity"] != "OK")
	frappe.db.set_value(IMPORT_DT, fuel_import, {
		"status": "Done",
		"progress": "; ".join(notes)[:140] if notes else "",
		"analysed_on": now_datetime(),
		"fills": len(fills),
		"vehicles": len(results),
		"tracked_vehicles": sum(1 for t in results if t["gps_points"]),
		"liters": round(sum(f["liters"] for f in fills), 1),
		"cost": round(sum(f["cost"] for f in fills), 2),
		"liters_at_risk": round(sum(t["liters_at_risk"] for t in results), 1),
		"cost_at_risk": round(sum(t["cost_at_risk"] for t in results), 2),
	})
	frappe.db.commit()
	_progress(fuel_import, _("Done: {0} flagged fills").format(flagged), stage="done", user=user)


def _store(fuel_import: str, fills: list, trucks: list) -> None:
	frappe.db.delete(FILL_DT, {"fuel_import": fuel_import})
	frappe.db.delete(VEHICLE_DT, {"fuel_import": fuel_import})
	now, user = now_datetime(), frappe.session.user
	meta_cols = ["name", "creation", "modified", "modified_by", "owner", "docstatus", "idx"]

	def rows(items, fields):
		for i, item in enumerate(items):
			item["fuel_import"] = fuel_import
			yield [frappe.generate_hash(length=12), now, now, user, user, 0, i] + [item.get(f) for f in fields]

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
def upload_report(file_url: str) -> dict:
	"""Register an uploaded .xlsx (already saved by Frappe's uploader) and queue its analysis."""
	frappe.only_for(RUN_ROLES)
	file_doc = frappe.get_doc("File", {"file_url": str(file_url or "").strip()})
	fills, skipped = parse_workbook(file_doc.get_full_path())
	if not fills:
		frappe.throw(_("No usable fills in this file."), title=_("Fuel Efficiency"))

	start, end = min(f["fill_time"] for f in fills), max(f["fill_time"] for f in fills)
	doc = frappe.get_doc({
		"doctype": IMPORT_DT,
		"title": f"{file_doc.file_name} · {start:%d %b} – {end:%d %b %Y}",
		"status": "Queued",
		"period_from": start.date(),
		"period_to": end.date(),
		"source_file": file_doc.file_url,
		"file_name": file_doc.file_name,
		"fills": len(fills),
		"progress": _("Queued"),
	}).insert(ignore_permissions=True)
	_enqueue(doc.name)
	return {"name": doc.name, "fills": len(fills), "skipped": skipped, "title": doc.title}


@frappe.whitelist()
def reanalyse(fuel_import: str) -> dict:
	frappe.only_for(RUN_ROLES)
	frappe.db.set_value(IMPORT_DT, fuel_import, {"status": "Queued", "progress": _("Queued"), "error": ""})
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
def get_summary(fuel_import: str) -> dict:
	frappe.only_for(READ_ROLES)
	doc = frappe.get_doc(IMPORT_DT, fuel_import)
	verdicts = dict(frappe.db.sql(f"select verdict, count(*) from `tab{VEHICLE_DT}` where fuel_import=%s group by verdict", fuel_import))
	severity = dict(frappe.db.sql(f"select severity, count(*) from `tab{FILL_DT}` where fuel_import=%s group by severity", fuel_import))
	away = frappe.db.count(FILL_DT, {"fuel_import": fuel_import, "gps_status": "Away from station"})
	return {
		"import": doc.as_dict(no_default_fields=True) | {"name": doc.name},
		"verdicts": verdicts,
		"severity": severity,
		"away_fills": away,
		"brands": [r[0] for r in frappe.db.sql(f"select distinct brand from `tab{VEHICLE_DT}` where fuel_import=%s and ifnull(brand,'')!='' order by brand", fuel_import)],
		"branches": [r[0] for r in frappe.db.sql(f"select distinct branch from `tab{VEHICLE_DT}` where fuel_import=%s and ifnull(branch,'')!='' order by branch", fuel_import)],
	}


def _like(value: str) -> str:
	return "%" + str(value).replace("%", "").replace("_", "\\_") + "%"


@frappe.whitelist()
def get_vehicles(fuel_import: str, verdict: str = "", brand: str = "", branch: str = "", source: str = "",
                 search: str = "", start: int = 0, limit: int = 100) -> dict:
	frappe.only_for(READ_ROLES)
	where, vals = ["fuel_import=%s"], [fuel_import]
	for field, value in (("verdict", verdict), ("brand", brand), ("branch", branch)):
		if value:
			where.append(f"{field}=%s")
			vals.append(value)
	if source == "tracked":
		where.append("gps_points > 0")
	elif source == "untracked":
		where.append("ifnull(gps_points, 0) = 0")
	if search:
		where.append("(plate like %s or plate_key like %s or drivers like %s or platform_name like %s)")
		vals += [_like(search)] * 4
	cond = " and ".join(where)
	order = ("field(verdict, 'Theft likely', 'Suspicious', 'OK', 'No GPS data', 'Not tracked'), "
	         "liters_at_risk desc, score desc, liters desc")
	total = frappe.db.sql(f"select count(*) from `tab{VEHICLE_DT}` where {cond}", vals)[0][0]
	rows = frappe.db.sql(
		f"select {', '.join(VEHICLE_FIELDS)} from `tab{VEHICLE_DT}` where {cond} order by {order} limit %s offset %s",
		vals + [cint(limit) or 100, cint(start)], as_dict=True)
	return {"total": total, "rows": rows}


@frappe.whitelist()
def get_fills(fuel_import: str, plate_key: str = "", plate: str = "", flagged: int = 0, severity: str = "",
              search: str = "", start: int = 0, limit: int = 500) -> dict:
	frappe.only_for(READ_ROLES)
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
	cond = " and ".join(where)
	order = "fill_time asc" if (plate_key or plate) else "field(severity, 'Theft likely', 'Suspicious', 'OK'), liters_at_risk desc, fill_time desc"
	total = frappe.db.sql(f"select count(*) from `tab{FILL_DT}` where {cond}", vals)[0][0]
	rows = frappe.db.sql(
		f"select name, {', '.join(FILL_FIELDS)} from `tab{FILL_DT}` where {cond} order by {order} limit %s offset %s",
		vals + [cint(limit) or 500, cint(start)], as_dict=True)
	return {"total": total, "rows": rows}
