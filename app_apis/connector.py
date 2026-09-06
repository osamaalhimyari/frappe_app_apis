"""
app_apis.connector -- the entire Pilot GPS integration, in one place.

Architecture (two components, nothing in between)
-------------------------------------------------
    Client Script (browser)
          |  frappe.call("app_apis.connector.get_snapshot", {ticket})
          v
    THIS MODULE  -- reads the xticket + Customer, resolves the IMEI and the
                    candidate accounts, opens the HTTP connection, parses the
                    payload, returns a clean dict
          |  HTTP GET (requests, Basic Auth)
          v
    Pilot GPS API

There is deliberately NO Server Script. Frappe's Server Script sandbox
(RestrictedPython) has no `import`, no `type`, no `getattr`, and the HTTP
helper bound into its namespace is not dependable across builds -- it differs
between the two safe_exec namespaces (`exec_safe_globals` binds
`frappe.integrations.utils.make_get_request`, `render_safe_globals` binds the
address-checking `make_safe_get_request`) and can be absent entirely. Code
whose behaviour depends on which one it got is code that breaks on upgrade.

There is a second, quieter reason the sandbox cannot do this job: Pilot answers
with `content-type: text/json;charset=UTF-8`. `make_get_request` only
JSON-decodes `application/json`; for anything else it returns the raw **string**,
so the next `body.get("code")` resolves to None and surfaces as the famously
opaque `'NoneType' object is not callable`. `requests.Response.json()` ignores
content-type, so this module never has that failure mode.

This module is ordinary application code: `import requests` works, exceptions
behave, and it is unit-testable from `bench console`.

Public surface: two whitelisted methods. `get_snapshot` is THE entry point for
a ticket. `get_vehicle_live` is the same live read and the same multi-account
retry, entered from a customer name instead of a ticket -- for the Fleet
Audit, where a row has no ticket to read `email_pilot` off of. Everything else
is internal (leading underscore) and unreachable from the browser.

NOTE: this app deliberately defines no BUSINESS doctypes. `xticket`,
`Customer Vehicle` and friends belong to the site. The app owns only its own
connector config (the `app_apis` Single, Pilot Connection section) and ships the Client Script as a fixture
so that installing it on a site that already has the doctypes yields a working
feature, not just a library.
"""

import json
import time

import frappe
from frappe import _

# Pilot splits one customer across several accounts by fleet type, so the
# address entitled to see a given IMEI may be in any of these fields. Listing a
# field that a particular site does not define is harmless: Document.get()
# returns None for an unknown fieldname rather than raising.
CUSTOMER_EMAIL_FIELDS = (
	"email_pilot",
	"email_pilot_tow",
	"email_pilot_sfda",
	"email_pilot_motorcycle",
	"email_im_platform",
	"email_pilot_tracking_only",
)

TICKET_IMEI_FIELDS = (
	"device_serial",
	"device_id_link",
	"device_serial_new",
	"device_id_scan",
)

# One field may hold several addresses glued together.
EMAIL_SEPARATORS = ("----", "---", "--", ",", ";", "|", "/", "\n", "\t")


# --------------------------------------------------------------------------
# Offline fixtures -- shaped exactly like a real Pilot `cmd=status` response,
# including its string-typed numbers. Used only when `pilot_offline` is on.
# --------------------------------------------------------------------------

VALID_ACCOUNTS = {
	"stastebakery@im2m.ws",
	"ops.backup@example.test",
	"fallback@example.test",
}

