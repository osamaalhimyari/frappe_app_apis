# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One IM connection (an IM / Trakzee login). The IM Connections table on the app_apis settings holds as
many as are needed; row 1 is IM account 1 (platform key "im"), row 2 is "im2", and so on
(see app_apis.im_connector.connections)."""

from frappe.model.document import Document


class AppApisIMConnection(Document):
	pass
