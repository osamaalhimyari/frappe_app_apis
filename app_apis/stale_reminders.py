# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Tell each technician, once a day, how many of their tickets have gone quiet.

Different trigger from auto_messages.py: that one fires off a save, this one
fires off the clock. Once an hour the scheduler calls `hourly`, which asks
`due_now` whether this is the configured hour of the day (the same trick
subscription_reminders.py uses, so the schedule is a setting an operator can
change from the desk instead of a cron string that needs a deploy). If it is,
every enabled row of the `app_apis` Stale Ticket Rules table is counted against
the tickets currently sitting in that row's status, untouched (their `modified`
timestamp) for at least that row's hours, grouped by the engineer they are
assigned to.

ONE MESSAGE PER TECHNICIAN PER ROW, NOT PER TICKET
--------------------------------------------------
The first version sent a row's Message once for every stuck ticket: 487
messages on the first run on this site, 181 of them to one engineer. Now each
row's Message goes once to each technician who has anything stuck in that row's
status, with {count} saying how many ("⏸️ On hold for a while: 70"). They open
their tickets and check them themselves. Everything is configured on the table
-- Message, WhatsApp Template, Template Variables and Send Once per row; there
is no other setting.

SEND ONCE
---------
Per row. Ticked: a technician is not sent the same row again while their count
for it is unchanged -- the same "70 on hold" every morning is noise people learn
to mute. It goes again as soon as the number moves, either way. Unticked: every
day. Never twice in one day either way, so a manual "Check now" after the
scheduled run does not message anybody twice. Both are read off App Apis Message
Log, whose Trigger Source records the status and the count of every message.

WHICH STATUS A TICKET IS IN
---------------------------
`workflow_state` when the ticket has one, `status` only when it does not. This
site's `status` column still says "Open" on ~23,500 tickets the workflow closed
long ago; matching either field (the first version did) queued 23,552 jobs a
day for the Open rule alone.

Sending happens in a background job per technician and row on the `long`
queue, so the scheduler tick -- or the "Check now" button -- never sits on
outbound HTTP calls to Chatwoot.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, getdate, now_datetime, today

LOGGER = "app_apis"
TEMPLATE = "stale_reminder"
TICKET_DOCTYPE = "xticket"

# A ticket's status as this module reads it -- see WHICH STATUS A TICKET IS IN.
EFFECTIVE_STATE = "coalesce(nullif(workflow_state, ''), status)"


def _settings():
	"""The settings Single, from cache. Cheap enough for an hourly tick."""
	return frappe.get_cached_doc("app_apis")


def _rules(settings) -> list[dict]:
	"""Enabled, fully-filled-in rows, in table order, as plain dicts.

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
			"whatsapp_template": str(row.get("whatsapp_template") or "").strip(),
			"template_variables": str(row.get("template_variables") or ""),
		})
	return rows


def due_now(settings, when=None) -> tuple[bool, str]:
	"""Is this the hour the operator asked for? See subscription_reminders.due_now."""
	when = when or now_datetime()
	hour = cint(settings.get("stale_ticket_reminder_hour"))
	if when.hour != hour:
		return False, f"not the configured hour (now {when.hour:02d}:00, want {hour:02d}:00)"
	return True, ""


# --------------------------------------------------------------------------
# Finding the work
# --------------------------------------------------------------------------


def counts(rules: list[dict]) -> dict:
	"""{assigned user: {row position: stuck tickets}}. The "" key is nobody assigned.

	One grouped COUNT per row, so the scan costs the same however many tickets
	are stuck.
	"""
	out = {}
	now = now_datetime()
	for position, rule in enumerate(rules):
		cutoff = add_to_date(now, hours=-rule["hours"])
		rows = frappe.db.sql(
			f"""select ifnull(assigned_to, '') as user, count(*) as n
			    from `tab{TICKET_DOCTYPE}`
			    where {EFFECTIVE_STATE} = %s and modified < %s
			    group by ifnull(assigned_to, '')""",
			(rule["state"], cutoff),
			as_dict=True,
		)
		for row in rows:
			out.setdefault(row.user, {})[position] = cint(row.n)
	return out


def _engineer(user: str) -> dict:
	"""Name and number for an assigned user, through the same lookup the ticket
	messages use (User.mobile_no, then Employee.cell_number)."""
	from app_apis import technicians

	found = technicians.engineer(frappe._dict({technicians.USER_FIELD: user}))
	return {
		"user": user,
		"name": frappe.db.get_value("User", user, "full_name") or user,
		"phone": found["phone"],
		"source": found["source"],
	}


def context(engineer: dict, rule: dict, count: int) -> dict:
	"""Placeholder values for one row's message to one technician."""
	return {
		"engineer": engineer["name"],
		"count": str(count),
		"state": rule["state"],
		"hours": str(rule["hours"]),
	}


