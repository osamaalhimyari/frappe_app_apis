# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Subscription reminder wording moves from two fixed fields into the Reminder
Messages table, one row per kind, so each kind can also carry a WhatsApp
template for customers outside the 24-hour window.

Wording an operator edited is carried across exactly as it is; a field never
filled gets the shipped text. The two retired fieldnames are then deleted from
tabSingles, which removing a field from a Single does not do on its own.

Rows are written directly instead of through the settings document: saving
app_apis runs its whole validate(), and a site whose Chatwoot or IM settings
are half-filled would fail this migration over something unrelated to it.
Self-contained on purpose -- a patch that imports runtime code breaks the day
that code changes, on every site that has not migrated yet.
"""

import frappe

CHILD = "App Apis Subscription Message"

# Kind label in the new table -> the retired settings field that held its text.
RETIRED = {
	"Expiring soon": "subscription_expiring_message",
	"Already expired": "subscription_reminder_message",
}

SHIPPED = {
	"Expiring soon": (
		"👋 أهلاً {customer}\n\n"
		"⏰ اشتراك التتبع للمركبات التالية ({count}) قارب على الانتهاء:\n"
		"⏰ Tracking for these ({count}) is about to run out:\n"
		"{vehicles}\n\n"
		"🔄 جدّده من الحين وما ينقطع عنك التتبع ولا يوم 🚗💨\n"
		"🔄 Renew now and you won't lose a single day of tracking 🚗💨\n\n"
		"تواصل معنا وإحنا في خدمتك / Reach out any time, we're here to help 🤝\n"
		"{contacts}"
	),
	"Already expired": (
		"👋 أهلاً {customer}\n\n"
		"⏰ انتهى اشتراك التتبع للمركبات التالية ({count}):\n"
		"⏰ Tracking has expired for these ({count}):\n"
		"{vehicles}\n\n"
		"🔄 نجدده لك بخطوتين بس، وترجع تتابع مركباتك على طول 🚗💨\n"
		"🔄 A quick renewal and you're back to tracking — two steps 🚗💨\n\n"
		"تواصل معنا وإحنا في خدمتك / Reach out any time, we're here to help 🤝\n"
		"{contacts}"
	),
}


def execute():
	if not frappe.db.exists("DocType", CHILD):
		return
	if frappe.db.count(CHILD, {"parent": "app_apis", "parentfield": "subscription_messages"}):
		return

	for idx, (kind, field) in enumerate(RETIRED.items(), start=1):
		old = frappe.db.sql("select value from `tabSingles` where doctype='app_apis' and field=%s", field)
		text = str((old[0][0] if old else "") or "").strip()
		frappe.get_doc({
			"doctype": CHILD,
			"name": frappe.generate_hash(length=10),
			"parent": "app_apis",
			"parenttype": "app_apis",
			"parentfield": "subscription_messages",
			"idx": idx,
			"enabled": 1,
			"kind": kind,
			"message": text or SHIPPED[kind],
		}).db_insert()

	frappe.db.sql(
		"delete from `tabSingles` where doctype='app_apis' and field in %s", (tuple(RETIRED.values()),)
	)
	frappe.clear_cache(doctype="app_apis")
