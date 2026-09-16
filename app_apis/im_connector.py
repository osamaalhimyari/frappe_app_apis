"""
app_apis.im_connector -- the Intelligent Machines (gps.im2m.ws) integration.

This is the only integration in the app. There is no second provider and no
offline stand-in: every read on this path is a live call to IM, and every lookup
is BY IMEI. If IM cannot be reached or the IMEI is unknown to the account, that
is what the caller is told -- nothing is substituted.

Architecture
------------
    Client Script (browser)
          |  frappe.call("app_apis.im_connector.get_snapshot", {ticket})
          v
    THIS MODULE  -- reads the xticket, resolves the IMEI, signs in once,
                    calls IM, parses the payload, returns a clean dict
          |  HTTP POST (requests, JSON, `auth-code` header)
          v
    https://gps.im2m.ws/webservice

What IM's shape forces on this module
-------------------------------------
1. ONE ACCOUNT, ALWAYS. IM signs in with a single username and password held in
   `app_apis` (IM Connection section), so there is no per-customer credential lookup.

2. IMEI IS THE ONLY KEY. `getTokenBaseLiveData` accepts `imei_nos`,
   `vehicle_nos` or `company_names`; this module sends only `imei_nos`. Plates
   are spelled several ways across the ERP and IM matches `vehicle_nos` as a
   literal string, so a plate lookup that "works" can quietly answer with a
   different vehicle. An IMEI either matches the device on the ticket or it
   matches nothing, which is the failure mode worth having.

3. TOKEN, NOT BASIC AUTH. `?token=generateAccessToken` returns an opaque token
   that later calls send in an `auth-code` HEADER. The token is cached in
   frappe's cache (see token_ttl_minutes) because minting one per request is
   both wasteful and an easy way to get throttled.

4. IM IS RATE LIMITED. The documented failure is literally:

       {"root": {"error": "The call exceeded the limit of one/two minute one call."}}

   So every live read goes through `_cached_or_fetch`, which serves a snapshot
   younger than `min_call_interval_seconds` from cache, and falls back to the
   last good payload (clearly flagged) when IM says we called too soon. This is
   also why the ignition test polls every ~90s.

5. THE PAYLOAD IS A FLAT, STRING-TYPED SHEET. IM does not send a sensor array;
   it sends fixed keys (Door1..Door4, AC, SOS, Power, IGN, Temperature, Fuel,
   ExternalVolt, battery_percentage) where an unwired port reads "--". So
   `_parse_ports` turns those keys into a uniform reading shape, and "--"
   becomes "not reported" rather than a confident value.

6. TIMESTAMPS ARE LOCAL STRINGS. "28-09-2020 22:43:29" carries no zone, so it is
   interpreted in `app_apis.im_data_timezone` (defaulting to the site's system
   timezone) before being turned into an epoch. Getting this wrong shifts the
   "last seen" age by whole hours, which is why it is configurable.

Public surface: `get_snapshot`, `get_track_logs`, `test_connection` and
`clear_cache_from_desk` are whitelisted. Everything else is internal.

This app defines no business doctypes. `xticket` and `Customer Vehicle` belong
to the site; this module owns only `app_apis` (IM Connection section).
"""

import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _

# Commands, exactly as IM names them in the query string.
CMD_TOKEN = "generateAccessToken"
CMD_LIVE = "getTokenBaseLiveData"
CMD_TRACK = "getVehicleTrackLogs"

# Where an IMEI may hide on an xticket, most specific first. Listing a field a
# site does not define is harmless: Document.get() returns None for an unknown
# fieldname rather than raising.
TICKET_IMEI_FIELDS = (
	"device_serial",
	"device_id_link",
	"device_serial_new",
	"device_id_scan",
)

# The fixed live-data keys IM uses for wired ports, in the order a technician
# reads them. `group` only drives how the readings are grouped on screen.
PORT_FIELDS = (
	("Temperature", "Temperature", "Sensors"),
	("Humidity", "Humidity", "Sensors"),
	("Fuel", "Fuel", "Sensors"),
	("Door1", "Door 1", "Doors"),
	("Door2", "Door 2", "Doors"),
	("Door3", "Door 3", "Doors"),
	("Door4", "Door 4", "Doors"),
	("AC", "A/C", "Body"),
	("SOS", "SOS", "Body"),
	("Immobilize_State", "Immobiliser", "Body"),
	("Power", "Power", "Power"),
	("ExternalVolt", "External Voltage", "Power"),
	("battery_percentage", "Battery", "Power"),
	("GPS", "GPS Port", "Signal"),
)

# IM writes "not wired / no reading" as any of these.
BLANKS = {"", "--", "-", "–", "—", "n/a", "N/A", "null", "None"}

# Datetime formats seen across the IM docs. Live data uses the first; track logs
# are documented with the second.
TIME_FORMATS = (
	"%d-%m-%Y %H:%M:%S",
	"%Y-%m-%d %H:%M:%S",
	"%d-%m-%Y %H:%M",
	"%Y-%m-%d %H:%M",
	"%d/%m/%Y %H:%M:%S",
)

CACHE_PREFIX = "im_api"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _settings() -> dict:
	"""IM's half of the merged `app_apis` settings doctype.

	Both providers live in one Single now, so every field here is read under its
	`im_` prefix. The prefix is load-bearing rather than cosmetic: Pilot and IM
	both have a base URL, a request timeout and a staleness threshold, and
	unprefixed names would have silently shared one value between two unrelated
	endpoints.
	"""
	s = frappe.get_cached_doc("app_apis")
	return {
		"base_url": str(s.im_base_url or "").strip().rstrip("?"),
		"project_id": str(s.im_project_id or "").strip(),
		"username": str(s.im_username or "").strip(),
		"timeout": frappe.utils.cint(s.im_request_timeout) or 20,
		"stale_after_minutes": frappe.utils.cint(s.im_stale_after_minutes) or 15,
		"min_call_interval": frappe.utils.cint(s.im_min_call_interval_seconds) or 60,
		"token_ttl": (frappe.utils.cint(s.im_token_ttl_minutes) or 45) * 60,
		"ignition_poll_seconds": frappe.utils.cint(s.im_ignition_poll_seconds) or 90,
		"ignition_window_minutes": frappe.utils.cint(s.im_ignition_window_minutes) or 10,
		"data_timezone": str(s.im_data_timezone or "").strip(),
		"doc": s,
	}


