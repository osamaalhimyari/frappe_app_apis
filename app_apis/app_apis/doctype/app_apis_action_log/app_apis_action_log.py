# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AppApisActionLog(Document):
	"""One thing someone did to an outside platform (Pilot, IM, WASL, Lebara). Written by app_apis.audit
	and by the shipped Server Scripts; never edited by hand."""

	pass
