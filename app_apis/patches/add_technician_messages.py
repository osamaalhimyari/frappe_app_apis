# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Add the engineer's side: seed the three technician templates and their rules.

The switch stays OFF. Turning on outbound messages to eighteen members of staff
is a decision somebody makes on purpose, not something a migration does on their
behalf while they are reading the changelog -- the same reason
`migrate_auto_valuation_to_auto_messages` left the customer switch off.

Rules are added, never replaced. An operator may have reworded, reordered or
unticked the customer rows, and a migration that rebuilt the table would throw
that away. A technician rule is only inserted if no row already names that
template, so running against a site that added its own rows by hand is a no-op.

Message wording is only written where the field is blank, so nothing an operator
typed is overwritten.
"""

import frappe

# state -> template, matching the customer stages already in the table. Kept
# here rather than read from auto_messages.DEFAULT_RULES so that changing the
# shipped defaults later cannot silently rewrite what this patch did on a site
# that already ran it.
TECHNICIAN_RULES = (
	{"state": "In Hand", "template": "tech_accepted", "send_once": 1},
	{"state": "Pending", "template": "tech_working", "send_once": 1},
	{"state": "Work's Done", "template": "tech_done", "send_once": 1},
)


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	from app_apis.chatwoot_connector import TEMPLATES, TO_TECHNICIAN

	settings = frappe.get_single("app_apis")
	filled, added = [], []

	for key, spec in TEMPLATES.items():
		if spec.get("to") != TO_TECHNICIAN:
			continue
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

	# Only meaningful once the table has been seeded at all. A site whose table
	# is still empty falls back to auto_messages.DEFAULT_RULES, which already
	# includes these three -- adding rows there would turn the fallback into a
	# half-populated table and lose the customer rules.
	if settings.get("auto_message_rules"):
		existing = {str(row.template or "").strip() for row in settings.auto_message_rules}
		for rule in TECHNICIAN_RULES:
			if rule["template"] in existing:
				continue
			settings.append("auto_message_rules", {"enabled": 1, **rule})
			added.append(rule["template"])

	if not filled and not added:
		return

	# validate() on this doctype expects a complete Chatwoot configuration,
	# which a site that has only ever filled in message wording will not have.
	settings.flags.ignore_validate = True
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	if filled:
		print(f"app_apis: wrote technician wording into -> {', '.join(filled)}")
	if added:
		print(f"app_apis: added status rules -> {', '.join(added)}")
	print(
		"app_apis: 'Send to Technicians' is OFF. Tick it in App Apis settings when "
		"you are ready, and make sure the engineers have a phone number "
		"(bench execute app_apis.technicians.missing_numbers)."
	)
