# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Fold each template's Arabic and English fields into one bilingual field.

Twelve message boxes and a language picker became six boxes, because the choice
the picker was making was never a good one: it read a `language` field on the
Customer that nobody maintained, and got it wrong for anyone whose record said
one thing and whose reading said another. A WhatsApp message is read once, on a
phone, by somebody who reads one of the two alphabets -- writing both costs
three lines and removes the guess entirely.

What this patch does with wording somebody typed:

  - Both old fields still hold the SHIPPED text  -> write the new shipped text.
    Nothing was reworded, so nothing is lost, and the site gets the short
    bilingual version everybody else gets.
  - Either side was edited                       -> stack them, Arabic first,
    separated by a blank line, into the new field. It is their wording, and it
    is the one thing on this site a customer actually reads: a migration does
    not get to throw it away or "improve" it. The result is longer than the
    shipped text and reads a little repetitively, which is exactly the prompt
    an operator needs to open the field and trim it.
  - Both were blank                              -> leave the new field blank
    and let the doctype default fill it.

Removing a field from a Single leaves its value row behind in `tabSingles`, so
the old fieldnames are deleted here too -- otherwise `chatwoot_accepted_message_ar`
sits in the database forever, invisible on the form and confusing to anyone who
goes looking with a query. The two language pickers go the same way.

