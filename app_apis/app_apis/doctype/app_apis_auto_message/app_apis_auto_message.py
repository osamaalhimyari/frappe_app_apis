# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One rule: when a ticket reaches this status, send that message.

A table rather than a setting per message, because the interesting part is the
pairing. Adding a fourth stage later is a row, not a deploy -- which is the
whole point, since which status means what is a decision about how the business
runs and not something that belongs in Python.
"""

from frappe.model.document import Document


class AppApisAutoMessage(Document):
	pass
