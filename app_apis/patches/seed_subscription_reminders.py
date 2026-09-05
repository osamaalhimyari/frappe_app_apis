# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Seed the Subscription Reminders settings on a site that already exists.

A doctype `default` only applies to records created after it, and the settings
Single always predates a newly added field -- so without this the whole section
reads blank on the form. Blank is not harmless here: `subscription_reminder_hour`
reading empty would be hour 0, and `subscription_reminder_max_days_expired`
reading empty would mean "no upper limit", which on this site is 1,490 customers
whose subscription died more than a year ago.

The two switches are seeded OFF and dry-run ON, and this patch will never turn
them the other way. Starting to message 966 real customers is a decision
somebody makes on purpose in front of the log, not something a migration does on
their behalf while they read a changelog. Same reasoning as
`add_technician_messages`, which leaves 'Send to Technicians' off.

Only fields that are still blank are written, so a site that has already
configured this is left exactly as it is.
"""

import frappe

# Read from the module rather than repeated here, so the shipped defaults and
# what a migration writes cannot drift apart.
# Handled below (the two message bodies) or deliberately left blank, which for
# customer types means "every type".
MESSAGE_FIELDS = ("subscription_expiring_message", "subscription_reminder_message")
SKIP = ("subscription_reminder_customer_types",) + MESSAGE_FIELDS


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	from app_apis.subscription_reminders import DEFAULTS

	settings = frappe.get_single("app_apis")
	meta = frappe.get_meta("app_apis")
	filled = []

	for field, value in DEFAULTS.items():
		# The field has to exist: this patch may run on a site mid-upgrade,
		# before the doctype sync has added the section.
		if meta.get_field(field) is None or field in SKIP:
			continue
		if str(settings.get(field) or "").strip() != "":
			continue
		settings.set(field, value)
		filled.append(field)

	# The message bodies live on their fields so an operator can edit them, but
	# the code falls back to the same text either way -- seeding them is about
	# the form showing what will actually be sent rather than an empty box.
	from app_apis.subscription_reminders import MESSAGE_FALLBACK, MESSAGE_FIELD

	for kind, field in MESSAGE_FIELD.items():
		if meta.get_field(field) is None:
			continue
		if str(settings.get(field) or "").strip():
			continue
		settings.set(field, MESSAGE_FALLBACK[kind])
		filled.append(field)

	if not filled:
		return

	# validate() on this doctype expects a complete Chatwoot configuration,
	# which a site that has only ever filled in message wording will not have.
	settings.flags.ignore_validate = True
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	print(f"app_apis: seeded subscription reminder settings -> {', '.join(filled)}")
	print(
		"app_apis: 'Send Subscription Reminders' is OFF and 'Dry Run' is ON.\n"
		"          Tick the first, read App Apis Reminder Log, and only then untick\n"
		"          Dry Run. There are 9,053 expired vehicles across 966 customers."
	)
