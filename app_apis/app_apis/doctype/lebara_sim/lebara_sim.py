# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One Lebara B2B SIM, as the portal's SIM list last reported it.

Filled every hour by the "Lebara SIM Sync" Server Script (action sync_sims of
the "Lebara API" script), through app_apis.core.store.upsert. The rows are a
copy of Lebara's list, so the desk can search and page 30,000 SIMs without a
call to Lebara each time; live status, session and location are still read
from Lebara on demand. Named by Lebara's subscriber Id, which every SIM action
needs.
"""

from frappe.model.document import Document


class LebaraSIM(Document):
	pass