def _password(settings: dict) -> str | None:
	"""The one IM password.

	`im_password` in site_config wins, so a deployment can keep the secret out
	of the database entirely; otherwise the encrypted field on app_apis.
	"""
	conf_pw = frappe.conf.get("im_password")
	if conf_pw:
		return str(conf_pw)

	doc = settings["doc"]
	return doc.get_password("im_password") if doc.im_password else None


# --------------------------------------------------------------------------
# Cache -- token and last-good snapshots
# --------------------------------------------------------------------------


def _cache_key(*parts) -> str:
	return "::".join([CACHE_PREFIX, *[str(p) for p in parts]])


def clear_cache() -> None:
	"""Drop the cached token and every cached snapshot.

	Called from app_apis.on_update: once the account or endpoint changes, a
	cached token belongs to the previous account and a cached snapshot may have
	come from a different project. `delete_keys` takes a prefix and handles the
	site-key mangling itself, so every im_api::* entry goes in one call.
	"""
	frappe.cache.delete_keys(CACHE_PREFIX)


@frappe.whitelist()
def clear_cache_from_desk() -> dict:
	frappe.only_for("System Manager")
	clear_cache()
	return {"ok": True}


# --------------------------------------------------------------------------
# Layer 1: HTTP. Internal -- the browser cannot reach any of this.
# --------------------------------------------------------------------------


def _fail(code: int, msg: str, meta: dict) -> dict:
	"""A failure shaped like a success, so callers never branch on exceptions.

	NEGATIVE codes only, so they can never collide with anything IM sends:

	    -401 credentials rejected      -404 no such vehicle on this project
	    -408 timed out                 -429 IM's own rate limit tripped
	    -502 could not connect         -500 unexpected / undecodable response
	"""
	return {"ok": False, "code": code, "msg": msg, "vehicles": [], "_im": meta}


def _meta(command: str, settings: dict) -> dict:
	return {
		"command": command,
		"account": settings["username"],
		"project_id": settings["project_id"],
		"http_status": None,
		"elapsed_ms": 0,
		"server_epoch": int(time.time()),
		"token_source": None,
		"cached": False,
		"throttled": False,
	}


def _post(command: str, settings: dict, body: dict, headers: dict | None = None) -> tuple[dict | None, dict]:
	"""One POST to IM. Returns (decoded_body, meta) or (None, meta_with_error).

	The error path never raises -- the second element always carries `error`
	(code + msg) when the first is None.
	"""
	meta = _meta(command, settings)

	base_url = settings["base_url"]
	if not base_url:
		meta["error"] = (-500, "IM Base URL is not set in app_apis settings (IM Connection).")
		return None, meta

	# Application code, not the Server Script sandbox: this import is the point.
	import requests

	params = {"token": command}
	if command == CMD_LIVE and settings["project_id"]:
		# IM spells it with capitals in the documented URL; send it verbatim.
		params["ProjectId"] = settings["project_id"]

	started = time.monotonic()
	try:
		resp = requests.post(
			base_url,
			params=params,
			json=body,
			headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
			timeout=settings["timeout"],
		)
		meta["http_status"] = resp.status_code
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		resp.raise_for_status()

	except requests.exceptions.Timeout:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		meta["error"] = (-408, f"timed out after {settings['timeout']}s waiting for IM.")
		return None, meta

	except requests.exceptions.HTTPError:
		status = meta["http_status"]
		if status in (401, 403):
			meta["error"] = (
				-401,
				f"HTTP {status} -- IM rejected the request. Check IM Username / IM Password "
				"in app_apis > IM Connection; no code change can work around a rejected credential.",
			)
		elif status == 404:
			meta["error"] = (-404, f"404 Not Found for {base_url} -- check IM Base URL in app_apis settings.")
		elif status == 429:
			meta["error"] = (-429, "HTTP 429 -- IM is rate limiting this account.")
		else:
			meta["error"] = (-500, f"IM returned HTTP {status}.")
		return None, meta

	except requests.exceptions.RequestException as e:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		meta["error"] = (-502, f"could not reach IM: {e.__class__.__name__}: {str(e)[:160]}")
		return None, meta

	# Decode explicitly rather than trusting the content-type header, and strip
	# a BOM if one is present.
	try:
		decoded = resp.json()
	except ValueError:
		try:
			decoded = json.loads(resp.text.strip().lstrip("﻿"))
		except ValueError:
			meta["error"] = (
				-500,
				f"IM returned non-JSON ({len(resp.text)} bytes): {resp.text[:160]!r}",
			)
			return None, meta

	if not isinstance(decoded, dict):
		meta["error"] = (-500, f"IM returned {type(decoded).__name__}, expected an object.")
		return None, meta

	return decoded, meta


# ---- token ---------------------------------------------------------------


def _get_token(settings: dict, force: bool = False) -> tuple[str | None, str, dict]:
	"""Return (token, source, meta). `source` is "cache", "fresh" or "offline"."""
	cache_key = _cache_key("token", settings["username"], settings["base_url"])

	if not force:
		cached = frappe.cache.get_value(cache_key)
		if cached:
			return str(cached), "cache", {"elapsed_ms": 0}

	if not settings["username"]:
		return None, "error", {"error": (-500, "IM Username is not set in app_apis settings (IM Connection).")}

	password = _password(settings)
	if not password:
		return None, "error", {
			"error": (
				-500,
				"no IM password available -- set app_apis > IM Password, or put "
				"`im_password` in site_config.json.",
			)
		}

	decoded, meta = _post(
		CMD_TOKEN, settings, {"username": settings["username"], "password": password}
	)
	if decoded is None:
		return None, "error", meta

	# Documented shape: {"result": 1, "data": {"token": "..."}, "message": ""}
	token = ((decoded.get("data") or {}) if isinstance(decoded.get("data"), dict) else {}).get("token")
	if not token:
		# Some deployments answer the error shape used by the live endpoint.
		root = decoded.get("root") if isinstance(decoded.get("root"), dict) else {}
		why = str(root.get("error") or decoded.get("message") or f"result={decoded.get('result')}")
		low = why.lower()

		# IM's two sign-in failures mean very different things, and telling them
		# apart saves a long hunt for a password that was never the problem:
		#
		#   "Incorrect username or password" -- the credential itself is wrong.
		#       IM reaches password validation, so the account is at least a
		#       normal one.
		#   "Something went wrong on server" -- returned for this account with
		#       ANY password, right or wrong. The failure happens BEFORE the
		#       password is checked, so it is the account that is unusable for
		#       the webservice (not provisioned / no project / no API access),
		#       and no password will fix it.
		if "incorrect username" in low or "incorrect password" in low:
			hint = _(
				"IM rejected the credential itself. Check IM Username and IM Password "
				"against a real sign-in at the IM portal."
			)
		elif "went wrong" in low or "server error" in low:
			hint = _(
				"IM returns this for account '{0}' whatever password is sent, which means "
				"the password is not the problem -- the account itself cannot use the "
				"webservice API. Ask IM to enable webservice/API access for it, or switch "
				"IM Username to an account that already has it."
			).format(settings["username"])
		elif "not available for you" in low:
			hint = _("This IM account exists but is not entitled to the API service.")
		else:
			hint = ""

		meta["error"] = (-401, f"IM did not return a token: {why}" + (f" -- {hint}" if hint else ""))
		return None, "error", meta

	frappe.cache.set_value(cache_key, str(token), expires_in_sec=settings["token_ttl"])
	return str(token), "fresh", meta


