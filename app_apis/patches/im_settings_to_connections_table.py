# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""The IM Connection and IM Upload (Add Vehicle) sections become row 1 of the IM Connections table.

Row 1 keeps the platform key "im", so LIVE_TARGETS and every saved reference keep their meaning. The
password is re-encrypted on the row and read back before the old copy is removed. The IM Device Models
lines are moved (not copied) under the row, so nothing an operator typed is lost.

Written straight to the database, and self-contained, for the same reasons as the Pilot patches:
saving app_apis runs its whole validate(), and a patch that imports runtime code breaks the day that
code changes.
"""

import frappe
from frappe.utils.password import get_decrypted_password, remove_encrypted_password, set_encrypted_password

CHILD = "App Apis IM Connection"
MODEL = "App Apis IM Model"
MOVES = {"im_base_url": "base_url", "im_project_id": "project_id", "im_username": "username",
         "im_request_timeout": "request_timeout", "im_stale_after_minutes": "stale_after_minutes",
         "im_data_timezone": "data_timezone", "im_min_call_interval_seconds": "min_call_interval_seconds",
         "im_token_ttl_minutes": "token_ttl_minutes", "im_ignition_poll_seconds": "ignition_poll_seconds",
         "im_ignition_window_minutes": "ignition_window_minutes", "im_admin_id": "admin_id",
         "im_default_reseller_id": "default_reseller_id", "im_reseller_ids": "reseller_ids",
         "im_sim_provider": "sim_provider"}


def _single(field):
	got = frappe.db.sql("select value from `tabSingles` where doctype='app_apis' and field=%s", field)
	return got[0][0] if got else None


def execute():
	if not frappe.db.exists("DocType", CHILD) or not frappe.db.has_column(MODEL, "connection"):
		return
	if frappe.db.count(CHILD, {"parent": "app_apis", "parentfield": "im_connections"}):
		return  # already moved, or filled by hand: never touch it again

	old = {f: _single(f) for f in MOVES}
	password = get_decrypted_password("app_apis", "app_apis", "im_password", raise_exception=False)
	models = frappe.get_all(MODEL, {"parent": "app_apis", "parentfield": "im_models"}, ["name"], order_by="idx asc")
	if not any(old.values()) and not password and not models:
		return  # IM was never set up on this site

	name, conn_id = frappe.generate_hash(length=10), frappe.generate_hash(length=10)
	values = {field: old[f] for f, field in MOVES.items() if old[f] not in (None, "")}
	frappe.get_doc({"doctype": CHILD, "name": name, "parent": "app_apis", "parenttype": "app_apis",
	                "parentfield": "im_connections", "idx": 1, "label": "IM (Trakzee)",
	                "enabled": 1 if (old["im_username"] and password) else 0, "email_fields": "im_platform",
	                "conn_id": conn_id, "password": ("*" * len(password)) if password else "", **values}).db_insert()
	if password:
		set_encrypted_password(CHILD, name, password, "password")
		if get_decrypted_password(CHILD, name, "password", raise_exception=False) != password:
			frappe.db.delete(CHILD, {"name": name})
			frappe.throw("Could not move im_password into IM Connections; the old password was left in place.")
		remove_encrypted_password("app_apis", "app_apis", "im_password")

	for i, m in enumerate(models, start=1):
		frappe.db.sql("update `tab%s` set parentfield='im_device_models', connection=%%s, idx=%%s where name=%%s" % MODEL,
		              (conn_id, i, m.name))

	for f in list(MOVES) + ["im_password"]:
		frappe.db.sql("delete from `tabSingles` where doctype='app_apis' and field=%s", f)
	frappe.clear_cache(doctype="app_apis")