_DEVICES = {
	# Mirrors the real device on the seeded ticket, flat sensor names and all,
	# so offline runs reproduce production behaviour truthfully.
	"863738078964856": {
		"agentid": 319789,
		"imei": "863738078964856",
		"configuration": "Teltonika FMC920",
		"type": "Truck",
		"vehiclenumber": "7229 - أ ط ب",
		"folder": "Queen's Taste Bakery &amp; Sweets",
		"driver_name": None,
		"model": "",
		"status": {
			"active": 1, "speed": 84, "direction": 203,
			"lat": "21.3627566", "lon": "40.4276183", "alt": 1602,
			"satsinview": 17, "moving": 3232, "parking": 0, "firing": 1,
		},
		"sensors_status": [
			{"name": "Ignition sensor", "id": "1849962", "hum_value": "On",
			 "dig_value": "1", "raw_value": "1", "group": ""},
			{"name": "GSM Signal", "id": "1849963", "hum_value": "High",
			 "dig_value": "High", "raw_value": "5", "group": ""},
			{"name": "Temperature", "id": "1849964", "hum_value": "32 C",
			 "dig_value": "32", "raw_value": "32", "group": ""},
			{"name": "Humidity", "id": "1849965", "hum_value": "15 %",
			 "dig_value": "15", "raw_value": "15", "group": ""},
		],
	},
	# A reefer using Pilot's "(T1)"/"(H1)" naming, so the probe-pairing branch
	# has something to exercise offline.
	"866897051234567": {
		"agentid": "AG-1001",
		"imei": "866897051234567",
		"vehiclenumber": "1234-ABC",
		"folder": "Al Rajhi Transport Co",
		"type": "Truck",
		"model": "Teltonika FMB920",
		"driver_name": "Sample Driver",
		"status": {
			"active": 1, "speed": 62.5, "direction": "NE",
			"lat": "24.7136", "lon": "46.6753", "satsinview": 11,
			"moving": 1, "parking": 0, "firing": "1",
		},
		"sensors_status": [
			{"id": 1, "name": "Reefer Temperature 1 (T1)", "hum_value": "-18.4 C",
			 "dig_value": -18.4, "raw_value": "1836", "group": "Reefer"},
			{"id": 2, "name": "Reefer Humidity 1 (H1)", "hum_value": "83 %",
			 "dig_value": 83, "raw_value": "830", "group": "Reefer"},
			{"id": 3, "name": "Reefer Temperature 2 (T2)", "hum_value": "-16.1 C",
			 "dig_value": -16.1, "raw_value": "1610", "group": "Reefer"},
			{"id": 4, "name": "Reefer Humidity 2 (H2)", "hum_value": "79 %",
			 "dig_value": 79, "raw_value": "790", "group": "Reefer"},
			{"id": 5, "name": "Door Sensor", "hum_value": "Closed",
			 "dig_value": 0, "raw_value": "0", "group": "Body"},
			{"id": 6, "name": "Main Battery", "hum_value": "12.7 V",
			 "dig_value": 12.7, "raw_value": "127", "group": "Power"},
		],
	},
}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def is_offline() -> bool:
	"""Live unless site_config deliberately turns the offline fixtures on.

	`pilot_offline` defaults to 0 (live) when the key is absent, so a site that
	never mentions it gets real Pilot calls -- fixtures are opt-in, not the
	silent default a bare install would otherwise fall back to.

	cint(), not bool(): `bench set-config pilot_offline 1` (without --parse)
	writes the STRING "1", and this must equal the int 1, not just be truthy in
	Python's own sense -- cint() is what makes "0" and "" both mean off.
	"""
	return bool(frappe.utils.cint(frappe.conf.get("pilot_offline", 0)))


def _settings() -> dict:
	"""Pilot's half of the merged `app_apis` settings doctype.

	Both providers live in one Single now, so every field here is read under its
	`pilot_` prefix. The prefix is load-bearing rather than cosmetic: Pilot and
	IM both have a base URL, a request timeout and a staleness threshold, and
	unprefixed names would have silently shared one value between two unrelated
	endpoints.
	"""
	s = frappe.get_cached_doc("app_apis")
	return {
		"base_url": str(s.pilot_base_url or "").strip(),
		"node": str(s.pilot_node or "5").strip(),
		"timeout": frappe.utils.cint(s.pilot_request_timeout) or 20,
		"stale_after_minutes": frappe.utils.cint(s.pilot_stale_after_minutes) or 15,
		"fallback_username": str(s.pilot_fallback_username or "").strip(),
		"doc": s,
	}


def _password_for(email: str, settings: dict) -> str | None:
	"""Resolve the password for one account.

	Pilot may or may not use the same password across a customer's accounts.
	Resolution order:

	  1. `pilot_passwords` in site_config -- an {email: password} map, for the
	     case where accounts genuinely differ.
	  2. `app_apis.pilot_password` -- the shared password, encrypted at rest,
	     which is what this deployment currently uses.

	Step 1 makes per-account credentials possible with no schema change. If
	per-account passwords become the norm rather than the exception, promote
	them to a child table on `app_apis` (fields `pilot_email` +
	`pilot_password`) so they are encrypted and editable from the desk.
	"""
	per = frappe.conf.get("pilot_passwords") or {}
	if isinstance(per, dict) and email:
		pw = per.get(email) or per.get(email.strip().lower())
		if pw:
			return str(pw)

	doc = settings["doc"]
	return doc.get_password("pilot_password") if doc.pilot_password else None


