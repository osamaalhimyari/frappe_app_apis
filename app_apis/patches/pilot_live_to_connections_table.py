# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""The "Pilot (WSL)" and "Pilot 2" sections (the per-customer live tracking settings)
move into rows 1 and 2 of the Pilot Connections table.

pilot_* goes to row 1 and pilot2_* to row 2 -- the same estates
pilot_admin_to_connections_table put there. A row is created when the estate
has live settings but no admin row yet. The lines of the old Pilot Passwords
tables are moved, not copied, into the Pilot Customer Passwords table with the
row's id, so they keep their names, their encrypted passwords and their order --
and every account's remembered password still points at the same line.

Written straight to the database, for the same reason as the patch before it:
saving app_apis runs its whole validate(). Self-contained on purpose -- a patch
that imports runtime code breaks the day that code changes.
"""

import frappe

CHILD = "App Apis Pilot Connection"
TABLE = "pilot_connections"
PW_CHILD = "App Apis Pilot Password"
PW_TABLE = "pilot_customer_passwords"

# old prefix, old password table, row number, name for a new row
ESTATES = (("pilot", "pilot_password_list", 1, "Pilot (WSL)"), ("pilot2", "pilot2_password_list", 2, "Pilot 2"))
MOVES = {"base_url": "live_base_url", "node": "live_node", "fallback_username": "fallback_username",
         "request_timeout": "live_request_timeout", "stale_after_minutes": "stale_after_minutes"}


def _single(field):
	got = frappe.db.sql("select value from `tabSingles` where doctype='app_apis' and field=%s", field)
	return got[0][0] if got else None


def execute():
	if not frappe.db.exists("DocType", CHILD) or not frappe.db.has_column(CHILD, "live_base_url") \
			or not frappe.db.has_column(PW_CHILD, "connection"):
		return

	next_idx = (frappe.db.sql("select max(idx) from `tab%s` where parent='app_apis' and parentfield=%%s" % PW_CHILD,
	                          PW_TABLE)[0][0] or 0)
	for prefix, pw_table, idx, label in ESTATES:
		old = {k: _single(prefix + "_" + k) for k in MOVES}
		pw_rows = frappe.get_all(PW_CHILD, {"parent": "app_apis", "parentfield": pw_table}, ["name"], order_by="idx asc")
		if not any(old.values()) and not pw_rows:
			continue  # nothing was set for this estate

		row = frappe.db.get_value(CHILD, {"parent": "app_apis", "parentfield": TABLE, "idx": idx}, ["name", "conn_id"], as_dict=True)
		if not row:
			row = frappe._dict(name=frappe.generate_hash(length=10), conn_id=frappe.generate_hash(length=10))
			frappe.get_doc({"doctype": CHILD, "name": row.name, "parent": "app_apis", "parenttype": "app_apis",
			                "parentfield": TABLE, "idx": idx, "label": label, "enabled": 0, "conn_id": row.conn_id,
			                "email_fields": "email_pilot" if idx == 1 else "email_pilot2",
			                "uses_wasl": 1 if idx == 1 else 0}).db_insert()
		elif not row.conn_id:
			row.conn_id = frappe.generate_hash(length=10)
			frappe.db.set_value(CHILD, row.name, "conn_id", row.conn_id, update_modified=False)

		values = {}
		for k, field in MOVES.items():
			if old[k] not in (None, "") and not frappe.db.get_value(CHILD, row.name, field):
				values[field] = old[k]
		if values:
			frappe.db.set_value(CHILD, row.name, values, update_modified=False)

		for r in pw_rows:
			next_idx += 1
			frappe.db.sql("update `tab%s` set parentfield=%%s, connection=%%s, idx=%%s where name=%%s" % PW_CHILD,
			              (PW_TABLE, row.conn_id, next_idx, r.name))

		for k in MOVES:
			frappe.db.sql("delete from `tabSingles` where doctype='app_apis' and field=%s", prefix + "_" + k)

	frappe.clear_cache(doctype="app_apis")
