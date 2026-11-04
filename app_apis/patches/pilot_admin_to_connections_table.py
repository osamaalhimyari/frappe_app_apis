# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""The two fixed Pilot Admin sections become the Pilot Connections table.

`pilot_admin_*` (Pilot (WSL)) becomes row 1 and `pilot_admin2_*` (Pilot 2) row 2,
so account 1 and account 2 -- and the keys pilot_wsl / pilot2 the scripts use --
mean what they meant before. Row 1 reads its account email from email_pilot and
uses WASL; row 2 reads email_pilot2 and does not, which is exactly what the code
did before this change.

Written straight to the database, and the password encrypted with the same call
the form uses, instead of saving the settings document: saving app_apis runs its
whole validate(), and a half-filled Chatwoot or IM section would fail this
migration over something unrelated. An old encrypted password is deleted only
after the new copy has been read back and matches. Self-contained on purpose --
a patch that imports runtime code breaks the day that code changes.
"""

import frappe
from frappe.utils.password import (
	get_decrypted_password,
	remove_encrypted_password,
	set_encrypted_password,
)

CHILD = "App Apis Pilot Connection"
TABLE = "pilot_connections"

# old prefix -> what the row is
ROWS = (
	("pilot_admin", {"label": "Pilot (WSL)", "email_fields": "email_pilot", "uses_wasl": 1}),
	("pilot_admin2", {"label": "Pilot 2", "email_fields": "email_pilot2", "uses_wasl": 0}),
)
SUFFIXES = ("enabled", "base_url", "username", "node", "request_timeout", "token_ttl_minutes")


def _single(field):
	got = frappe.db.sql("select value from `tabSingles` where doctype='app_apis' and field=%s", field)
	return got[0][0] if got else None


def execute():
	if not frappe.db.exists("DocType", CHILD):
		return
	if frappe.db.count(CHILD, {"parent": "app_apis", "parentfield": TABLE}):
		return  # already moved, or an operator has filled the table: never touch it again

	for idx, (prefix, fixed) in enumerate(ROWS, start=1):
		old = {s: _single(prefix + "_" + s) for s in SUFFIXES}
		password = get_decrypted_password("app_apis", "app_apis", prefix + "_password", raise_exception=False)
		if not any([old["base_url"], old["username"], password, frappe.utils.cint(old["enabled"])]):
			continue  # that section was never filled in: no row for it

		row = frappe.get_doc({
			"doctype": CHILD,
			"name": frappe.generate_hash(length=10),
			"parent": "app_apis",
			"parenttype": "app_apis",
			"parentfield": TABLE,
			"idx": idx,
			"enabled": frappe.utils.cint(old["enabled"]),
			"base_url": old["base_url"] or "",
			"username": old["username"] or "",
			"node": old["node"] or "",
			"request_timeout": frappe.utils.cint(old["request_timeout"]) or 20,
			"token_ttl_minutes": frappe.utils.cint(old["token_ttl_minutes"]) or 45,
			"password": ("*" * len(password)) if password else "",
			**fixed,
		})
		row.db_insert()
		if password:
			set_encrypted_password(CHILD, row.name, password, "password")
			if get_decrypted_password(CHILD, row.name, "password", raise_exception=False) != password:
				# Never drop the only copy of a password on the strength of an unchecked write.
				frappe.db.delete(CHILD, {"name": row.name})
				frappe.throw("Could not move %s_password into Pilot Connections; the old password was left in place." % prefix)
			remove_encrypted_password("app_apis", "app_apis", prefix + "_password")

		# the retired fields: removing a field from a Single leaves its value row behind
		for s in SUFFIXES + ("password",):
			frappe.db.sql("delete from `tabSingles` where doctype='app_apis' and field=%s", prefix + "_" + s)

	frappe.clear_cache(doctype="app_apis")
