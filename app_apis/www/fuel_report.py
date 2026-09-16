# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Public Fuel Efficiency report -- `/fuel_report?k=<key>`.

Served to Guest. The key is the report's own share key, made and stopped from
the dashboard (app_apis.fuel_efficiency.share_link): it opens that one report,
read-only, and nothing else. The page runs the dashboard's own block, with a
small stand-in for the desk's `frappe` object whose calls all go through
app_apis.fuel_efficiency.public_call, which forces this report into every read.
"""

import frappe

from app_apis.fuel_efficiency import _shared_import

try:
	from frappe.utils.jinja_globals import bundled_asset
except ImportError:  # an older frappe: the block then runs without the desk's styles
	bundled_asset = None

# Live results of one report: never served from the website cache.
no_cache = 1

STRINGS = {
	"ar": {
		"title": "تقرير كفاءة الوقود",
		"invalid_title": "هذا الرابط غير صالح",
		"invalid_body": "ربما أُوقف الرابط، أو نُسخ ناقصاً. اطلب رابطاً جديداً ممن أرسله إليك.",
		"failed": "تعذّر تحميل التقرير. ربما أُوقف الرابط.",
		"close": "إغلاق",
	},
	"en": {
		"title": "Fuel Efficiency report",
		"invalid_title": "This link is not valid",
		"invalid_body": "It may have been stopped, or copied only in part. Ask whoever sent it for a new one.",
		"failed": "The report could not be loaded. The link may have been stopped.",
		"close": "Close",
	},
}


def get_context(context):
	# Arabic first, as on the valuation page; ?lang=en for English.
	lang = "en" if (frappe.form_dict.get("lang") or "").lower() == "en" else "ar"
	t = STRINGS[lang]
	context.no_cache = 1
	context.show_sidebar = False
	context.full_width = True
	context.lang = lang
	context.text_dir = "rtl" if lang == "ar" else "ltr"
	context.t = t
	context.title = t["title"]

	key = str(frappe.form_dict.get("k") or "")
	try:
		_shared_import(key)
	except frappe.PermissionError:
		context.invalid = True
		return context

	# Deliberately unchecked: the caller is Guest. The key verified above is
	# what lets this page run the dashboard's block.
	block = frappe.db.get_value("Custom HTML Block", "Fuel Efficiency", ["html", "style", "script"], as_dict=True) or {}
	context.invalid = False
	context.page_data = frappe.as_json({
		"key": key, "lang": lang, "failed": t["failed"], "close": t["close"],
		"html": block.get("html") or "", "css": block.get("style") or "", "js": block.get("script") or "",
		"desk_css": bundled_asset("desk.bundle.css") if bundled_asset else "",
	}, indent=None).replace("</", "<\\/")
	return context
