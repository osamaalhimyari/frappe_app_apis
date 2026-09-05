# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class app_apis_sim_import(Document):
	"""One SIM from the last uploaded Lebara export.

	Replaced whole on every upload -- `app_apis.fleet_audit.import_sim_file`
	truncates and rewrites this table, so it is marked read-only: an edit
	here would be thrown away by the next upload without telling anyone.
	Fix the SIM's status at Lebara, then re-upload.
	"""

	pass
