# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Move Valuation's `technician*` columns onto their `engineer*` names.

The role is called the installation engineer everywhere now -- on the public
feedback page, in the Chatwoot templates, and on this doctype -- so the
fieldnames follow. Ratings were renamed at the same time to make the three
sections symmetric: engineer_rating / speed_rating / company_rating.

Runs post_model_sync, so by the time it executes the doctype sync has already
ADDED the new columns (empty) and LEFT the old ones in place -- frappe never
drops a column it no longer recognises. That is why this copies and then drops
rather than calling rename_field, which cannot rename onto a name that already
exists.

Idempotent: a pair is only processed while the old column is still there.
"""

import frappe

# (old column, new column). Order is irrelevant; each pair is independent.
RENAMES = (
	("rating", "engineer_rating"),
	("description", "engineer_description"),
	("technician_name", "engineer_name"),
	("technician", "engineer_driver"),
)


def execute():
	if not frappe.db.table_exists("Valuation"):
		return

	columns = {row.Field for row in frappe.db.sql("DESC `tabValuation`", as_dict=True)}

	for old, new in RENAMES:
		if old not in columns:
			# Already migrated, or a fresh install that never had the old name.
			continue

		if new not in columns:
			# The sync should have created it. Leaving the old column alone is
			# the safe failure: the data is still there to migrate next time.
			frappe.log_error(
				f"Valuation.{new} missing; left Valuation.{old} in place",
				"rename_valuation_technician_to_engineer",
			)
			continue

		# Only fill blanks, so re-running can never overwrite a value written
		# through the new name since the last run.
		frappe.db.sql(
			f"UPDATE `tabValuation` SET `{new}` = `{old}` "
			f"WHERE (`{new}` IS NULL OR `{new}` = '') AND `{old}` IS NOT NULL"
		)
		frappe.db.sql_ddl(f"ALTER TABLE `tabValuation` DROP COLUMN `{old}`")

	frappe.db.commit()
	frappe.clear_cache(doctype="Valuation")
