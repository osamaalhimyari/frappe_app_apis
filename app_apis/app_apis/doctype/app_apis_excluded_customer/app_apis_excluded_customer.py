# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One customer or number excluded from automatic messages.

A child of the app_apis settings Single. Read by app_apis.do_not_contact, which
merges it with the standalone App Apis Do Not Contact list -- see that module.
"""

from frappe.model.document import Document


class AppApisExcludedCustomer(Document):
	pass
