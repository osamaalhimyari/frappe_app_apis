# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Remove the two message templates nobody used.

The settings form had grown to ten message boxes -- five templates in two
languages -- and the three that actually fire were buried among them. The
generic `status` message and the post-feedback `thanks` message were both
dropped: nothing in the app ever called either one.

Dropping the fields from the doctype JSON is not enough on its own. A Single
keeps its values as rows in `tabSingles`, and removing a field from the doctype
leaves its row behind -- invisible in the form, still in the database, and still
returned by `get_single`. This deletes them.

Nothing is lost that mattered: `{contacts}`, the placeholder that made the
thanks message worth having, still works in any of the three remaining
templates, so the phone numbers can simply be appended to the feedback message.

Also clears any rule row that pointed at a template that no longer exists --
otherwise the automation would try to send a message it cannot render.
"""

import frappe

RETIRED_FIELDS = (
	"chatwoot_message_en",
	"chatwoot_message_ar",
	"chatwoot_thanks_message_en",
	"chatwoot_thanks_message_ar",
)

RETIRED_TEMPLATES = ("status", "thanks")


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	done = []

	for field in RETIRED_FIELDS:
		if frappe.db.sql(
			"select value from tabSingles where doctype=%s and field=%s", ("app_apis", field)
		):
			frappe.db.sql(
				"delete from tabSingles where doctype=%s and field=%s", ("app_apis", field)
			)
			done.append(field)

	# A rule naming a dead template would log a "Unknown message template"
	# error on every transition. Disable rather than delete, so whoever set it
	# up can see what happened and repoint it.
	if frappe.db.table_exists("App Apis Auto Message"):
		stale = frappe.db.sql(
			"""select name, template from `tabApp Apis Auto Message`
			   where parent='app_apis' and template in %(templates)s and enabled=1""",
			{"templates": RETIRED_TEMPLATES},
			as_dict=True,
		)
		for row in stale:
			frappe.db.set_value("App Apis Auto Message", row.name, "enabled", 0)
			done.append(f"disabled rule using '{row.template}'")

	if not done:
		return

	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")
	print(f"app_apis: retired unused message templates -> {', '.join(done)}")
