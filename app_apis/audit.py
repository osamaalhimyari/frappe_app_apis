"""The action log: one App Apis Action Log row for every change made on an outside platform.

Written where the change is actually made, once:

* app_apis.core.platforms -- create / delete / block through the core (and the core.api endpoints);
* app_apis.wasl           -- WASL link and WASL-only delete (whoever called them);
* app_apis.core.lebara    -- SIM suspend / resume / activate through the core;
* the shipped Server Scripts write their own rows (they cannot import this module) for the buttons:
  upload, edit on Pilot, delete, block / unblock of expired devices, Lebara SIM actions and SMS.

Logging is best effort: it never raises, so it can never change the outcome of the action it records.
Read-only lookups are not logged.
"""

import json

import frappe

DOCTYPE = "App Apis Action Log"


def _text(value, limit=1000) -> str:
	if value is None:
		return ""
	if not isinstance(value, str):
		try:
			value = json.dumps(value, default=str, ensure_ascii=False)
		except Exception:
			value = str(value)
	return value[:limit]


def record(action: str, platform: str = "", vehicle: str = "", imei: str = "", ok=False, result=None,
           source: str = "core", user: str | None = None) -> str | None:
	"""Add one row. Returns its name, or None when it could not be written (never raises)."""
	try:
		if not frappe.db.exists("DocType", DOCTYPE):
			return None
		if vehicle and not frappe.db.exists("Customer Vehicle", vehicle):
			vehicle = ""
		doc = frappe.get_doc({
			"doctype": DOCTYPE,
			"user": user or (frappe.session.user if getattr(frappe.local, "session", None) else "Administrator"),
			"action": _text(action, 140),
			"platform": _text(platform, 140),
			"vehicle": vehicle or None,
			"imei": _text(imei, 140),
			"ok": 1 if ok else 0,
			"result": _text(result),
			"source": _text(source, 140),
		})
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		try:
			frappe.log_error(title="app_apis: action log row could not be written")
		except Exception:
			pass
		return None
