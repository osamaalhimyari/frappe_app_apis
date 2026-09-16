# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

""""Stop messaging me." The one list every automatic send has to ask first.

A customer who has asked to be left alone is not a preference to be weighed
against a renewal target -- it is the whole reason a messaging feature is
allowed to exist at all. So this is a hard gate, checked in three places:

    subscription_reminders.plan()      the scheduled renewal reminders
    auto_messages._run()               the automatic ticket-stage messages
    chatwoot_connector.send_ticket_message()   an agent sending a template

The list lives in this app, not as a field on `Customer`: the site's doctypes
stay untouched, and a block can name somebody who has no Customer record at all
-- which matters, because the renewal reminder often resolves to a DRIVER's
number, and a driver who says stop is a person, not an account.

There are two places to add somebody, and both are read every time:

    App Apis settings -> Excluded Customers   a table at the bottom of the form,
                                              for whoever is already in there
    App Apis Do Not Contact                   a standalone list with its own
                                              permissions and change history,
                                              for an agent acting on a complaint

Neither is authoritative over the other. A row in either one stops the message.

TWO WAYS TO NAME SOMEBODY, AND WHY BOTH
---------------------------------------
    customer   blocks every message for that account, on any number
    phone      blocks that number, whoever it currently belongs to

A row may carry either or both. The phone form is the one that matches what
actually happens: somebody replies "stop" on WhatsApp and all anybody has is the
number they said it from.

SCOPE
-----
"Stop the renewal spam" and "stop contacting me entirely" are different
sentences, so a row says which it is:

    All messages            nothing automatic or templated goes out
    Subscription reminders  only the renewal scan is blocked
    Ticket messages         only the ticket-stage messages are blocked

`All messages` is the default. Somebody who asked to be left alone and got a
message anyway because the operator picked the narrower option is exactly the
failure this module exists to prevent.

WHAT IS NOT BLOCKED
-------------------
`chatwoot_connector.send_message` -- the raw sender an agent uses to type a
reply -- is deliberately left alone. A customer who writes in with a question
must get an answer; refusing to let a human reply to a human is not respecting
their wishes, it is ignoring them differently. What is blocked is everything
sent AT them without being asked for.

REVERSIBLE, NEVER DELETED
-------------------------
A row has an `enabled` check rather than being removed when somebody opts back
in, so the record of having asked survives. If it ever comes up again, "they
asked us to stop on 3 March and lifted it themselves on 9 May" is the answer you
want to have.
"""

import frappe
from frappe import _
from frappe.utils import cint

from app_apis.phone import normalise

DOCTYPE = "App Apis Do Not Contact"

# Scopes. The values are also the Select options on the doctype.
ALL = "All messages"
REMINDERS = "Subscription reminders"
TICKETS = "Ticket messages"

SCOPES = (ALL, REMINDERS, TICKETS)


