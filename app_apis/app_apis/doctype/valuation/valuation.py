# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

# Every star field on this doctype. Listed once so a new rating cannot be added
# to the form and quietly skip the clamp below.
RATING_FIELDS = ("engineer_rating", "speed_rating", "company_rating")


class Valuation(Document):
	"""One customer's rating of a ticket: the installation engineer, the speed
	of the installation, and the company.

	Written almost entirely by Guest, through the public link in
	app_apis/valuation.py -- the token in that link is the only thing standing
	between the internet and this table, so nothing here should assume a
	trusted caller.
	"""

	def validate(self):
		# Ratings are fractions (0-1) rendered as five stars. A value outside
		# that range renders as a broken widget rather than an error, so clamp
		# instead of trusting whatever reached us.
		for field in RATING_FIELDS:
			self.set(field, min(max(frappe.utils.flt(self.get(field)), 0), 1))
