# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AppApisContact(Document):
	"""One published contact number.

	A child of the `app_apis` settings Single. Deliberately dumb: the whole
	point of this table is that the numbers are data an operator or a script
	can change, so no logic here decides what a valid number looks like.
	"""

	pass
