# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Warn a customer their tracking subscription is about to run out, on a schedule.

Reads `Customer Vehicle`, finds the ones whose `subscription_expiry_date` is
inside the reminder window, and messages the customer through the same Chatwoot
connector the ticket messages use. Nothing outside this app is touched: no field
is added to `Customer Vehicle`, no core doctype is edited, and the only state
written is this app's own `App Apis Reminder Log`.

BEFORE, NOT AFTER
-----------------
The window opens `subscription_reminder_days_before` days ahead of the expiry
date -- 50 by default -- so the customer hears about it while they can still
renew without losing tracking. It stays open for `subscription_reminder_days_after`
days past the date, because a lapse that nobody acted on is still worth one more
nudge.

That is why there are TWO messages and not one. A reminder sent 50 days early
that says "انتهى اشتراكك" is simply false, and a customer who checks their
dashboard and finds it working will learn to ignore the next one. So:

    Expiring soon    -- "it runs out soon, renew and lose nothing"
    Already expired  -- "it has run out, let's get you back on"

Both live in the Reminder Messages table on the settings form, one row per
kind, each with its own Send tick. A kind that is not ticked is never
mentioned at all: untick "Already expired" and a customer's expired vehicles
drop out of the plan before anything is counted or listed.

Which one a customer gets is decided by their MOST URGENT vehicle among the
kinds being sent: if the soonest date has already passed, they hear the
expired wording, because that is the one that needs acting on today.

ONE MESSAGE PER CUSTOMER, NOT PER VEHICLE
-----------------------------------------
This site has 9,053 already-expired vehicles and 966 customers holding them, so
a message per vehicle would mean forty WhatsApp bubbles in a row for a haulage
firm. The scan groups by customer and sends one message listing their plates,
soonest first. That is also the message a person can act on: "these eleven need
renewing" is a decision, "this one needs renewing", forty times, is a mess to
reconcile.

WHAT STOPS IT MESSAGING SOMEBODY TWICE
--------------------------------------
Every attempt writes a row to `App Apis Reminder Log`, and a customer with a
Sent row inside `subscription_reminder_repeat_days` (7 by default) is skipped.
The window is per CUSTOMER, not per vehicle: a fleet whose trucks expire on
different days must not turn into a daily drip.

Note what a 50-day lead plus a 7-day repeat window means in practice -- a
customer who does not renew hears from us about once a week for those 50 days.
Widen `subscription_reminder_repeat_days` if that is too eager; it is the one
number that controls how insistent this feature is.

Failed and Skipped rows deliberately do NOT hold the next attempt off. A send
that failed did not reach anybody, and a week of silence is not the right answer
to a temporary outage.

THE THREE GUARDS, AND WHY THERE ARE THREE
-----------------------------------------
Outbound messaging to hundreds of real customers is not something to switch on
by accident, so getting there is deliberately three deliberate acts:

    subscription_reminder_enabled   OFF  -- the scheduler does nothing at all
    subscription_reminder_dry_run   ON   -- it scans and logs, and sends nothing
    subscription_reminder_batch_size 50  -- a live run is capped, per pass

Turn on `enabled` first and read the log: every row says exactly who would have
been written to, on what number, with the full message body. Untick `dry_run`
only once that list looks right.

`subscription_reminder_days_after` matters as much as the switches. 1,490 of
these subscriptions died more than a year ago; without an upper bound the first
live run wakes up customers who left in 2023.

