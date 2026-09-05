# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Fold `Pilot API Settings` and `IM Settings` into the single `app_apis` doctype.

They were two Singles because the two providers were built weeks apart. They are
two connections of one app, not two apps, so an operator should configure them on
one screen rather than discover the second one by reading the source.

Runs in **post_model_sync**: `app_apis` has to exist before anything can be
written into it, and doctypes are synced before this section runs.

Three things make this more than a field copy:

1. **Passwords live in two places.** Frappe keeps the secret encrypted in
   `__Auth` and a masked placeholder in `tabSingles`. `get_password()` reads
   `__Auth`, but both connectors check the doc field -- the placeholder -- first
   and treat an empty one as "no password configured". Move only `__Auth` and the
   app reports "no password available" while the secret sits there intact.

2. **The copy must survive a re-run.** Patches can be replayed, and by then the
   old doctypes may be gone. Every read is therefore guarded, and an already
   populated target is left alone rather than overwritten with blanks.

3. **Read before delete.** The old Singles are dropped at the end, in the same
   transaction, only after their values have been written across.
"""

import frappe

# old doctype -> {old fieldname: new fieldname}
FIELD_MAP = {
	"Pilot API Settings": {
		"base_url": "pilot_base_url",
		"node": "pilot_node",
		"fallback_username": "pilot_fallback_username",
		"request_timeout": "pilot_request_timeout",
		"stale_after_minutes": "pilot_stale_after_minutes",
	},
	"IM Settings": {
		"base_url": "im_base_url",
		"project_id": "im_project_id",
		"im_username": "im_username",
		"request_timeout": "im_request_timeout",
		"stale_after_minutes": "im_stale_after_minutes",
		"data_timezone": "im_data_timezone",
		"min_call_interval_seconds": "im_min_call_interval_seconds",
		"token_ttl_minutes": "im_token_ttl_minutes",
		"ignition_poll_seconds": "im_ignition_poll_seconds",
		"ignition_window_minutes": "im_ignition_window_minutes",
	},
}

# old doctype -> (old password fieldname, new password fieldname)
PASSWORD_MAP = {
	"Pilot API Settings": ("pilot_password", "pilot_password"),
	"IM Settings": ("im_password", "im_password"),
}

NEW = "app_apis"


def _single_value(doctype: str, field: str):
	"""Read one row out of `tabSingles`.

	Deliberately raw SQL: `frappe.db.get_value` appends `ORDER BY creation`, and
	`tabSingles` has no `creation` column -- it is a bare (doctype, field, value)
	sheet -- so the ORM helper fails with "Unknown column 'creation'".
	"""
	row = frappe.db.sql(
		"select value from tabSingles where doctype = %s and field = %s",
		(doctype, field),
	)
	return row[0][0] if row else None


def execute():
	if not frappe.db.exists("DocType", NEW):
		# Nothing to merge into. Sync must have failed; do not destroy the source.
		return

	target = frappe.get_single(NEW)
	moved = []

	for old_dt, mapping in FIELD_MAP.items():
		if not frappe.db.exists("DocType", old_dt):
			continue

		for old_field, new_field in mapping.items():
			value = _single_value(old_dt, old_field)
			if value in (None, ""):
				continue
			# Do not clobber a value someone has already entered on the new form.
			if (target.get(new_field) or "") in ("", None):
				target.set(new_field, value)
				moved.append(f"{old_dt}.{old_field} -> {NEW}.{new_field}")

	# Passwords: move the encrypted row AND restore the masked placeholder.
	for old_dt, (old_field, new_field) in PASSWORD_MAP.items():
		if not frappe.db.exists("DocType", old_dt):
			continue

		row = frappe.db.sql(
			"""select `password` from `__Auth`
			   where doctype = %s and name = %s and fieldname = %s""",
			(old_dt, old_dt, old_field),
		)
		if not row or not row[0][0]:
			continue

		encrypted = row[0][0]
		frappe.db.sql(
			"""insert into `__Auth` (doctype, name, fieldname, `password`, encrypted)
			   values (%s, %s, %s, %s, 1)
			   on duplicate key update `password` = values(`password`), encrypted = 1""",
			(NEW, NEW, new_field, encrypted),
		)

		# The placeholder the connectors actually test before calling
		# get_password(). Without it the secret above is invisible to them.
		placeholder = _single_value(old_dt, old_field) or ("*" * 16)
		target.set(new_field, placeholder)
		moved.append(f"{old_dt}.{old_field} -> {NEW}.{new_field} (encrypted)")

	target.flags.ignore_mandatory = True
	target.flags.ignore_permissions = True
	target.save()

	for line in moved:
		print(f"  app_apis: migrated {line}")

	# Only now that everything is across: drop the old Singles.
	for old_dt in FIELD_MAP:
		if frappe.db.exists("DocType", old_dt):
			frappe.delete_doc("DocType", old_dt, force=True, ignore_permissions=True)
			frappe.db.delete("Singles", {"doctype": old_dt})
			frappe.db.sql("delete from `__Auth` where doctype = %s", (old_dt,))
			print(f"  app_apis: removed superseded doctype {old_dt}")

	frappe.db.commit()
