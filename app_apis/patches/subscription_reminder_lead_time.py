# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Move the reminder window from "after expiry" to "before it".

The first version of this feature only chased subscriptions that had ALREADY
lapsed, which is the least useful moment to mention it: by then the customer has
lost tracking and the conversation starts with an apology. The window now opens
`subscription_reminder_days_before` days ahead of the expiry date -- 50 by
default -- so they can renew without a gap.

Two settings are replaced rather than reinterpreted:

    subscription_reminder_min_days_expired  ->  (dropped)
    subscription_reminder_max_days_expired  ->  subscription_reminder_days_after

`min_days_expired` has no successor: "ignore anything that lapsed less than N
days ago" is the opposite question now, and silently turning it into a lead time
would give a site that had set it to 30 a 30-day warning it never asked for.
`max_days_expired` carries over unchanged -- it always meant "stop chasing after
this long", and it still does.

Removing a field from a Single leaves its value row behind, so both old
fieldnames are deleted from `tabSingles` here.

The new `subscription_expiring_message` is left to
`seed_subscription_reminders` and to the doctype default; this patch only moves
what already existed.
"""

import frappe

OLD_MAX = "subscription_reminder_max_days_expired"
NEW_AFTER = "subscription_reminder_days_after"
DROPPED = ("subscription_reminder_min_days_expired", OLD_MAX)


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	from app_apis.subscription_reminders import DEFAULTS

	carried = frappe.db.sql(
		"select value from tabSingles where doctype = 'app_apis' and field = %s", (OLD_MAX,)
	)

	settings = frappe.get_single("app_apis")
	meta = frappe.get_meta("app_apis")
	notes = []

	if meta.get_field(NEW_AFTER) and not str(settings.get(NEW_AFTER) or "").strip():
		# The operator's own number wins over the shipped default: they chose it
		# for a reason, and it means exactly what it meant before.
		value = carried[0][0] if carried else DEFAULTS[NEW_AFTER]
		settings.set(NEW_AFTER, value)
		notes.append(f"{NEW_AFTER} = {value}")

	if meta.get_field("subscription_reminder_days_before") and not str(
		settings.get("subscription_reminder_days_before") or ""
	).strip():
		value = DEFAULTS["subscription_reminder_days_before"]
		settings.set("subscription_reminder_days_before", value)
		notes.append(f"subscription_reminder_days_before = {value}")

	if notes:
		settings.flags.ignore_validate = True
		settings.flags.ignore_permissions = True
		settings.save()

	removed = frappe.db.sql(
		"delete from tabSingles where doctype = 'app_apis' and field in %(fields)s",
		{"fields": DROPPED},
	)
	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	if notes:
		print("app_apis: reminder window -> %s" % ", ".join(notes))
	print(
		"app_apis: reminders now warn BEFORE expiry. Check "
		"'Warn Before Expiry (days)' in App Apis settings. (%s)"
		% (removed if removed else "old rows cleared")
	)
