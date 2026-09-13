# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""WhatsApp message templates, as Chatwoot mirrors them from Meta.

WhatsApp lets a business send free text only inside the 24-hour customer
service window -- the day after the customer last wrote in. Outside it Meta
delivers nothing but a pre-approved template, and Chatwoot 4.x enforces that
itself: a plain message posted to a closed conversation is created, then marked
failed with "the WhatsApp 24-hour customer service window is closed and no
template parameters were provided". An automatic "we have your job" message
lands outside the window almost every time, because nobody writes first.

This module owns the template side of that:

  * `sync` copies every WhatsApp inbox's templates into App Apis WhatsApp
    Template, so a Status Rules row picks one from a Link field. Meta approves
    templates per WhatsApp Business Account -- per phone number -- so each
    record keeps its inbox, and the sender uses that inbox.
  * `variables` / `missing` read a row's Template Variables ("{{1}} = {plate}")
    against the template's own variables. `build` renders them with the same
    {placeholders} every other message here uses and returns Chatwoot's
    `template_params` for one send.
  * `send_test` sends one template to one number, so it can be seen arriving
    before a real ticket depends on it.

`build` re-reads the template from Chatwoot at send time instead of trusting
the last sync: a template Meta paused or deleted since then fails here with a
sentence, not at Meta with an error number.

Deciding *whether* a template is needed -- the 24-hour window -- is the
connector's job; see chatwoot_connector._route_window.
"""

import re
import time

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

DOCTYPE = "App Apis WhatsApp Template"

# {{1}} or {{customer_name}}: Meta's POSITIONAL and NAMED parameter formats.
VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# "{{1}} = {plate}" -- one assignment per line of a row's Template Variables.
ASSIGN_RE = re.compile(r"^\s*\{\{\s*([A-Za-z0-9_]+)\s*\}\}\s*[=:]\s*(.*?)\s*$")

MEDIA_HEADERS = ("IMAGE", "VIDEO", "DOCUMENT", "LOCATION")

# Buttons whose tap target is fixed inside the template, so there is nothing to
# fill. A URL button counts only while its URL has no variable in it.
STATIC_BUTTONS = ("QUICK_REPLY", "PHONE_NUMBER", "URL")

# After asking Chatwoot to re-read Meta, how long to wait before reading the
# list back. Chatwoot runs that refresh in its own background queue.
REFRESH_PAUSE = 4


# --------------------------------------------------------------------------
# Reading a template
# --------------------------------------------------------------------------


def _parts(tpl: dict) -> dict:
	"""A template's components, flattened: header (and its format), body, footer, buttons."""
	parts = {"header": "", "header_format": "", "body": "", "footer": "", "buttons": []}
	for comp in tpl.get("components") or []:
		kind = str(comp.get("type") or "").upper()
		if kind == "HEADER":
			parts["header_format"] = str(comp.get("format") or "TEXT").upper()
			parts["header"] = str(comp.get("text") or "")
		elif kind == "BODY":
			parts["body"] = str(comp.get("text") or "")
		elif kind == "FOOTER":
			parts["footer"] = str(comp.get("text") or "")
		elif kind == "BUTTONS":
			parts["buttons"] = list(comp.get("buttons") or [])
	return parts


def params_of(text: str) -> list[str]:
	"""The variables in a template's body, in the order they must be sent.

	Chatwoot maps the values it is given onto a positional template's {{1}},
	{{2}}... by position, not by key, so positional variables come back sorted
	numerically whatever order the sentence uses them in. Named variables are
	sent by name; they keep their order of appearance.
	"""
	seen = []
	for key in VAR_RE.findall(text or ""):
		if key not in seen:
			seen.append(key)
	if seen and all(key.isdigit() for key in seen):
		seen.sort(key=int)
	return seen


def params_list(stored: str | None) -> list[str]:
	"""The `params` column of a synced record ("1, 2") back as a list."""
	return [p.strip() for p in str(stored or "").split(",") if p.strip()]