def message_for(rule: dict, ctx: dict) -> str:
	from app_apis import chatwoot_connector as cw

	return cw._render(rule["message"], ctx)


def _trigger(rule: dict, count: int) -> str:
	"""What the log's Trigger Source says -- and what Send Once compares."""
	return f"{rule['state']} · {count} stuck ≥{rule['hours']}h"


def _last_sent(phone: str, rule: dict):
	"""The last message of this row that reached this number, or None."""
	rows = frappe.db.sql(
		"""select trigger_source, sent_on from `tabApp Apis Message Log`
		   where template = %s and phone = %s and status = 'Sent' and trigger_source like %s
		   order by sent_on desc limit 1""",
		(TEMPLATE, phone, f"{rule['state']} · %"),
		as_dict=True,
	)
	return rows[0] if rows else None


def _skip_reason(engineer: dict, rule: dict, count: int) -> str:
	"""Why this message would not go out now, or "" if it would."""
	if not engineer["phone"]:
		return _("No phone number on file for {0}. Add one to their User or Employee record.").format(engineer["name"])
	last = _last_sent(engineer["phone"], rule)
	if last:
		if getdate(last.sent_on) == getdate(today()):
			return _("Already sent today.")
		if rule["send_once"] and last.trigger_source == _trigger(rule, count):
			return _("Same count as the last message; Send Once is on.")
	return ""


def plan(settings=None) -> dict:
	"""Every (technician, row) message this run would produce, with the exact
	text and whether it would go. Writes nothing, so the desk preview and the
	live run agree."""
	settings = settings or _settings()
	rules = _rules(settings)
	per_user = counts(rules) if rules else {}
	unassigned = sum(per_user.pop("", {}).values())

	from app_apis import technicians

	entries = []
	for user, per_row in per_user.items():
		engineer = _engineer(user)
		# On the Excluded Technicians table for scheduled reminders: counted and
		# shown in the preview, never sent.
		excluded = technicians.excluded(user, technicians.EXCLUDE_SCHEDULED, settings)
		for position, count in sorted(per_row.items()):
			rule = rules[position]
			entries.append({
				**engineer,
				"position": position,
				"rule": rule,
				"count": count,
				"message": message_for(rule, context(engineer, rule, count)),
				"excluded": excluded,
				"skip": (_("{0} is on the Excluded Technicians table.").format(engineer["name"]) if excluded
				         else _skip_reason(engineer, rule, count)),
			})
	entries.sort(key=lambda e: (e["name"], e["position"]))
	return {"rules": rules, "entries": entries, "unassigned": unassigned}


def _log(engineer: dict, status: str, reason: str = "", message: str = "",
         result: dict | None = None, trigger: str = ""):
	"""Record one outcome. Guarded: a logging failure must not lose the send."""
	result = result or {}
	try:
		entry = frappe.new_doc("App Apis Message Log")
		entry.recipient = "Technician"
		entry.engineer = str(engineer.get("name") or "")[:140]
		entry.template = TEMPLATE
		if result.get("via") == "template":
			entry.sent_as = f"WhatsApp template: {result.get('whatsapp_template') or ''}"
		elif result.get("via") == "text":
			entry.sent_as = "Text"
		entry.status = status
		entry.trigger_source = trigger[:140]
		entry.reason = reason
		entry.phone = engineer.get("phone") or ""
		entry.code = cint(result.get("code"))
		entry.message = result.get("message") or message
		entry.conversation_id = str(result.get("conversation_id") or "")
		entry.message_id = str(result.get("message_id") or "")
		entry.sent_on = now_datetime() if status == "Sent" else None
		entry.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"stale-reminder log failed: {engineer.get('user')}")


# --------------------------------------------------------------------------
# The worker. Runs in its own process, seconds after the scan.
# --------------------------------------------------------------------------


def send_one(user: str, rule: dict, count: int):
	"""Background entry point. Never raises -- there is nobody to raise to."""
	try:
		_send_one(user, rule, count)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"stale-reminder send failed: {user}")


