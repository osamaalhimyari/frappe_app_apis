"""Let Customize Form work on the app_apis settings Single.

Frappe refuses every Single in Customize Form ("Single DocTypes cannot be
customized"), though Custom Fields and Property Setters apply to a Single's
meta exactly as they do to any other doctype. This lifts the refusal for the
doctypes in CUSTOMIZABLE_SINGLES only; every other Single is still refused.

The one step in saving a customization that assumes a real table is the
"is existing data too long for the new fieldtype" check, which selects from
`tab<doctype>`. A Single has no such table (its values are TEXT rows in
`tabSingles`), so that check is skipped for these doctypes.
"""

import frappe
from frappe import _
from frappe.custom.doctype.customize_form.customize_form import CustomizeForm as FrappeCustomizeForm
from frappe.model import core_doctypes_list

CUSTOMIZABLE_SINGLES = ("app_apis",)


class CustomizeForm(FrappeCustomizeForm):
	def validate_doctype(self, meta):
		if self.doc_type in CUSTOMIZABLE_SINGLES:
			if meta.custom:
				frappe.throw(_("Only standard DocTypes are allowed to be customized from Customize Form."))
			return
		super().validate_doctype(meta)

	def validate_fieldtype_length(self):
		if self.doc_type in CUSTOMIZABLE_SINGLES:
			self.flags.update_db = True
			return
		super().validate_fieldtype_length()


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def doctype_query(doctype, txt, searchfield, start, page_len, filters):
	"""Customize Form's "Enter Form Type" list: frappe's usual choices plus
	CUSTOMIZABLE_SINGLES."""
	dt = frappe.qb.DocType("DocType")
	allowed = (dt.issingle == 0) | (dt.name.isin(CUSTOMIZABLE_SINGLES))
	domains = frappe.get_active_domains()
	domain = dt.restrict_to_domain.isnull() | (dt.restrict_to_domain == "")
	if domains:
		domain = domain | dt.restrict_to_domain.isin(domains)
	return (
		frappe.qb.from_(dt)
		.select(dt.name)
		.where(allowed)
		.where(dt.custom == 0)
		.where(dt.name.notin(core_doctypes_list))
		.where(domain)
		.where(dt.name.like(f"%{txt}%"))
		.orderby(dt.name)
		.limit(page_len)
		.offset(start)
		.run()
	)
