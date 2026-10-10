"""The role-checked, whitelisted front door to the platform layer.

app_apis.core.platforms, .im, .pilot_panel and .messages are plain Python: another app imports them and
calls them directly. THIS module is for everything that has to arrive over HTTP or through
`frappe.call` -- Server Scripts, Client Scripts, a desk button. Each method here checks the caller's role
and then calls the same function.

    frappe.call("app_apis.core.api.find", platform="im", imei="123...", vehicle="...")
    frappe.call("app_apis.core.api.create", platform="pilot2", vehicle="...", dry_run=0)
    frappe.call("app_apis.core.api.delete", platform="pilot2", vehicle="...", confirm="DELETE")

Safety defaults: `create` is a dry run unless you pass dry_run=0, and `delete` does nothing without
confirm="DELETE". IM calls need a System Manager login (the HTTP helper they go through is limited to
that role); the Pilot ones work for System Manager and Technical.

`notify` lets a script that did something ITSELF (for example the vehicle_upload_api Server Script)
report it, so handlers registered through app_apis.core.events hear about it too.
"""

import frappe

from app_apis.core import events, lebara, messages, platforms

OPERATORS = ("System Manager", "Technical")
READERS = ("System Manager", "Technical", "Support Team")
DELETE_WORD = "DELETE"
MANAGERS = ("System Manager",)  # the HTTP layer under live Lebara calls is limited to this role
REPORTABLE = ("after_create", "after_delete", "after_block", "after_status", "after_wasl")


def _only(roles):
	frappe.only_for(roles)


@frappe.whitelist(methods=["POST"])
def find(platform: str, imei: str, vehicle: str = ""):
	_only(READERS)
	return platforms.find(platform, imei, vehicle)


@frappe.whitelist(methods=["POST"])
def build(platform: str, vehicle: str):
	"""What create would send, resolved. Sends nothing."""
	_only(READERS)
	return platforms.build(platform, vehicle)


@frappe.whitelist(methods=["POST"])
def create(platform: str, vehicle: str, dry_run=1):
	"""Create the vehicle on a platform. A DRY RUN unless dry_run=0."""
	_only(OPERATORS)
	return platforms.create(platform, vehicle, dry_run=bool(frappe.utils.cint(dry_run)), source="api")


@frappe.whitelist(methods=["POST"])
def delete(platform: str, vehicle: str, confirm: str = "", pin: str = ""):
	"""Delete the vehicle from a platform. Needs confirm="DELETE"; IM may also need its Security PIN."""
	_only(OPERATORS)
	if str(confirm or "").strip() != DELETE_WORD:
		frappe.throw("Nothing deleted: pass confirm=%s." % DELETE_WORD)
	return platforms.delete(platform, vehicle, pin=pin, source="api")


@frappe.whitelist(methods=["POST"])
def block(platform: str, imei: str, blocked=1, agent_id: str = "", node: str = ""):
	_only(OPERATORS)
	return platforms.block(platform, imei, bool(frappe.utils.cint(blocked)), agent_id, node, source="api")


@frappe.whitelist(methods=["POST"])
def live(platform: str, imei: str, vehicle: str = ""):
	_only(READERS)
	return platforms.live(platform, imei, vehicle)


@frappe.whitelist(methods=["POST"])
def wasl_status(imei: str):
	_only(READERS)
	return platforms.wasl_status(imei)


@frappe.whitelist(methods=["POST"])
def wasl_check(vehicle: str):
	_only(READERS)
	return platforms.wasl_check(vehicle)


@frappe.whitelist(methods=["POST"])
def wasl_delete(vehicle: str, confirm: str = ""):
	"""Delete from WASL only. Needs confirm="DELETE"."""
	_only(OPERATORS)
	if str(confirm or "").strip() != DELETE_WORD:
		frappe.throw("Nothing deleted: pass confirm=%s." % DELETE_WORD)
	return platforms.wasl_delete(vehicle, source="api")


@frappe.whitelist()
def summary(vehicle: str):
	"""What is already stored about where the vehicle is. Needs read permission on the vehicle."""
	return platforms.summary(vehicle)


# ------------------------------------------------------------------ custom messages
@frappe.whitelist()
def message_codes():
	_only(READERS)
	return [{"code": r["code"], "has_template": bool(r["template"]), "message": r["message"]} for r in messages.rows()]