def _rows() -> list[dict]:
	"""Every live block, cached for the request.

	Read whole and filtered in Python rather than queried per customer: the
	list is small (a few hundred at worst), the reminder scan asks about it
	hundreds of times in one pass, and a phone match has to compare NORMALISED
	numbers -- which SQL cannot do against however the number was typed in.
	"""
	cached = getattr(frappe.local, "_app_apis_dnc", None)
	if cached is not None:
		return cached

	rows = []

	# Source 1: the standalone list. Where an agent files a complaint as it
	# happens -- it has its own permissions, so somebody who may not edit the
	# settings Single can still act on "stop messaging me" immediately.
	try:
		for row in frappe.get_all(
			DOCTYPE,
			filters={"enabled": 1},
			fields=["name", "customer", "phone", "scope", "reason", "modified"],
			limit_page_length=0,
		):
			rows.append({
				"name": row.name,
				"source": DOCTYPE,
				"customer": (row.customer or "").strip(),
				"phone": normalise(row.phone) or "",
				"scope": row.scope or ALL,
				"reason": row.reason or "",
			})
	except Exception:
		# The doctype may not exist yet on a site part-way through its upgrade.
		# An unreadable list must not take the messaging with it -- but it must
		# not silently unblock anybody either, so it is logged loudly.
		frappe.logger("app_apis").error("do-not-contact list unreadable", exc_info=True)

	# Source 2: the Excluded Customers table at the bottom of the settings form.
	# The same rule, somewhere easier to find: an operator looking at the
	# messaging settings should be able to add an exclusion without being told
	# to go and open a different doctype.
	#
	# Read as well as, not instead of, source 1. Two places to add a row is a
	# small price for the list being wherever the person happens to be looking;
	# an exclusion that exists in one place and is ignored in the other is not.
	try:
		settings = frappe.get_cached_doc("app_apis")
		for row in settings.get("excluded_customers") or []:
			if not cint(getattr(row, "enabled", 1)):
				continue
			customer = str(getattr(row, "customer", "") or "").strip()
			phone = normalise(getattr(row, "phone", "")) or ""
			if not customer and not phone:
				# A half-typed row in a grid is normal while somebody is still
				# typing. Blocking nobody is the right reading of it.
				continue
			rows.append({
				"name": row.name,
				"source": "app_apis.excluded_customers",
				"customer": customer,
				"phone": phone,
				"scope": str(getattr(row, "scope", "") or "") or ALL,
				"reason": str(getattr(row, "reason", "") or ""),
			})
	except Exception:
		frappe.logger("app_apis").error("excluded-customers table unreadable", exc_info=True)

	frappe.local._app_apis_dnc = rows
	return rows


def clear_cache():
	"""Drop the request cache. Called when a row is written."""
	if hasattr(frappe.local, "_app_apis_dnc"):
		delattr(frappe.local, "_app_apis_dnc")


def _covers(row: dict, scope: str) -> bool:
	"""Does a row's scope cover the kind of message being sent?"""
	return row["scope"] == ALL or row["scope"] == scope


def check(customer=None, phone=None, scope: str = ALL) -> dict:
	"""Is this recipient blocked? Always returns a dict, never raises.

	    {"blocked": bool, "reason": str, "matched": "customer" | "phone" | "",
	     "row": name}

	`scope` is the kind of message about to be sent -- REMINDERS or TICKETS --
	not the scope of the block. Passing ALL asks "is anything blocked at all",
	which is what a caller wants when it has not decided yet.
	"""
	customer = str(customer or "").strip()
	wanted = normalise(phone) or ""

	for row in _rows():
		if not _covers(row, scope):
			continue

		if customer and row["customer"] and row["customer"] == customer:
			return {
				"blocked": True,
				"matched": "customer",
				"row": row["name"],
				"reason": _("{0} asked not to be contacted ({1}).").format(
					customer, row["scope"]
				) + ((" " + row["reason"]) if row["reason"] else ""),
			}

		if wanted and row["phone"] and row["phone"] == wanted:
			return {
				"blocked": True,
				"matched": "phone",
				"row": row["name"],
				"reason": _("{0} asked not to be contacted ({1}).").format(
					wanted, row["scope"]
				) + ((" " + row["reason"]) if row["reason"] else ""),
			}

	return {"blocked": False, "matched": "", "row": "", "reason": ""}


def is_blocked(customer=None, phone=None, scope: str = ALL) -> bool:
	"""`check` when the caller only needs the yes or no."""
	return check(customer=customer, phone=phone, scope=scope)["blocked"]


def blocked_phones(scope: str = ALL) -> set:
	"""Every blocked number, normalised. For a caller walking a list of numbers.

	Used by the reminder scan, which has several numbers to choose from: a
	blocked DRIVER number should make it try the next one, not give up on the
	customer entirely.
	"""
	return {row["phone"] for row in _rows() if row["phone"] and _covers(row, scope)}


# --------------------------------------------------------------------------
# Public surface -- whitelisted, safe to call from a Client Script
# --------------------------------------------------------------------------