THE 24-HOUR WINDOW
------------------
WhatsApp only delivers free text to somebody who wrote to the business in the
last 24 hours -- almost nobody, for a renewal reminder. With Use WhatsApp
Templates on, the row's WhatsApp Template goes instead whenever that window is
closed (see chatwoot_connector._route_window), and a row without one is logged
Skipped rather than posted to fail. Every live send then waits for WhatsApp's
answer, so a Sent row means WhatsApp took the message, not just Chatwoot.
"""

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime, today

from app_apis import chatwoot_connector as cw
from app_apis import do_not_contact
from app_apis.contacts import BILINGUAL, format_contacts
from app_apis.customers import csv_types, type_allowed
from app_apis.phone import normalise

LOGGER = "app_apis"

# The vehicle master, and the columns read off it. Named here because they
# belong to the site rather than to this app -- one place to edit if a column is
# ever renamed, and nothing in this module reaches for a field that is not on
# this list.
VEHICLE_DOCTYPE = "Customer Vehicle"
VEHICLE_FIELDS = (
	"name", "customer", "license_plate", "e_license_plate", "plate_num",
	"driver_mobile", "subscription_expiry_date", "device_statues", "paying",
)

# Only a device that is actually fitted and still on the car is worth chasing.
# The other three statuses are 5,195 rows of history: a `Deleted` or `Canceled
# Installation` subscription has not lapsed, it ended, and messaging about it
# reads as a bill for something the customer already cancelled.
LIVE_STATUS = "Installed"

# The two things a reminder can be about. Also the values written to the log's
# `reminder_type`, so a row says which wording went out without anybody having
# to work it back out from the dates.
EXPIRING = "Expiring soon"
EXPIRED = "Expired"

# The same two kinds as the Reminder Messages table spells them.
KIND_LABEL = {EXPIRING: "Expiring soon", EXPIRED: "Already expired"}

# How many plates a message lists before it stops and says "and N more". The
# largest fleet here holds 146; a WhatsApp bubble with 146 lines in it is not a
# reminder, it is a denial of service on somebody's phone.
MAX_PLATES_LISTED = 8

# What the shipped messages say. Same shape as the ticket templates in
# chatwoot_connector: a greeting, the facts once under emoji that need no
# translating, one Arabic line and one English line, a sign-off. Kept in step
# with the `default` on each field -- the field is what an operator edits, this
# is what a site that never touched it sends.
DEFAULT_EXPIRING_MESSAGE = (
	"👋 أهلاً {customer}\n\n"
	"⏰ اشتراك التتبع للمركبات التالية ({count}) قارب على الانتهاء:\n"
	"⏰ Tracking for these ({count}) is about to run out:\n"
	"{vehicles}\n\n"
	"🔄 جدّده من الحين وما ينقطع عنك التتبع ولا يوم 🚗💨\n"
	"🔄 Renew now and you won't lose a single day of tracking 🚗💨\n\n"
	"تواصل معنا وإحنا في خدمتك / Reach out any time, we're here to help 🤝\n"
	"{contacts}"
)

DEFAULT_EXPIRED_MESSAGE = (
	"👋 أهلاً {customer}\n\n"
	"⏰ انتهى اشتراك التتبع للمركبات التالية ({count}):\n"
	"⏰ Tracking has expired for these ({count}):\n"
	"{vehicles}\n\n"
	"🔄 نجدده لك بخطوتين بس، وترجع تتابع مركباتك على طول 🚗💨\n"
	"🔄 A quick renewal and you're back to tracking — two steps 🚗💨\n\n"
	"تواصل معنا وإحنا في خدمتك / Reach out any time, we're here to help 🤝\n"
	"{contacts}"
)

# The two retired settings fields that held the wording before the Reminder
# Messages table. Nothing here reads them any more; they stay because the older
# patches seed_subscription_reminders and warm_message_wording import them, and
# a site that has not run those yet must still be able to migrate.
MESSAGE_FIELD = {
	EXPIRING: "subscription_expiring_message",
	EXPIRED: "subscription_reminder_message",
}
MESSAGE_FALLBACK = {
	EXPIRING: DEFAULT_EXPIRING_MESSAGE,
	EXPIRED: DEFAULT_EXPIRED_MESSAGE,
}

# Settings fieldnames, with the value used when the field has never been filled.
# A Single that predates a field reads it as None, and every default here is the
# cautious end of its range for that reason.
DEFAULTS = {
	"subscription_reminder_enabled": 0,
	"subscription_reminder_dry_run": 1,
	"subscription_reminder_hour": 9,
	"subscription_reminder_weekday": "Every day",
	"subscription_reminder_days_before": 50,
	"subscription_reminder_days_after": 90,
	"subscription_reminder_repeat_days": 7,
	"subscription_reminder_batch_size": 50,
	"subscription_reminder_customer_types": "",
	"subscription_reminder_phone_source": "Customer, then vehicle",
}

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def settings() -> dict:
	"""The reminder settings, every key present and typed."""
	s = frappe.get_cached_doc("app_apis")
	out = {}
	for field, fallback in DEFAULTS.items():
		value = s.get(field)
		if isinstance(fallback, int):
			# cint("") is 0, which is wrong for a field that has never been
			# saved: an unset batch size means "the shipped 50", not "send to
			# nobody" -- and an unset repeat window must not mean "no window".
			out[field] = cint(value) if str(value or "").strip() != "" else fallback
		else:
			out[field] = str(value or "").strip() or fallback
	out["doc"] = s
	return out


def message_rows(cfg: dict) -> dict:
	"""{EXPIRING/EXPIRED: row} for every kind the Reminder Messages table sends.

	A kind with no ticked row is a kind nobody is messaged about -- unticking
	"Already expired" is how "warn before, never chase after" is spelled. The
	first ticked row of a kind wins.
	"""
	kind_of = {label: kind for kind, label in KIND_LABEL.items()}
	rows = {}
	for row in cfg["doc"].get("subscription_messages") or []:
		kind = kind_of.get(str(row.get("kind") or "").strip())
		message = str(row.get("message") or "").strip()
		if kind and kind not in rows and cint(row.get("enabled")) and message:
			rows[kind] = {
				"message": message,
				"whatsapp_template": str(row.get("whatsapp_template") or "").strip(),
				"template_variables": str(row.get("template_variables") or ""),
			}
	return rows


def due_now(cfg: dict, when=None) -> tuple[bool, str]:
	"""Is this the hour the operator asked for? Returns (yes, why not).

	The scheduler ticks hourly and this decides whether the tick is the one that
	matters, which keeps the schedule a setting an operator can change from the
	desk rather than a cron string in hooks.py that needs a deploy.
	"""
	when = when or now_datetime()

	hour = cint(cfg["subscription_reminder_hour"])
	if when.hour != hour:
		return False, f"not the configured hour (now {when.hour:02d}:00, want {hour:02d}:00)"

	wanted = cfg["subscription_reminder_weekday"]
	if wanted and wanted != "Every day":
		day = WEEKDAYS[when.weekday()]
		if day != wanted:
			return False, f"not the configured day (today {day}, want {wanted})"

	return True, ""


# --------------------------------------------------------------------------
# Finding the work
# --------------------------------------------------------------------------


def vehicles_in_window(cfg: dict) -> list[dict]:
	"""Every live vehicle whose subscription expires inside the reminder window.

	The window runs from `days_before` in the FUTURE back to `days_after` in the
	past, so one query covers both the warning and the chase:

	    today + 50                today                 today - 90
	         |------------------------|---------------------|
	           expires soon                already expired

	Bounded at both ends on purpose. The forward bound is the whole point of the
	feature -- a customer who hears about it 50 days out can renew without
	losing a day of tracking. The backward bound is what stops the first live run
	messaging the 1,490 customers whose subscription died over a year ago and
	who are, by any reasonable reading, no longer customers.
	"""
	days_before = cint(cfg["subscription_reminder_days_before"])
	days_after = cint(cfg["subscription_reminder_days_after"])

	conditions = [
		"device_statues = %(status)s",
		"ifnull(subscription_expiry_date, '') != ''",
		"subscription_expiry_date <= date_add(curdate(), interval %(days_before)s day)",
	]
	# 0 is the honest way to say "no bound", and it has to be spellable: an
	# operator who genuinely wants every lapsed subscription should not have to
	# type 99999. Note it means something different on each side -- no forward
	# bound is "only chase what has already expired".
	if days_after > 0:
		conditions.append(
			"subscription_expiry_date >= date_sub(curdate(), interval %(days_after)s day)"
		)

	return frappe.db.sql(
		"""select {fields} from `tab{dt}`
		   where {where}
		   order by customer, subscription_expiry_date""".format(
			fields=", ".join("`%s`" % f for f in VEHICLE_FIELDS),
			dt=VEHICLE_DOCTYPE,
			where=" and ".join(conditions),
		),
		{"status": LIVE_STATUS, "days_before": days_before, "days_after": days_after},
		as_dict=True,
	)


def _plate(vehicle: dict) -> str:
	"""The plate to print. Arabic first -- it is the one stamped on the metal."""
	for field in ("license_plate", "e_license_plate", "plate_num"):
		value = str(vehicle.get(field) or "").strip()
		if value:
			return value
	return str(vehicle.get("name") or "")


def _days_to_expiry(vehicle: dict) -> int:
	"""Positive = days still to run. Negative = days since it lapsed."""
	return (getdate(vehicle["subscription_expiry_date"]) - getdate(today())).days


def _recipient_phone(customer: str, vehicles: list[dict], cfg: dict) -> tuple[str, str]:
	"""Where this customer's reminder should go. Returns (phone, source).

	A subscription is a billing matter, so the Customer's own number leads. The
	per-vehicle `driver_mobile` is a fallback and a configurable one: it is the
	driver of that particular truck, and a haulage firm would not thank anybody
	for sending a renewal notice to whoever happens to be behind the wheel.

	Both are run through app_apis.phone.normalise, which is the same rule the
	rest of the app uses -- most of these numbers are stored as a bare "+966" or
	as a local "507320980", and neither is deliverable as written.
	"""
	source = cfg["subscription_reminder_phone_source"]

	# Numbers whose owner has asked to be left alone. Skipped and stepped over
	# rather than failing the customer: the reminder often lands on a DRIVER's
	# number, and one driver opting out should not cost their employer the
	# renewal notice for a fleet of forty.
	blocked = do_not_contact.blocked_phones(do_not_contact.REMINDERS)

	if source != "Vehicle only":
		phone = normalise(frappe.db.get_value("Customer", customer, "mobile_no"))
		if phone and phone not in blocked:
			return phone, "Customer.mobile_no"
		if source == "Customer only":
			return "", ""

	# First usable driver number, in the order the vehicles came back -- which
	# is by expiry date, so it is the number attached to the most urgent one.
	for vehicle in vehicles:
		phone = normalise(vehicle.get("driver_mobile"))
		if phone and phone not in blocked:
			return phone, "%s.driver_mobile" % _plate(vehicle)

	return "", ""


def recently_messaged(customer: str, days: int) -> str:
	"""When this customer was last successfully messaged, or "".

	Reads Sent rows only. A Failed row means nobody was reached and a Skipped
	row means nothing was attempted; holding the next run off for a week on
	either would turn one bad afternoon into a silent week.

	Deliberately blind to WHICH of the two messages was sent. A customer who was
	warned on Monday that their subscription runs out should not get an "it has
	expired" message on Tuesday just because the date rolled over -- from their
	side that is the same subject twice in two days.
	"""
	if days <= 0:
		return ""

	row = frappe.db.sql(
		"""select sent_on from `tabApp Apis Reminder Log`
		   where customer = %(customer)s and status = 'Sent'
		     and sent_on >= date_sub(now(), interval %(days)s day)
		   order by sent_on desc limit 1""",
		{"customer": customer, "days": days},
	)
	return str(row[0][0]) if row else ""


def plan(cfg: dict | None = None) -> list[dict]:
	"""Who would be messaged this run, and why anybody would not be.

	Returns one entry per customer, `send` True or False with a `reason`. The
	whole decision is made here and nothing is written, so the desk preview and
	the live run are guaranteed to agree about what is about to happen.
	"""
	cfg = cfg or settings()

	# A vehicle whose kind is not ticked never enters the plan -- not in the
	# count, not in the plate list, not as the "most urgent" one -- so with only
	# Expiring soon ticked, a fleet with one truck already dark and ten
	# expiring next month hears about the ten and nothing else.
	sending = message_rows(cfg)
	vehicles = [
		v for v in vehicles_in_window(cfg)
		if (EXPIRED if _days_to_expiry(v) < 0 else EXPIRING) in sending
	]

	# Group first, decide second: the per-customer rules (phone, repeat window,
	# customer type) cannot be answered while still walking rows.
	grouped = {}
	for vehicle in vehicles:
		grouped.setdefault(vehicle["customer"], []).append(vehicle)

	allowed_types = csv_types(cfg["subscription_reminder_customer_types"])
	repeat_days = cint(cfg["subscription_reminder_repeat_days"])
	entries = []

	for customer, own in sorted(grouped.items()):
		# Soonest first, so the message leads with the most urgent vehicle and
		# {expiry}/{days} describe that one.
		own.sort(key=lambda v: getdate(v["subscription_expiry_date"]))
		soonest = own[0]
		days_left = _days_to_expiry(soonest)

		entry = {
			"customer": customer,
			"customer_name": frappe.db.get_value("Customer", customer, "customer_name") or customer,
			"vehicles": own,
			"count": len(own),
			"plates": [_plate(v) for v in own],
			"expiry": str(getdate(soonest["subscription_expiry_date"])),
			"days_to_expiry": days_left,
			# Decided by the most urgent vehicle, not by the majority: a fleet
			# with one truck already dark and ten expiring next month needs to
			# hear about the dark one today.
			"kind": EXPIRED if days_left < 0 else EXPIRING,
			"send": False,
			"reason": "",
			"phone": "",
			"phone_source": "",
		}

		# First question, before customer type and before the repeat window:
		# somebody who asked to be left alone is left alone, and no other
		# setting on this form gets a vote.
		asked = do_not_contact.check(customer=customer, scope=do_not_contact.REMINDERS)
		if asked["blocked"]:
			entry["reason"] = asked["reason"]
			entries.append(entry)
			continue

		if not type_allowed(customer, allowed_types):
			entry["reason"] = _("Customer type is not in the reminder list.")
			entries.append(entry)
			continue

		last = recently_messaged(customer, repeat_days)
		if last:
			entry["reason"] = _("Already reminded on {0}; the repeat window is {1} days.").format(
				last[:16], repeat_days
			)
			entries.append(entry)
			continue

		phone, source = _recipient_phone(customer, own, cfg)
		if not phone:
			entry["reason"] = _("No usable phone number on the Customer or their vehicles.")
			entries.append(entry)
			continue

		entry["phone"] = phone
		entry["phone_source"] = source
		entry["send"] = True
		entries.append(entry)

	# Due first, and among those the most urgent first -- already-expired before
	# expiring-soon, oldest lapse before newest. A run capped by batch_size
	# should spend its budget on the customers who need it most, not on whoever
	# sorts first alphabetically.
	entries.sort(key=lambda e: (not e["send"], e["days_to_expiry"], e["customer"]))
	return entries


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _vehicle_lines(entry: dict) -> str:
	"""The plate block: one line each, then "and N more" if it was capped."""
	lines = []
	for vehicle in entry["vehicles"][:MAX_PLATES_LISTED]:
		lines.append("🚗 %s — %s" % (_plate(vehicle), getdate(vehicle["subscription_expiry_date"])))

	remaining = entry["count"] - len(lines)
	if remaining > 0:
		# The count appears once, with both words after it: "➕ 138 أخرى / more".
		# Writing the number twice is how a capped list reads like a bug.
		lines.append("➕ %d أخرى / more" % remaining)

	return "\n".join(lines)


def context(entry: dict) -> dict:
	"""Placeholder values. Flat and string-only, like the ticket templates:
	these are pasted into a message, and a None arriving as "None" is the
	failure worth designing against."""
	days = entry["days_to_expiry"]
	return {
		"customer": entry["customer_name"],
		"count": str(entry["count"]),
		"vehicles": _vehicle_lines(entry),
		"plate": entry["plates"][0] if entry["plates"] else "",
		"expiry": entry["expiry"],
		# Always a positive number of days: the template wording already says
		# which side of the date it is on, and "expired -14 days ago" is not a
		# sentence anybody wants to send a customer.
		"days": str(abs(days)),
		"company": frappe.defaults.get_user_default("Company") or "",
		"contacts": format_contacts(BILINGUAL),
	}


def message_for(entry: dict, cfg: dict | None = None) -> str:
	"""The exact text this customer would receive, for their kind of reminder."""
	cfg = cfg or settings()
	row = message_rows(cfg).get(entry.get("kind") or EXPIRED)
	# Reuses the connector's renderer, so a whole-line placeholder that comes
	# out empty drops its line here exactly as it does in a ticket message.
	return cw._render(row["message"], context(entry)) if row else ""


# --------------------------------------------------------------------------
# Doing the work
# --------------------------------------------------------------------------


def _log(entry: dict, status: str, cfg: dict, reason: str = "",
         message: str = "", result: dict | None = None):
	"""Record one outcome. Guarded: a logging failure must not lose a send.

	Every row carries the full body and the plate list, because the dry run is
	the review step -- an operator deciding whether to go live is reading these
	rows, and a row that does not say what would have been sent is no use to
	them.
	"""
	result = result or {}
	try:
		row = frappe.new_doc("App Apis Reminder Log")
		row.customer = entry["customer"]
		row.customer_name = entry["customer_name"]
		row.phone = entry.get("phone") or ""
		row.phone_source = entry.get("phone_source") or ""
		row.status = status
		row.reminder_type = entry.get("kind") or ""
		if result.get("via") == "template":
			row.sent_as = f"WhatsApp template: {result.get('whatsapp_template') or ''}"
		elif result.get("via") == "text":
			row.sent_as = "Text"
		row.dry_run = cint(cfg["subscription_reminder_dry_run"])
		row.vehicle_count = entry["count"]
		row.plates = ", ".join(entry["plates"][:50])
		row.expiry = entry["expiry"] or None
		row.days_to_expiry = entry["days_to_expiry"]
		row.reason = reason
		row.message = message
		row.code = cint(result.get("code"))
		row.conversation_id = str(result.get("conversation_id") or "")
		row.message_id = str(result.get("message_id") or "")
		# Only a real, delivered-to-Chatwoot send stamps this: `recently_messaged`
		# reads it, so a dry run stamping it would make the next live run think
		# everybody had already been told.
		row.sent_on = now_datetime() if status == "Sent" else None
		row.insert(ignore_permissions=True)
	except Exception:
		frappe.logger(LOGGER).error(
			"subscription reminder: could not log %s for %s" % (status, entry.get("customer")),
			exc_info=True,
		)


def run(force: bool = False, limit: int | None = None) -> dict:
	"""One pass: scan, decide, send, log. The scheduler's entry point.

	`force` skips the hour/weekday gate for a manual run from the desk; it does
	NOT skip the enabled switch, the dry-run switch, or the repeat window. Those
	three are the safety of this feature, and a convenience argument must not be
	able to turn them off.
	"""
	cfg = settings()

	if not cint(cfg["subscription_reminder_enabled"]):
		return {"ok": True, "ran": False, "reason": "Subscription reminders are switched off."}

	if not force:
		due, why = due_now(cfg)
		if not due:
			return {"ok": True, "ran": False, "reason": why}

	rows = message_rows(cfg)
	if not rows:
		return {"ok": True, "ran": False, "reason": "No ticked row in Reminder Messages -- nothing to send."}
	use_templates = cint(cfg["doc"].get("chatwoot_use_templates"))

	ready, why = cw._ready(cw.active_settings())
	dry_run = cint(cfg["subscription_reminder_dry_run"])
	if not ready and not dry_run:
		# A dry run still has something useful to say with Chatwoot down, so it
		# is allowed to proceed; a live run has nothing to send through.
		return {"ok": False, "ran": False, "reason": why}

	entries = plan(cfg)
	due_entries = [e for e in entries if e["send"]]
	batch = cint(limit) if limit else cint(cfg["subscription_reminder_batch_size"])
	if batch > 0:
		held_back = max(0, len(due_entries) - batch)
		due_entries = due_entries[:batch]
	else:
		held_back = 0

	sent = failed = no_template = 0
	for entry in due_entries:
		row = rows[entry["kind"]]
		ctx = context(entry)
		body = cw._render(row["message"], ctx)

		if dry_run:
			note = _("Dry run: nothing was sent.")
			if use_templates and row["whatsapp_template"]:
				title = frappe.db.get_value("App Apis WhatsApp Template", row["whatsapp_template"], "title")
				note += " " + _("Outside the 24-hour window it would go as WhatsApp template {0}.").format(
					title or row["whatsapp_template"])
			_log(entry, "Dry run", cfg, reason=note, message=body)
			continue

		# Outside the 24-hour window only a template is delivered -- see the
		# module docstring. Confirmed either way, so Sent means WhatsApp took it.
		fallback = None
		if use_templates:
			fallback = {"template": row["whatsapp_template"], "variables": row["template_variables"], "ctx": ctx}
		result = cw._send(
			body,
			phone=entry["phone"],
			name=entry["customer_name"],
			context={"customer": entry["customer_name"], "template": "subscription_reminder"},
			template_fallback=fallback,
			confirm=True,
		) or {}

		if result.get("ok"):
			sent += 1
			_log(entry, "Sent", cfg, message=result.get("message") or body, result=result)
		elif result.get("code") == -409:
			# Window closed and no usable template: declined, nothing posted.
			no_template += 1
			_log(entry, "Skipped", cfg, reason=str(result.get("msg") or "")[:500], message=body, result=result)
		else:
			failed += 1
			_log(entry, "Failed", cfg, reason=str(result.get("msg") or "")[:500],
			     message=body, result=result)

	# Everything that was NOT going to be messaged is logged once too, so the
	# log answers "why did my customer not get this" without anybody having to
	# re-run the scan by hand. Capped: hundreds of customers with no phone number
	# would otherwise write hundreds of rows every single pass.
	skipped_logged = 0
	for entry in entries:
		if entry["send"] or skipped_logged >= 25:
			continue
		_log(entry, "Skipped", cfg, reason=entry["reason"])
		skipped_logged += 1

	frappe.db.commit()

	summary = {
		"ok": True,
		"ran": True,
		"dry_run": bool(dry_run),
		"customers_in_window": len(entries),
		"expiring_soon": len([e for e in entries if e["kind"] == EXPIRING]),
		"already_expired": len([e for e in entries if e["kind"] == EXPIRED]),
		"due": len([e for e in entries if e["send"]]),
		"attempted": len(due_entries),
		"sent": sent,
		"failed": failed,
		"skipped_no_template": no_template,
		"held_back_by_batch_size": held_back,
		"chatwoot_ready": ready,
	}
	frappe.logger(LOGGER).warning("subscription reminders: %s" % summary)
	return summary


def hourly():
	"""What hooks.py calls. Never raises: a scheduled job that throws is retried
	and can turn one bad configuration into a queue full of tracebacks."""
	try:
		return run()
	except Exception:
		frappe.logger(LOGGER).error("subscription reminders: pass failed", exc_info=True)
		return {"ok": False, "ran": False, "reason": "see the app_apis error log"}


# --------------------------------------------------------------------------
# Public surface -- whitelisted, safe to call from a Client Script
# --------------------------------------------------------------------------


@frappe.whitelist()
def preview(limit: int = 20) -> dict:
	"""What the next run would do, without doing any of it.

	System Manager only: this reports customer names and phone numbers across
	the whole book, which is a much broader read than any one ticket.
	"""
	frappe.only_for("System Manager")

	cfg = settings()
	entries = plan(cfg)
	due = [e for e in entries if e["send"]]
	ready, why = cw._ready(cw.active_settings())
	is_due, not_due_why = due_now(cfg)

	rows = []
	for entry in (due or entries)[:cint(limit) or 20]:
		rows.append({
			"customer": entry["customer"],
			"customer_name": entry["customer_name"],
			"phone": entry["phone"],
			"phone_source": entry["phone_source"],
			"kind": entry["kind"],
			"vehicles": entry["count"],
			"plates": entry["plates"][:MAX_PLATES_LISTED],
			"expiry": entry["expiry"],
			"days_to_expiry": entry["days_to_expiry"],
			"send": entry["send"],
			"reason": entry["reason"],
			"message": message_for(entry, cfg) if entry["send"] else "",
		})

	return {
		"ok": True,
		"enabled": bool(cint(cfg["subscription_reminder_enabled"])),
		"dry_run": bool(cint(cfg["subscription_reminder_dry_run"])),
		"kinds_sent": [KIND_LABEL[k] for k in message_rows(cfg)],
		"due_now": is_due,
		"not_due_reason": not_due_why,
		"chatwoot_ready": ready,
		"chatwoot_reason": why,
		"customers_in_window": len(entries),
		"expiring_soon": len([e for e in entries if e["kind"] == EXPIRING]),
		"already_expired": len([e for e in entries if e["kind"] == EXPIRED]),
		"due": len(due),
		"batch_size": cint(cfg["subscription_reminder_batch_size"]),
		"repeat_days": cint(cfg["subscription_reminder_repeat_days"]),
		"window": "from %s days before expiry to %s days after" % (
			cfg["subscription_reminder_days_before"],
			cfg["subscription_reminder_days_after"] or "no limit",
		),
		"rows": rows,
	}


@frappe.whitelist()
def run_now(limit: int | None = None) -> dict:
	"""Run a pass immediately, ignoring the hour and weekday.

	Still obeys `enabled`, `dry_run` and the repeat window -- see `run`.
	"""
	frappe.only_for("System Manager")
	return run(force=True, limit=cint(limit) if limit else None)