# --------------------------------------------------------------------------
# Layer 1: the HTTP call. Internal -- the browser cannot reach this.
# --------------------------------------------------------------------------


def _fail(code: int, msg: str, meta: dict) -> dict:
	return {"code": code, "msg": msg, "data": [], "_pilot": meta}


def _offline_status(imei: str, email: str, meta: dict) -> dict:
	if email not in VALID_ACCOUNTS:
		# NOT -401: this is a local fixture miss, not a rejected credential, and
		# must never be counted by the `auth_failures` tally in get_snapshot /
		# get_vehicle_live or worded like a Pilot rejection -- no request was
		# sent, so nothing was actually rejected.
		return _fail(
			-900,
			f"OFFLINE FIXTURES -- the offline stand-in has no account '{email}' in its "
			"list. This is not a Pilot response; no request was sent.",
			meta,
		)

	dev = _DEVICES.get(imei)
	if not dev:
		return _fail(-404, f"device {imei} is not registered on this node.", meta)

	now = int(time.time())
	dev = json.loads(json.dumps(dev))  # deep copy; never mutate the fixture
	dev["status"]["unixtimestamp"] = str(now)
	for s in dev["sensors_status"]:
		s["change_ts"] = str(now - 30)
	return {"code": 0, "msg": "OK", "data": [dev], "_pilot": meta}


def _fetch_status(imei: str, email: str, node: str, settings: dict) -> dict:
	"""One `cmd=status` request for one account.

	NEVER raises for an unusable account -- an account that does not work is a
	value the retry loop iterates over, not an exception. Always returns a dict
	shaped like a Pilot response:

	    {"code": 0, "msg": "OK", "data": [ {...} ], "_pilot": {...}}

	Pilot's own failures keep Pilot's positive `code`. Transport and auth
	failures use NEGATIVE codes, so they can never collide with a value Pilot
	itself might send:

	    -401 credentials rejected      -404 endpoint/device not found
	    -403 forbidden for this node   -408 timed out
	    -502 could not connect         -500 unexpected / undecodable response
	    -900 offline fixtures miss (see `is_offline`) -- never a real Pilot
	         answer, and deliberately NOT -401: nothing was sent, so nothing
	         was rejected.

	`_pilot` carries non-secret diagnostics. The password is never returned,
	logged or echoed.
	"""
	meta = {
		"account": email,
		"imei": imei,
		"node": node,
		"offline": is_offline(),
		"http_status": None,
		"elapsed_ms": 0,
		"server_epoch": int(time.time()),
	}

	if is_offline():
		return _offline_status(imei, email, meta)

	base_url = settings["base_url"]
	if not base_url:
		return _fail(-500, "Pilot Base URL is not set in app_apis settings (Pilot Connection).", meta)

	password = _password_for(email, settings)
	if not password:
		return _fail(
			-500,
			f"no password available for '{email}' -- set app_apis > Pilot "
			"Password, or add an entry to `pilot_passwords` in site_config.",
			meta,
		)

	# Application code, not the sandbox: this import is the whole point.
	import requests

	started = time.monotonic()
	try:
		resp = requests.get(
			base_url,
			auth=(email, password),  # HTTP Basic
			params={"cmd": "status", "node": node, "imei": imei},
			timeout=settings["timeout"],
			headers={"Accept": "application/json"},
		)
		meta["http_status"] = resp.status_code
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		resp.raise_for_status()

	except requests.exceptions.Timeout:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		return _fail(-408, f"timed out after {settings['timeout']}s waiting for Pilot.", meta)

	except requests.exceptions.HTTPError:
		status = meta["http_status"]
		if status == 401:
			# The single most common outcome, and never fixable in code.
			return _fail(
				-401,
				"401 Unauthorized -- Pilot rejected these credentials. This is an "
				"account problem at Pilot's end, not a bug in this integration.",
				meta,
			)
		if status == 403:
			return _fail(
				-403,
				f"403 Forbidden -- authenticated, but not entitled to node {node} "
				f"or to IMEI {imei}.",
				meta,
			)
		if status == 404:
			return _fail(-404, f"404 Not Found for {base_url} -- check Base URL.", meta)
		return _fail(-500, f"Pilot returned HTTP {status}.", meta)

	except requests.exceptions.RequestException as e:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		return _fail(-502, f"could not reach Pilot: {e.__class__.__name__}: {str(e)[:160]}", meta)

	# Pilot sends `text/json`, so decode explicitly rather than trusting the
	# content-type. resp.json() already ignores it; the fallback covers a body
	# with a BOM or stray whitespace.
	try:
		body = resp.json()
	except ValueError:
		try:
			body = json.loads(resp.text.strip().lstrip("﻿"))
		except ValueError:
			return _fail(
				-500,
				f"Pilot returned non-JSON ({len(resp.text)} bytes): {resp.text[:160]!r}",
				meta,
			)

	if not isinstance(body, dict):
		return _fail(-500, f"Pilot returned {type(body).__name__}, expected an object.", meta)

	body["_pilot"] = meta
	body.setdefault("data", [])
	return body