def unsupported_reason(tpl: dict) -> str:
	"""Why this template cannot be sent from a Status Rules row, or "" if it can.

	A row fills the body's variables and nothing else. A template that needs a
	value anywhere else -- a media header, a variable in the header, a dynamic
	URL button, a one-time code -- would reach Meta incomplete and be refused,
	so it is kept out of the picker instead of failing on the first ticket.
	"""
	if str(tpl.get("category") or "").upper() == "AUTHENTICATION":
		return _("authentication templates need a one-time code")

	parts = _parts(tpl)
	if parts["header_format"] in MEDIA_HEADERS:
		return _("its {0} header needs a file").format(parts["header_format"].lower())
	if VAR_RE.search(parts["header"]):
		return _("its header has a variable")

	for button in parts["buttons"]:
		kind = str(button.get("type") or "").upper()
		if kind == "URL" and VAR_RE.search(str(button.get("url") or "")):
			return _("a URL button has a variable")
		if kind not in STATIC_BUTTONS:
			return _("{0} buttons are not supported").format(kind.replace("_", " ").lower() or _("unknown"))

	return ""


# --------------------------------------------------------------------------
# A row's Template Variables
# --------------------------------------------------------------------------


def variables(text: str | None, params: list[str]) -> dict[str, str]:
	"""A row's Template Variables mapped onto the template's variables.

	"{{1}} = {plate}" assigns one explicitly. A line without that prefix takes
	the next variable not yet assigned, in order, so a bare "{customer}" on
	line one is enough for a one-variable template. An assignment to a variable
	the template does not have is ignored.
	"""
	assigned, loose = {}, []
	for line in str(text or "").splitlines():
		if not line.strip():
			continue
		match = ASSIGN_RE.match(line)
		if match:
			if match.group(1) in params:
				assigned[match.group(1)] = match.group(2)
		else:
			loose.append(line.strip())

	for key in params:
		if key not in assigned and loose:
			assigned[key] = loose.pop(0)
	return assigned


def missing(params: list[str], assigned: dict) -> list[str]:
	"""Variables with nothing assigned to them."""
	return [key for key in params if not str(assigned.get(key) or "").strip()]


def _single_line(value: str) -> str:
	"""Meta refuses a parameter holding a newline, a tab or more than four
	spaces in a row (error 132018), and several placeholders here are whole
	multi-line blocks -- so every value is collapsed onto one line."""
	return re.sub(r"\s+", " ", str(value or "")).strip()


# --------------------------------------------------------------------------
# Building one send
# --------------------------------------------------------------------------


def _live_template(inbox_id: int, name: str, language: str, settings: dict):
	"""The template as Chatwoot holds it now.

	Returns (template, None), ({}, None) when the inbox no longer has it, or
	(None, refusal) when the inbox could not be read. Memoised per job, so a
	batch of sends reads each inbox once.
	"""
	from app_apis import chatwoot_connector as cw

	cache = getattr(frappe.local, "_wa_live_templates", None)
	if cache is None:
		cache = frappe.local._wa_live_templates = {}

	if inbox_id not in cache:
		data, meta = cw._request("GET", f"/inboxes/{inbox_id}", settings)
		if data is None:
			return None, cw._fail(*meta["error"], meta)
		cache[inbox_id] = (cw._payload(data) or {}).get("message_templates") or []

	for tpl in cache[inbox_id]:
		if tpl.get("name") == name and tpl.get("language") == language:
			return tpl, None
	return {}, None


