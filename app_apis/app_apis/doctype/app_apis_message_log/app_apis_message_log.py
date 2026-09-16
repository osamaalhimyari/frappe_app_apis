# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""A record of one automatic outbound message.

Written only by `app_apis.auto_valuation`. Every field is read_only and the
doctype is `in_create`, so the desk can read the history but cannot invent it --
a row here is meant to be evidence of what the system did, and an editable row
would be worth nothing as evidence.

Rows are never written for tickets the automation was not interested in. A
`Company` customer, or a save that did not cross into a trigger state, is
filtered out before anything is logged; otherwise the table would fill with
thousands of "not applicable" rows and hide the handful that matter.
"""

import frappe
from frappe.model.document import Document


class AppApisMessageLog(Document):
	pass


def clear_old_logs(days: int = 180):
	"""Drop rows older than `days`. Wired to nothing; call it from a scheduler
	entry or a console if the table ever grows enough to be worth pruning.

	Left manual on purpose: this is an audit trail of messages sent to real
	customers, and deleting it on a timer that nobody asked for is not a
	decision this module should make on its own.
	"""
	frappe.db.delete(
		"App Apis Message Log",
		{"creation": ("<", frappe.utils.add_days(frappe.utils.nowdate(), -abs(int(days))))},
	)
	frappe.db.commit()
