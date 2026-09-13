# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One uploaded fuel-card report. See app_apis/fuel_efficiency.py."""

import frappe
from frappe.model.document import Document


class AppApisFuelImport(Document):
	def on_trash(self):
		# Its fills and trucks mean nothing without it, and they Link here, so
		# they go first -- otherwise the link check would refuse the delete.
		frappe.db.delete("App Apis Fuel Fill", {"fuel_import": self.name})
		frappe.db.delete("App Apis Fuel Vehicle", {"fuel_import": self.name})