def build(template: str, variables_text: str, ctx: dict, settings: dict) -> dict:
	"""Chatwoot's `template_params` for one send, or a -409 refusal saying why.

	`template` is an App Apis WhatsApp Template name; `ctx` holds the ticket's
	placeholder values (chatwoot_connector._context). -409 means "declined, and
	nothing was posted": the template is gone, not approved, or a variable came
	out empty for this particular ticket -- Meta refuses an empty parameter.
	"""
	from app_apis import chatwoot_connector as cw

	rec = frappe.db.get_value(
		DOCTYPE, template, ["title", "template_name", "language", "inbox_id"], as_dict=True
	)
	if not rec:
		return cw._fail(-409, _("WhatsApp template {0} is not in the synced list. Press Sync Templates from Chatwoot.").format(template))

	live, failed = _live_template(cint(rec.inbox_id), rec.template_name, rec.language, settings)
	if failed:
		return failed
	if not live:
		return cw._fail(-409, _("WhatsApp template {0} is no longer on its inbox in Chatwoot. Press Sync Templates from Chatwoot and pick another.").format(rec.title))

	status = str(live.get("status") or "").upper()
	if status != "APPROVED":
		return cw._fail(-409, _("WhatsApp template {0} is {1} at Meta, not approved.").format(rec.title, status.lower() or _("unknown")))

	reason = unsupported_reason(live)
	if reason:
		return cw._fail(-409, _("WhatsApp template {0} cannot be sent from here: {1}.").format(rec.title, reason))

	parts = _parts(live)
	params = params_of(parts["body"])
	assigned = variables(variables_text, params)
	values = {key: _single_line(cw._render(assigned.get(key) or "", ctx)) for key in params}

	empty = [key for key in params if not values[key]]
	if empty:
		return cw._fail(-409, _("WhatsApp template {0}: {1} came out empty for this ticket (Template Variables says: {2}).").format(
			rec.title,
			", ".join("{{%s}}" % key for key in empty),
			"; ".join("{{%s}} = %s" % (key, assigned.get(key) or "") for key in empty),
		))

	body = VAR_RE.sub(lambda m: values.get(m.group(1), m.group(0)), parts["body"])
	header = parts["header"] if parts["header_format"] in ("", "TEXT") else ""
	# What the Chatwoot agent sees in the conversation: the message as the
	# recipient reads it, header and footer included.
	content = "\n\n".join(part.strip() for part in (header, body, parts["footer"]) if part and part.strip())

	template_params = {
		"name": live.get("name"),
		"category": live.get("category"),
		"language": live.get("language"),
		# The shape Chatwoot 4.x's own template composer sends. Dict order is
		# the send order -- see params_of.
		"processed_params": {"body": values} if values else {},
	}
	if live.get("namespace"):
		template_params["namespace"] = live["namespace"]

	return {
		"ok": True,
		"inbox_id": cint(rec.inbox_id),
		"label": f"{rec.template_name} ({rec.language})",
		"title": rec.title,
		"content": content,
		"template_params": template_params,
	}


# --------------------------------------------------------------------------
# Sync from Chatwoot
# --------------------------------------------------------------------------


def _whatsapp_inboxes(settings: dict):
	"""(WhatsApp inboxes with their templates, None) or ([], a _fail())."""
	from app_apis import chatwoot_connector as cw

	data, meta = cw._request("GET", "/inboxes", settings)
	if data is None:
		return [], cw._fail(*meta["error"], meta)
	return [
		ib for ib in (cw._payload(data) or [])
		if "whatsapp" in str(ib.get("channel_type") or "").lower()
	], None


def _record(inbox: dict, tpl: dict) -> dict:
	"""One Chatwoot template as an App Apis WhatsApp Template's fields."""
	parts = _parts(tpl)
	inbox_id = cint(inbox.get("id"))
	inbox_name = str(inbox.get("name") or inbox_id)
	name = str(tpl.get("name") or "")
	language = str(tpl.get("language") or "")
	status = str(tpl.get("status") or "").upper()

	reason = unsupported_reason(tpl)
	if not reason and status != "APPROVED":
		reason = _("not approved ({0})").format(status.lower() or _("no status"))

	return {
		"template_key": f"{inbox_id}-{name}-{language}",
		"title": f"{name} ({language}) · {inbox_name}",
		"template_name": name,
		"language": language,
		"inbox_id": inbox_id,
		"inbox_name": inbox_name,
		"category": str(tpl.get("category") or "").upper(),
		"status": status,
		"supported": 0 if reason else 1,
		"unsupported_reason": reason,
		"parameter_format": str(tpl.get("parameter_format") or "POSITIONAL").upper(),
		"params": ", ".join(params_of(parts["body"])),
		"header": parts["header"] if parts["header_format"] in ("", "TEXT") else parts["header_format"],
		"body": parts["body"],
		"footer": parts["footer"],
		"buttons": "\n".join(str(b.get("text") or b.get("type") or "") for b in parts["buttons"]),
		"chatwoot_template_id": str(tpl.get("id") or ""),
	}


