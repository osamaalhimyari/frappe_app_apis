# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Add the IM cache fields to Customer.

The vehicle_upload_api Server Script works out which IM company a vehicle belongs to
from its IM Platform email, then remembers the answer on the Customer so the next
upload needs no lookup. This creates the three fields it remembers it in, if the
site does not have them yet. Read-only in the form: the script fills them.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

FIELDS = (
	("im_company_id", "IM Company ID"),
	("im_reseller_id", "IM Reseller ID"),
	("im_branch_id", "IM Branch ID"),
)


def execute():
	if not frappe.get_meta("Customer").has_field("email_im_platform"):
		return
	meta = frappe.get_meta("Customer")
	prev = "email_im_platform"
	for fieldname, label in FIELDS:
		if not meta.has_field(fieldname):
			create_custom_field("Customer", {
				"fieldname": fieldname,
				"label": label,
				"fieldtype": "Data",
				"insert_after": prev,
				"read_only": 1,
				"description": "Filled by vehicle_upload_api from the IM company whose username is this "
				               "Customer's IM Platform email.",
			})
		prev = fieldname
