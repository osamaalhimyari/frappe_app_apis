# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One action Lebara performed on a SIM (activate, suspend, resume, deactivate), who asked for it and how it ended. Lebara's own history -- every portal and API action, not only ours. Read incrementally by Id by app_apis.core.lebara.sync_transactions."""

from frappe.model.document import Document


class LebaraTransaction(Document):
	pass
