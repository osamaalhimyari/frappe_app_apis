"""Who may use app_apis.

The app keeps no role lists: every signed-in user can reach every endpoint, page and
report it ships. Access is set on the site (Role Permission Manager, Customize Form,
workspace / block roles), not in the code, so an app update never changes it.

Guest is still turned away; the public links (valuation, shared fuel report) carry their
own signed key and are checked where they are served.
"""

import frappe
from frappe import _


def require_login() -> None:
	if frappe.session.user == "Guest":
		frappe.throw(_("Please sign in first."), frappe.PermissionError)