def _send_one(user: str, rule: dict, count: int):
	from app_apis import chatwoot_connector as cw

	settings = _settings()

	# Re-checked here as well as at scan time: either switch can be turned off
	# in the time between the scan and the worker actually running.
	if not cint(settings.get("stale_ticket_reminder_enabled")) or not cint(settings.get("technician_message_enabled")):
		return

	count = cint(count)
	if count <= 0:
		return

	from app_apis import technicians

	# Re-checked here too: the table can change between the scan and the worker.
	if technicians.excluded(user, technicians.EXCLUDE_SCHEDULED, settings):
		return

	engineer = _engineer(user)
	ctx = context(engineer, rule, count)
	body = message_for(rule, ctx)
	trigger = _trigger(rule, count)

	reason = _skip_reason(engineer, rule, count)
	if reason:
		# A missing number is worth one log row per count, so the operator sees
		# who needs one; "already sent" and "unchanged" are the guard working,
		# and logging them every day would bury the rows that matter.
		if not engineer["phone"] and not frappe.db.exists(
			"App Apis Message Log",
			{"template": TEMPLATE, "engineer": engineer["name"][:140], "trigger_source": trigger, "status": "Skipped"},
		):
			_log(engineer, "Skipped", reason=reason, message=body, trigger=trigger)
		return

	# Outside WhatsApp's 24-hour window only a template is delivered (see
	# chatwoot_connector._route_window) -- and an engineer rarely writes to the
	# business number, so for this message a closed window is the usual case.
	fallback = None
	if cint(settings.get("chatwoot_use_templates")):
		fallback = {
			"template": rule.get("whatsapp_template") or "",
			"variables": rule.get("template_variables") or "",
			"ctx": ctx,
		}

	frappe.set_user("Administrator")
	# Confirmed, so a message WhatsApp refused is logged Failed, not Sent.
	result = cw._send(
		body,
		phone=engineer["phone"],
		name=engineer["name"],
		context={"template": TEMPLATE, "recipient": "technician", "engineer": engineer["name"]},
		template_fallback=fallback,
		confirm=True,
	) or {}

	reason = "" if result.get("ok") else str(result.get("msg") or "")[:500]
	if result.get("ok"):
		status = "Sent"
	elif result.get("code") in (-404, -409):
		# No usable number, or the window is closed and there is no template:
		# nothing was attempted, and nothing is broken.
		status = "Skipped"
		if result.get("code") == -409 and not rule.get("whatsapp_template"):
			reason = _("WhatsApp's 24-hour window is closed for this number and this row has no WhatsApp "
			           "Template, so nothing was sent.")
	else:
		status = "Failed"

	_log(engineer, status, reason=reason, message=body, result=result, trigger=trigger)


# --------------------------------------------------------------------------
# The scan. Runs inline, in the scheduler's own request -- a handful of COUNTs;
# the slow part is queued away above.
# --------------------------------------------------------------------------


def run(force: bool = False) -> dict:
	"""One pass: count every row, queue one message per technician per row.

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

	p = plan(settings)
	if not p["rules"]:
		return {"ok": True, "ran": True, "checked": 0, "queued": 0, "reason": "No enabled rules."}

	queued = 0
	for entry in p["entries"]:
		if entry["skip"]:
			continue
		frappe.enqueue(
			"app_apis.stale_reminders.send_one",
			queue="long",
			timeout=300,
			enqueue_after_commit=True,
			job_id=f"stale-reminder::{entry['user']}::{entry['position']}",
			deduplicate=True,
			user=entry["user"],
			rule=entry["rule"],
			count=entry["count"],
		)
		queued += 1

	# Technicians with no number still get their one Skipped row each, from the
	# worker -- queued here so the scan itself stays read-only.
	for entry in p["entries"]:
		if entry["skip"] and not entry["phone"] and not entry["excluded"]:
			frappe.enqueue(
				"app_apis.stale_reminders.send_one",
				queue="long",
				timeout=120,
				enqueue_after_commit=True,
				job_id=f"stale-reminder::{entry['user']}::{entry['position']}",
				deduplicate=True,
				user=entry["user"],
				rule=entry["rule"],
				count=entry["count"],
			)

	frappe.db.commit()
	return {
		"ok": True,
		"ran": True,
		"rules": len(p["rules"]),
		"checked": sum(e["count"] for e in p["entries"]),
		"technicians": len({e["user"] for e in p["entries"]}),
		"queued": queued,
		"unassigned": p["unassigned"],
	}


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

	Ignores the hour gate; still obeys the enabled switch, Send Once and the
	once-a-day guard -- see `run`.
	"""
	frappe.only_for("System Manager")
	return run(force=True)


@frappe.whitelist()
def preview(limit: int = 60) -> dict:
	"""What the next run would send, to whom, without sending any of it.

	System Manager only: it lists staff names and phone numbers.
	"""
	frappe.only_for("System Manager")

	settings = _settings()
	p = plan(settings)
	due, why = due_now(settings)
	going = [e for e in p["entries"] if not e["skip"]]
	return {
		"ok": True,
		"enabled": bool(cint(settings.get("stale_ticket_reminder_enabled"))),
		"due_now": due,
		"not_due_reason": why,
		"messages": len(p["entries"]),
		"would_send": len(going),
		"technicians": len({e["user"] for e in p["entries"]}),
		"stuck_tickets": sum(e["count"] for e in p["entries"]),
		"unassigned": p["unassigned"],
		"rows": [
			{
				"user": e["user"],
				"name": e["name"],
				"phone": e["phone"] or "",
				"state": e["rule"]["state"],
				"count": e["count"],
				"skip": e["skip"],
				"message": e["message"],
			}
			for e in p["entries"][: cint(limit) or 60]
		],
	}
