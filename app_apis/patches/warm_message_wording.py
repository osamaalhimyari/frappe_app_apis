# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Replace the first bilingual wording with the warmer version.

The messages that came out of `merge_message_languages` were correct and
unreadably cold -- "✅ تم قبول طلبك، وسنوافيك عند بدء العمل." is a status field
with a tick in front of it, not something a person says. This swaps them for
wording that sounds like the company rather than the database, at roughly the
same length.

A field is only rewritten when it still holds that first wording byte for byte.
Anything an operator has since reworded is reported and left exactly as it is:
rewriting somebody's customer-facing words during a migration is not a
migration, and the site's own voice beats a shipped default every time.

A field that is blank is filled, for the same reason `seed_blank_chatwoot_messages`
fills them -- the code already falls back to the new text, and an empty box on
the form tells an operator nothing about what will actually be sent.

The seventh message here is the subscription reminder, which is not in
chatwoot_connector.TEMPLATES: it is not a ticket message and has its own
placeholders. It is swapped by the same rule.
"""

import frappe

# The wording being replaced, frozen. Read from nothing: this describes a past
# state, and a patch that asks the current code what the old text was will start
# matching nothing the next time the shipped wording changes.
COLD = {
	"chatwoot_accepted_message": (
		"👋 {customer}\n🎫 {ticket}\n{vehicle}\n🔧 {engineer}\n\n"
		"✅ تم قبول طلبك، وسنوافيك عند بدء العمل.\n"
		"✅ Your request is accepted — we'll update you when work starts."
	),
	"chatwoot_working_message": (
		"👋 {customer}\n🎫 {ticket}\n{vehicle}\n🔧 {engineer}\n\n"
		"🛠️ العمل جارٍ الآن، وسنبلغك فور الانتهاء.\n"
		"🛠️ Work is under way — we'll let you know when it's done."
	),
	"chatwoot_valuation_message": (
		"👋 {customer}\n🎫 {ticket}\n{vehicle}\n🔧 {engineer}\n\n"
		"⭐ تم إنجاز طلبك. نسعد بتقييمك خلال ٢٤ ساعة.\n"
		"⭐ Your request is complete. Please rate our service within 24 hours.\n\n"
		"🔗 {link}"
	),
	"tech_accepted_message": (
		"👋 {engineer}\n🎫 {ticket}\n👤 {customer}\n{vehicle}\n{location}\n{phone}\n\n"
		"📋 طلب جديد مُسند إليك — تواصل مع العميل لتحديد الموعد.\n"
		"📋 New job assigned — contact the customer to arrange the visit."
	),
	"tech_working_message": (
		"👋 {engineer}\n🎫 {ticket}\n👤 {customer}\n{vehicle}\n{location}\n\n"
		"🛠️ الطلب قيد التنفيذ — حدّثه فور انتهاء التركيب.\n"
		"🛠️ Job in progress — update it once the installation is done."
	),
	"tech_done_message": (
		"👋 {engineer}\n🎫 {ticket}\n👤 {customer}\n{vehicle}\n\n"
		"✅ تم إغلاق الطلب، شكراً لجهودك. أُرسل طلب التقييم للعميل ⭐\n"
		"✅ Job closed — thank you. The customer has been asked to rate it ⭐"
	),
	"subscription_reminder_message": (
		"👋 {customer}\n{vehicles}\n\n"
		"⚠️ انتهى اشتراك التتبع للمركبات أعلاه ({count}). للتجديد تواصل معنا.\n"
		"⚠️ Tracking subscription expired for the vehicles above ({count}). Contact us to renew.\n\n"
		"{contacts}"
	),
}


def _warm() -> dict:
	"""fieldname -> the current shipped wording, read from the code.

	Read rather than frozen, unlike COLD: what this patch writes should be
	whatever the app ships today, so a site upgrading across two rounds of
	wording lands on the current one instead of an intermediate.
	"""
	from app_apis.chatwoot_connector import TEMPLATES
	from app_apis.subscription_reminders import (
		DEFAULT_EXPIRED_MESSAGE,
		DEFAULT_EXPIRING_MESSAGE,
	)

	warm = {spec["field"]: spec["text"] for spec in TEMPLATES.values()}
	warm["subscription_reminder_message"] = DEFAULT_EXPIRED_MESSAGE
	# Not in COLD -- this field did not exist while the cold wording did, so it
	# is only ever filled here when it is blank, never swapped.
	warm["subscription_expiring_message"] = DEFAULT_EXPIRING_MESSAGE
	return warm


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	warm = _warm()
	settings = frappe.get_single("app_apis")
	meta = frappe.get_meta("app_apis")
	swapped, filled, kept = [], [], []

	# COLD drives the loop, so a field with no cold ancestor is reached through
	# `warm` below rather than here.
	for field, cold in list(COLD.items()) + [
		(f, None) for f in warm if f not in COLD
	]:
		# The field may not exist yet on a site part-way through its upgrade.
		if meta.get_field(field) is None or field not in warm:
			continue

		stored = str(settings.get(field) or "").strip()
		if not stored:
			settings.set(field, warm[field])
			filled.append(field)
		elif cold is not None and stored == cold.strip():
			settings.set(field, warm[field])
			swapped.append(field)
		else:
			kept.append(field)

	if not swapped and not filled:
		return

	# validate() on this doctype expects a complete Chatwoot configuration,
	# which a site that has only ever filled in message wording will not have.
	settings.flags.ignore_validate = True
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	if swapped:
		print(f"app_apis: warmed up the wording in -> {', '.join(swapped)}")
	if filled:
		print(f"app_apis: filled blank messages -> {', '.join(filled)}")
	if kept:
		print(
			"app_apis: left your own wording alone in -> %s" % ", ".join(kept)
		)
