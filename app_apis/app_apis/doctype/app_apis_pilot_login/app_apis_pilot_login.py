# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""What app_apis.connector has learned about one Pilot customer account.

One row per (estate, account email): which row of the Pilot Passwords table
worked last time, so the next check tries it first instead of walking the list,
and when every password was rejected, so an account that needs attention is
visible in one list. It holds the position of the working password, never the
password itself.

Written only by app_apis.connector. Deleting a row is harmless: the next check
simply walks the list from the top again."""

from frappe.model.document import Document


class AppApisPilotLogin(Document):
	pass
