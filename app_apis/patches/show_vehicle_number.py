# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Turn on the vehicle number: seed its customer-type rule, add {vehicle} to the
message wording that has not been edited.

Two jobs, one patch, because they are one feature -- a site that got the rule
without the placeholder would show the plate on the feedback page and not in the
message that carried the link to it.

The wording half is deliberately conservative. The only change made to the
shipped templates was inserting "{vehicle}\\n\\n", so the pre-vehicle text is
derived by removing it again rather than frozen as six more copies here: one
source of truth, and no chance of a stray character making the comparison fail
and silently skipping the upgrade.

A field an operator has reworded is left exactly as it is and reported instead.
Editing somebody's message text on their behalf during a migration is not a
migration, and a message is the one thing on this site a customer actually
reads.
"""

import frappe

# What `show_vehicle_customer_types` starts life as. The doctype carries the
# same default, but a `default` only applies to records created after it and
# this Single already exists -- so without this the field would read blank on
# the form even though the code treats blank as Individual.
DEFAULT_VEHICLE_TYPES = "Individual"

# The exact fragment added to each shipped template. Removing it from the new
# default reconstructs the old one.
VEHICLE_LINE = "{vehicle}\n\n"


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	from app_apis.chatwoot_connector import TEMPLATES

	settings = frappe.get_single("app_apis")
	changed, upgraded, untouched = [], [], []

	if not str(settings.get("show_vehicle_customer_types") or "").strip():
		settings.set("show_vehicle_customer_types", DEFAULT_VEHICLE_TYPES)
		changed.append("show_vehicle_customer_types")

	for spec in TEMPLATES.values():
		for lang in ("en", "ar"):
			# The ar/en split is gone: each message is one bilingual field now.
			# `.get` rather than `[...]` so this patch is a no-op on a site that
			# installs the app after that change, instead of a KeyError mid-migrate.
			field = spec.get(f"{lang}_field")
			if not field or not spec.get(lang):
				continue
			new = spec[lang]
			stored = str(settings.get(field) or "").strip()

			# Already carries the placeholder, or was never filled in. Nothing to
			# preserve either way.
			if "{vehicle}" in stored:
				continue

			if not stored or stored == new.replace(VEHICLE_LINE, "").strip():
				settings.set(field, new)
				changed.append(field)
				upgraded.append(field)
			else:
				untouched.append(field)

	if changed:
		# validate() on this doctype expects a complete Chatwoot configuration,
		# which a site that has only ever filled in message wording will not
		# have. Skipping it lets the patch run there rather than aborting the
		# whole migration.
		settings.flags.ignore_validate = True
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()
		frappe.clear_cache(doctype="app_apis")

	if upgraded:
		print(f"app_apis: added the vehicle line to -> {', '.join(upgraded)}")

	if untouched:
		# Not a failure, and not something to fix automatically -- but the
		# operator has to be told, or they will wonder why one of their messages
		# never mentions the vehicle.
		print(
			"app_apis: left your own wording alone in -> %s\n"
			"          add {vehicle} on its own line if you want the plate in them"
			% ", ".join(untouched)
		)
