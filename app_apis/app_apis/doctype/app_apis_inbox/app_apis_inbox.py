# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One Chatwoot inbox to search for a customer's open conversation.

A table rather than a single Inbox ID, because a customer may have written to
either of two WhatsApp numbers and the reply has to go out on the one they
actually used. The send path reads the enabled rows here and posts into
whichever listed inbox already holds an open conversation for the contact.
"""

from frappe.model.document import Document


class AppApisInbox(Document):
	pass
