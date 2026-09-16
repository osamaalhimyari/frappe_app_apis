# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One technician left out of automatic messages: the status messages, the
scheduled stuck-ticket reminders, or both. Read by app_apis.technicians.excluded."""

from frappe.model.document import Document


class AppApisExcludedTechnician(Document):
	pass
