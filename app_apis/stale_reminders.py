# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Nudge the technician on a ticket that has gone quiet.

Different trigger from auto_messages.py: that one fires off a save, this one
fires off the clock. Once an hour the scheduler calls `hourly`, which asks
`due_now` whether this is the configured hour of the day (the same trick
subscription_reminders.py uses, so the schedule is a setting an operator can
change from the desk instead of a cron string that needs a deploy). If it is,
every enabled row in the `app_apis` Stale Ticket Rules table is checked against
every xticket currently sitting in that row's status, and any ticket that has
not been touched (its `modified` timestamp) for at least that row's Hours
Without Update gets a message queued to whoever is assigned to it.

Sending happens in a background job on the `long` queue, exactly like
auto_messages -- the scheduler tick that found forty stale tickets should not
sit on forty outbound HTTP calls to Chatwoot.

The wording is always the rule row's own Message field: this module renders it
through chatwoot_connector's normal {placeholder} substitution, but it never
falls back to canned text the way the three save-triggered templates do -- the
Message field is required, so there is always an operator's own wording to
send. The `stale_reminder` entry in chatwoot_connector.TEMPLATES exists only so
that substitution treats this as a technician message (right-hand {vehicle},
{location}, {phone} lines) the same as tech_accepted/tech_working/tech_done.

