# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One Pilot connection (a Pilot Administrator login on one estate). The Pilot Connections table on the
app_apis settings holds as many as are needed; row 1 is account 1, row 2 is account 2, and so on
(see app_apis.pilot_admin.connections)."""

from frappe.model.document import Document


class AppApisPilotConnection(Document):
	pass
