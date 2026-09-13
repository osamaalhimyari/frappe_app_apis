# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""A WhatsApp template as Chatwoot last reported it.

Filled only by app_apis.whatsapp_templates.sync; nothing here is typed by hand,
because the source of truth is Meta, where templates are written and approved.
Kept as a real doctype rather than a hidden list so a Status Rules row can Link
to one -- which gives the row a searchable picker, and a save-time check that
the template it names exists.
"""

from frappe.model.document import Document


class AppApisWhatsAppTemplate(Document):
	pass