# ---- live data -----------------------------------------------------------


def _fetch_live(imei: str, settings: dict, retry_auth: bool = True) -> dict:
	"""One `getTokenBaseLiveData` call, by IMEI. Never raises for a bad account.

	`imei_nos` is the only filter this module ever sends. IM would also accept
	`vehicle_nos` / `company_names`, but both can match a vehicle other than the
	one on the ticket, and a wrong answer that looks right is worse than no
	answer at all.
	"""
	token, source, tmeta = _get_token(settings)
	meta = _meta(CMD_LIVE, settings)
	meta["token_source"] = source
	meta["lookup"] = {"imei_nos": imei}

	if not token:
		code, msg = tmeta.get("error", (-500, "could not obtain an IM access token."))
		return _fail(code, msg, meta)

	decoded, pmeta = _post(
		CMD_LIVE,
		settings,
		meta["lookup"],
		headers={"auth-code": token},
	)
	meta.update({k: pmeta[k] for k in ("http_status", "elapsed_ms") if k in pmeta})

	if decoded is None:
		code, msg = pmeta.get("error", (-500, "IM call failed."))
		# A token can expire before its cached TTL runs out. Mint a new one and
		# try exactly once more -- never in a loop.
		if code == -401 and retry_auth:
			frappe.cache.delete_value(_cache_key("token", settings["username"], settings["base_url"]))
			_get_token(settings, force=True)
			return _fetch_live(imei, settings, retry_auth=False)
		return _fail(code, msg, meta)

	root = decoded.get("root") if isinstance(decoded.get("root"), dict) else decoded

	error = root.get("error") if isinstance(root, dict) else None
	if error:
		text = str(error)
		low = text.lower()
		if "exceeded the limit" in low:
			return _fail(-429, text, meta)
		if (
			"incorrect username" in low
			or "user inactive" in low
			or "not available for you" in low
			# A token that expired inside its cached TTL comes back as this.
			# Mapping it to -401 is what lets the retry above mint a fresh one
			# instead of surfacing a dead end to the technician.
			or "invalid token" in low
		):
			# Drop the cached token so the next attempt does not keep replaying
			# one that IM has stopped accepting.
			frappe.cache.delete_value(_cache_key("token", settings["username"], settings["base_url"]))
			return _fail(-401, text, meta)
		if "no vehicle found" in low or "no data found" in low or "no company found" in low:
			return _fail(-404, text, meta)
		return _fail(-500, text, meta)

	rows = root.get("VehicleData") if isinstance(root, dict) else None
	if isinstance(rows, dict):
		# A single vehicle is sometimes returned unwrapped.
		rows = [rows]
	if not isinstance(rows, list):
		return _fail(-500, f"IM returned no VehicleData: {json.dumps(decoded)[:200]}", meta)

	return {"ok": True, "code": 0, "msg": "OK", "vehicles": rows, "_im": meta}


def _cached_or_fetch(imei: str, settings: dict) -> dict:
	"""Live read with IM's rate limit designed around, not ignored.

	IM answers "The call exceeded the limit of one/two minute one call." if
	asked again too soon, so:

	  1. A payload younger than `min_call_interval_seconds` is served straight
	     from cache -- IM is never called at all.
	  2. Otherwise IM is called. A good answer refreshes the cache.
	  3. If IM says we called too soon and a stale payload exists, that payload
	     is returned with `throttled` set, because a two-minute-old position is
	     far more useful to a technician than an error dialog.

	The cache is keyed by the IMEI, so two devices do not evict each other, and
	by account + project, so changing either cannot serve a payload from the
	previous configuration.
	"""
	key = _cache_key("live", settings["username"], settings["project_id"], imei)
	now = int(time.time())
	cached = frappe.cache.get_value(key)

	if isinstance(cached, dict) and cached.get("stored_at"):
		age = now - frappe.utils.cint(cached["stored_at"])
		if age < settings["min_call_interval"]:
			body = cached["body"]
			body["_im"] = {**body.get("_im", {}), "cached": True, "cache_age": age, "server_epoch": now}
			return body

	body = _fetch_live(imei, settings)

	if body.get("ok") and body.get("vehicles"):
		frappe.cache.set_value(
			key,
			{"stored_at": now, "body": body},
			# Keep a stale copy well past the call interval: it is the fallback
			# for step 3 above.
			expires_in_sec=max(settings["min_call_interval"] * 10, 900),
		)
		return body

	if body.get("code") == -429 and isinstance(cached, dict) and cached.get("body"):
		stale = cached["body"]
		stale["_im"] = {
			**stale.get("_im", {}),
			"cached": True,
			"throttled": True,
			"cache_age": now - frappe.utils.cint(cached["stored_at"]),
			"server_epoch": now,
			"throttle_msg": body.get("msg"),
		}
		return stale

	return body


# --------------------------------------------------------------------------
# Layer 2: resolving the inputs from the ERP
# --------------------------------------------------------------------------


def _resolve_imei(ticket: "frappe.Document", imei: str | None) -> dict:
	"""The one IMEI to ask IM about, and where it came from.

	An explicit `imei` argument wins outright -- someone diagnosing a specific
	device must get that device. Otherwise the ticket's own device fields are
	read in order, and the linked Customer Vehicle is the last resort (it is
	right when the ticket was raised before the serial was filled in).

	There is deliberately no plate fallback. IM matches `vehicle_nos` as a
	literal string and this site spells the same plate several ways, so a plate
	lookup can succeed against the WRONG vehicle -- and nothing on screen would
	betray it. Failing here is recoverable; a confident wrong answer is not.
	"""
	out = {"imei": None, "imei_source": None}

	if imei:
		out["imei"], out["imei_source"] = str(imei).strip(), "argument"
		return out

	for fn in TICKET_IMEI_FIELDS:
		val = ticket.get(fn)
		if val:
			out["imei"], out["imei_source"] = str(val).strip(), f"xticket.{fn}"
			return out

	plate = ticket.get("license_plate")
	if plate:
		try:
			vehicle = frappe.get_cached_doc("Customer Vehicle", plate)
		except frappe.DoesNotExistError:
			vehicle = None

		if vehicle and vehicle.get("device_serial"):
			out["imei"] = str(vehicle.get("device_serial")).strip()
			out["imei_source"] = "Customer Vehicle.device_serial"
			return out

	frappe.throw(
		_(
			"This ticket has no device serial (IMEI), and IM is queried by IMEI only. "
			"Fill in the device serial on the ticket, or link a Customer Vehicle that "
			"has one."
		),
		title=_("No IMEI on This Ticket"),
	)


