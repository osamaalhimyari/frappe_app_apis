# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""What Lebara billed ONE SIM for ONE month: tariff plan, status at billing, amount (SAR, before VAT). Read from the invoice's Excel export by app_apis.core.lebara.sync_invoice; this is the real bill, not an estimate."""

from frappe.model.document import Document


class LebaraInvoiceLine(Document):
	pass
