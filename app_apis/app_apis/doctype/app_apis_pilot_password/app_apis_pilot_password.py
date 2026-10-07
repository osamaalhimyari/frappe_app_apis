# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One Pilot customer-account password. A Pilot estate has a short list of these
(Pilot Passwords on the settings form); app_apis.connector tries them top to
bottom for each customer account and remembers which one worked."""

from frappe.model.document import Document


class AppApisPilotPassword(Document):
	pass