# --------------------------------------------------------------------------
# Layer 3: parsing the IM payload
# --------------------------------------------------------------------------


def _blank(value) -> bool:
	"""True when IM means "nothing wired here" rather than a reading."""
	if value is None:
		return True
	if isinstance(value, (list, tuple, dict)):
		return len(value) == 0
	return str(value).strip() in BLANKS


def _as_number(value):
	"""Numeric reading, or None when the reading is not a number.

	IM mixes numbers ("63", "-18.4") with words ("ON", "--"). flt("ON") returns
	0.0, which would publish a confident, wrong zero for every text reading --
	so return None instead and let `display` carry the original text.
	"""
	if _blank(value):
		return None
	try:
		return float(str(value).strip())
	except (TypeError, ValueError):
		return None


def _labelled_number(value):
	"""Pull the number out of a labelled BLE reading.

	Devices with a Bluetooth sensor do not send a bare figure. They send

	    "Temperature": "BLE Temperature 1 : 2.5"
	    "Humidity":    "BLE Humidity 1 : 4.7 %RH"

	which `_as_number` rejects, so a freezer truck's only meaningful reading
	would publish as None. Take the LAST number in the string: the leading "1" is
	the sensor's index, not its reading, and taking the first would report every
	BLE probe as 1 degree.
	"""
	if _blank(value):
		return None

	direct = _as_number(value)
	if direct is not None:
		return direct

	found = re.findall(r"-?\d+(?:\.\d+)?", str(value))
	return float(found[-1]) if found else None


def _as_bool(value):
	"""'ON'/'OFF'/'--' -> True/False/None. Never guesses."""
	if _blank(value):
		return None
	s = str(value).strip().lower()
	if s in ("on", "1", "true", "yes", "open", "active"):
		return True
	if s in ("off", "0", "false", "no", "closed", "inactive"):
		return False
	return None


def _epoch(text: str, settings: dict) -> int | None:
	"""'28-09-2020 22:43:29' -> epoch, read in the configured timezone.

	IM stamps a local wall-clock string with no offset, so the zone has to come
	from configuration. Guessing UTC would report a vehicle seen 30 seconds ago
	as three hours stale on a Riyadh site.
	"""
	if _blank(text):
		return None

	raw = str(text).strip()
	parsed = None
	for fmt in TIME_FORMATS:
		try:
			parsed = datetime.strptime(raw, fmt)
			break
		except ValueError:
			continue

	if parsed is None:
		return None

	tzname = settings.get("data_timezone") or None
	if not tzname:
		try:
			tzname = frappe.utils.get_system_timezone()
		except Exception:
			tzname = None

	if tzname:
		try:
			return int(parsed.replace(tzinfo=ZoneInfo(tzname)).timestamp())
		except Exception:
			# A timezone name the host has no data for should degrade to the
			# server clock, not blow up the whole snapshot.
			pass

	return int(parsed.timestamp())


def _reading(name: str, value, group: str, ts: int | None) -> dict:
	"""One port, in the same shape the Pilot side produces for a sensor.

	Keeping the shape identical is what lets the IM Client Script reuse the
	Pilot layout without a translation layer in the browser.
	"""
	num = _as_number(value)
	flag = _as_bool(value)

	if _blank(value):
		display = None
	elif flag is not None and num is None:
		display = "ON" if flag else "OFF"
	else:
		display = str(value).strip()

	return {
		"name": name,
		"display": display,
		"value": num,
		"bool": flag,
		"raw_value": None if isinstance(value, (list, dict)) else value,
		"group": group,
		"ts": ts,
		"reported": not _blank(value),
	}


def _parse_fuel(value, ts: int | None) -> list[dict]:
	"""IM sends Fuel as an array whose shape is not documented.

	Rather than guess at keys, publish whatever numeric-looking members it has
	and always keep the original under `raw` on the snapshot.
	"""
	if _blank(value):
		return []

	if not isinstance(value, list):
		return [_reading("Fuel", value, "Fuel", ts)]

	out = []
	for i, item in enumerate(value, start=1):
		if isinstance(item, dict):
			label = item.get("name") or item.get("Name") or item.get("tank") or f"Fuel {i}"
			reading = (
				item.get("value")
				if item.get("value") is not None
				else item.get("Value") if item.get("Value") is not None else item.get("fuel")
			)
			entry = _reading(str(label), reading, "Fuel", ts)
			entry["raw_value"] = json.dumps(item)[:200]
			out.append(entry)
		else:
			out.append(_reading(f"Fuel {i}", item, "Fuel", ts))
	return out


def _parse_ports(vehicle: dict, ts: int | None) -> tuple[list[dict], list[dict]]:
	"""Split the fixed IM keys into (reported, not_reported).

	Everything is returned -- an unwired port is evidence too ("Door 3 is not
	wired" is a finding, not an absence) -- but the two lists are kept apart so
	the UI can lead with what is actually live.
	"""
	reported: list[dict] = []
	missing: list[dict] = []

	for key, label, group in PORT_FIELDS:
		value = vehicle.get(key)

		if key == "Fuel":
			fuel = _parse_fuel(value, ts)
			if fuel:
				reported.extend(fuel)
			else:
				missing.append(_reading(label, None, group, ts))
			continue

		if key == "Temperature":
			# A wired probe sends a bare number; a BLE probe sends
			# "BLE Temperature 1 : 2.5" and repeats the figure in `Temperature1`.
			# Prefer the dedicated field, fall back to digging it out of the
			# label, and keep IM's original string as raw_value either way.
			entry = _reading(label, value, group, ts)
			numeric = _as_number(vehicle.get("Temperature1"))
			if numeric is None:
				numeric = _labelled_number(value)
			if numeric is not None:
				entry["value"] = numeric
				entry["display"] = f"{numeric} °C"
		elif key == "Humidity":
			entry = _reading(label, value, group, ts)
			numeric = _labelled_number(value)
			if numeric is not None:
				entry["value"] = numeric
				entry["display"] = f"{numeric} %RH"
		elif key == "battery_percentage":
			entry = _reading(label, value, group, ts)
			if entry["value"] is not None:
				entry["display"] = f"{frappe.utils.flt(entry['value'], 0):.0f} %"
		elif key == "ExternalVolt":
			entry = _reading(label, value, group, ts)
			if entry["value"] is not None:
				entry["display"] = f"{entry['value']} V"
		else:
			entry = _reading(label, value, group, ts)

		(reported if entry["reported"] else missing).append(entry)

	return reported, missing