# --------------------------------------------------------------------------
# Layer 2: resolving the inputs from the ERP
# --------------------------------------------------------------------------


def _resolve_imei(ticket: "frappe.Document", override: str | None) -> tuple[str, str]:
	"""Return (imei, where_it_came_from)."""
	if override:
		return str(override).strip(), "argument"

	for fn in TICKET_IMEI_FIELDS:
		val = ticket.get(fn)
		if val:
			return str(val).strip(), f"xticket.{fn}"

	plate = ticket.get("license_plate")
	if plate:
		val = frappe.db.get_value("Customer Vehicle", plate, "device_serial")
		if val:
			return str(val).strip(), "Customer Vehicle.device_serial"

	frappe.throw(
		_("This ticket has no device serial (IMEI), and neither does its vehicle."),
		title=_("No IMEI"),
	)


def _split_emails(raw: str) -> list[str]:
	"""'a@x.com----b@x.com' -> ['a@x.com', 'b@x.com']"""
	cleaned = str(raw)
	for sep in EMAIL_SEPARATORS:
		cleaned = cleaned.replace(sep, " ")
	return [p.strip() for p in cleaned.split() if "@" in p]


def _collect_emails(ticket: "frappe.Document", settings: dict) -> list[tuple[str, str]]:
	"""Every candidate Pilot account, in order of specificity.

	Returns [(email, where_it_came_from), ...] with duplicates removed but
	order preserved -- the ticket's own address is tried before the customer's.
	"""
	sources: list[tuple[str, str]] = []

	if ticket.get("email_pilot"):
		sources.append(("xticket.email_pilot", ticket.get("email_pilot")))

	customer_name = ticket.get("customer")
	if customer_name:
		customer = frappe.get_cached_doc("Customer", customer_name)
		for fn in CUSTOMER_EMAIL_FIELDS:
			val = customer.get(fn)
			if val:
				sources.append((f"Customer.{fn}", val))

	out: list[tuple[str, str]] = []
	seen: set[str] = set()
	for origin, raw in sources:
		for email in _split_emails(raw):
			if email not in seen:
				seen.add(email)
				out.append((email, origin))

	if not out and settings["fallback_username"]:
		out.append((settings["fallback_username"], "app_apis.pilot_fallback_username"))

	if not out:
		frappe.throw(
			_(
				"No Pilot email found for this ticket. Checked xticket.email_pilot and "
				"{0} email fields on Customer {1}, plus the fallback username in "
				"app_apis."
			).format(len(CUSTOMER_EMAIL_FIELDS), customer_name or "-"),
			title=_("No Pilot Account"),
		)

	return out


# --------------------------------------------------------------------------
# Layer 3: parsing the device payload
# --------------------------------------------------------------------------


def _clean_label(name) -> str:
	"""Text inside the LAST parentheses: 'Reefer Temp 1 (T1)' -> 'T1'."""
	s = str(name or "").strip()
	if s.endswith(")") and "(" in s:
		return s[s.rfind("(") + 1 : -1].strip()
	return ""


def _label_kind(label: str) -> tuple[str, str]:
	"""'T1' -> ('temperature','1'), 'H2' -> ('humidity','2'), else ('','')."""
	if len(label) >= 2 and label[1:].isdigit():
		if label[0] in "Tt":
			return "temperature", label[1:]
		if label[0] in "Hh":
			return "humidity", label[1:]
	return "", ""


