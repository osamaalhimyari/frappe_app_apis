# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Carry the one-message automation over to the three-message rule table.

The first cut of this feature sent a single message -- the feedback link -- and
kept its trigger in a comma-separated `auto_valuation_states` field. It now
sends a message at each stage of a job, so the trigger became a table pairing a
status with a template, and the settings lost their `valuation` flavour.

Three jobs, in order:

  1. Rename the surviving scalar settings. A Single stores its values as rows in
     `tabSingles`, so a rename is an UPDATE on the `field` column rather than a
     schema change -- and doing it here is what stops the operator's `enabled`
     choice from silently resetting to the new field's default.
  2. Drop the two settings the table replaced.
  3. Seed the rule table, but only if it is empty, so a site that has already
     arranged its own stages is left alone.

Idempotent: safe on a fresh install, where step 1 finds nothing to rename and
step 3 does all the work.
"""

import frappe

RENAMES = {
	"auto_valuation_enabled": "auto_message_enabled",
	"auto_valuation_customer_types": "auto_message_customer_types",
}

# Replaced by the rule table: the state list became the rows, and "once" became
# a column on each row.
#
# `auto_valuation_language` used to be renamed to `auto_message_language` and is
# now simply dropped: each message carries Arabic and English together, so there
# is no longer anything for a language setting to choose between. Retiring it
# here rather than leaving the rename in place keeps this patch from writing a
# row that `merge_message_languages` would only have to delete again.
RETIRED = ("auto_valuation_states", "auto_valuation_once", "auto_valuation_language")


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	done = []

	for old, new in RENAMES.items():
		old_value = frappe.db.sql(
			"select value from tabSingles where doctype=%s and field=%s", ("app_apis", old)
		)
		if not old_value:
			continue
		# The new row may already exist from the doctype default. The operator's
		# old choice is the one that means something, so it wins.
		frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", ("app_apis", new))
		frappe.db.sql(
			"update tabSingles set field=%s where doctype=%s and field=%s", (new, "app_apis", old)
		)
		done.append(f"{old} -> {new}")

	for field in RETIRED:
		if frappe.db.sql("select value from tabSingles where doctype=%s and field=%s", ("app_apis", field)):
			frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", ("app_apis", field))
			done.append(f"dropped {field}")

	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	# Read the table straight from its own tabDoctype rather than through
	# get_single: the Single's cached doc may still be the pre-rename shape.
	existing = frappe.db.count("App Apis Auto Message", {"parent": "app_apis"})
	if not existing:
		from app_apis.auto_messages import DEFAULT_RULES

		settings = frappe.get_single("app_apis")
		for rule in DEFAULT_RULES:
			settings.append("auto_message_rules", {"enabled": 1, **rule})

		# validate() on this doctype expects a complete Chatwoot configuration,
		# which a half-configured site will not have. Skipping it lets the patch
		# run there instead of aborting the whole migration.
		settings.flags.ignore_validate = True
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()
		frappe.clear_cache(doctype="app_apis")
		done.append(f"seeded {len(DEFAULT_RULES)} status rules")

	if done:
		print("app_apis: " + "; ".join(done))