def _driver_name(vehicle: dict) -> str | None:
	parts = [
		vehicle.get("Driver_First_Name"),
		vehicle.get("Driver_Middle_Name"),
		vehicle.get("Driver_Last_Name"),
	]
	name = " ".join(str(p).strip() for p in parts if not _blank(p))
	return name or None


def _pick_vehicle(rows: list, imei: str) -> dict | None:
	"""The row for exactly this IMEI, or None.

	Returning None rather than rows[0] is the whole point: an `imei_nos` filter
	that came back with somebody else's device is a bug at IM's end, and quietly
	reporting that device as the ticket's would be undetectable on screen.
	"""
	wanted = str(imei).strip()
	return next((r for r in rows if str(r.get("Imeino") or "").strip() == wanted), None)


def _shape(vehicle: dict, settings: dict) -> dict:
	"""The IM payload, normalised. Shared by get_snapshot and its callers."""
	gps_epoch = _epoch(vehicle.get("GPSActualTime"), settings)
	insert_epoch = _epoch(vehicle.get("Datetime"), settings)
	ts = gps_epoch or insert_epoch

	reported, missing = _parse_ports(vehicle, ts)

	lat = _as_number(vehicle.get("Latitude"))
	lon = _as_number(vehicle.get("Longitude"))
	# IM sends 0/0 for a device that has never had a fix. Treating that as a
	# position would drop a pin in the Gulf of Guinea.
	has_fix = bool(lat or lon)

	return {
		"device": {
			"imei": str(vehicle.get("Imeino") or "").strip() or None,
			"name": None if _blank(vehicle.get("Vehicle_No")) else str(vehicle.get("Vehicle_No")).strip(),
			"vehicle_name": None
			if _blank(vehicle.get("Vehicle_Name"))
			else str(vehicle.get("Vehicle_Name")).strip(),
			"company": None if _blank(vehicle.get("Company")) else vehicle.get("Company"),
			"folder": None if _blank(vehicle.get("Branch")) else vehicle.get("Branch"),
			"type": None if _blank(vehicle.get("Vehicletype")) else vehicle.get("Vehicletype"),
			"model": None if _blank(vehicle.get("DeviceModel")) else vehicle.get("DeviceModel"),
			"driver_name": _driver_name(vehicle),
			# IM reports the odometer in metres on every account seen so far
			# (57260700 for a truck is 57 260 km, not 57 million km), so publish
			# both and let the UI show km.
			"odometer_raw": _as_number(vehicle.get("Odometer")),
			"current_mileage": (
				round(_as_number(vehicle.get("Odometer")) / 1000.0, 1)
				if _as_number(vehicle.get("Odometer")) is not None
				else None
			),
		},
		"state": {
			"status_text": None if _blank(vehicle.get("Status")) else str(vehicle.get("Status")).strip(),
			"active": 1 if str(vehicle.get("Status") or "").strip().upper() == "ACTIVE" else 0,
			"ignition": _as_bool(vehicle.get("IGN")),
			"ignition_raw": vehicle.get("IGN"),
			"speed": _as_number(vehicle.get("Speed")),
			"direction": _as_number(vehicle.get("Angle")),
			"gps": _as_bool(vehicle.get("GPS")),
			"power": _as_bool(vehicle.get("Power")),
			"sos": _as_bool(vehicle.get("SOS")),
			"ac": _as_bool(vehicle.get("AC")),
			"immobilised": _as_bool(vehicle.get("Immobilize_State")),
			"battery_percentage": _as_number(vehicle.get("battery_percentage")),
			"external_volt": _as_number(vehicle.get("ExternalVolt")),
			# `Temperature1` is the bare figure a BLE probe also reports as the
			# labelled `Temperature` string; on a freezer truck this is the
			# reading the whole job is about, so it must not fall through to None.
			"temperature": (
				_as_number(vehicle.get("Temperature1"))
				if _as_number(vehicle.get("Temperature1")) is not None
				else _labelled_number(vehicle.get("Temperature"))
			),
			"humidity": _labelled_number(vehicle.get("Humidity")),
		},
		"location": {
			"lat": lat if has_fix else None,
			"lon": lon if has_fix else None,
			"source": "live" if has_fix else "none",
			# Newer firmware reports `satellite_count`; older rows omit it. None
			# means "not reported", which is not the same as zero satellites.
			"sats": _as_number(vehicle.get("satellite_count")),
			"altitude": _as_number(vehicle.get("Altitude")),
			"hdop": _as_number(vehicle.get("gps_hdop")),
			"address": None if _blank(vehicle.get("Location")) else vehicle.get("Location"),
			"poi": None if _blank(vehicle.get("POI")) else vehicle.get("POI"),
		},
		"last_update": {
			"epoch": ts,
			"gps_time_text": None if _blank(vehicle.get("GPSActualTime")) else vehicle.get("GPSActualTime"),
			"insert_time_text": None if _blank(vehicle.get("Datetime")) else vehicle.get("Datetime"),
			"insert_epoch": insert_epoch,
		},
		"readings": reported,
		"unwired": missing,
	}


# --------------------------------------------------------------------------
# The public methods
# --------------------------------------------------------------------------