Idempotent: a new field that already holds text is never rewritten, so running
this twice does nothing the second time.
"""

import frappe

# old_ar, old_en -> new. Frozen here rather than read from
# chatwoot_connector.TEMPLATES: that constant no longer knows the old fieldnames,
# and a patch that describes a past shape has to carry it.
MERGES = (
	("chatwoot_accepted_message_ar", "chatwoot_accepted_message_en", "chatwoot_accepted_message"),
	("chatwoot_working_message_ar", "chatwoot_working_message_en", "chatwoot_working_message"),
	("chatwoot_valuation_message_ar", "chatwoot_valuation_message_en", "chatwoot_valuation_message"),
	("tech_accepted_message_ar", "tech_accepted_message_en", "tech_accepted_message"),
	("tech_working_message_ar", "tech_working_message_en", "tech_working_message"),
	("tech_done_message_ar", "tech_done_message_en", "tech_done_message"),
)

# The wording this app shipped in the split fields, exactly as
# chatwoot_connector.TEMPLATES held it the day before the merge. A stored value
# equal to one of these was never touched by anybody, so it can be replaced with
# the new shipped text rather than preserved.
#
# Two generations are listed for the customer templates because
# `show_vehicle_number` rewrote them in place: a site that upgraded across that
# patch holds the second form, and one that has not upgraded since holds the
# first. Both are "untouched" as far as an operator is concerned.
SHIPPED = {
	"chatwoot_accepted_message_ar": (
		"مرحباً {customer} 👋\n\n✅ تم قبول طلبك رقم {ticket}.\n\n"
		"🔧 مهندس التركيب: {engineer}\n\nسنوافيكم فور بدء العمل 🙏",
		"مرحباً {customer} 👋\n\n✅ تم قبول طلبك رقم {ticket}.\n\n{vehicle}\n\n"
		"🔧 مهندس التركيب: {engineer}\n\nسنوافيكم فور بدء العمل 🙏",
	),
	"chatwoot_accepted_message_en": (
		"Hello {customer} 👋\n\n✅ Your request {ticket} has been accepted.\n\n"
		"🔧 Installation Engineer: {engineer}\n\nWe will be in touch as soon as the work begins 🙏",
		"Hello {customer} 👋\n\n✅ Your request {ticket} has been accepted.\n\n{vehicle}\n\n"
		"🔧 Installation Engineer: {engineer}\n\nWe will be in touch as soon as the work begins 🙏",
	),
	"chatwoot_working_message_ar": (
		"مرحباً {customer} 👋\n\n🛠️ العمل جارٍ الآن على طلبك رقم {ticket}.\n\n"
		"🔧 مهندس التركيب: {engineer}\n\nسنبلغكم فور الانتهاء 🙏",
		"مرحباً {customer} 👋\n\n🛠️ العمل جارٍ الآن على طلبك رقم {ticket}.\n\n{vehicle}\n\n"
		"🔧 مهندس التركيب: {engineer}\n\nسنبلغكم فور الانتهاء 🙏",
	),
	"chatwoot_working_message_en": (
		"Hello {customer} 👋\n\n🛠️ Work on your request {ticket} is now under way.\n\n"
		"🔧 Installation Engineer: {engineer}\n\nWe will let you know as soon as it is complete 🙏",
		"Hello {customer} 👋\n\n🛠️ Work on your request {ticket} is now under way.\n\n{vehicle}\n\n"
		"🔧 Installation Engineer: {engineer}\n\nWe will let you know as soon as it is complete 🙏",
	),
	"chatwoot_valuation_message_ar": (
		"مرحباً {customer} 👋\n\n✅ تم إنجاز طلبك رقم {ticket} بواسطة {engineer}.\n\n"
		"⭐ نسعد بتقييمك للخدمة، ولن يستغرق الأمر أكثر من دقيقة:\n\n🔗 {link}\n\n"
		"⏰ هذا الرابط صالح لمدة ٢٤ ساعة.\n\nشكراً لثقتكم بنا 🙏",
		"مرحباً {customer} 👋\n\n✅ تم إنجاز طلبك رقم {ticket} بواسطة {engineer}.\n\n{vehicle}\n\n"
		"⭐ نسعد بتقييمك للخدمة، ولن يستغرق الأمر أكثر من دقيقة:\n\n🔗 {link}\n\n"
		"⏰ هذا الرابط صالح لمدة ٢٤ ساعة.\n\nشكراً لثقتكم بنا 🙏",
	),
	"chatwoot_valuation_message_en": (
		"Hello {customer} 👋\n\n✅ Your request {ticket} has been completed by {engineer}.\n\n"
		"⭐ We'd appreciate your feedback on our service — it takes less than a minute:\n\n"
		"🔗 {link}\n\n⏰ This link is valid for 24 hours.\n\nThank you for your trust 🙏",
		"Hello {customer} 👋\n\n✅ Your request {ticket} has been completed by {engineer}.\n\n{vehicle}\n\n"
		"⭐ We'd appreciate your feedback on our service — it takes less than a minute:\n\n"
		"🔗 {link}\n\n⏰ This link is valid for 24 hours.\n\nThank you for your trust 🙏",
	),
	"tech_accepted_message_ar": (
		"مرحباً {engineer} 👋\n\n📋 تم إسناد طلب جديد إليك: {ticket}\n\n"
		"👤 العميل: {customer}\n{vehicle}\n{location}\n{phone}\n\n"
		"الرجاء التواصل مع العميل لتحديد موعد الزيارة 🙏",
	),
	"tech_accepted_message_en": (
		"Hello {engineer} 👋\n\n📋 A new job has been assigned to you: {ticket}\n\n"
		"👤 Customer: {customer}\n{vehicle}\n{location}\n{phone}\n\n"
		"Please get in touch with the customer to arrange the visit 🙏",
	),
	"tech_working_message_ar": (
		"مرحباً {engineer} 👋\n\n🛠️ تم تحديث الطلب {ticket} إلى حالة العمل الجاري.\n\n"
		"👤 العميل: {customer}\n{vehicle}\n{location}\n\n"
		"الرجاء تحديث الطلب فور الانتهاء من التركيب 🙏",
	),
	"tech_working_message_en": (
		"Hello {engineer} 👋\n\n🛠️ Job {ticket} is now marked as in progress.\n\n"
		"👤 Customer: {customer}\n{vehicle}\n{location}\n\n"
		"Please update the ticket once the installation is complete 🙏",
	),
	"tech_done_message_ar": (
		"مرحباً {engineer} 👋\n\n✅ تم إغلاق الطلب {ticket}. شكراً لجهودك.\n\n"
		"👤 العميل: {customer}\n{vehicle}\n\nتم إرسال طلب تقييم التركيب إلى العميل ⭐",
	),
	"tech_done_message_en": (
		"Hello {engineer} 👋\n\n✅ Job {ticket} has been marked as done. Thank you.\n\n"
		"👤 Customer: {customer}\n{vehicle}\n\nThe customer has been asked to rate the installation ⭐",
	),
}

# The single "." each of the three customer templates was left holding after a
# round of send testing. Treated as untouched wording rather than as an edit:
# nobody meant a full stop to be the message a customer receives, and carrying
# it forward would keep sending it.
PLACEHOLDER_EDITS = (".", "-", "test", "…")

# Removed from the form, so their rows in `tabSingles` have to go too.
DROPPED = [old for pair in MERGES for old in pair[:2]] + [
	"chatwoot_default_language",
	"auto_message_language",
]


def _untouched(field: str, value: str) -> bool:
	"""Is this exactly what the app shipped, or obvious leftover test text?"""
	if value in PLACEHOLDER_EDITS:
		return True
	return any(value == shipped.strip() for shipped in SHIPPED.get(field, ()))


def execute():
	if not frappe.db.exists("DocType", "app_apis"):
		return

	from app_apis.chatwoot_connector import TEMPLATES

	# fieldname -> the new shipped bilingual wording.
	shipped_now = {spec["field"]: spec["text"] for spec in TEMPLATES.values()}

	settings = frappe.get_single("app_apis")
	merged, kept, skipped = [], [], []

	for old_ar, old_en, new in MERGES:
		# Already filled by a previous run, or by an operator who got there
		# first. Either way it is not this patch's to overwrite.
		if str(settings.get(new) or "").strip():
			skipped.append(new)
			continue

		ar = str(settings.get(old_ar) or "").strip()
		en = str(settings.get(old_en) or "").strip()

		edited = [
			text for field, text in ((old_ar, ar), (old_en, en))
			if text and not _untouched(field, text)
		]

		if not edited:
			# Nothing worth keeping. Blank is a valid answer -- the code falls
			# back to TEMPLATES -- but writing the text makes the form show what
			# it will actually send instead of an empty box.
			settings.set(new, shipped_now.get(new, ""))
			merged.append(new)
			continue

		# Arabic first, matching the reading order of everything else here.
		settings.set(new, "\n\n".join(edited))
		kept.append(new)

	settings.flags.ignore_validate = True
	settings.flags.ignore_permissions = True
	settings.save()

	# The form no longer shows these, so their values would otherwise sit in
	# tabSingles forever -- invisible, and misleading to anyone querying it.
	removed = frappe.db.sql(
		"""delete from `tabSingles` where doctype = 'app_apis' and field in %(fields)s""",
		{"fields": tuple(DROPPED)},
	)
	frappe.db.commit()
	frappe.clear_cache(doctype="app_apis")

	if merged:
		print(f"app_apis: wrote the shipped bilingual wording into -> {', '.join(merged)}")
	if kept:
		print(
			"app_apis: kept your own wording, stacked ar+en, in -> %s\n"
			"          open App Apis -> Chatwoot Messages and trim them: each now\n"
			"          holds both of your old messages one after the other."
			% ", ".join(kept)
		)
	if skipped:
		print(f"app_apis: already filled, left alone -> {', '.join(skipped)}")
	print(f"app_apis: dropped {len(DROPPED)} retired settings rows ({removed if removed else 'ok'})")
