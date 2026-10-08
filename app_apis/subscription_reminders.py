# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Warn a customer their tracking subscription is about to run out, on a schedule.

Reads `Customer Vehicle`, finds the ones whose `subscription_expiry_date` is
inside the reminder window, and messages the customer through the same Chatwoot
connector the ticket messages use. Nothing outside this app is touched: no field
is added to `Customer Vehicle`, no core doctype is edited, and the only state
written is this app's own `App Apis Reminder Log`.

HOW A DAY WORKS
---------------
The scheduler ticks hourly. From `subscription_reminder_hour` until
`subscription_reminder_end_hour` each tick is one ROUND: it sends at most
`subscription_reminder_per_round` messages, one after the other, and each one
waits for WhatsApp to say whether it took it -- so a Sent row means WhatsApp
accepted the message, not just Chatwoot. The next round is an hour later, which
is what keeps Meta from being handed the whole day's list at once.

    subscription_reminder_batch_size          the limit per DAY (0 = none)
    subscription_reminder_per_round           the limit per hourly ROUND
    subscription_reminder_max_per_customer    per CUSTOMER per day, so one big
                                              fleet cannot take the whole day
    subscription_reminder_stop_after_failures this many failures in a row end
                                              the round (Meta throttling,
                                              Chatwoot down ...)

ONE MESSAGE PER VEHICLE
-----------------------
A vehicle is the unit that expires, is paid for and is renewed, so it is the
unit that is reminded: one message, one log row, one plate. A customer with
forty vehicles is not sent forty messages in a minute -- the per-customer daily
cap spreads them over days -- and a vehicle that fails is simply tried again,
without a message for its neighbours being held hostage to it.

