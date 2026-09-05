# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class app_apis(Document):
	"""One Single doctype holding every connection this app makes.

	Pilot and IM used to own a settings doctype each, and Chatwoot would have
	been a third. They are merged here because they are connections of one app,
	not three apps -- an operator configuring this integration should see one
	screen, not hunt for others whose existence is only discoverable by reading
	the code.

	Every fieldname is prefixed `pilot_`, `im_` or `chatwoot_`. That is not
	decoration: all three have a base URL and a request timeout, so unprefixed
	names would collide and silently share a value between unrelated endpoints.
	"""

	def validate(self):
		# IM signs in with one account for the whole site, so a missing username
		# is a configuration error worth catching here rather than surfacing
		# later as an opaque 401 from IM. Pilot has no equivalent check: its
		# account comes off the ticket, not from this form.
		if (self.im_base_url or "").strip() and not (self.im_username or "").strip():
			frappe.throw(
				_("IM Username is required when an IM Base URL is set."),
				title=_("App APIs"),
			)

		# IM rate-limits live data to about one call per minute per account.
		# Letting these drop to 0 turns the ignition test into a request storm
		# that IM answers with "The call exceeded the limit".
		if frappe.utils.cint(self.im_min_call_interval_seconds) < 10:
			self.im_min_call_interval_seconds = 60
		if frappe.utils.cint(self.im_ignition_poll_seconds) < 30:
			self.im_ignition_poll_seconds = 90

		self._validate_pilot_admin()
		self._validate_pilot_admin2()
		self._validate_chatwoot()
		self._normalise_excluded_customers()

	def _normalise_excluded_customers(self):
		"""Rewrite the Excluded Customers grid into full international form.

		app_apis.do_not_contact normalises on read, so a locally-typed number
		already matched -- but the grid showed "0503434764" while the field said
		it stored "+966503434764", and a row that displays one thing and means
		another is how somebody loses an hour deciding the exclusion is broken.

		A row is rejected rather than quietly dropped: a number that cannot be
		made sense of is somebody who thinks they have been excluded and has
		not been, which is the worst outcome this table can produce.

		Child doctypes do not get their own `validate` called by Frappe, so this
		belongs on the parent even though the data belongs to the row.
		"""
		from app_apis.phone import normalise

		for row in self.get("excluded_customers") or []:
			row.customer = (row.customer or "").strip() or None

			if row.phone:
				cleaned = normalise(row.phone)
				if not cleaned:
					frappe.throw(
						_("Row {0}: {1} is not a usable phone number. Use the "
						  "full international form, for example +966512345678.")
						.format(row.idx, row.phone),
						title=_("Excluded Customers"),
					)
				row.phone = cleaned

			if not row.customer and not row.phone:
				frappe.throw(
					_("Row {0}: name a customer, a phone number, or both.").format(row.idx),
					title=_("Excluded Customers"),
				)

	def _validate_pilot_admin(self):
		"""Only checked when the admin connection is switched on.

		Same rule as Chatwoot, for the same reason: half-filled settings are a
		legitimate saved state while somebody is still collecting the login, and
		nothing calls Pilot until the enabled flag is set.
		"""
		if not frappe.utils.cint(self.pilot_admin_enabled):
			return

		missing = [
			label
			for label, value in (
				(_("Pilot Admin Base URL"), self.pilot_admin_base_url),
				(_("Pilot Admin Login"), self.pilot_admin_username),
				# Read through the field rather than get_password(): the password
				# may legitimately live in site_config instead of here.
				(_("Pilot Admin Password"), self.pilot_admin_password or frappe.conf.get("pilot_admin_password")),
			)
			if not str(value or "").strip()
		]

		if missing:
			frappe.throw(
				_("Pilot Admin is enabled but these are not set: {0}").format(", ".join(missing)),
				title=_("App APIs"),
			)

	def _validate_pilot_admin2(self):
		"""The second Pilot Administrator account -- a different estate, not a
		backup for the one above. Same rule: only checked once enabled."""
		if not frappe.utils.cint(self.pilot_admin2_enabled):
			return

		missing = [
			label
			for label, value in (
				(_("Pilot Admin Base URL (2)"), self.pilot_admin2_base_url),
				(_("Pilot Admin Login (2)"), self.pilot_admin2_username),
				(_("Pilot Admin Password (2)"),
				 self.pilot_admin2_password or frappe.conf.get("pilot_admin2_password")),
			)
			if not str(value or "").strip()
		]

		if missing:
			frappe.throw(
				_("Pilot Admin 2 is enabled but these are not set: {0}").format(", ".join(missing)),
				title=_("App APIs"),
			)

	def _validate_chatwoot(self):
		"""Only checked when Chatwoot is switched on.

		Half-filled settings are a legitimate saved state while an operator is
		still collecting the account id and token, so the enabled flag is what
		makes them mandatory -- and nothing sends until that flag is set.
		"""
		if not frappe.utils.cint(self.chatwoot_enabled):
			return

		missing = [
			label
			for label, value in (
				(_("Chatwoot Domain"), self.chatwoot_base_url),
				(_("Chatwoot Account ID"), self.chatwoot_account_id),
				# Read through the field rather than get_password(): the token
				# may legitimately live in site_config instead of here.
				(_("Chatwoot API Token"), self.chatwoot_api_token or frappe.conf.get("chatwoot_api_token")),
			)
			if not str(value or "").strip()
		]

		# The single `chatwoot_inbox_id` became the Chatwoot Inboxes table, so
		# what has to be true now is that at least one row is enabled -- an
		# empty table means a send has no inbox to open a conversation on and
		# fails at the far end with nothing on this form explaining why.
		enabled_inboxes = [
			row for row in (self.get("chatwoot_inboxes") or [])
			if frappe.utils.cint(getattr(row, "enabled", 1))
			and frappe.utils.cint(getattr(row, "inbox_id", 0))
		]
		if not enabled_inboxes:
			missing.append(_("an enabled row in Chatwoot Inboxes"))

		if missing:
			frappe.throw(
				_("Chatwoot is enabled but these are not set: {0}").format(", ".join(missing)),
				title=_("App APIs"),
			)

	def on_update(self):
		# Credentials or endpoint changed -> the cached IM token and any cached
		# snapshots describe the previous account. Drop them rather than serve
		# them. Imported lazily so a broken connector can never make this
		# doctype unsaveable.
		from app_apis.im_connector import clear_cache

		clear_cache()

		# The Pilot admin token is minted from the login, password and server
		# on this form. Change any of them and the cached token belongs to a
		# configuration that no longer exists.
		from app_apis.pilot_admin import clear_cache as clear_pilot_admin_cache

		clear_pilot_admin_cache()

		# An exclusion somebody just typed has to apply to the very next
		# message, not whenever the request cache happens to turn over.
		from app_apis.do_not_contact import clear_cache as clear_dnc_cache

		clear_dnc_cache()
