# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Company contact numbers, read from the `app_apis` settings doctype.

The numbers live in a child table on the settings Single, NOT in this file.
Nothing here hardcodes a phone number, and nothing should: the point of the
table is that the numbers can be added, reordered, relabelled or switched off
from the desk or from a script, with no deploy.

    # from a Server Script, a Client Script via frappe.call, or bench execute
    from app_apis.contacts import get_contacts
    get_contacts("ar")        # -> [{"label": "الدعم الفني", "phone": "+966…"}, …]

This is a leaf module on purpose. The public feedback page and the Chatwoot
connector both need the same list, and importing one from the other would tie
the website to the messaging integration.
"""

import frappe
from frappe.utils import cint

# Which language column to read a label from. Anything else falls back to ar,
# matching the rest of the app.
LANGUAGES = ("ar", "en")

# Both labels on one line, for the Chatwoot messages -- they carry Arabic and
# English together and have no single language to pick a column by. Matches
# app_apis.vehicle.BILINGUAL, which answers the same question for the plate.
BILINGUAL = "both"


def get_contacts(lang: str = "ar", public_only: bool = True) -> list[dict]:
	"""Contact numbers for display, in the order set on the settings form.

	`public_only` keeps rows whose "Show on Feedback Page" is off out of
	customer-facing surfaces, so an internal escalation number can live in the
	same table without being published.
	"""
	lang = lang if lang in LANGUAGES or lang == BILINGUAL else "ar"

	try:
		settings = frappe.get_cached_doc("app_apis")
	except frappe.DoesNotExistError:
		# The app can be installed before its settings Single is first saved.
		return []

	rows = []
	for row in settings.get("contact_numbers") or []:
		if public_only and not cint(row.show_on_valuation):
			continue

		phone = str(row.phone or "").strip()
		if not phone:
			continue

		# Falling back to the other language's label beats showing a bare
		# number: a half-translated row is still useful, a blank one is not.
		if lang == BILINGUAL:
			# Both, deduped -- a row labelled "Support" in both columns should
			# not read "Support / Support".
			seen, both = set(), []
			for code in LANGUAGES:
				value = str(row.get(f"label_{code}") or "").strip()
				if value and value not in seen:
					seen.add(value)
					both.append(value)
			label = " / ".join(both)
		else:
			label = str(row.get(f"label_{lang}") or "").strip()
			if not label:
				other = "en" if lang == "ar" else "ar"
				label = str(row.get(f"label_{other}") or "").strip()

		rows.append({
			"label": label,
			"phone": phone,
			# Strip everything a tel: link cannot use, so the number can be
			# written in the form however reads best.
			"tel": "+" + "".join(c for c in phone if c.isdigit()),
			"is_whatsapp": bool(cint(row.is_whatsapp)),
		})

	return rows


def format_contacts(lang: str = "ar", separator: str = "\n") -> str:
	"""The same numbers as a text block, for pasting into a message.

	Used for the {contacts} placeholder in the Chatwoot templates. Returns an
	empty string when nothing is configured, so a template that mentions
	contacts degrades to a message without them rather than one that says
	"None".
	"""
	lines = []
	for contact in get_contacts(lang):
		# WhatsApp is the brand name either way, so the bilingual line says it
		# once rather than "(WhatsApp / واتساب)".
		suffix = " (WhatsApp)" if contact["is_whatsapp"] and lang != "ar" else ""
		if contact["is_whatsapp"] and lang == "ar":
			suffix = " (واتساب)"
		lines.append(f"{contact['label']}: {contact['phone']}{suffix}" if contact["label"]
		             else f"{contact['phone']}{suffix}")

	return separator.join(lines)


@frappe.whitelist()
def contacts_for_desk(lang: str = "ar") -> dict:
	"""Whitelisted read, so a Client Script can render the same list.

	No role check: these are the numbers the company publishes to customers on
	a page that needs no account at all. Rows marked internal are excluded by
	`public_only`.
	"""
	return {"ok": True, "lang": lang, "contacts": get_contacts(lang)}
