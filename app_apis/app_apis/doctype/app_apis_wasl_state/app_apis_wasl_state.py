# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One row per IMEI that is on Pilot's WASL list: what WASL last said about it.

Written only by app_apis.wasl.sync_wasl_states (the hourly "App Apis - WASL Status Sync" job). Read by
app_apis.vehicle_systems, which puts it on the Customer Vehicle form without calling Pilot."""

from frappe.model.document import Document


class AppApisWASLState(Document):
	pass
