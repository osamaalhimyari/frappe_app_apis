# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Remove the retired `xticket-chatwoot-test` Client Script.

It was a scratch button that sent the valuation link to a number hardcoded at
the top of the script. Everything it did now lives in `xticket-valuation-link`,
which picks the number off the ticket instead.

Dropping it from the fixture is not enough: fixtures import records, they never
delete them, so a site that already imported the test button would keep it --
and keep a button on the ticket form that messages a hardcoded number.
"""

import frappe

NAME = "xticket-chatwoot-test"


def execute():
	if not frappe.db.exists("Client Script", NAME):
		return

	frappe.delete_doc("Client Script", NAME, force=1, ignore_permissions=True)
	frappe.db.commit()
	print(f"app_apis: removed retired Client Script {NAME}")
