# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""The "Pilot (WSL)" and "Pilot 2" sections (the per-customer live tracking settings)
move into rows 1 and 2 of the Pilot Connections table.

pilot_* goes to row 1 and pilot2_* to row 2 -- the same estates
pilot_admin_to_connections_table put there. A row is created when the estate
has live settings but no admin row yet. The Pilot Passwords tables become the
row's Customer Passwords (comma-separated, same order), and every account's
remembered password is re-pointed at the same password, so no customer is
tried from the top again.

Written straight to the database, for the same reason as the patch before it:
saving app_apis runs its whole validate(). Old values are deleted only after
the new encrypted copy has been read back and matches. Self-contained on
purpose -- a patch that imports runtime code breaks the day that code changes.
"""

import hashlib

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

CHILD = "App Apis Pilot Connection"
TABLE = "pilot_connections"
PW_CHILD = "App Apis Pilot Password"
LOGIN_DT = "App Apis Pilot Login"

# old prefix, old password table, row number, name for a new row
ESTATES = (("pilot", "pilot_password_list", 1, "Pilot (WSL)"), ("pilot2", "pilot2_password_list", 2, "Pilot 2"))
MOVES = {"base_url": "live_base_url", "node": "live_node", "fallback_username": "fallback_username",
         "request_timeout": "live_request_timeout", "stale_after_minutes": "stale_after_minutes"}


def _single(field):
	got = frappe.db.sql("select value from `tabSingles` where doctype='app_apis' and field=%s", field)
	return got[0][0] if got else None


def _password_id(password):
	return "pw:" + hashlib.sha256(str(password).encode()).hexdigest()[:12]


def execute():
	if not frappe.db.exists("DocType", CHILD) or not frappe.db.has_column(CHILD, "customer_passwords"):
		return

	for prefix, pw_table, idx, label in ESTATES:
		old = {k: _single(prefix + "_" + k) for k in MOVES}
		pw_rows = frappe.get_all(PW_CHILD, {"parent": "app_apis", "parentfield": pw_table}, ["name"], order_by="idx asc") \
			if frappe.db.exists("DocType", PW_CHILD) else []
		passwords, ids = [], {}
		for r in pw_rows:
			pw = get_decrypted_password(PW_CHILD, r.name, "password", raise_exception=False)
			if pw and pw not in passwords:
				passwords.append(pw)
			if pw:
				ids[r.name] = _password_id(pw)
		if not any(old.values()) and not passwords:
			continue  # nothing was set for this estate

		row = frappe.db.get_value(CHILD, {"parent": "app_apis", "parentfield": TABLE, "idx": idx}, "name")
		if not row:
			row = frappe.generate_hash(length=10)
			frappe.get_doc({"doctype": CHILD, "name": row, "parent": "app_apis", "parenttype": "app_apis",
			                "parentfield": TABLE, "idx": idx, "label": label, "enabled": 0,
			                "email_fields": "email_pilot" if idx == 1 else "email_pilot2",
			                "uses_wasl": 1 if idx == 1 else 0}).db_insert()

		values = {}
		for k, field in MOVES.items():
			current = frappe.db.get_value(CHILD, row, field)
			if old[k] not in (None, "") and not current:
				values[field] = old[k]
		if values:
			frappe.db.set_value(CHILD, row, values, update_modified=False)

		if passwords and not frappe.db.get_value(CHILD, row, "customer_passwords"):
			joined = ",".join(passwords)
			frappe.db.set_value(CHILD, row, "customer_passwords", "*" * len(joined), update_modified=False)
			set_encrypted_password(CHILD, row, joined, "customer_passwords")
			if get_decrypted_password(CHILD, row, "customer_passwords", raise_exception=False) != joined:
				frappe.throw("Could not move %s into Pilot Connections row %s; the old passwords were left in place." % (pw_table, idx))
			# the per-account memory pointed at the old table rows; point it at the same passwords
			for old_row, new_id in ids.items():
				frappe.db.sql("update `tab%s` set password_row=%%s where password_row=%%s" % LOGIN_DT, (new_id, old_row))
			for r in pw_rows:
				frappe.db.sql("delete from `__Auth` where doctype=%s and name=%s", (PW_CHILD, r.name))
			frappe.db.delete(PW_CHILD, {"parent": "app_apis", "parentfield": pw_table})

		for k in MOVES:
			frappe.db.sql("delete from `tabSingles` where doctype='app_apis' and field=%s", prefix + "_" + k)

	frappe.clear_cache(doctype="app_apis")