Logged into the same App Apis Message Log table as auto_messages, under
template `stale_reminder`, so every outbound message -- transition-triggered or
clock-triggered -- shows up in one place.
"""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

LOGGER = "app_apis"
TEMPLATE = "stale_reminder"


def _settings():
	"""The settings Single, from cache. Cheap enough for an hourly tick."""
	return frappe.get_cached_doc("app_apis")


def _rules(settings) -> list[dict]:
	"""Enabled, fully-filled-in rows, as plain dicts.

	A row missing a status or a message cannot do anything useful, so it is
	dropped here rather than failing loudly on every tick -- half-filled-in rows
	are a normal thing to have while an operator is still typing.
	"""
	rows = []
	for row in settings.get("stale_ticket_rules") or []:
		state = str(row.get("state") or "").strip()
		message = str(row.get("message") or "").strip()
		if not cint(row.get("enabled")) or not state or not message:
			continue
		rows.append({
			"state": state,
			"hours": cint(row.get("hours")) or 24,
			"message": message,
			"send_once": cint(row.get("send_once")),
		})
	return rows


def due_now(settings, when=None) -> tuple[bool, str]:
	"""Is this the hour the operator asked for? See subscription_reminders.due_now."""
	when = when or now_datetime()
	hour = cint(settings.get("stale_ticket_reminder_hour"))
	if when.hour != hour:
		return False, f"not the configured hour (now {when.hour:02d}:00, want {hour:02d}:00)"
	return True, ""


def _stale_tickets(rule: dict) -> list[str]:
	"""Every xticket sitting in this rule's status, untouched, past its hours.

	Both spellings of "the ticket's state" are checked, same as auto_messages --
	`workflow_state` is what the workflow writes, `status` is what an operator
	reads, and this site keeps them in step but nothing here should assume that.
	"""
	cutoff = add_to_date(now_datetime(), hours=-rule["hours"])
	names = set()
	for field in ("workflow_state", "status"):
		names.update(
			frappe.get_all(
				"xticket",
				filters={field: rule["state"], "modified": ["<", cutoff]},
				pluck="name",
			)
		)
	return sorted(names)


def _last_nudge(ticket: str):
	"""When this ticket was last successfully nudged, or None."""
	rows = frappe.get_all(
		"App Apis Message Log",
		filters={"ticket": ticket, "template": TEMPLATE, "status": "Sent"},
		fields=["creation"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0]["creation"] if rows else None


def _log(doc, status: str, reason: str = "", result: dict | None = None, trigger: str = ""):
	"""Record one outcome. Guarded: a logging failure must not lose the send."""
	result = result or {}
	try:
		from app_apis import technicians

		entry = frappe.new_doc("App Apis Message Log")
		entry.ticket = doc.name
		entry.customer = str(doc.get("customer") or "")[:140]
		entry.recipient = "Technician"
		entry.engineer = str(technicians.name(doc) or "")[:140]
		entry.template = TEMPLATE
		entry.status = status
		entry.trigger_source = trigger
		entry.reason = reason
		entry.phone = result.get("phone") or ""
		entry.code = cint(result.get("code"))
		entry.message = result.get("message") or ""
		entry.conversation_id = str(result.get("conversation_id") or "")
		entry.message_id = str(result.get("message_id") or "")
		entry.sent_on = now_datetime() if status == "Sent" else None
		entry.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"stale-reminder log failed: {getattr(doc, 'name', doc)}")


# --------------------------------------------------------------------------
# The worker. Runs in its own process, seconds to hours after the scan.
# --------------------------------------------------------------------------


def send_one(ticket: str, rule: dict):
	"""Background entry point. Never raises -- there is nobody to raise to."""
	try:
		_send_one(ticket, rule)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"stale-reminder send failed: {ticket}")


def _send_one(ticket: str, rule: dict):
	from app_apis import chatwoot_connector as cw

	settings = _settings()

	# Re-checked here as well as at scan time: the switch can be turned off, or
	# the ticket can move on, in the time between the scan and the worker
	# actually running.
	if not cint(settings.get("technician_message_enabled")):
		return

	try:
		doc = frappe.get_doc("xticket", ticket)
	except frappe.DoesNotExistError:
		return

	current = str(doc.get("workflow_state") or doc.get("status") or "").strip().lower()
	if current != rule["state"].strip().lower():
		return  # moved on since the scan -- nothing to nudge about anymore

	trigger = f"{rule['state']}, stale >= {rule['hours']}h"

	if rule["send_once"]:
		last = _last_nudge(ticket)
		if last and doc.modified <= last:
			return  # already nudged, and the ticket has not been touched since

	frappe.set_user("Administrator")

	body = cw._render(rule["message"], cw._context(doc, TEMPLATE))
	result = cw.send_ticket_message(ticket, template=TEMPLATE, text=body) or {}

	if result.get("ok"):
		status = "Sent"
	elif result.get("code") == -404:
		# No phone on file: nothing was attempted, and nothing is broken.
		status = "Skipped"
	else:
		status = "Failed"

	_log(
		doc,
		status,
		reason="" if result.get("ok") else str(result.get("msg") or "")[:500],
		result=result,
		trigger=trigger,
	)


# --------------------------------------------------------------------------
# The scan. Runs inline, in the scheduler's own request -- cheap, since it is
# only ever a handful of `get_all` calls; the slow part is queued away above.
# --------------------------------------------------------------------------


def run(force: bool = False) -> dict:
	"""One pass: scan every rule, queue a nudge for every ticket that matches.

	`force` skips the hour gate for a manual run from the desk; it does NOT
	skip the enabled switch. That switch is the safety of this feature, and a
	convenience argument must not be able to turn it off.
	"""
	settings = _settings()

	if not cint(settings.get("stale_ticket_reminder_enabled")):
		return {"ok": True, "ran": False, "reason": "Stale ticket reminders are switched off."}

	if not force:
		due, why = due_now(settings)
		if not due:
			return {"ok": True, "ran": False, "reason": why}

	rules = _rules(settings)
	if not rules:
		return {"ok": True, "ran": True, "checked": 0, "queued": 0, "reason": "No enabled rules."}

	checked = 0
	queued = 0

	for rule in rules:
		tickets = _stale_tickets(rule)
		checked += len(tickets)
		for ticket in tickets:
			frappe.enqueue(
				"app_apis.stale_reminders.send_one",
				queue="long",
				timeout=300,
				enqueue_after_commit=True,
				job_id=f"stale-reminder::{ticket}::{rule['state']}",
				deduplicate=True,
				ticket=ticket,
				rule=rule,
			)
			queued += 1

	frappe.db.commit()
	return {"ok": True, "ran": True, "rules": len(rules), "checked": checked, "queued": queued}


def hourly():
	"""What hooks.py calls. Never raises: a scheduled job that throws is
	retried and can turn one bad configuration into a queue full of tracebacks.
	"""
	try:
		return run()
	except Exception:
		frappe.logger(LOGGER).error("stale reminders: pass failed", exc_info=True)
		return {"ok": False, "ran": False, "reason": "see the app_apis error log"}


# --------------------------------------------------------------------------
# Public surface -- whitelisted, safe to call from a Client Script
# --------------------------------------------------------------------------


@frappe.whitelist()
def check_now() -> dict:
	"""Manual trigger for the button on the settings form.

	Ignores the hour gate; still obeys the enabled switch and every per-ticket
	check `send_one` makes -- see `run`.
	"""
	frappe.only_for("System Manager")
	return run(force=True)
