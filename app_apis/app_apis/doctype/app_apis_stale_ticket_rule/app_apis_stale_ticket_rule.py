# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One rule: if a ticket sits in this status, untouched, past this many hours,
nudge the technician with this message.

A table rather than a fixed schedule, for the same reason App Apis Auto Message
is one: which statuses are worth chasing, how long is too long, and what to say
are decisions about how the business runs, not something that belongs in
Python. See app_apis/stale_reminders.py for the scan that reads this table.
"""

from frappe.model.document import Document


class AppApisStaleTicketRule(Document):
	pass
