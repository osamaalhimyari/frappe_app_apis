# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""A record of one subscription-expiry reminder, or one decision not to send it.

Written only by `app_apis.subscription_reminders`. Every field is read_only and
the doctype is `in_create`, so the desk can read the history but cannot invent
it -- a row here is evidence of what the system did to a real customer, and an
editable row is worth nothing as evidence.

This table is load-bearing, not just an audit trail. `subscription_reminders`
reads it to answer "have we already told this customer this week", so deleting
rows inside the repeat window will cause the next run to message those customers
again. That is the reason there is no automatic pruning wired up.

One row per CUSTOMER per attempt, not one per vehicle: the reminder itself is
grouped that way, and `plates` carries the whole list.
"""

import frappe
from frappe.model.document import Document


class AppApisReminderLog(Document):
	pass


def clear_old_logs(days: int = 365):
	"""Drop rows older than `days`. Wired to nothing; call it from a console if
	the table ever grows enough to be worth pruning.

	The floor is deliberately not settable below the repeat window, because a
	prune that reaches inside it does not tidy the log -- it re-arms the
	reminder for everybody it deleted.
	"""
	days = abs(int(days))
	repeat = frappe.db.get_single_value("app_apis", "subscription_reminder_repeat_days") or 7
	if days <= int(repeat):
		frappe.throw(
			f"Refusing to prune to {days} days: the reminder repeat window is "
			f"{repeat} days, and deleting rows inside it would message those "
			f"customers again on the next run."
		)

	frappe.db.delete(
		"App Apis Reminder Log",
		{"creation": ("<", frappe.utils.add_days(frappe.utils.nowdate(), -days))},
	)
	frappe.db.commit()