@frappe.whitelist()
def get_snapshot(ticket: str, imei: str | None = None) -> dict:
	"""Live IM snapshot for one xticket, looked up BY IMEI. THE public entry point.

	Called from the Client Script as:

	    frappe.call({
	        method: "app_apis.im_connector.get_snapshot",
	        args: { ticket: frm.doc.name },
	    })

	`imei` is an optional override, useful for diagnosing one device without
	editing the ticket. It is the only override there is: there is no plate or
	company fallback, so this method either answers about the IMEI it was given
	or fails saying so.

	Raises (via frappe.throw) when IM produces no row for that IMEI.
	"""
	if not ticket:
		frappe.throw(_("ticket is required"), title=_("Missing Argument"))

	doc = frappe.get_doc("xticket", ticket)
	if not doc.has_permission("read"):
		frappe.throw(
			_("You are not permitted to read ticket {0}").format(ticket),
			frappe.PermissionError,
		)

	settings = _settings()
	ref = _resolve_imei(doc, imei)
	target = ref["imei"]

	result = _cached_or_fetch(target, settings)
	rows = result.get("vehicles") or []

	if not result.get("ok") or not rows:
		reason = result.get("msg") or _("no vehicle returned")

		if frappe.utils.cint(result.get("code")) == -401:
			# Not a "no data" problem at all -- say so plainly, so nobody goes
			# hunting for a vehicle when the account is what is broken.
			frappe.throw(
				_("IM sign-in failed, so no vehicle could be read.")
				+ f"\n\n{reason}\n\n"
				+ _(
					"This is a credential/account problem at IM's end, not a bug in this "
					"integration. Fix it in app_apis settings (IM Connection) ({0}), then press Test Connection."
				).format(settings["username"] or "-"),
				title=_("IM: Sign-in Failed"),
			)

		frappe.throw(
			_("IM returned no data for IMEI {0} (from {1}).").format(target, ref["imei_source"])
			+ f"\n\n{reason}\n\n"
			+ _(
				"Lookups are by IMEI only, so nothing else from the ticket was substituted. "
				"Account {0}, project {1} -- confirm this device is registered under that "
				"account and project on {2}."
			).format(settings["username"] or "-", settings["project_id"] or "-", settings["base_url"]),
			title=_("IM: No Data"),
		)

	vehicle = _pick_vehicle(rows, target)
	if vehicle is None:
		frappe.throw(
			_(
				"IM answered the lookup for IMEI {0} with {1} vehicle(s), none of which "
				"carry that IMEI ({2}). Nothing is shown rather than risk reporting a "
				"different device as this one."
			).format(target, len(rows), ", ".join(str(r.get("Imeino") or "?") for r in rows[:5])),
			title=_("IM: Wrong Device Returned"),
		)

	diag = result.get("_im") or {}
	shaped = _shape(vehicle, settings)

	last_epoch = shaped["last_update"]["epoch"]
	now_epoch = frappe.utils.cint(diag.get("server_epoch")) or int(time.time())
	age_seconds = (now_epoch - last_epoch) if last_epoch else None
	is_stale = (age_seconds > settings["stale_after_minutes"] * 60) if age_seconds is not None else None

	shaped["last_update"]["age_seconds"] = age_seconds
	shaped["last_update"]["is_stale"] = is_stale

	return {
		"ticket": ticket,
		"platform": "IM",
		**shaped,
		"ticket_info": {
			"customer": doc.get("customer"),
			"license_plate": doc.get("license_plate"),
			"issue_type": doc.get("issue_type"),
			"status": doc.get("status"),
		},
		"stale_after_minutes": settings["stale_after_minutes"],
		"account": settings["username"],
		"project_id": settings["project_id"],
		"matched_by": "imei",
		"matched_value": target,
		"imei_source": ref["imei_source"],
		"lookup": {"imei": target, "by": "imei_nos"},
		# The browser must not invent its own poll rate: IM's rate limit is the
		# constraint, and it lives in app_apis settings.
		"poll_interval_seconds": settings["ignition_poll_seconds"],
		"ignition_window_minutes": settings["ignition_window_minutes"],
		"min_call_interval_seconds": settings["min_call_interval"],
		"diagnostics": {
			"http_status": diag.get("http_status"),
			"elapsed_ms": diag.get("elapsed_ms"),
			"token_source": diag.get("token_source"),
			"cached": bool(diag.get("cached")),
			"cache_age": diag.get("cache_age"),
			"throttled": bool(diag.get("throttled")),
			"throttle_msg": diag.get("throttle_msg"),
			"im_msg": result.get("msg"),
			"server_epoch": now_epoch,
			"rows_returned": len(rows),
			"base_url": settings["base_url"],
		},
		# Everything IM sent for this vehicle, untouched. The curated keys above
		# cover the UI; this guarantees a field IM adds later is still reachable
		# without another round trip or a code change.
		"raw": vehicle,
	}


@frappe.whitelist()
def get_vehicle_live(imei: str) -> dict:
	"""Live IM snapshot for one IMEI, with no ticket in the picture.

	`get_snapshot` is bound to an xticket -- it checks that ticket's read
	permission and reports the ticket's own fields alongside the platform
	data. This is the same read (same cache, same `_shape`) for a caller that
	has no ticket at all: a click on a Fleet Audit row, where the only
	question is "what is IM saying about this device right now".

	Raises (via frappe.throw) when IM produces no row for that IMEI -- same
	as `get_snapshot`, just without a ticket to blame the lookup on.
	"""
	frappe.only_for(["System Manager", "Technical", "Support Team"])

	imei = str(imei or "").strip()
	if not imei:
		frappe.throw(_("imei is required."), title=_("IM"))

	settings = _settings()
	result = _cached_or_fetch(imei, settings)
	rows = result.get("vehicles") or []

	if not result.get("ok") or not rows:
		reason = result.get("msg") or _("no vehicle returned")

		if frappe.utils.cint(result.get("code")) == -401:
			frappe.throw(
				_("IM sign-in failed, so no vehicle could be read.") + f"\n\n{reason}\n\n"
				+ _(
					"This is a credential/account problem at IM's end, not a bug in this "
					"integration. Fix it in app_apis settings (IM Connection), then press "
					"Test Connection."
				),
				title=_("IM: Sign-in Failed"),
			)

		frappe.throw(
			_("IM returned no data for IMEI {0}.").format(imei) + f"\n\n{reason}",
			title=_("IM: No Data"),
		)

	vehicle = _pick_vehicle(rows, imei)
	if vehicle is None:
		frappe.throw(
			_(
				"IM answered the lookup for IMEI {0} with {1} vehicle(s), none of which "
				"carry that IMEI."
			).format(imei, len(rows)),
			title=_("IM: Wrong Device Returned"),
		)

	diag = result.get("_im") or {}
	shaped = _shape(vehicle, settings)

	last_epoch = shaped["last_update"]["epoch"]
	now_epoch = frappe.utils.cint(diag.get("server_epoch")) or int(time.time())
	age_seconds = (now_epoch - last_epoch) if last_epoch else None
	shaped["last_update"]["age_seconds"] = age_seconds
	shaped["last_update"]["is_stale"] = (
		(age_seconds > settings["stale_after_minutes"] * 60) if age_seconds is not None else None
	)

	# Same shape as `get_snapshot`, minus `ticket`/`ticket_info`/`lookup` --
	# there is no ticket here. Kept this close on purpose: the Fleet Audit's
	# dialog reuses the same renderer that draws "Check IM" on a ticket.
	return {
		"platform": "IM",
		**shaped,
		"stale_after_minutes": settings["stale_after_minutes"],
		"account": settings["username"],
		"project_id": settings["project_id"],
		"matched_by": "imei",
		"matched_value": imei,
		"imei_source": "argument",
		"poll_interval_seconds": settings["ignition_poll_seconds"],
		"ignition_window_minutes": settings["ignition_window_minutes"],
		"min_call_interval_seconds": settings["min_call_interval"],
		"diagnostics": {
			"http_status": diag.get("http_status"),
			"elapsed_ms": diag.get("elapsed_ms"),
			"token_source": diag.get("token_source"),
			"cached": bool(diag.get("cached")),
			"cache_age": diag.get("cache_age"),
			"throttled": bool(diag.get("throttled")),
			"throttle_msg": diag.get("throttle_msg"),
			"im_msg": result.get("msg"),
			"server_epoch": now_epoch,
			"rows_returned": len(rows),
			"base_url": settings["base_url"],
		},
		"raw": vehicle,
	}


