# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class app_apis_fleet_audit(Document):
	"""One reconciled device: what the ERP believes, and what IM and Pilot say.

	A snapshot row, not a record. `app_apis.fleet_audit.run_audit` truncates and
	rewrites this table on every run, so the doctype is marked read-only: an
	edit here would be thrown away by the next refresh without telling anyone.
	Fix what the audit found in the ERP or on the platform, then re-run.
	"""

	pass
