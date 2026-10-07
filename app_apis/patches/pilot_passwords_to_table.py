# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""The single Pilot password per estate becomes a short table of passwords.

Pilot customers do not all share one password, so each estate (Pilot (WSL) and
Pilot 2) now holds a list that is tried top to bottom for every customer
account. Whatever password was saved in the old field becomes row 1 of its
table, so nothing stops working on the day this runs.

Rows are written directly, and the password is encrypted with the same call the
settings form uses, instead of saving the settings document: saving app_apis
runs its whole validate(), and a site whose Chatwoot or IM settings are
half-filled would fail this migration over something unrelated to it. The old
encrypted value is deleted only after the new one has been read back and
matches. Self-contained on purpose -- a patch that imports runtime code breaks
the day that code changes, on every site that has not migrated yet.
"""

import frappe
from frappe.utils.password import (
	get_decrypted_password,
	remove_encrypted_password,
	set_encrypted_password,
)

CHILD = "App Apis Pilot Password"

# retired single-password field -> the table that replaces it
MOVES = {
	"pilot_password": "pilot_password_list",
	"pilot2_password": "pilot2_password_list",
}


def execute():
	if not frappe.db.exists("DocType", CHILD):
		return

	for old_field, table in MOVES.items():
		old = get_decrypted_password("app_apis", "app_apis", old_field, raise_exception=False)

		if old and not frappe.db.count(CHILD, {"parent": "app_apis", "parentfield": table}):
			row = frappe.get_doc({
				"doctype": CHILD,
				"name": frappe.generate_hash(length=10),
				"parent": "app_apis",
				"parenttype": "app_apis",
				"parentfield": table,
				"idx": 1,
				"password": "*" * len(old),
			})
			row.db_insert()
			set_encrypted_password(CHILD, row.name, old, "password")
			if get_decrypted_password(CHILD, row.name, "password", raise_exception=False) != old:
				# Never drop the only copy of a password on the strength of an unchecked write.
				frappe.db.delete(CHILD, {"name": row.name})
				frappe.throw("Could not move %s into the %s table; the old password was left in place." % (old_field, table))

		# Moved (now or earlier), or never set: either way the old field is retired.
		if old and frappe.db.count(CHILD, {"parent": "app_apis", "parentfield": table}):
			remove_encrypted_password("app_apis", "app_apis", old_field)
		if not old or frappe.db.count(CHILD, {"parent": "app_apis", "parentfield": table}):
			frappe.db.sql("delete from `tabSingles` where doctype='app_apis' and field=%s", old_field)

	frappe.clear_cache(doctype="app_apis")
