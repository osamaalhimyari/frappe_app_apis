# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One month's SIM cost, kept after the snapshot that produced it is gone.

`app_apis_fleet_audit` is truncated and rebuilt on every run, so it can only
ever answer "what does this cost today". This table answers the question that
actually gets asked -- is the waste going up or down -- by keeping one row per
month per plan per carrier. A few dozen rows a year.

Written only by `app_apis.fleet_audit._record_cost_history`, at the end of
every audit run. Every field is read_only and the doctype is `in_create`: a row
here is evidence of what the estate cost in a given month, and an editable row
is worth nothing as evidence.

`price` is the tariff AS IT WAS at the moment of the reading, stored rather
than looked up. Recomputing an old month against today's price list would
silently rewrite history every time somebody corrected a tariff -- which is
exactly the kind of quiet change a cost trend exists to make visible.
"""

import frappe
from frappe.model.document import Document


class app_apis_cost_history(Document):
	pass