@frappe.whitelist()
def get_track_logs(
	ticket: str,
	start_date: str | None = None,
	end_date: str | None = None,
	imei: str | None = None,
	hours: float | None = None,
) -> dict:
	"""Historical track points for one ticket's device.

	Wraps `?token=getVehicleTrackLogs`, which takes a VEHICLE NUMBER (not an
	IMEI) and a `YYYY-MM-DD HH:mm:ss` window.

	That is the one place IM forces a vehicle number on us, so the number is
	taken from the LIVE ROW for this IMEI rather than from the ticket's plate
	fields: it is IM's own spelling of the plate for exactly this device, which
	no ERP field can be trusted to match. The live read is normally already in
	cache from the snapshot the user is looking at, so this costs no extra call.

	`hours` is a convenience: `hours=6` means "the last six hours", which is
	what the map's track overlay asks for. Explicit `start_date`/`end_date` win.

	IM's documentation shows the request but not the response, so the point
	extractor is deliberately tolerant and `raw` always carries what came back.
	"""
	if not ticket:
		frappe.throw(_("ticket is required"), title=_("Missing Argument"))

	doc = frappe.get_doc("xticket", ticket)
	if not doc.has_permission("read"):
		frappe.throw(
			_("You are not permitted to read ticket {0}").format(ticket),
			frappe.PermissionError,
		)

	settings = _settings()
	ref = _resolve_imei(doc, imei)
	target = ref["imei"]

	live = _cached_or_fetch(target, settings)
	row = _pick_vehicle(live.get("vehicles") or [], target) if live.get("ok") else None
	vehicle_no = None if row is None or _blank(row.get("Vehicle_No")) else str(row["Vehicle_No"]).strip()

	if not vehicle_no:
		frappe.throw(
			_(
				"IM asks for track logs by vehicle number, and the live record for IMEI {0} "
				"does not carry one ({1}). Nothing else is substituted, because a guessed "
				"plate would draw another vehicle's track on this ticket."
			).format(target, live.get("msg") or _("no live row")),
			title=_("No Vehicle Number for This Device"),
		)

	if not (start_date and end_date):
		window_hours = frappe.utils.flt(hours) or 6.0
		end = frappe.utils.now_datetime()
		start = frappe.utils.add_to_date(end, hours=-window_hours)
		start_date = start.strftime("%Y-%m-%d %H:%M:%S")
		end_date = end.strftime("%Y-%m-%d %H:%M:%S")

	body = {"vehicle_no": vehicle_no, "start_date": start_date, "end_date": end_date}

	token, source, tmeta = _get_token(settings)
	if not token:
		code, msg = tmeta.get("error", (-500, "could not obtain an IM access token."))
		frappe.throw(_("IM sign-in failed: {0}").format(msg), title=_("IM: Track Logs"))

	decoded, pmeta = _post(CMD_TRACK, settings, body, headers={"auth-code": token})
	if decoded is None:
		code, msg = pmeta.get("error", (-500, "IM call failed."))
		frappe.throw(_("IM track log request failed: {0}").format(msg), title=_("IM: Track Logs"))

	root = decoded.get("root") if isinstance(decoded.get("root"), dict) else decoded
	if isinstance(root, dict) and root.get("error"):
		frappe.throw(str(root["error"]), title=_("IM: Track Logs"))

	points = _extract_points(root, settings)

	return {
		"ticket": ticket,
		"imei": target,
		"imei_source": ref["imei_source"],
		"vehicle_no": vehicle_no,
		"vehicle_no_source": "IM live record for this IMEI",
		"start_date": start_date,
		"end_date": end_date,
		"points": points,
		"count": len(points),
		"token_source": source,
		"elapsed_ms": pmeta.get("elapsed_ms"),
		"raw": decoded,
	}


def _extract_points(root, settings: dict) -> list[dict]:
	"""Pull [{lat, lon, ts, speed}] out of whatever IM returned.

	The response schema is not in the documentation, so this walks the object
	looking for the first list whose members carry a latitude-shaped key, rather
	than hard-coding a container name that may not exist.
	"""
	candidates = []

	def walk(node, depth=0):
		if depth > 4:
			return
		if isinstance(node, list):
			if node and isinstance(node[0], dict):
				candidates.append(node)
			return
		if isinstance(node, dict):
			for value in node.values():
				walk(value, depth + 1)

	walk(root)

	def key_of(row: dict, *names):
		for n in names:
			for k in row:
				if str(k).strip().lower() == n:
					return k
		return None

	for rows in candidates:
		first = rows[0]
		lat_key = key_of(first, "latitude", "lat")
		lon_key = key_of(first, "longitude", "lon", "lng")
		if not (lat_key and lon_key):
			continue

		ts_key = key_of(first, "gpsactualtime", "datetime", "date_time", "timestamp", "time")
		speed_key = key_of(first, "speed")
		angle_key = key_of(first, "angle", "direction")

		points = []
		for row in rows:
			if not isinstance(row, dict):
				continue
			lat = _as_number(row.get(lat_key))
			lon = _as_number(row.get(lon_key))
			if lat is None or lon is None or (not lat and not lon):
				continue
			points.append(
				{
					"lat": lat,
					"lon": lon,
					"ts": _epoch(row.get(ts_key), settings) if ts_key else None,
					"time_text": str(row.get(ts_key)) if ts_key else None,
					"speed": _as_number(row.get(speed_key)) if speed_key else None,
					"angle": _as_number(row.get(angle_key)) if angle_key else None,
				}
			)

		if points:
			return points

	return []


