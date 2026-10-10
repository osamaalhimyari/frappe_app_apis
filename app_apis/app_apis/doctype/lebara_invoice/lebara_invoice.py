# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One Lebara B2B invoice: its number, month and the totals from its Summary sheet. Filled by app_apis.core.lebara.sync_invoice from Lebara's own Excel export; the per-SIM lines are Lebara Invoice Line."""

from frappe.model.document import Document


class LebaraInvoice(Document):
	pass
