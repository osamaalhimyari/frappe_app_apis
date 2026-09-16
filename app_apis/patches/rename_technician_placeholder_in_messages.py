# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Update Chatwoot message text that still says technician.

The `default` on a doctype field only applies to records created after it, and
the settings Single already existed -- so a site that had saved the earlier
wording keeps rendering a literal `{technician}` in its messages, since unknown
placeholders are deliberately left visible rather than blanked.

Only the exact old strings are touched. Wording an operator has since rewritten
around them is left alone, and a fresh install where the defaults are already
correct is a no-op.
"""

import frappe

FIELDS = (
	"chatwoot_message_en",
	"chatwoot_message_ar",
	"chatwoot_valuation_message_en",
	"chatwoot_valuation_message_ar",
)

REPLACEMENTS = (
	("{technician}", "{engineer}"),
	("Technician:", "Installation Engineer:"),
	("الفني:", "مهندس التركيب:"),
)


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	settings = frappe.get_single("app_apis")
	changed = []

	for field in FIELDS:
		text = settings.get(field)
		if not text:
			continue

		updated = text
		for old, new in REPLACEMENTS:
			updated = updated.replace(old, new)

		if updated != text:
			settings.set(field, updated)
			changed.append(field)

	if not changed:
		return

	# flags rather than save(): this runs during migrate, where the doctype's
	# own validate() would demand a full Chatwoot configuration on a site that
	# has only ever filled in the message wording.
	settings.flags.ignore_validate = True
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	print(f"app_apis: updated technician wording in {', '.join(changed)}")