WHO IS NEXT
-----------
Installed devices only, subscription expiring inside the window ("Warn Before
Expiry" days ahead, "Keep Chasing After" days behind). Order: every vehicle
still running first, nearest expiry first (0 days left, 1, 2 ... up to the
lead time); then the expired ones, newest lapse first (-1, -2 ... down to the
chase limit). Someone who can still renew without losing a day of tracking is
worth reaching before someone who has been dark for a month.

There are TWO messages, not one, because "your subscription has expired" sent
50 days early is simply false:

    Expiring soon    -- "it runs out soon, renew and lose nothing"
    Already expired  -- "it has run out, let's get you back on"

Both live in the Reminder Messages table, one row per kind, each with its own
Send tick. A kind that is not ticked is never mentioned at all.

WHAT IS NOT SENT (counted in the round's note, never logged)
------------------------------------------------------------
customer types outside "Customer Types"; Excluded Customers / Do Not Contact;
a vehicle already reminded within "Don't Repeat Within" days; a vehicle whose
renewal is already paid (a Subscription Renewal waiting to be applied, or a
renewal invoice for this expiry); an "Installed" vehicle with a Deletion Date;
a customer already messaged "max per customer" times today; a vehicle that has
failed twice today (tried again tomorrow, so a dead number at the front of the
queue cannot starve everything behind it); and a vehicle with no Saudi mobile
number (+9665XXXXXXXX -- a number that cannot be one is not used and the next
one is tried).

WHICH NUMBER
An Individual is the driver, so the vehicle's driver_mobile is used first and
the Customer's mobile_no only if that is missing or unusable. A Company gets
the Customer's mobile_no only: its drivers are employees, never the person to
bill. It is a Company if EITHER the Customer Type is not Individual OR Paying
is "Company" (the vehicle's, else the customer's).

Those are counted rather than logged because they repeat every hour: logging
them would write the same few hundred rows a day.

THE LOG
-------
`App Apis Reminder Log` holds ONLY vehicles a message was attempted for: one
row per message, Sent / Failed / Dry run (or Skipped when WhatsApp's 24-hour
window was closed and the row has no template). It is load-bearing: the repeat
window, the daily limit and the per-customer cap are all read from it, so
deleting rows re-arms the reminder for everybody deleted.

TIME
----
Every date and time here is the app's (System Settings time zone), never the
database clock. The database runs on UTC, 3 hours behind Riyadh, so SQL's
`curdate()` and `now()` would move the window edge, the day boundary and the
repeat window by three hours.

THE 24-HOUR WINDOW
------------------
WhatsApp only delivers free text to somebody who wrote to the business in the
last 24 hours -- almost nobody, for a renewal reminder. So a row WITH a WhatsApp
Template is always sent as that template (its Template Variables fill the
{{1}}, {{2}} ...; an empty variable gets {plate}), and a row without one is
sent as text only while the window is open, otherwise logged Skipped rather
than posted to fail.
"""

import time

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, cint, date_diff, getdate, now_datetime, today
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from app_apis import chatwoot_connector as cw
from app_apis import do_not_contact
from app_apis.contacts import BILINGUAL, format_contacts
from app_apis.customers import csv_types
from app_apis.phone import saudi_mobile

LOGGER = "app_apis"
SETTINGS = "app_apis"
LOG = "App Apis Reminder Log"
TEMPLATE_DT = "App Apis WhatsApp Template"

# The vehicle master, and the columns read off it. Named here because they
# belong to the site rather than to this app -- one place to edit if a column is
# ever renamed, and nothing in this module reaches for a field that is not on
# this list.
VEHICLE_DOCTYPE = "Customer Vehicle"
VEHICLE_FIELDS = (
	"name", "customer", "license_plate", "e_license_plate", "plate_num",
	"driver_mobile", "subscription_expiry_date", "device_statues", "paying",
	"deletion_date",
)

# Only a device that is actually fitted and still on the car is worth chasing.
# The other three statuses are 5,195 rows of history: a `Deleted` or `Canceled
# Installation` subscription has not lapsed, it ended, and messaging about it
# reads as a bill for something the customer already cancelled.
LIVE_STATUS = "Installed"

# Items on a Sales Invoice that mean "this vehicle's subscription was renewed".
RENEWAL_ITEM_CODES = ("Subscription renewal", "subscription")

# The two things a reminder can be about. Also the values written to the log's
# `reminder_type`, so a row says which wording went out without anybody having
# to work it back out from the dates.
EXPIRING = "Expiring soon"
EXPIRED = "Expired"

# The same two kinds as the Reminder Messages table spells them.
KIND_LABEL = {EXPIRING: "Expiring soon", EXPIRED: "Already expired"}

# How many plates a message lists before it stops and says "and N more". A
# reminder is one vehicle now, so this only matters to a message written for
# the old per-customer list.
MAX_PLATES_LISTED = 8

# A vehicle that failed this many times today is passed over until tomorrow.
MAX_FAILED_TRIES_PER_DAY = 2

# A round must end well inside the "long" queue's 25-minute job limit.
ROUND_MAX_SECONDS = 20 * 60

LOCK_NAME = "app_apis_subscription_reminders"

# Run notes are also posted here, as a history next to the one in the settings.
NOTE_SCRIPT = "App Apis - Subscription Reminders"

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
	"subscription_reminder_end_hour": 21,
	"subscription_reminder_weekday": "Every day",
	"subscription_reminder_days_before": 50,
	"subscription_reminder_days_after": 90,
	"subscription_reminder_repeat_days": 7,
	"subscription_reminder_batch_size": 50,
	"subscription_reminder_per_round": 50,
	"subscription_reminder_max_per_customer": 6,
	"subscription_reminder_stop_after_failures": 3,
	"subscription_reminder_template_inbox": 0,
	"subscription_reminder_customer_types": "",
}

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def settings() -> dict:
	"""The reminder settings, every key present and typed."""
	s = frappe.get_cached_doc("app_apis")
	# Which fields have actually been saved. A Single keeps one row per saved
	# field, and that row is the only way to tell "never saved" from "saved as
	# 0": loading the document turns a missing Check into 0 as well.
	saved = frappe.db.get_singles_dict("app_apis")
	out = {}
	for field, fallback in DEFAULTS.items():
		value = s.get(field)
		if isinstance(fallback, int):
			# Only a field that was never saved, or saved empty, takes the
			# default: an unset batch size means "the shipped 50" and an unset
			# Dry Run means "dry run". A saved 0 is a choice and is kept --
			# unticked Dry Run, sending hour 00, batch size 0 (no cap). This used
			# to read `value or ""`, which turned every one of those back into
			# its default, so unticking Dry Run never let anything be sent.
			if field in saved and value is not None and str(value).strip() != "":
				out[field] = cint(value)
			else:
				out[field] = fallback
		else:
			out[field] = str(value or "").strip() or fallback
	out["doc"] = s
	return out


def _variables_text(raw) -> str:
	"""A Template Variables value with every empty variable filled with {plate}.

	"{{1}} = " is what an operator leaves when the template has one variable and
	it is the plate; Meta refuses an empty parameter, so it is filled in rather
	than failing every send.
	"""
	lines = []
	for line in str(raw or "").split("\n"):
		if "=" in line and not line.split("=", 1)[1].strip():
			line = line.split("=", 1)[0].rstrip() + " = {plate}"
		if line.strip():
			lines.append(line.strip())
	return "\n".join(lines) or "{{1}} = {plate}"


def message_rows(cfg: dict) -> dict:
	"""{EXPIRING/EXPIRED: row} for every kind the Reminder Messages table sends.

	A kind with no ticked row is a kind nobody is messaged about -- unticking
	"Already expired" is how "warn before, never chase after" is spelled. The
	first ticked row of a kind wins. A row needs a Message, or -- with Use
	WhatsApp Templates on -- a WhatsApp Template: a template-only row is the
	normal shape, because that is what is actually delivered.
	"""
	kind_of = {label: kind for kind, label in KIND_LABEL.items()}
	use_templates = bool(cint(cfg["doc"].get("chatwoot_use_templates")))
	rows = {}
	for row in cfg["doc"].get("subscription_messages") or []:
		kind = kind_of.get(str(row.get("kind") or "").strip())
		if not kind or kind in rows or not cint(row.get("enabled")):
			continue
		message = str(row.get("message") or "").strip()
		template = str(row.get("whatsapp_template") or "").strip() if use_templates else ""
		if not message and not template:
			continue
		rows[kind] = {
			"message": message,
			"whatsapp_template": template,
			"template_variables": _variables_text(row.get("template_variables")) if template else "",
			"template_title": "",
		}
	return rows


def usable_rows(cfg: dict) -> tuple[dict, list[str]]:
	"""`message_rows`, minus any kind whose template cannot be sent from here.

	Returns (rows, problems). A template that is not in the synced list, or that
	belongs to a different Chatwoot inbox than "Template Inbox ID" (Meta approves
	templates per phone number and refuses one sent from a number that does not
	own it), takes its whole kind out for this round: sending that kind as plain
	text instead would be a different message from the one the operator chose.
	"""
	rows = message_rows(cfg)
	problems = []
	inbox = cint(cfg["subscription_reminder_template_inbox"])
	for kind in list(rows):
		name = rows[kind]["whatsapp_template"]
		if not name:
			continue
		rec = frappe.db.get_value(TEMPLATE_DT, name, ["title", "inbox_id"], as_dict=True)
		if not rec:
			problems.append('"%s": template %s is not in the synced template list' % (KIND_LABEL[kind], name))
			del rows[kind]
		elif inbox and cint(rec.inbox_id) != inbox:
			problems.append('"%s": template %s belongs to inbox %s, not inbox %s' % (
				KIND_LABEL[kind], rec.title or name, rec.inbox_id, inbox))
			del rows[kind]
		else:
			rows[kind]["template_title"] = rec.title or name
	return rows, problems


def due_now(cfg: dict, when=None) -> tuple[bool, str]:
	"""Is this an hour a round may run in? Returns (yes, why not).

	The scheduler ticks hourly and this decides whether the tick matters, which
	keeps the schedule a setting an operator can change from the desk rather
	than a cron string in hooks.py that needs a deploy. A round runs on every
	tick from the start hour until the end hour (exclusive); an end hour at or
	before the start hour still allows the start hour itself, so a mistyped
	setting means one round a day, never none.
	"""
	when = when or now_datetime()

	start = cint(cfg["subscription_reminder_hour"])
	end = max(cint(cfg["subscription_reminder_end_hour"]), start + 1)
	if not (start <= when.hour < end):
		return False, f"not a sending hour (now {when.hour:02d}:00, want {start:02d}:00 to {end:02d}:00)"

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
	"""Every live vehicle whose subscription expires inside the reminder window,
	in the order they should be messaged.

	The window runs from `days_before` in the FUTURE back to `days_after` in the
	past, so one query covers both the warning and the chase:

	    today + 50                today                 today - 90
	         |------------------------|---------------------|
	           expires soon                already expired

	Bounded at both ends on purpose. The forward bound is the whole point of the
	feature -- a customer who hears about it 50 days out can renew without
	losing a day of tracking. The backward bound is what stops the first live run
	messaging customers whose subscription died over a year ago and who are, by
	any reasonable reading, no longer customers. 0 means "no bound" on the
	backward side.

	Order: still running first, nearest expiry first; then expired, newest lapse
	first. `today` is the app's, not the database's -- see TIME in the module
	docstring.
	"""
	days_before = cint(cfg["subscription_reminder_days_before"])
	days_after = cint(cfg["subscription_reminder_days_after"])

	conditions = [
		"device_statues = %(status)s",
		"ifnull(subscription_expiry_date, '') != ''",
		"ifnull(customer, '') != ''",
		"subscription_expiry_date <= date_add(%(app_today)s, interval %(days_before)s day)",
	]
	if days_after > 0:
		conditions.append(
			"subscription_expiry_date >= date_sub(%(app_today)s, interval %(days_after)s day)"
		)

	return frappe.db.sql(
		"""select {fields} from `tab{dt}`
		   where {where}
		   order by (subscription_expiry_date < %(app_today)s),
		            abs(datediff(subscription_expiry_date, %(app_today)s)), customer, name""".format(
			fields=", ".join("`%s`" % f for f in VEHICLE_FIELDS),
			dt=VEHICLE_DOCTYPE,
			where=" and ".join(conditions),
		),
		{"status": LIVE_STATUS, "days_before": days_before, "days_after": days_after, "app_today": today()},
		as_dict=True,
	)


def is_individual(customer, vehicle=None) -> bool:
	"""True only when nothing says "company". Either signal is enough to make it
	a company: the Customer Type is anything but Individual, or Paying is
	"Company" -- on the vehicle, else on the customer if the vehicle has none."""
	if str((customer or {}).get("customer_type") or "").strip().lower() != "individual":
		return False
	paying = str((vehicle or {}).get("paying") or (customer or {}).get("paying") or "").strip().lower()
	return paying != "company"


def _plate(vehicle: dict) -> str:
	"""The plate to print. Arabic first -- it is the one stamped on the metal."""
	for field in ("license_plate", "e_license_plate", "plate_num"):
		value = str(vehicle.get(field) or "").strip()
		if value:
			return value
	return str(vehicle.get("name") or "")


def _days_to_expiry(vehicle: dict) -> int:
	"""Positive = days still to run. Negative = days since it lapsed."""
	return date_diff(vehicle["subscription_expiry_date"], today())


def _count(reasons: dict, why: str):
	reasons[why] = reasons.get(why, 0) + 1


def _today_counts(dry_run: int) -> tuple[int, dict, dict]:
	"""What today's rounds have already done: (sent, per customer, failures per vehicle).

	Only Sent rows count towards the limits (and Dry run rows while dry-running,
	so a dry run walks down the list instead of showing the same first fifty
	every hour). Failed rows, and Skipped rows that were an attempt (they carry a
	code), count per vehicle -- see MAX_FAILED_TRIES_PER_DAY.
	"""
	sent = 0
	per_customer = {}
	failures = {}
	for r in frappe.db.sql(
		"select customer, plates, status, code from `tab%s` where creation >= %%(d)s "
		"and status in ('Sent', 'Dry run', 'Failed', 'Skipped')" % LOG,
		{"d": today() + " 00:00:00"},
		as_dict=True,
	):
		if r.status == "Sent" or (dry_run and r.status == "Dry run"):
			sent += 1
			per_customer[r.customer] = per_customer.get(r.customer, 0) + 1
		elif r.status in ("Failed", "Skipped") and cint(r.code):
			for plate in str(r.plates or "").split(","):
				if plate.strip():
					key = "%s||%s" % (r.customer, plate.strip())
					failures[key] = failures.get(key, 0) + 1
	return sent, per_customer, failures


def _recently_reminded(repeat_days: int, dry_run: int) -> set:
	"""{"customer||plate"} for every vehicle reminded inside the repeat window.

	Reads Sent rows only (and Dry run rows while dry-running). A Failed row means
	nobody was reached; holding the next attempt off for a week on it would turn
	one bad afternoon into a silent week. Blind to WHICH of the two messages was
	sent: someone warned on Monday that their subscription runs out should not
	get "it has expired" on Tuesday because the date rolled over.

	At least one day even when set to 0: with hourly rounds, "no repeat window"
	would mean the same message every hour.
	"""
	since = str(add_to_date(now_datetime(), days=-max(repeat_days, 1)))
	recent = set()
	for r in frappe.db.sql(
		"select customer, plates from `tab%s` where "
		"((status = 'Sent' and sent_on >= %%(since)s) "
		" or (%%(dry)s = 1 and status = 'Dry run' and creation >= %%(since)s))" % LOG,
		{"since": since, "dry": cint(dry_run)},
		as_dict=True,
	):
		for plate in str(r.plates or "").split(","):
			if plate.strip():
				recent.add("%s||%s" % (r.customer or "", plate.strip()))
	return recent


def _renewal_state(days_before: int, days_after: int) -> tuple[dict, dict]:
	"""Vehicles whose renewal is already paid for: ({vehicle: [renewals]}, {vehicle: [invoices]}).

	A Subscription Renewal that has not been applied yet is a customer who has
	paid and is waiting for their date to move; an invoice for the renewal item
	posted since the start of this expiry's reminder window is the same customer
	a little later. Reminding either is the single most annoying message this
	feature can send.
	"""
	pending = {}
	if frappe.db.exists("DocType", "Subscription Renewal"):
		for r in frappe.db.sql(
			"select rv.vehicle, r.name, ifnull(rv.new_end_date, r.new_end_date) as new_end "
			"from `tabSubscription Renewal` r join `tabSubscription Renewal Vehicle` rv on rv.parent = r.name "
			"where r.docstatus < 2 and ifnull(r.update_completed, 0) = 0",
			as_dict=True,
		):
			pending.setdefault(r.vehicle, []).append(r)

	invoiced = {}
	if frappe.get_meta("Sales Invoice Item").get_field("customer_vehicle"):
		for r in frappe.db.sql(
			"select i.customer_vehicle as vehicle, inv.name, inv.posting_date "
			"from `tabSales Invoice` inv join `tabSales Invoice Item` i on i.parent = inv.name "
			"where inv.docstatus = 1 and inv.posting_date >= date_sub(%(app_today)s, interval %(back)s day) "
			"and i.item_code in %(items)s and ifnull(i.customer_vehicle, '') != ''",
			{"back": days_before + days_after + 1, "app_today": today(), "items": RENEWAL_ITEM_CODES},
			as_dict=True,
		):
			invoiced.setdefault(r.vehicle, []).append(r)
	return pending, invoiced


def plan(cfg: dict | None = None, budget: int | None = None, rows: dict | None = None) -> dict:
	"""Who would be messaged this round, and why anybody would not be.

	Returns {"due": [entry ...], "passed": {reason: count}, "in_window",
	"expiring_soon", "already_expired", "sent_today", "budget"}. `due` is one
	entry per VEHICLE, most urgent first, and never longer than the round's
	budget: `budget` (the per-round cap) cut down by what is left of today's
	limit. The whole decision is made here and nothing is written, so the desk
	preview and the live round are guaranteed to agree about what is about to
	happen.
	"""
	cfg = cfg or settings()
	if rows is None:
		rows = usable_rows(cfg)[0]
	dry_run = cint(cfg["subscription_reminder_dry_run"])
	days_before = cint(cfg["subscription_reminder_days_before"])
	days_after = cint(cfg["subscription_reminder_days_after"])
	repeat_days = cint(cfg["subscription_reminder_repeat_days"])
	daily_limit = cint(cfg["subscription_reminder_batch_size"])
	per_customer_cap = cint(cfg["subscription_reminder_max_per_customer"])

	vehicles = vehicles_in_window(cfg)
	out = {"due": [], "passed": {}, "in_window": len(vehicles), "expiring_soon": 0,
	       "already_expired": 0, "sent_today": 0, "budget": 0}
	for v in vehicles:
		if _days_to_expiry(v) < 0:
			out["already_expired"] += 1
		else:
			out["expiring_soon"] += 1

	sent_today, customer_today, failed_today = _today_counts(dry_run)
	out["sent_today"] = sent_today

	room = cint(budget) if budget is not None else len(vehicles)
	if daily_limit > 0:
		room = min(room, daily_limit - sent_today)
	out["budget"] = max(room, 0)
	if room <= 0 or not vehicles or not rows:
		return out

	passed = out["passed"]
	due = out["due"]

	allowed_types = csv_types(cfg["subscription_reminder_customer_types"])
	all_types = (not allowed_types) or ("all" in allowed_types) or ("*" in allowed_types)

	customers = {}
	names = sorted({v.customer for v in vehicles})
	for i in range(0, len(names), 500):
		for c in frappe.db.sql(
			"select name, customer_name, mobile_no, customer_type, paying from `tabCustomer` where name in %(n)s",
			{"n": tuple(names[i:i + 500])},
			as_dict=True,
		):
			customers[c.name] = c

	blocked_phones = do_not_contact.blocked_phones(do_not_contact.REMINDERS)
	recent = _recently_reminded(repeat_days, dry_run)
	pending_renewals, renewal_invoices = _renewal_state(days_before, days_after)

	seen = set()
	for v in vehicles:
		if len(due) >= room:
			break

		days = _days_to_expiry(v)
		kind = EXPIRED if days < 0 else EXPIRING
		if kind not in rows:
			continue

		c = customers.get(v.customer) or {}
		if not all_types and str(c.get("customer_type") or "").strip().lower() not in allowed_types:
			_count(passed, "customer type not in the list")
			continue

		plate = _plate(v)
		key = "%s||%s" % (v.customer, plate)
		if key in recent:
			continue
		if failed_today.get(key, 0) >= MAX_FAILED_TRIES_PER_DAY:
			_count(passed, "failed %s times today (tried again tomorrow)" % MAX_FAILED_TRIES_PER_DAY)
			continue
		if per_customer_cap > 0 and customer_today.get(v.customer, 0) >= per_customer_cap:
			_count(passed, "waiting because their customer already has %s today" % per_customer_cap)
			continue
		if do_not_contact.check(customer=v.customer, scope=do_not_contact.REMINDERS)["blocked"]:
			_count(passed, "excluded / do not contact")
			continue
		if v.deletion_date:
			_count(passed, "Installed but has a Deletion Date")
			continue

		expiry = getdate(v.subscription_expiry_date)
		paid = any(
			r.new_end and getdate(r.new_end) > expiry
			for r in pending_renewals.get(v.name) or []
		)
		if not paid:
			cycle_start = getdate(add_days(expiry, -days_before))
			paid = any(
				r.posting_date and getdate(r.posting_date) >= cycle_start
				for r in renewal_invoices.get(v.name) or []
			)
		if paid:
			_count(passed, "already renewed")
			continue

		# Who gets the message depends on who the customer is. An individual IS
		# the driver, so the vehicle's own number comes first and the customer's
		# is the fallback. A company's drivers are employees, not the person who
		# pays, so only the customer's number is ever used. Numbers whose owner
		# asked to be left alone are stepped over rather than failing the vehicle.
		phone = source = ""
		tries = []
		if is_individual(c, v):
			tries.append((v.driver_mobile, "%s.driver_mobile" % plate))
			tries.append((c.get("mobile_no"), "Customer.mobile_no"))
		else:
			tries.append((c.get("mobile_no"), "Customer.mobile_no"))
		for raw, label in tries:
			p = saudi_mobile(raw)
			if p and p not in blocked_phones:
				phone, source = p, label
				break
		if not phone:
			_count(passed, "no +9665 number")
			continue
		if (phone + "||" + plate) in seen:
			continue
		seen.add(phone + "||" + plate)
		customer_today[v.customer] = customer_today.get(v.customer, 0) + 1

		due.append({
			"customer": v.customer,
			"customer_name": c.get("customer_name") or v.customer,
			"vehicle": v.name,
			"vehicles": [v],
			"count": 1,
			"plates": [plate],
			"expiry": str(expiry),
			"days_to_expiry": days,
			"kind": kind,
			"template_title": rows[kind].get("template_title") or "",
			"phone": phone,
			"phone_source": source,
		})

	return out


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
		"company": frappe.db.get_single_value("Global Defaults", "default_company")
		or frappe.defaults.get_user_default("Company") or "",
		"contacts": format_contacts(BILINGUAL),
	}


def message_for(entry: dict, cfg: dict | None = None, rows: dict | None = None) -> str:
	"""The exact text this vehicle's customer would receive as plain text."""
	cfg = cfg or settings()
	row = (rows if rows is not None else message_rows(cfg)).get(entry.get("kind") or EXPIRED)
	# Reuses the connector's renderer, so a whole-line placeholder that comes
	# out empty drops its line here exactly as it does in a ticket message.
	return cw._render(row["message"], context(entry)) if row and row["message"] else ""


# --------------------------------------------------------------------------
# Doing the work
# --------------------------------------------------------------------------


def _log(entry: dict, status: str, cfg: dict, reason: str = "",
         message: str = "", result: dict | None = None):
	"""Record one outcome. Guarded: a logging failure must not lose a send.

	Every row carries the full body and the plate, because the dry run is the
	review step -- an operator deciding whether to go live is reading these
	rows, and a row that does not say what would have been sent is no use to
	them.
	"""
	result = result or {}
	try:
		row = frappe.new_doc(LOG)
		row.customer = entry["customer"]
		row.customer_name = entry["customer_name"]
		row.phone = entry.get("phone") or ""
		row.phone_source = entry.get("phone_source") or ""
		row.status = status
		row.reminder_type = entry.get("kind") or ""
		via = result.get("via") or ("template" if entry.get("template_title") else "")
		if via == "template":
			row.sent_as = "WhatsApp template: %s" % (
				entry.get("template_title") or result.get("whatsapp_template") or "")
		elif via == "text":
			row.sent_as = "Text"
		row.dry_run = cint(cfg["subscription_reminder_dry_run"])
		row.vehicle_count = entry["count"]
		row.plates = ", ".join(entry["plates"][:50])
		row.expiry = entry["expiry"] or None
		row.days_to_expiry = entry["days_to_expiry"]
		row.reason = str(reason or "")[:500]
		row.message = message
		row.code = cint(result.get("code"))
		row.conversation_id = str(result.get("conversation_id") or "")
		row.message_id = str(result.get("message_id") or "")
		# Only a real, delivered-to-Chatwoot send stamps this: the repeat window
		# reads it, so a dry run stamping it would make the next live run think
		# everybody had already been told.
		row.sent_on = now_datetime() if status == "Sent" else None
		row.insert(ignore_permissions=True)
	except Exception:
		frappe.logger(LOGGER).error(
			"subscription reminder: could not log %s for %s" % (status, entry.get("customer")),
			exc_info=True,
		)


def _store_note(note: str):
	"""Keep the round's one-line summary: on the settings form, and as a comment
	on the trigger's Server Script so the history is readable. Never raises."""
	try:
		frappe.db.set_value(SETTINGS, SETTINGS, "subscription_reminder_last_run", note[:1000],
		                    update_modified=False)
		if frappe.db.exists("Server Script", NOTE_SCRIPT):
			frappe.get_doc({
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": "Server Script",
				"reference_name": NOTE_SCRIPT,
				"content": note.replace("&", "&amp;").replace("<", "&lt;"),
			}).insert(ignore_permissions=True)
	except Exception:
		frappe.logger(LOGGER).error("subscription reminder: could not store the run note", exc_info=True)


def _round(cfg: dict, rows: dict, problems: list, limit: int | None, ready: bool) -> dict:
	"""One round: plan, send, log. Holds the lock; see `run`."""
	dry_run = cint(cfg["subscription_reminder_dry_run"])
	per_round = cint(cfg["subscription_reminder_per_round"])
	if per_round < 1:
		per_round = DEFAULTS["subscription_reminder_per_round"]
	if limit:
		per_round = min(per_round, cint(limit))
	stop_after = cint(cfg["subscription_reminder_stop_after_failures"])
	if stop_after < 1:
		stop_after = DEFAULTS["subscription_reminder_stop_after_failures"]
	daily_limit = cint(cfg["subscription_reminder_batch_size"])
	use_templates = bool(cint(cfg["doc"].get("chatwoot_use_templates")))

	found = plan(cfg, budget=per_round, rows=rows)
	due = found["due"]
	passed = found["passed"]

	sent = failed = 0
	in_a_row = 0
	stopped = ""
	started = time.monotonic()

	for entry in due:
		row = rows[entry["kind"]]
		ctx = context(entry)
		body = cw._render(row["message"], ctx) if row["message"] else ""

		if dry_run:
			if row["whatsapp_template"]:
				note = _("Dry run: nothing was sent. It would go as WhatsApp template {0}.").format(
					row["template_title"] or row["whatsapp_template"])
			else:
				note = _("Dry run: nothing was sent.")
			_log(entry, "Dry run", cfg, reason=note, message=body)
			continue

		# A row with a template is ALWAYS sent as that template ("force"): the
		# 24-hour window is closed for almost everyone a renewal reminder goes
		# to, and a message that is text for one customer and a template for the
		# next is harder to reason about than one that is always the template.
		# A row without one goes as text while the window is open, else -409.
		fallback = None
		if use_templates:
			fallback = {
				"template": row["whatsapp_template"],
				"variables": row["template_variables"],
				"ctx": ctx,
				"force": bool(row["whatsapp_template"]),
			}
		text = body or _("Subscription reminder: {0}").format(row["template_title"] or entry["kind"])

		try:
			result = cw._send(
				text,
				phone=entry["phone"],
				name=entry["customer_name"],
				context={"customer": entry["customer_name"], "template": "subscription_reminder"},
				template_fallback=fallback,
				confirm=True,
			) or {}
		except Exception as e:
			result = {"ok": False, "code": -500, "msg": "Send raised an error: %s" % str(e)[:400]}

		if result.get("ok"):
			sent += 1
			in_a_row = 0
			reason = _("WhatsApp accepted it") if result.get("delivery") != "pending" \
				else _("Sent; WhatsApp's confirmation was still pending")
			_log(entry, "Sent", cfg, reason=reason, message=result.get("message") or body, result=result)
		else:
			failed += 1
			in_a_row += 1
			# -409: the window was closed and there was no usable template, so
			# nothing was posted. Not a failure of the send, but the same fix is
			# needed for every vehicle behind it, so it counts towards the stop.
			status = "Skipped" if result.get("code") == -409 else "Failed"
			_log(entry, status, cfg, reason=str(result.get("msg") or "")[:500], message=body, result=result)

		# Committed per message: a round that is killed half-way must not lose
		# the rows the repeat window and the daily limit are read from.
		frappe.db.commit()

		if in_a_row >= stop_after:
			stopped = " Stopped early after %s failures in a row (last: %s)." % (
				in_a_row, str(result.get("msg") or "")[:200])
			break
		if time.monotonic() - started > ROUND_MAX_SECONDS:
			stopped = " Stopped early: the round reached its %s-minute limit." % (ROUND_MAX_SECONDS // 60)
			break

	sent_total = found["sent_today"] + sent + (len(due) if dry_run else 0)
	if due:
		note = "%s round at %s -- sent %s, failed %s of %s. Today: %s sent%s.%s%s" % (
			"DRY RUN (nothing sent)" if dry_run else "LIVE",
			str(now_datetime())[:16],
			sent, failed, len(due),
			sent_total,
			(" of %s" % daily_limit) if daily_limit > 0 else "",
			stopped,
			(" Passed over while filling this round (not logged): " +
			 "; ".join("%s %s" % (passed[k], k) for k in passed) + ".") if passed else "",
		)
		if problems:
			note += " Left out: " + "; ".join(problems) + "."
		_store_note(note)
	frappe.db.commit()

	summary = {
		"ok": True,
		"ran": True,
		"dry_run": bool(dry_run),
		"vehicles_in_window": found["in_window"],
		"expiring_soon": found["expiring_soon"],
		"already_expired": found["already_expired"],
		"planned": len(due),
		"sent": sent,
		"failed": failed,
		"sent_today": sent_total,
		"daily_limit": daily_limit,
		"stopped_early": stopped.strip(),
		"passed_over": passed,
		"problems": problems,
		"chatwoot_ready": ready,
	}
	frappe.logger(LOGGER).warning("subscription reminders: %s" % summary)
	return summary


def run(force: bool = False, limit: int | None = None) -> dict:
	"""One round: scan, decide, send, log. The scheduler's entry point.

	`force` skips the hour/weekday gate for a manual run from the desk; it does
	NOT skip the enabled switch, the dry-run switch, the repeat window or the
	daily limit. Those are the safety of this feature, and a convenience
	argument must not be able to turn them off.

	Two rounds never overlap: a manual run while the hourly one is still going
	(or an hourly one that outlived its hour) would otherwise message the same
	vehicles twice, because neither has logged them yet.
	"""
	cfg = settings()

	if not cint(cfg["subscription_reminder_enabled"]):
		return {"ok": True, "ran": False, "reason": "Subscription reminders are switched off."}

	if not force:
		due, why = due_now(cfg)
		if not due:
			return {"ok": True, "ran": False, "reason": why}

	rows, problems = usable_rows(cfg)
	if not rows:
		reason = "No usable row in Reminder Messages -- nothing to send."
		if problems:
			reason += " " + "; ".join(problems) + "."
		_store_note("Did not run: " + reason)
		frappe.db.commit()
		return {"ok": True, "ran": False, "reason": reason}

	ready, why = cw._ready(cw.active_settings())
	if not ready and not cint(cfg["subscription_reminder_dry_run"]):
		# A dry run still has something useful to say with Chatwoot down, so it
		# is allowed to proceed; a live run has nothing to send through.
		return {"ok": False, "ran": False, "reason": why}

	try:
		with filelock(LOCK_NAME, timeout=0):
			return _round(cfg, rows, problems, limit, ready)
	except LockTimeoutError:
		return {"ok": True, "ran": False, "reason": "Another reminder round is still running."}


def hourly():
	"""What app_apis.core.jobs calls. Never raises: a scheduled job that throws is retried
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
	"""What the next round would do, without doing any of it.

	System Manager only: this reports customer names and phone numbers across
	the whole book, which is a much broader read than any one ticket.
	"""
	frappe.only_for("System Manager")

	cfg = settings()
	rows, problems = usable_rows(cfg)
	shown = cint(limit) or 20
	round_cap = cint(cfg["subscription_reminder_per_round"]) or DEFAULTS["subscription_reminder_per_round"]
	found = plan(cfg, budget=round_cap, rows=rows)
	ready, why = cw._ready(cw.active_settings())
	is_due, not_due_why = due_now(cfg)

	out_rows = []
	for entry in found["due"][:shown]:
		out_rows.append({
			"customer": entry["customer"],
			"customer_name": entry["customer_name"],
			"phone": entry["phone"],
			"phone_source": entry["phone_source"],
			"kind": entry["kind"],
			"vehicles": 1,
			"plates": entry["plates"],
			"expiry": entry["expiry"],
			"days_to_expiry": entry["days_to_expiry"],
			"sent_as": ("WhatsApp template: " + entry["template_title"]) if entry["template_title"] else "Text",
			"message": message_for(entry, cfg, rows),
		})

	return {
		"ok": True,
		"enabled": bool(cint(cfg["subscription_reminder_enabled"])),
		"dry_run": bool(cint(cfg["subscription_reminder_dry_run"])),
		"kinds_sent": [KIND_LABEL[k] for k in rows],
		"problems": problems,
		"due_now": is_due,
		"not_due_reason": not_due_why,
		"chatwoot_ready": ready,
		"chatwoot_reason": why,
		"vehicles_in_window": found["in_window"],
		"expiring_soon": found["expiring_soon"],
		"already_expired": found["already_expired"],
		"this_round": len(found["due"]),
		"round_budget": found["budget"],
		"sent_today": found["sent_today"],
		"daily_limit": cint(cfg["subscription_reminder_batch_size"]),
		"passed_over": found["passed"],
		"repeat_days": cint(cfg["subscription_reminder_repeat_days"]),
		"window": "from %s days before expiry to %s days after" % (
			cfg["subscription_reminder_days_before"],
			cfg["subscription_reminder_days_after"] or "no limit",
		),
		"rows": out_rows,
	}


@frappe.whitelist()
def run_now(limit: int | None = None) -> dict:
	"""Run a round immediately, ignoring the hour and weekday.

	Still obeys `enabled`, `dry_run`, the repeat window and the daily limit --
	see `run`.
	"""
	frappe.only_for("System Manager")
	return run(force=True, limit=cint(limit) if limit else None)