def _as_number(value):
	"""Numeric reading, or None when the reading is not a number.

	Sensors mix numbers ("32", "-18.4") with words ("High", "On"). flt("High")
	returns 0.0, which would publish a confident, wrong zero for every text
	reading -- so return None instead and let `display` carry the words.
	"""
	if value is None or value == "":
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _parse_sensors(sensors: list) -> tuple[list, list, str]:
	"""Split sensors into paired probes and everything else.

	Returns (probes, others, naming_rule).

	Primary rule (as specified): pair by the "(T1)"/"(H1)" suffix Pilot uses on
	reefer fleets.

	Fallback: some accounts report flat names -- "Temperature", "Humidity",
	with no suffix at all. For those the primary rule matches nothing, every
	reading lands in `others`, and `probes` comes back uselessly empty. So when
	NOT ONE sensor in the payload carries a (T#)/(H#) suffix, pair on the name
	itself and number the probes in order of appearance. The returned
	`naming_rule` says which rule actually fired, so the UI can be honest about
	where "Probe 1" came from.
	"""
	entries = []
	for s in sensors:
		name = str(s.get("name") or "").strip()
		label = _clean_label(name)
		entries.append(
			(
				s,
				label,
				{
					"id": s.get("id"),
					"name": name,
					"label": label,
					"display": str(s.get("hum_value") or "").strip(),
					"value": _as_number(s.get("dig_value")),
					"raw_value": s.get("raw_value"),
					"group": s.get("group"),
					"ts": frappe.utils.cint(s.get("change_ts")),
				},
			)
		)

	has_labelled = any(_label_kind(label)[0] for _s, label, _e in entries)
	naming = "label" if has_labelled else "none"

	probes: dict[str, dict] = {}
	others: list[dict] = []
	seq = {"temperature": 0, "humidity": 0}

	for _s, label, entry in entries:
		if has_labelled:
			kind, idx = _label_kind(label)
		else:
			low = entry["name"].lower()
			kind = "temperature" if "temp" in low else ("humidity" if "humid" in low else "")
			idx = ""
			if kind:
				seq[kind] += 1
				idx = str(seq[kind])
				naming = "keyword"

		if not (kind and idx):
			others.append(entry)
			continue

		probe = probes.setdefault(
			idx,
			{
				"index": frappe.utils.cint(idx),
				"label": f"Probe {idx}",
				"group": entry.get("group"),
				"temperature": None,
				"humidity": None,
			},
		)
		probe[kind] = entry
		if not probe.get("group"):
			probe["group"] = entry.get("group")

	probe_list = [probes[k] for k in sorted(probes, key=frappe.utils.cint)]
	return probe_list, others, naming


def _parse_location(device: dict) -> dict:
	"""lat/lon from `status`, falling back to `configuration`/`config`."""
	status = device.get("status") or {}
	lat = frappe.utils.flt(status.get("lat"))
	lon = frappe.utils.flt(status.get("lon"))
	source = "status"

	if not lat and not lon:
		raw = device.get("configuration") or device.get("config")
		parsed = None
		if raw:
			try:
				parsed = frappe.parse_json(raw)
			except Exception:
				parsed = None
		# `configuration` is a plain model name ("Teltonika FMC920") on most
		# accounts, so only trust it when it parsed into something with a lat.
		if isinstance(parsed, dict) and parsed.get("lat"):
			lat = frappe.utils.flt(parsed.get("lat"))
			lon = frappe.utils.flt(parsed.get("lon"))
			source = "config"

	if not lat and not lon:
		return {"lat": None, "lon": None, "source": "none",
		        "sats": frappe.utils.cint(status.get("satsinview"))}

	return {"lat": lat, "lon": lon, "source": source,
	        "sats": frappe.utils.cint(status.get("satsinview"))}


# --------------------------------------------------------------------------
# The one public method
# --------------------------------------------------------------------------