@frappe.whitelist(methods=["POST"])
def message_render(code: str, context=None):
	_only(READERS)
	return {"code": code, "text": messages.render(code, frappe.parse_json(context) if context else {})}


@frappe.whitelist(methods=["POST"])
def message_send(code: str, phone: str, context=None, name: str = "", dry_run=1):
	"""Send a custom message. A DRY RUN unless dry_run=0."""
	_only(OPERATORS)
	return messages.send(code, phone, frappe.parse_json(context) if context else {}, name or None,
	                     dry_run=bool(frappe.utils.cint(dry_run)))


# ------------------------------------------------------------------ events reported by scripts
@frappe.whitelist(methods=["POST"])
def notify(event: str, platform: str = "", vehicle: str = "", imei: str = "", detail=None):
	"""Tell the registered handlers that a script did something. Only the after_* events can be reported
	this way (a script cannot veto: it has already acted), and the handlers see source="script"."""
	if frappe.session.user == "Guest":
		frappe.throw("Sign in first.", frappe.PermissionError)
	if event not in REPORTABLE:
		frappe.throw("Only these events can be reported: %s" % ", ".join(REPORTABLE))
	data = frappe.parse_json(detail) if detail else {}
	data = data if isinstance(data, dict) else {}
	for reserved in ("event", "platform", "vehicle", "imei", "source", "user"):
		data.pop(reserved, None)                 # the caller cannot overwrite what the event itself carries
	if vehicle and not frappe.has_permission("Customer Vehicle", "read", vehicle):
		frappe.throw("No permission on that vehicle.", frappe.PermissionError)
	return {"handlers_called": len(events.emit(event, platform=platform, vehicle=vehicle, imei=imei,
	                                           source="script", **data))}


# ------------------------------------------------------------------ Lebara
def _lebara(fn, *args, **kwargs):
	"""Run a core Lebara function, turning its own errors into a normal message for the caller."""
	try:
		return fn(*args, **kwargs)
	except lebara.LebaraError as e:
		frappe.throw(str(e))


@frappe.whitelist()
def lebara_status():
	"""Session and last-sync state of the Lebara connection. No request to Lebara."""
	_only(READERS)
	return lebara.status()


@frappe.whitelist(methods=["POST"])
def lebara_find(msisdn: str = "", iccid: str = "", imsi: str = "", id=0):
	"""One SIM, live from Lebara."""
	_only(MANAGERS)
	return _lebara(lebara.find_sim, msisdn=msisdn, iccid=iccid, imsi=imsi, id=id)


@frappe.whitelist(methods=["POST"])
def lebara_sync(what: str):
	"""Refresh a Lebara mirror now: sims | invoice | transactions. Returns what changed."""
	_only(MANAGERS)
	return _lebara(lebara.sync, what)


@frappe.whitelist(methods=["POST"])
def lebara_invoice(year, month, store=0):
	"""That month's invoice from Lebara. With store=1 it is also saved (Lebara Invoice / Invoice Line)."""
	_only(MANAGERS)
	if frappe.utils.cint(store):
		return _lebara(lebara.sync_invoice, year, month)
	got = _lebara(lebara.fetch_invoice, year, month)
	got["sims"] = len(got["lines"])
	got["lines"] = got["lines"][:20]  # a preview, not 26,000 rows
	return got


@frappe.whitelist()
def lebara_loss(period: str, as_of: str = ""):
	"""What Lebara billed for a month against what the ERP knows. Reads the stored mirrors only."""
	_only(READERS)
	return _lebara(lebara.loss_report, period, as_of or None)


@frappe.whitelist(methods=["POST"])
def lebara_sim_action(action: str, subscriber_id, confirm: str = ""):
	"""SuspendSIM | ResumeSIM | ActivateSIM on Lebara. Changes the SIM, so it needs confirm="CONFIRM"."""
	_only(MANAGERS)
	if str(confirm or "").strip() != "CONFIRM":
		frappe.throw("Nothing changed: pass confirm=CONFIRM.")
	return _lebara(lebara.sim_action, action, subscriber_id)


@frappe.whitelist(methods=["POST"])
def lebara_sync_scheduled(what: str):
	"""What the scheduler's Server Scripts call: skips quietly when Lebara is off or logged out and
	returns the outcome rather than raising."""
	_only(MANAGERS)
	return lebara.run_scheduled(what)
