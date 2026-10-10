# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""One custom message: a short code, its text, and the WhatsApp template it goes out as.

Read by app_apis.core.messages (get / render / send), so a script of your own can say
`messages.send("BS", phone, {"customer": ...})` and the wording and template stay editable in settings."""

from frappe.model.document import Document


class AppApisCustomMessage(Document):
	pass