@frappe.whitelist()
def get_snapshot(ticket: str, imei: str | None = None, node: str | None = None) -> dict:
	"""Live Pilot snapshot for one xticket. THE public entry point.

	Called from the Client Script as:

	    frappe.call({
	        method: "app_apis.connector.get_snapshot",
	        args: { ticket: frm.doc.name },
	    })

	`imei` and `node` are optional overrides, useful for diagnosing one device
	without editing the ticket.

	Raises (via frappe.throw) when no account can produce data, with a message
	naming every account tried and why each was rejected.
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
	node = str(node or settings["node"]).strip()

	imei, imei_source = _resolve_imei(doc, imei)
	candidates = _collect_emails(doc, settings)

	# ---- multi-account retry -------------------------------------------
	# Pilot accounts are per-fleet, so only one of a customer's addresses is
	# usually entitled to a given IMEI. Try each in turn and keep the reason
	# every rejected account was rejected, so a total failure is diagnosable
	# from the error message alone.
	result = None
	used_email = None
	used_origin = None
	rejected: list[str] = []
	auth_failures = 0

	for email, origin in candidates:
		body = _fetch_status(imei, email, node, settings)
		code = frappe.utils.cint(body.get("code"))
		rows = body.get("data") or []

		if code == 0 and rows:
			result, used_email, used_origin = body, email, origin
			break

		if code == 0:
			why = "authenticated, but Pilot returned no device for this IMEI"
		else:
			why = str(body.get("msg") or f"code {code}")

		if code == -401:
			auth_failures += 1

		rejected.append(f"{email} ({origin}) -> {why}")

	if not result:
		offline = is_offline()

		detail = _("Pilot returned no data for IMEI {0} on node {1}.").format(imei, node)
		detail += "\n\n" + _("Tried {0} account(s):").format(len(candidates))
		detail += "\n- " + "\n- ".join(rejected)

		if offline:
			# Deliberately avoids the words "401" / "unauthorized" / "credential" /
			# "rejected": the Client Script's error handler pattern-matches on
			# those to show a "Pilot Login Rejected" dialog, and nothing here was
			# ever rejected -- no request reached Pilot at all. See `is_offline`.
			detail += "\n\n" + _(
				"This site is running on OFFLINE FIXTURES -- `pilot_offline` is set in "
				"site_config.json, so no request was ever sent to Pilot and no password "
				"was ever checked. The account(s) above were only compared against the "
				"offline stand-in's own short list. Set `pilot_offline` to 0 (or remove "
				"it) to make Check Pilot a live call."
			)
		elif auth_failures and auth_failures == len(candidates):
			detail += "\n\n" + _(
				"EVERY account was rejected with 401 Unauthorized. That is a credential "
				"problem on the Pilot side, not a bug in this integration. Confirm the "
				"email and password work when signing in to the Pilot portal, confirm "
				"node {0} is correct, and confirm IMEI {1} is registered under one of "
				"these accounts."
			).format(node, imei)

		frappe.throw(detail, title=_("Pilot: Offline Fixtures") if offline else _("Pilot: No Data"))

	# ---- shape the result ----------------------------------------------
	device = (result.get("data") or [])[0]
	status = device.get("status") or {}
	diag = result.get("_pilot") or {}

	probes, others, naming = _parse_sensors(device.get("sensors_status") or [])

	last_epoch = frappe.utils.cint(status.get("unixtimestamp"))
	now_epoch = frappe.utils.cint(diag.get("server_epoch"))
	age_seconds = (now_epoch - last_epoch) if (last_epoch and now_epoch) else None
	is_stale = (age_seconds > settings["stale_after_minutes"] * 60) if age_seconds is not None else None

	return {
		"ticket": ticket,
		"device": {
			"imei": device.get("imei") or imei,
			"name": device.get("vehiclenumber"),
			"folder": device.get("folder"),
			"type": device.get("type"),
			"typeid": device.get("typeid"),
			"model": device.get("model") or device.get("configuration"),
			"configuration": device.get("configuration"),
			"agentid": device.get("agentid"),
			"uniqid": device.get("uniqid"),
			"vin": device.get("vin"),
			"info": device.get("info"),
			"driver_name": device.get("driver_name"),
			"driver_phone": device.get("driver_phone"),
			"created_time": frappe.utils.cint(device.get("created_time")) or None,
			"current_mileage": _as_number(device.get("current_mileage")),
			"initial_mileage": _as_number(device.get("initial_mileage")),
		},
		"ticket_info": {
			"customer": doc.get("customer"),
			"license_plate": doc.get("license_plate"),
			"issue_type": doc.get("issue_type"),
			"status": doc.get("status"),
		},
		"state": {
			"active": frappe.utils.cint(status.get("active")),
			"ignition": status.get("firing"),
			# `moving` and `parking` are DURATION COUNTERS on this account
			# (e.g. moving=3650), not booleans -- Pilot appears to report
			# seconds in the current state. Both raw values are passed through
			# so the UI can show the duration rather than a misleading Yes/No.
			"moving": frappe.utils.cint(status.get("moving")),
			"parking": frappe.utils.cint(status.get("parking")),
			"speed": frappe.utils.flt(status.get("speed")),
			"direction": status.get("direction"),
			"altitude": _as_number(status.get("alt")),
		},
		"last_update": {
			"epoch": last_epoch,
			"age_seconds": age_seconds,
			"is_stale": is_stale,
		},
		"location": _parse_location(device),
		"probes": probes,
		"probe_naming": naming,
		"others": others,
		"stale_after_minutes": settings["stale_after_minutes"],
		"account": used_email,
		"account_source": used_origin,
		"imei_source": imei_source,
		"node": node,
		"diagnostics": {
			"offline": diag.get("offline"),
			"http_status": diag.get("http_status"),
			"elapsed_ms": diag.get("elapsed_ms"),
			"pilot_msg": result.get("msg"),
			"pilot_request_time": result.get("reqest_time"),  # Pilot's own spelling
			"server_epoch": now_epoch,
			"accounts_tried": len(rejected) + 1,
			"rejected": rejected,
		},
		# Everything Pilot sent for this device, untouched. The curated keys
		# above cover the UI; this guarantees a field Pilot adds later is still
		# reachable without another round trip or a code change.
		"raw": device,
	}


def _collect_emails_for_customer(customer_name: str | None, settings: dict) -> list[tuple[str, str]]:
	"""Every candidate Pilot account for a CUSTOMER, with no ticket to read.

	Same `Customer.email_pilot*` fields `_collect_emails` reads -- just without
	the ticket-level `email_pilot` override a ticket can carry, since there is
	no ticket here. Falls back to `pilot_fallback_username`, same as the
	ticket path, when the customer has none of these fields set (or there is
	no customer at all -- a Fleet Audit row can be Pilot/IM-only, with nothing
	in ERPNext to read a customer off of).
	"""
	sources: list[tuple[str, str]] = []
	if customer_name:
		try:
			customer = frappe.get_cached_doc("Customer", customer_name)
		except frappe.DoesNotExistError:
			customer = None
		if customer:
			for fn in CUSTOMER_EMAIL_FIELDS:
				val = customer.get(fn)
				if val:
					sources.append((f"Customer.{fn}", val))

	out: list[tuple[str, str]] = []
	seen: set[str] = set()
	for origin, raw in sources:
		for email in _split_emails(raw):
			if email not in seen:
				seen.add(email)
				out.append((email, origin))

	if not out and settings["fallback_username"]:
		out.append((settings["fallback_username"], "app_apis.pilot_fallback_username"))

	return out


@frappe.whitelist()
def get_vehicle_live(imei: str, customer: str | None = None, node: str | None = None) -> dict:
	"""Live Pilot snapshot for one IMEI, with no ticket in the picture.

	Same account resolution and multi-account retry as `get_snapshot` -- Pilot
	accounts are per-fleet, so only one of a customer's own addresses is
	usually entitled to a given IMEI -- entered from a CUSTOMER name (off a
	Fleet Audit row) instead of an xticket. `get_snapshot` itself is untouched;
	this is a second, independent entry point, not a refactor of it.

	Raises (via frappe.throw) when no account can produce data, with a message
	naming every account tried and why each was rejected -- same contract as
	`get_snapshot`.
	"""
	frappe.only_for(["System Manager", "Technical", "Support Team"])

	imei = str(imei or "").strip()
	if not imei:
		frappe.throw(_("imei is required."), title=_("Pilot"))

	settings = _settings()
	node = str(node or settings["node"]).strip()
	candidates = _collect_emails_for_customer(customer, settings)

	if not candidates:
		frappe.throw(
			_(
				"No Pilot email found. Checked {0} email fields on Customer {1}, plus "
				"the fallback username in app_apis."
			).format(len(CUSTOMER_EMAIL_FIELDS), customer or "-"),
			title=_("No Pilot Account"),
		)

	result = None
	used_email = None
	used_origin = None
	rejected: list[str] = []
	auth_failures = 0

	for email, origin in candidates:
		body = _fetch_status(imei, email, node, settings)
		code = frappe.utils.cint(body.get("code"))
		rows = body.get("data") or []

		if code == 0 and rows:
			result, used_email, used_origin = body, email, origin
			break

		if code == 0:
			why = "authenticated, but Pilot returned no device for this IMEI"
		else:
			why = str(body.get("msg") or f"code {code}")

		if code == -401:
			auth_failures += 1

		rejected.append(f"{email} ({origin}) -> {why}")

	if not result:
		offline = is_offline()

		detail = _("Pilot returned no data for IMEI {0} on node {1}.").format(imei, node)
		detail += "\n\n" + _("Tried {0} account(s):").format(len(candidates))
		detail += "\n- " + "\n- ".join(rejected)

		if offline:
			# Same wording constraint as get_snapshot: avoid "401" / "unauthorized"
			# / "credential" / "rejected" so the Client Script's error-matching
			# regex cannot mistake this for a Pilot login rejection.
			detail += "\n\n" + _(
				"This site is running on OFFLINE FIXTURES -- `pilot_offline` is set in "
				"site_config.json, so no request was ever sent to Pilot and no password "
				"was ever checked. Set `pilot_offline` to 0 (or remove it) to make this a "
				"live call."
			)
		elif auth_failures and auth_failures == len(candidates):
			detail += "\n\n" + _(
				"EVERY account was rejected with 401 Unauthorized. That is a credential "
				"problem on the Pilot side, not a bug in this integration."
			)

		frappe.throw(detail, title=_("Pilot: Offline Fixtures") if offline else _("Pilot: No Data"))

	device = (result.get("data") or [])[0]
	status_ = device.get("status") or {}
	diag = result.get("_pilot") or {}

	probes, others, naming = _parse_sensors(device.get("sensors_status") or [])

	last_epoch = frappe.utils.cint(status_.get("unixtimestamp"))
	now_epoch = frappe.utils.cint(diag.get("server_epoch"))
	age_seconds = (now_epoch - last_epoch) if (last_epoch and now_epoch) else None
	is_stale = (age_seconds > settings["stale_after_minutes"] * 60) if age_seconds is not None else None

	# Same shape as `get_snapshot` (device/state/probes/others included), minus
	# `ticket`/`ticket_info`/`lookup` -- there is no ticket here. Kept this
	# close on purpose: the Fleet Audit's dialog reuses the same renderer that
	# draws "Check Pilot" on a ticket, and a reduced shape would just mean
	# reduced sections in that renderer for no reason.
	return {
		"device": {
			"imei": device.get("imei") or imei,
			"name": device.get("vehiclenumber"),
			"folder": device.get("folder"),
			"type": device.get("type"),
			"typeid": device.get("typeid"),
			"model": device.get("model") or device.get("configuration"),
			"configuration": device.get("configuration"),
			"agentid": device.get("agentid"),
			"uniqid": device.get("uniqid"),
			"vin": device.get("vin"),
			"info": device.get("info"),
			"driver_name": device.get("driver_name"),
			"driver_phone": device.get("driver_phone"),
			"created_time": frappe.utils.cint(device.get("created_time")) or None,
			"current_mileage": _as_number(device.get("current_mileage")),
			"initial_mileage": _as_number(device.get("initial_mileage")),
		},
		"state": {
			"active": frappe.utils.cint(status_.get("active")),
			"ignition": status_.get("firing"),
			"moving": frappe.utils.cint(status_.get("moving")),
			"parking": frappe.utils.cint(status_.get("parking")),
			"speed": frappe.utils.flt(status_.get("speed")),
			"direction": status_.get("direction"),
			"altitude": _as_number(status_.get("alt")),
		},
		"last_update": {
			"epoch": last_epoch,
			"age_seconds": age_seconds,
			"is_stale": is_stale,
		},
		"location": _parse_location(device),
		"probes": probes,
		"probe_naming": naming,
		"others": others,
		"stale_after_minutes": settings["stale_after_minutes"],
		"account": used_email,
		"account_source": used_origin,
		"imei_source": "argument",
		"node": node,
		"diagnostics": {
			"http_status": diag.get("http_status"),
			"elapsed_ms": diag.get("elapsed_ms"),
			"pilot_msg": result.get("msg"),
			"pilot_request_time": result.get("reqest_time"),
			"server_epoch": now_epoch,
			"accounts_tried": len(rejected) + 1,
			"rejected": rejected,
		},
		"raw": device,
	}


# --------------------------------------------------------------------------
# Introspection -- NOT whitelisted; `bench console` only, by design.
# --------------------------------------------------------------------------


def status() -> dict:
	cfg = _settings()
	return {
		"offline": is_offline(),
		"base_url": cfg["base_url"],
		"node": cfg["node"],
		"timeout": cfg["timeout"],
		"stale_after_minutes": cfg["stale_after_minutes"],
		"shared_password_set": bool(cfg["doc"].pilot_password),
		"per_account_passwords": sorted((frappe.conf.get("pilot_passwords") or {}).keys()),
		"offline_devices": sorted(_DEVICES),
		"offline_accounts": sorted(VALID_ACCOUNTS),
	}
