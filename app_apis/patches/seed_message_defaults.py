# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Write the shipped wording into any message field still sitting blank.

`seed_blank_chatwoot_messages` does the same job, but it had already run on
this site before the `accepted` and `working` templates existed -- and a patch
only ever runs once, so those four fields were left empty. The code falls back
to `chatwoot_connector.TEMPLATES` so nothing was actually broken, but an
operator opening the form saw four empty boxes with no way to know what the
system would send in their place.

Named separately rather than editing the old patch, because editing a patch that
has already run changes nothing on the sites that ran it -- which is exactly the
trap that produced the empty fields in the first place.

Only blank fields are written, so any wording that has been edited survives.
The text is read from TEMPLATES rather than repeated here, so there stays one
place to change a message rather than three.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	from app_apis.chatwoot_connector import TEMPLATES

	settings = frappe.get_single("app_apis")
	filled = []

	for spec in TEMPLATES.values():
		for lang in ("en", "ar"):
			# The ar/en split is gone: each message is one bilingual field now.
			# `.get` rather than `[...]` so this patch is a no-op on a site that
			# installs the app after that change, instead of a KeyError mid-migrate.
			field = spec.get(f"{lang}_field")
			if not field or not spec.get(lang):
				continue
			if str(settings.get(field) or "").strip():
				continue
			settings.set(field, spec[lang])
			filled.append(field)

	if not filled:
		return

	# validate() on this doctype expects a complete Chatwoot configuration, which
	# a site that has only ever filled in message wording will not have. Skipping
	# it lets the patch run there rather than aborting the whole migration.
	settings.flags.ignore_validate = True
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	print(f"app_apis: wrote default wording into -> {', '.join(filled)}")