@frappe.whitelist()
def sync(connector: str | None = None) -> dict:
	"""Copy every WhatsApp inbox's templates out of Chatwoot. Sends nothing.

	Drives the Sync Templates button on the settings form. Chatwoot is first
	asked to re-read Meta (its own "sync templates" action, where the version
	has it); that runs in Chatwoot's queue, so the list is read back after a
	short pause, and a template approved a minute ago may need a second press.

	Records are upserted, never deleted: one that has left Chatwoot is marked
	REMOVED, because a Status Rules row may still point at it. Only inboxes
	this token can see are reconciled -- an inbox missing from the answer is
	left alone rather than having all its templates declared gone.
	"""
	frappe.only_for("System Manager")

	from app_apis import chatwoot_connector as cw

	settings = cw._settings_for(connector)
	if settings.get("method") == "webhook":
		return cw._fail(-400, _("The Chatwoot connector is in webhook mode, which has no inboxes to read templates from."))
	if not settings["base_url"] or not settings["account_id"] or not cw._token(settings):
		return cw._fail(-400, _("Domain, Account ID and API Token must all be set."))

	inboxes, failed = _whatsapp_inboxes(settings)
	if failed:
		return failed

	refreshed = 0
	for inbox in inboxes:
		data, _meta = cw._request("POST", f"/inboxes/{cint(inbox.get('id'))}/sync_templates", settings)
		refreshed += data is not None
	if refreshed:
		time.sleep(REFRESH_PAUSE)
		inboxes, failed = _whatsapp_inboxes(settings)
		if failed:
			return failed

	now = now_datetime()
	seen, rows = set(), []
	for inbox in inboxes:
		for tpl in inbox.get("message_templates") or []:
			if not tpl.get("name") or not tpl.get("language"):
				continue
			record = _record(inbox, tpl)
			seen.add(record["template_key"])

			if frappe.db.exists(DOCTYPE, record["template_key"]):
				doc = frappe.get_doc(DOCTYPE, record["template_key"])
			else:
				doc = frappe.new_doc(DOCTYPE)
			doc.update(record)
			doc.last_synced = now
			doc.save(ignore_permissions=True)

			rows.append({
				"name": record["template_name"],
				"language": record["language"],
				"inbox": record["inbox_name"],
				"category": record["category"],
				"params": record["params"],
				"supported": bool(record["supported"]),
				"reason": record["unsupported_reason"],
			})

	removed = []
	inbox_ids = [cint(inbox.get("id")) for inbox in inboxes]
	if inbox_ids:
		for old in frappe.get_all(
			DOCTYPE,
			filters={"inbox_id": ["in", inbox_ids], "status": ["!=", "REMOVED"]},
			fields=["name", "title"],
		):
			if old.name in seen:
				continue
			frappe.db.set_value(DOCTYPE, old.name, {
				"status": "REMOVED",
				"supported": 0,
				"unsupported_reason": _("no longer in Chatwoot"),
				"last_synced": now,
			})
			removed.append(old.title)

	frappe.db.commit()
	return {
		"ok": True,
		"refreshed": bool(refreshed),
		"inboxes": [{"id": cint(ib.get("id")), "name": ib.get("name")} for ib in inboxes],
		"templates": rows,
		"removed": removed,
	}


# --------------------------------------------------------------------------
# Proving a template by hand
# --------------------------------------------------------------------------


@frappe.whitelist()
def send_test(template: str, phone: str, variables: str = "") -> dict:
	"""Send one template to one number, now. System Manager only.

	For seeing a template arrive before a real ticket relies on it, so it
	always sends the template itself -- even to a number inside its 24-hour
	window -- and waits to report whether WhatsApp accepted it. There is no
	ticket here to fill {customer} or {plate} from: type sample values.
	"""
	frappe.only_for("System Manager")

	from app_apis import chatwoot_connector as cw

	destination = cw._normalise_phone(phone)
	if not destination:
		return cw._fail(-400, _("{0} is not a usable phone number.").format(phone))

	title = frappe.db.get_value(DOCTYPE, template, "title") or template
	return cw._send(
		_("Test of WhatsApp template {0}").format(title),
		phone=destination,
		template_fallback={"template": template, "variables": variables, "ctx": {}, "force": True},
	)
