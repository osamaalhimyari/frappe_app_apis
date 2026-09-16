# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One subscription-reminder wording: which kind it is for, the text, and the
WhatsApp template to send instead when the 24-hour window is closed. Read by
app_apis.subscription_reminders."""

from frappe.model.document import Document


class AppApisSubscriptionMessage(Document):
	pass
