# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Who worked the ticket, and how to reach them.

The installation engineer is `assigned_to` -- a User, set on 97% of tickets. The
`technician` field is a Driver link and is set on 0.3%, so it is a fallback here
rather than the answer, despite its name.

Their phone number is the hard part. Nothing on this site was ever obliged to
record one, so it is looked for in several places and the first real number
wins:

    User.mobile_no          the field an admin edits to control this
    Employee.cell_number    HR's copy, matched on Employee.user_id
    Driver.cell_number      only when the ticket names a Driver
    User.phone              last resort, usually a landline

At the time of writing 5 of the 18 engineers who worked a ticket in the last six
months can be reached at all. That is a data problem, not a code one, and this
module is built to make it visible rather than to paper over it: `phone()`
returns None, the caller logs a Skipped row naming the engineer, and
`missing_numbers()` lists everybody who needs one filled in.

Employee and Driver are both optional -- the app has to keep installing on a
site without HRMS -- so each lookup is guarded on the doctype existing.
"""

import frappe

from app_apis.phone import normalise

# The ticket's own fieldnames. `xticket` belongs to the site rather than to this
# app, so they are read here and nowhere else.
USER_FIELD = "assigned_to"
DRIVER_FIELD = "technician"
NAME_FIELDS = ("assigned_full_name", "assigned_to_name")

# How long back `missing_numbers` looks. Somebody who has not touched a ticket
# in six months is not the reason today's message did not go out.
ACTIVE_DAYS = 180


def name(doc) -> str:
	"""Best available human name for whoever worked the ticket.

	Same order the valuation page uses, and for the same reason: the Driver link
	is set on well under 1% of tickets, so keying off it would leave almost
	every message addressed to nobody.
	"""
	if doc is None:
		return ""

	for field in NAME_FIELDS:
		value = str(doc.get(field) or "").strip()
		if value:
			return value

	return str(doc.get(DRIVER_FIELD) or doc.get(USER_FIELD) or "").strip()


def _from_user(user: str):
	row = frappe.db.get_value("User", user, ["mobile_no", "phone"], as_dict=True)
	if not row:
		return None, ""
	return (
		(normalise(row.get("mobile_no")), "User.mobile_no")
		if normalise(row.get("mobile_no"))
		else (normalise(row.get("phone")), "User.phone")
	)


def _from_employee(user: str):
	# HRMS is not a dependency of this app; a site without it simply has no
	# Employee table, and that must not be an exception.
	if not frappe.db.exists("DocType", "Employee"):
		return None, ""

	cell = frappe.db.get_value("Employee", {"user_id": user}, "cell_number")
	return normalise(cell), "Employee.cell_number"


def _from_driver(driver: str):
	if not driver or not frappe.db.exists("DocType", "Driver"):
		return None, ""

	cell = frappe.db.get_value("Driver", driver, "cell_number")
	return normalise(cell), "Driver.cell_number"


def engineer(doc) -> dict:
	"""Everything known about the ticket's engineer, reachable or not.

	Always returns a dict. `phone` is None when no number could be found, and
	`source` then says "" -- callers report that as a skip, never as a failure
	to send, because nothing was ever attempted.
	"""
	if doc is None:
		return {"user": "", "driver": "", "name": "", "phone": None, "source": ""}

	user = str(doc.get(USER_FIELD) or "").strip()
	driver = str(doc.get(DRIVER_FIELD) or "").strip()

	phone_number, source = None, ""
	if user:
		phone_number, source = _from_user(user)
		if not phone_number:
			phone_number, source = _from_employee(user)
	if not phone_number:
		phone_number, source = _from_driver(driver)

	return {
		"user": user,
		"driver": driver,
		"name": name(doc),
		"phone": phone_number,
		"source": source if phone_number else "",
	}


def phone(doc) -> str | None:
	"""The engineer's number in E.164, or None if nobody recorded one."""
	return engineer(doc)["phone"]


@frappe.whitelist()
def for_ticket(ticket: str) -> dict:
	"""What this ticket knows about its engineer. For previews and scripts."""
	doc = frappe.get_doc("xticket", ticket)
	doc.check_permission("read")

	found = engineer(doc)
	return {"ok": True, "ticket": doc.name, **found, "reachable": bool(found["phone"])}


@frappe.whitelist()
def missing_numbers(days: int = ACTIVE_DAYS) -> dict:
	"""Every engineer working tickets who has no number on file.

	The one report worth having here: without it, "the technician was not
	messaged" is invisible until somebody reads the log, and the fix -- typing a
	number into a User or Employee record -- is not something the code can do.

	System Manager only. It reads staff phone numbers across the whole site,
	which is a wider view than working one ticket earns.
	"""
	frappe.only_for("System Manager")

	rows = frappe.db.sql(
		"""
		select assigned_to as user, count(*) as tickets, max(modified) as last_ticket
		from `tabxticket`
		where ifnull(assigned_to, '') != ''
		  and modified > date_sub(now(), interval %s day)
		group by assigned_to
		order by tickets desc
		""",
		(int(days or ACTIVE_DAYS),),
		as_dict=True,
	)

	reachable, missing = [], []
	for row in rows:
		found = _from_user(row.user)[0] or _from_employee(row.user)[0]
		entry = {
			"user": row.user,
			"full_name": frappe.db.get_value("User", row.user, "full_name") or "",
			"tickets": row.tickets,
			"last_ticket": str(row.last_ticket or ""),
			"phone": found or "",
		}
		(reachable if found else missing).append(entry)

	return {
		"ok": True,
		"days": int(days or ACTIVE_DAYS),
		"reachable": reachable,
		"missing": missing,
		"summary": f"{len(reachable)} of {len(rows)} engineers can be messaged; "
		f"{len(missing)} need a number on their User or Employee record.",
	}
