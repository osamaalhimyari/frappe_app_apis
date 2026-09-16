# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Public valuation page -- `/valuation?t=<ticket>&k=<token>&e=<expiry>`.

Served to Guest. This is where the customer rates the installation engineer who
worked their ticket, how fast the installation was, and the company overall;
everything it is allowed to show or accept is decided by the token check below.
See app_apis/valuation.py for why the link is signed rather than the doctype
being opened up to Guest.

The page is bilingual and carries its own string table rather than calling
`_()`. Frappe's translations only cover strings that exist in an app's .csv
files, so `_("How did we do?")` would quietly render in English for an Arabic
customer -- exactly the audience this page is written for. Two languages, one
dict, no silent fallbacks.

The contact numbers in the footer are NOT written here. They come from the
child table on the `app_apis` settings doctype via app_apis.contacts, so they
can be changed from the desk or a script without touching this file.
"""

from urllib.parse import urlencode

import frappe
from frappe.sessions import get_csrf_token

from app_apis.contacts import get_contacts
from app_apis.valuation import (
	TOKEN_EXPIRED,
	TOKEN_OK,
	check_token,
	get_valuation,
)
from app_apis.vehicle import for_customer as vehicle_for_customer

# The page shows one ticket's live state and the customer's own submission, so
# it must never be served from the website cache to the next visitor.
no_cache = 1

# Arabic first: these links go to customers in Saudi. English is one tap away
# via the toggle, and an explicit `?lang=` or an English browser still wins.
DEFAULT_LANGUAGE = "ar"

# Each language names *the other one* in its own script, so the toggle reads as
# an offer ("English") rather than a label for where you already are.
LANGUAGES = {
	"ar": {"dir": "rtl", "other": "en", "other_name": "English"},
	"en": {"dir": "ltr", "other": "ar", "other_name": "العربية"},
}

# Keys are looked up in Jinja as `t.<key>`, which tries attribute access before
# item access -- so a key that shares a name with a dict method (`update`,
# `items`, `keys`, `get`, `copy`, `pop`, `values`) silently renders the bound
# method instead of the string. Hence `submit_again` rather than `update`.
STRINGS = {
	"en": {
		"eyebrow": "Service Feedback",
		"title": "How did we do?",
		"intro": "Three quick ratings — it takes less than a minute.",
		"ticket": "Ticket",
		"customer": "Customer",
		"vehicle": "Vehicle",
		"engineer": "Installation Engineer",
		"status": "Status",
		"engineer_heading": "The installation engineer",
		"engineer_hint": "The person who carried out the work.",
		"engineer_comment": "Comments about the engineer",
		"engineer_placeholder": "Punctuality, professionalism, quality of the work…",
		"speed_heading": "Installation speed",
		"speed_hint": "How quickly the installation was completed.",
		"company_heading": "The company",
		"company_hint": "Your experience with us overall.",
		"company_comment": "Comments about the company",
		"company_placeholder": "Booking, communication, follow-up…",
		"optional": "optional",
		"submit": "Submit Feedback",
		"submit_again": "Update Feedback",
		"sending": "Sending…",
		"already": "You have already rated this service. You can change your answers below.",
		"validity": "This link stays open for 24 hours.",
		"contacts_heading": "Need us? Get in touch",
		"err_missing": "Please choose a rating in all three sections.",
		"err_failed": "Sorry, that did not go through. Please try again.",
		"err_expired": "This link has expired. Please ask us for a new one.",
		"ok_title": "Thank you!",
		"ok_body": "Your feedback has been recorded. It goes straight to the team that handled your ticket.",
		"ok_contacts": "Whenever you need us:",
		"ok_close": "Done",
		"expired_title": "This link has expired",
		"expired_body": "Feedback links stay open for 24 hours. Please contact us and we will send you a new one.",
		"stars": ["Poor", "Fair", "Good", "Very good", "Excellent"],
	},
	"ar": {
		"eyebrow": "تقييم الخدمة",
		"title": "كيف كانت خدمتنا؟",
		"intro": "ثلاثة تقييمات سريعة — لن يستغرق الأمر أكثر من دقيقة.",
		"ticket": "رقم الطلب",
		"customer": "العميل",
		"vehicle": "المركبة",
		"engineer": "مهندس التركيب",
		"status": "الحالة",
		"engineer_heading": "مهندس التركيب",
		"engineer_hint": "الشخص الذي قام بتنفيذ العمل.",
		"engineer_comment": "ملاحظاتك عن المهندس",
		"engineer_placeholder": "الالتزام بالموعد، الاحترافية، جودة العمل…",
		"speed_heading": "سرعة التركيب",
		"speed_hint": "مدى سرعة إنجاز عملية التركيب.",
		"company_heading": "الشركة",
		"company_hint": "تجربتك معنا بشكل عام.",
		"company_comment": "ملاحظاتك عن الشركة",
		"company_placeholder": "الحجز، التواصل، المتابعة…",
		"optional": "اختياري",
		"submit": "إرسال التقييم",
		"submit_again": "تحديث التقييم",
		"sending": "جارٍ الإرسال…",
		"already": "لقد قمت بتقييم هذه الخدمة مسبقاً، ويمكنك تعديل إجاباتك أدناه.",
		"validity": "هذا الرابط صالح لمدة ٢٤ ساعة.",
		"contacts_heading": "تحتاج مساعدة؟ تواصل معنا",
		"err_missing": "الرجاء اختيار تقييم في الأقسام الثلاثة.",
		"err_failed": "عذراً، لم يتم إرسال التقييم. الرجاء المحاولة مرة أخرى.",
		"err_expired": "انتهت صلاحية هذا الرابط. الرجاء طلب رابط جديد منا.",
		"ok_title": "شكراً لك!",
		"ok_body": "تم تسجيل تقييمك بنجاح، وسيصل مباشرة إلى الفريق الذي تولى طلبك.",
		"ok_contacts": "لأي استفسار يمكنك التواصل معنا عبر:",
		"ok_close": "إغلاق",
		"expired_title": "انتهت صلاحية هذا الرابط",
		"expired_body": "روابط التقييم صالحة لمدة ٢٤ ساعة. الرجاء التواصل معنا وسنرسل لك رابطاً جديداً.",
		"stars": ["ضعيف", "مقبول", "جيد", "جيد جداً", "ممتاز"],
	},
}


def get_context(context):
	ticket = frappe.form_dict.get("t")
	token = frappe.form_dict.get("k")
	expires = frappe.form_dict.get("e")

	lang = _pick_language()
	_apply_language(context, lang, ticket, token, expires)

	# Shown on every state of this page, including the expired one: a customer
	# whose link died is exactly who needs a number to call.
	context.contacts = get_contacts(lang)

	status = check_token(ticket, token, expires)

	# An expired link was genuinely issued by us, so it is safe -- and much
	# kinder -- to say so plainly instead of showing a dead end.
	if status == TOKEN_EXPIRED:
		context.expired = True
		return context

	# One response for a bad signature AND for a ticket that does not exist:
	# distinguishing them would let anyone probe which ticket names are real.
	if status != TOKEN_OK or not frappe.db.exists("xticket", ticket):
		raise frappe.PermissionError("This valuation link is not valid or has expired.")

	# Deliberately unchecked: the caller is Guest and has no roles at all. The
	# token verified above is what authorises this single read.
	doc = frappe.get_doc("xticket", ticket)

	context.expired = False
	context.ticket = doc.name
	context.token = token
	context.expires = expires
	context.state = doc.workflow_state
	context.customer = doc.get("translated_customer_name") or doc.get("customer")
	context.engineer_name = _engineer_name(doc)

	# The plate as written in the language this page is rendering in, and only
	# for a customer type that is shown one -- an individual sees the car the
	# visit was about, a company contact would see one plate out of a fleet and
	# learn nothing. Empty for everyone else, and the row is dropped rather than
	# shown blank. Same rule and same source as the {vehicle} line in the
	# message that carried this link, so the two never disagree.
	context.vehicle = vehicle_for_customer(doc, lang)

	# Whatever the customer submitted before, so the form comes back filled in
	# rather than pretending nothing was sent.
	context.existing = get_valuation(doc.name)

	# Frappe only enforces CSRF when the session already carries a token.
	# Minting one here means the POST below is checked rather than waved
	# through just because the visitor is a Guest.
	context.csrf_token = get_csrf_token()

	return context


def _pick_language():
	"""Which of the two languages to render in.

	An explicit `?lang=` wins -- that is the toggle, and a deliberate choice
	outranks any guess. Otherwise fall back to whatever frappe already resolved
	from the visitor's Accept-Language header and the site default, and to
	Arabic when that is neither of the two.
	"""
	requested = (frappe.form_dict.get("lang") or "").lower()
	if requested in LANGUAGES:
		return requested

	resolved = (frappe.local.lang or "").lower()
	for code in LANGUAGES:
		if resolved.startswith(code):
			return code

	return DEFAULT_LANGUAGE


def _apply_language(context, lang, ticket, token, expires):
	"""Put the chosen language, its direction, and the toggle into the context."""
	meta = LANGUAGES[lang]

	frappe.local.lang = lang

	context.no_cache = 1
	context.show_sidebar = False
	context.lang = lang
	context.text_dir = meta["dir"]
	context.t = STRINGS[lang]
	context.title = STRINGS[lang]["title"]

	# The toggle is this same link with the other language pinned on it, so the
	# choice survives a reload and can be shared as-is.
	query = {"t": ticket or "", "k": token or "", "e": expires or "", "lang": meta["other"]}
	context.toggle_url = f"/valuation?{urlencode(query)}"
	context.toggle_label = meta["other_name"]


def _engineer_name(doc):
	"""Best available human name for whoever worked the ticket.

	`assigned_full_name` first: `assigned_to` is set on ~97% of tickets while
	the legacy `technician` Driver link -- the xticket's own fieldname, which
	belongs to the site rather than this app -- is set on well under 1%, so
	keying the page off Driver would leave almost every customer rating a blank.
	"""
	return doc.get("assigned_full_name") or doc.get("technician") or doc.get("assigned_to")