@frappe.whitelist()
def test_connection() -> dict:
	"""Sign in and report the outcome. Used by the button on the app_apis settings form."""
	frappe.only_for(["System Manager", "Support Team", "Technical"])

	settings = _settings()
	started = time.monotonic()
	token, source, meta = _get_token(settings, force=True)
	elapsed = int((time.monotonic() - started) * 1000)

	if token:
		return {
			"ok": True,
			"account": settings["username"],
			"elapsed_ms": elapsed,
			"message": _("IM issued an access token ({0}, {1} characters).").format(source, len(token)),
		}

	code, msg = meta.get("error", (-500, _("IM did not return a token.")))
	return {
		"ok": False,
		"account": settings["username"],
		"elapsed_ms": elapsed,
		"message": f"[{code}] {msg}",
	}


# --------------------------------------------------------------------------
# Introspection -- NOT whitelisted; `bench console` only, by design.
# --------------------------------------------------------------------------


def status() -> dict:
	cfg = _settings()
	return {
		"mode": "live-only, IMEI-only",
		"base_url": cfg["base_url"],
		"project_id": cfg["project_id"],
		"username": cfg["username"],
		"password_set": bool(cfg["doc"].im_password or frappe.conf.get("im_password")),
		"timeout": cfg["timeout"],
		"stale_after_minutes": cfg["stale_after_minutes"],
		"min_call_interval_seconds": cfg["min_call_interval"],
		"token_ttl_seconds": cfg["token_ttl"],
		"ignition_poll_seconds": cfg["ignition_poll_seconds"],
		"data_timezone": cfg["data_timezone"] or frappe.utils.get_system_timezone(),
	}


# --------------------------------------------------------------------------
# Fleet enumeration -- added for the Fleet Audit dashboard.
#
# Appended, not woven in: nothing above this line changed. `get_snapshot` and
# `get_track_logs` still answer exactly what they answered before.
# --------------------------------------------------------------------------


def fetch_fleet(settings: dict | None = None, retry_auth: bool = True) -> dict:
	"""EVERY device on the IM account, in ONE call. Never raises.

	`getTokenBaseLiveData` is documented as a FILTERED live read -- `imei_nos`,
	`vehicle_nos` or `company_names`. It is not documented as an enumeration.
	But sending an EMPTY body returns the entire fleet: verified against this
	account, 6,515 rows under `root.VehicleData`. IM offers no other way to
	list what it holds, so this is the one the audit uses.

	The rest of this module refuses to look a vehicle up by anything but IMEI,
	because a plate match can silently answer about the wrong vehicle. That
	rule is not weakened here: this sends NO filter at all and then keys every
	row by its own `Imeino`, so nothing is matched by name either.

	Returns this module's usual shape -- `code` 0 on success, a negative code
	on failure, `_im` carrying diagnostics.
	"""
	settings = settings or _settings()
	meta = _meta(CMD_LIVE, settings)
	meta["lookup"] = {"(no filter)": "whole fleet"}

	token, source, tmeta = _get_token(settings)
	meta["token_source"] = source
	if not token:
		code, msg = tmeta.get("error", (-500, "could not obtain an IM access token."))
		return _fail(code, msg, meta)

	decoded, pmeta = _post(CMD_LIVE, settings, {}, headers={"auth-code": token})
	meta.update({k: pmeta[k] for k in ("http_status", "elapsed_ms") if k in pmeta})

	if decoded is None:
		code, msg = pmeta.get("error", (-500, "IM call failed."))
		# Same one-shot re-auth the single-vehicle path uses: a token can die
		# inside its cached TTL.
		if code == -401 and retry_auth:
			frappe.cache.delete_value(_cache_key("token", settings["username"], settings["base_url"]))
			_get_token(settings, force=True)
			return fetch_fleet(settings, retry_auth=False)
		return _fail(code, msg, meta)

	root = decoded.get("root") if isinstance(decoded.get("root"), dict) else decoded

	error = root.get("error") if isinstance(root, dict) else None
	if error:
		text = str(error)
		if "exceeded the limit" in text.lower():
			# IM allows roughly one call a minute. Worth saying plainly: the
			# audit is not broken, it was asked to run twice in a minute.
			return _fail(-429, text, meta)
		return _fail(-500, text, meta)

	rows = root.get("VehicleData") if isinstance(root, dict) else None
	if rows is None:
		# Do not guess: if IM renames the key, say so rather than silently
		# reporting an empty fleet, which would read as "every device is gone".
		return _fail(
			-500,
			"IM answered without a `VehicleData` list (keys: %s). The fleet cannot "
			"be enumerated from this response." % (list(root.keys())[:10] if isinstance(root, dict) else type(root).__name__),
			meta,
		)
	if isinstance(rows, dict):
		rows = [rows]

	out = []
	for row in rows:
		if not isinstance(row, dict):
			continue
		imei = str(row.get("Imeino") or "").strip()
		if not imei:
			continue
		when = str(row.get("Datetime") or row.get("GPSActualTime") or "").strip()
		out.append({
			"imei": imei,
			"vehicle_no": str(row.get("Vehicle_No") or "").strip(),
			"vehicle_name": str(row.get("Vehicle_Name") or "").strip(),
			"company": str(row.get("Company") or "").strip(),
			"branch": str(row.get("Branch") or "").strip(),
			"status": str(row.get("Status") or "").strip(),
			"model": str(row.get("DeviceModel") or "").strip(),
			"vehicle_type": str(row.get("Vehicletype") or "").strip(),
			"last_seen_text": when,
			"last_seen_epoch": _epoch(when, settings) if when else None,
		})

	meta["returned"] = len(rows)
	meta["with_imei"] = len(out)
	return {"code": 0, "msg": "OK", "rows": out, "_im": meta}


@frappe.whitelist()
def list_fleet() -> dict:
	"""The whole IM fleet, for the desk. READ-ONLY."""
	frappe.only_for(["System Manager", "Technical", "Support Team"])

	result = fetch_fleet()
	if frappe.utils.cint(result.get("code")) != 0:
		frappe.throw(
			_("IM could not list the fleet.") + "\n\n[{0}] {1}".format(result.get("code"), result.get("msg")),
			title=_("IM"),
		)
	return {
		"count": len(result.get("rows") or []),
		"rows": result.get("rows") or [],
		"diagnostics": result.get("_im"),
	}