@frappe.whitelist()
def block(customer: str | None = None, phone: str | None = None,
          scope: str = ALL, reason: str = "", source: str = "Customer asked") -> dict:
	"""Add somebody to the list. What an agent's "Stop messaging" button calls.

	Gated on write access to the doctype rather than on System Manager: the
	agent reading "stop sending me this" in Chatwoot is the person who should be
	able to act on it, immediately, without finding a manager. The reverse --
	`unblock` -- is the one that needs more than that.
	"""
	frappe.has_permission(DOCTYPE, "create", throw=True)

	customer = str(customer or "").strip()
	phone_raw = str(phone or "").strip()
	if not customer and not phone_raw:
		return {"ok": False, "msg": _("Give a customer or a phone number.")}

	if scope not in SCOPES:
		return {"ok": False, "msg": _("Unknown scope: {0}").format(scope)}

	normalised = normalise(phone_raw) if phone_raw else ""
	if phone_raw and not normalised:
		return {"ok": False, "msg": _("{0} is not a usable phone number.").format(phone_raw)}

	# Re-enable rather than duplicate: somebody who opts out, opts back in and
	# opts out again should be one row with a history, not three rows.
	# ifnull on both sides -- an unset customer is NULL, and NULL never equals
	# the empty string, so a plain filter would create a second row every time
	# somebody blocked the same number twice.
	existing = frappe.db.sql(
		"""select name from `tabApp Apis Do Not Contact`
		   where ifnull(customer, '') = %(customer)s
		     and ifnull(phone, '') = %(phone)s
		     and scope = %(scope)s
		   limit 1""",
		{"customer": customer or "", "phone": normalised or "", "scope": scope},
	)
	existing = existing[0][0] if existing else None
	if existing:
		doc = frappe.get_doc(DOCTYPE, existing)
		was = cint(doc.enabled)
		doc.enabled = 1
		if reason:
			doc.reason = reason
		doc.source = source
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		clear_cache()
		return {
			"ok": True,
			"name": doc.name,
			"created": False,
			"msg": _("Already on the list.") if was else _("Put back on the list."),
		}

	doc = frappe.new_doc(DOCTYPE)
	doc.customer = customer or None
	doc.phone = normalised or None
	doc.scope = scope
	doc.reason = reason
	doc.source = source
	doc.enabled = 1
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	clear_cache()

	return {
		"ok": True,
		"name": doc.name,
		"created": True,
		"msg": _("{0} will not be messaged automatically any more.").format(
			customer or normalised
		),
	}


@frappe.whitelist()
def unblock(name: str, reason: str = "") -> dict:
	"""Lift a block. The row stays, so the history of having asked survives.

	System Manager only, and deliberately stricter than `block`: putting
	somebody back on the receiving end of automatic messages after they asked
	not to be is a decision, not a correction.
	"""
	frappe.only_for("System Manager")

	if not frappe.db.exists(DOCTYPE, name):
		return {"ok": False, "msg": _("No such row: {0}").format(name)}

	doc = frappe.get_doc(DOCTYPE, name)
	doc.enabled = 0
	if reason:
		doc.reason = (doc.reason or "") + "\n" + _("Lifted: ") + reason
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	clear_cache()

	return {"ok": True, "name": name, "msg": _("Messages to {0} are switched back on.").format(
		doc.customer or doc.phone
	)}


@frappe.whitelist()
def status(customer: str | None = None, phone: str | None = None) -> dict:
	"""Whether this recipient is blocked, and by which rows. For a form button."""
	frappe.has_permission(DOCTYPE, "read", throw=True)

	return {
		"ok": True,
		"customer": customer or "",
		"phone": normalise(phone) or "" if phone else "",
		"all": check(customer, phone, ALL),
		"reminders": check(customer, phone, REMINDERS),
		"tickets": check(customer, phone, TICKETS),
		"list_size": len(_rows()),
	}
