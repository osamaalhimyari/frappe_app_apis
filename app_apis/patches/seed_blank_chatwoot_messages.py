# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Fill blank Chatwoot message fields with the shipped defaults.

A doctype `default` only applies to records created after it, and the settings
Single almost always predates a newly added field -- so on an upgraded site the
new message fields sit empty while a fresh install gets the text. The code does
fall back to chatwoot_connector.TEMPLATES, but an operator opening the form sees
an empty box and no way to know what would actually be sent.

Only blank fields are written, so wording anyone has edited survives untouched.
Reads the text straight from TEMPLATES rather than repeating it, so there is one
place to change a message and not three.
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

	# Stored as 0 on sites that saved the form before this field had a default,
	# which only works because the code substitutes 20. Make the data honest.
	if not frappe.utils.cint(settings.chatwoot_request_timeout):
		settings.chatwoot_request_timeout = 20
		filled.append("chatwoot_request_timeout")

	if not filled:
		return

	# flags rather than a plain save: this runs during migrate, where the
	# doctype's validate() would demand a full Chatwoot configuration on a site
	# that has only ever filled in message wording.
	settings.flags.ignore_validate = True
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	print(f"app_apis: filled blank settings -> {', '.join(filled)}")
