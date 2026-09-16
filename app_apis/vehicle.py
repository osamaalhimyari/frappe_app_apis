# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""The vehicle number on a ticket, and who is allowed to be shown it.

This site stores the plate twice, once per script:

    license_plate     Link to Customer Vehicle,  "9032 - أ ط ق"
    e_license_plate   Data,                      "9032 - G T A"

They are the same plate, transliterated -- Saudi plates carry both alphabets on
the metal. So which one to show is a language question, not a fallback chain: an
Arabic message must carry the Arabic plate, because that is the one the customer
reads off their own car. Picking `e_license_plate` first regardless of language,
which is what the Chatwoot context used to do, put Latin letters in an Arabic
sentence for every customer on the site.

Both fields belong to `xticket`, a doctype the site owns rather than this app, so
their names are read here and nowhere else -- one place to edit if the site
renames them.

Who gets shown a plate is `show_vehicle_customer_types` on the settings Single.
See app_apis/customers.py for why an individual is treated differently from a
company, and why blank means Individual rather than everybody.
"""

import frappe

from app_apis.customers import may_see_vehicle

# Best field first, per language. The other one is a genuine fallback: about 3%
# of tickets carry one plate and not the other, and a Latin plate in an Arabic
# message still beats a blank line where the vehicle should be.
PLATE_FIELDS = {
	"ar": ("license_plate", "e_license_plate"),
	"en": ("e_license_plate", "license_plate"),
}

# Fallback language for anything that is neither ar nor en, matching the rest of
# the app.
DEFAULT_LANGUAGE = "ar"

# "Write it for both readers at once." Asked for by the Chatwoot messages, which
# carry Arabic and English in a single bubble and so have no one language to
# label a line in. The plate itself still has to be one of the two -- a plate
# printed twice is noise, not a translation -- so this reads the Arabic form,
# the one stamped on the metal here and the one a customer checks it against.
BILINGUAL = "both"

PLATE_FIELDS[BILINGUAL] = PLATE_FIELDS["ar"]

# The label used by `line()`. A built-in, in the same sense the message text in
# chatwoot_connector.TEMPLATES is a built-in: an operator who wants different
# wording writes their own line in the message template using the bare {plate}
# placeholder instead of {vehicle}.
#
# The bilingual label is the emoji alone. "🚗 المركبة / Vehicle: 9032 - أ ط ق"
# would spend more of the line on saying "vehicle" twice than on the plate, and
# a car emoji needs neither alphabet.
LABELS = {
	"ar": "🚗 المركبة: ",
	"en": "🚗 Vehicle: ",
	BILINGUAL: "🚗 ",
}


def _lang(lang) -> str:
	code = str(lang or "").strip().lower()
	if code == BILINGUAL:
		return BILINGUAL
	# Truncated to two so a full locale -- "ar-SA", "en_GB" -- still lands on
	# its language. BILINGUAL is matched whole above, before this bites it.
	code = code[:2]
	return code if code in PLATE_FIELDS else DEFAULT_LANGUAGE


def plate(doc, lang: str = DEFAULT_LANGUAGE) -> str:
	"""The plate as written in `lang`, ignoring who is allowed to see it.

	Used where the audience is already known to be internal -- a desk preview,
	a log line. Customer-facing callers want `for_customer` below.
	"""
	if doc is None:
		return ""

	for field in PLATE_FIELDS[_lang(lang)]:
		value = str(doc.get(field) or "").strip()
		if value:
			return value

	return ""


def for_customer(doc, lang: str = DEFAULT_LANGUAGE, settings=None) -> str:
	"""The plate to show this ticket's customer, or "" if they get none.

	Empty covers both halves of the rule and does so identically: a company,
	whose office contact has no use for one vehicle out of a fleet, and a ticket
	that simply has no plate on it. Callers render nothing either way, so a
	missing plate can never leave a dangling label in a customer's message.
	"""
	if doc is None:
		return ""

	if not may_see_vehicle(doc.get("customer"), settings):
		return ""

	return plate(doc, lang)


def _labelled(value: str, lang: str) -> str:
	# The separator lives in the label, not here: the bilingual label is a bare
	# emoji, and "🚗: 9032" reads like a broken template.
	return f"{LABELS[_lang(lang)]}{value}" if value else ""


def line(doc, lang: str = DEFAULT_LANGUAGE, settings=None) -> str:
	"""One ready-made line -- "🚗 Vehicle: 9032 - G T A" -- or "".

	Returned whole, label included, so a template can carry {vehicle} on its own
	line and have the entire line disappear for a customer who is not shown one.
	A bare {plate} placeholder next to a label typed in the template would leave
	"Vehicle:" with nothing after it, which is the one outcome worth designing
	against: it looks like the system lost the data.
	"""
	return _labelled(for_customer(doc, lang, settings), lang)


def staff_line(doc, lang: str = DEFAULT_LANGUAGE) -> str:
	"""The same line for somebody who works here, with no customer-type gate.

	`show_vehicle_customer_types` answers "does this customer want to read their
	plate", which is a question about a customer. An engineer being sent to a
	job needs to know which vehicle whoever owns it -- a fleet job is exactly
	the case where guessing is worst -- so the gate is deliberately not applied
	on this side.
	"""
	return _labelled(plate(doc, lang), lang)


@frappe.whitelist()
def for_ticket(ticket: str, lang: str = DEFAULT_LANGUAGE) -> dict:
	"""What a ticket would show its customer. For previews and scripts.

	Gated on read access to the ticket: this reports a field already on the form
	to somebody who can open the form.
	"""
	doc = frappe.get_doc("xticket", ticket)
	doc.check_permission("read")

	language = _lang(lang)
	return {
		"ok": True,
		"ticket": doc.name,
		"lang": language,
		"plate": plate(doc, language),
		"shown_to_customer": bool(for_customer(doc, language)),
		"line": line(doc, language),
	}
