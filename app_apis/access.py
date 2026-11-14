"""Who may use app_apis.

The code keeps no role lists. Two rules hold everywhere:

* only staff logins (System Users) get in -- Guest and portal (Website User) logins are refused;
* each action belongs to a group (GROUPS). The site decides which roles may use a group, in
  app_apis settings > Access. A group with no rows there is open to every staff login, so an
  untouched site behaves exactly as before. Administrator and System Manager always pass, so
  nobody can lock the site out of its own settings.

The rules live in the site's database, never in the code, so an app update never changes who
may do what. Record-level access (who may open a Customer Vehicle, the settings page, the logs)
stays with Role Permission Manager as usual.

Server Scripts cannot import this module; they ask `app_apis.access.can` through frappe.call.
"""

import frappe
from frappe import _

SETTINGS = "app_apis"

# The action groups, as they appear in the Access table. The text is the stored value: keep it.
VIEW = "View platform data"
UPLOAD = "Upload and edit vehicles"
DELETE = "Delete and block vehicles"
WASL = "WASL link and delete"
LEBARA = "Lebara SIM changes"
MESSAGES = "Send messages"
REPORTS = "Run reports and syncs"
ADMIN = "Admin tools"

GROUPS = (VIEW, UPLOAD, DELETE, WASL, LEBARA, MESSAGES, REPORTS, ADMIN)

ALWAYS = ("System Manager",)


def is_staff(user: str | None = None) -> bool:
	"""True for a signed-in System User. False for Guest and for portal (Website User) logins."""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False
	if user == "Administrator":
		return True
	return frappe.get_cached_value("User", user, "user_type") == "System User"


def require_login() -> None:
	"""Refuse anyone who is not a staff login. For building blocks every staff action needs
	(the HTTP helper, labels for a form); an action that does something uses `require(group)`."""
	if frappe.session.user == "Guest":
		frappe.throw(_("Please sign in first."), frappe.PermissionError)
	if not is_staff():
		frappe.throw(_("This is for staff accounts only."), frappe.PermissionError)


def rules() -> dict:
	"""{group: [role, ...]} from app_apis settings > Access. Groups without rows are left out."""
	try:
		doc = frappe.get_cached_doc(SETTINGS)
	except Exception:
		return {}
	out = {}
	for row in doc.get("access_rules") or []:
		group, role = (row.get("action_group") or "").strip(), (row.get("role") or "").strip()
		if group and role:
			out.setdefault(group, []).append(role)
	return out


def roles_for(group: str) -> list:
	"""The roles allowed to use `group`; [] means every staff login."""
	return rules().get(group, [])


def allowed(group: str, user: str | None = None) -> bool:
	"""May `user` (default: the session user) use this group of actions?"""
	if group not in GROUPS:
		raise ValueError("Unknown app_apis access group %r" % group)
	user = user or frappe.session.user
	if not is_staff(user):
		return False
	if user == "Administrator":
		return True
	wanted = roles_for(group)
	if not wanted:
		return True
	have = set(frappe.get_roles(user))
	return bool(have & set(wanted)) or bool(have & set(ALWAYS))


def require(group: str) -> None:
	"""Throw PermissionError unless the session user may use `group`."""
	require_login()
	if not allowed(group):
		frappe.throw(
			_("You do not have access to: {0}. Ask an admin to add one of your roles to this group in app_apis settings > Access.").format(_(group)),
			frappe.PermissionError,
		)


@frappe.whitelist()
def can(group: str) -> bool:
	"""For Server Scripts and forms: may the session user use `group`? Never throws for a staff login."""
	require_login()
	return allowed(group)


@frappe.whitelist()
def my_access() -> dict:
	"""{group: True/False} for the session user, so a form can hide buttons it would refuse anyway."""
	require_login()
	return {g: allowed(g) for g in GROUPS}
