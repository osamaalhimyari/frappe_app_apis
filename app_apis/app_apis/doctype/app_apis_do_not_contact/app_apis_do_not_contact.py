# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One person or account that has asked not to be messaged.

Read by `app_apis.do_not_contact`, which every automatic send asks before it
writes to anybody. See that module for what is blocked, what deliberately is
not, and why a row is disabled rather than deleted.

`track_changes` is on: this is a record of somebody's stated wishes, and being
able to say when it was added, by whom, and when it was lifted is the point.
"""

import frappe
from frappe.model.document import Document

from app_apis.phone import normalise


class AppApisDoNotContact(Document):
	def validate(self):
		self.customer = (self.customer or "").strip() or None

		# Stored normalised so a lookup can compare like with like. The number
		# somebody types into this form comes off a Chatwoot conversation and
		# will be written every way a number can be written; matching raw text
		# against `driver_mobile` would miss almost every time.
		if self.phone:
			normalised = normalise(self.phone)
			if not normalised:
				frappe.throw(
					frappe._("{0} is not a usable phone number. Use the full "
					         "international form, for example +966512345678.").format(self.phone)
				)
			self.phone = normalised

		if not self.customer and not self.phone:
			frappe.throw(frappe._("Name a customer, a phone number, or both."))

		self._no_duplicate()

	def _no_duplicate(self):
		"""One row per (customer, phone, scope).

		Two rows saying the same thing is not harmful -- the check stops at the
		first match -- but it makes the list impossible to read, and somebody
		lifting a block would clear one row and be baffled that the messages
		kept coming.
		"""
		# ifnull on both sides, not a plain equality: an unset customer is NULL
		# in the database and NULL never equals the empty string, so a
		# phone-only row would happily be created twice.
		twin = frappe.db.sql(
			"""select name from `tab{dt}`
			   where ifnull(customer, '') = %(customer)s
			     and ifnull(phone, '') = %(phone)s
			     and scope = %(scope)s
			     and name != %(name)s
			   limit 1""".format(dt=self.doctype),
			{
				"customer": self.customer or "",
				"phone": self.phone or "",
				"scope": self.scope,
				"name": self.name or "",
			},
		)
		twin = twin[0][0] if twin else None
		if twin:
			frappe.throw(
				frappe._("{0} is already on the list for “{1}”.").format(
					self.customer or self.phone, self.scope
				),
				title=frappe._("Already listed"),
			)

	def on_update(self):
		self._drop_cache()

	def on_trash(self):
		self._drop_cache()

	def _drop_cache(self):
		from app_apis.do_not_contact import clear_cache

		clear_cache()
