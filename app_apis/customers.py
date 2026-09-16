# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Customer-type rules, for anything that treats an individual differently.

An `Individual` customer owns the one vehicle the ticket is about; a `Company`
owns a fleet, and whoever reads the message is an office contact who did not
drive anything anywhere. That distinction decides two separate things -- who is
messaged automatically, and who is shown their vehicle number -- so the reading
of it lives here rather than being written out twice.

A leaf module on purpose, for the same reason contacts.py is one: the messaging
connector, the public feedback page and the automatic-message hook all need this
answer, and none of them should have to import either of the others to get it.

Both settings are comma-separated type names, and both share one convention:

    blank   -> the fallback the caller passes, never "everybody"
    All / * -> the filter is off, every customer type qualifies
    unknown -> excluded

Blank meaning "the safe default" and not "everyone" is deliberate. Companies
outnumber individuals here roughly five to one, so a field someone clears by
accident must not silently start messaging every business contact on the site.
Switching a filter off is something you have to write down.
"""

import frappe

# What each setting falls back to when its field is blank. Kept together so the
# two answers to "who counts as an individual here" cannot drift apart.
DEFAULT_MESSAGE_TYPES = "Individual"
DEFAULT_VEHICLE_TYPES = "Individual"

# The settings fieldnames these read. Named here so a rename is one edit.
MESSAGE_TYPES_FIELD = "auto_message_customer_types"
VEHICLE_TYPES_FIELD = "show_vehicle_customer_types"


def csv_types(raw, fallback: str = "") -> list[str]:
	"""Split a comma-separated setting into a clean lowercase list.

	Lowercased because these fields are typed by hand in the desk, and the
	automation quietly stopping because somebody wrote `individual` would be a
	miserable thing to debug.
	"""
	value = str(raw or "").strip() or fallback
	return [part.strip().lower() for part in value.split(",") if part.strip()]


def _settings():
	"""The settings Single, from cache. Cheap enough for the save path."""
	return frappe.get_cached_doc("app_apis")


def customer_type(customer) -> str:
	"""The Customer's type, lowercased, or "" when it cannot be read.

	Cached: this is asked once per message and once per page render, and the
	answer changes about as often as the customer record does.
	"""
	name = str(customer or "").strip()
	if not name:
		return ""

	try:
		value = frappe.get_cached_value("Customer", name, "customer_type")
	except Exception:
		# A deleted customer, or a `customer` field holding something that was
		# never a Customer. Unknown is the honest answer, and every caller
		# already treats unknown as "no".
		return ""

	return str(value or "").strip().lower()


def type_allowed(customer, allowed: list[str]) -> bool:
	"""Is this customer one of `allowed`? `All` or `*` in the list means yes.

	An unset or unknown customer is never allowed through. If the system cannot
	tell who it is dealing with, it should not act as though it knows.
	"""
	if not allowed or "all" in allowed or "*" in allowed:
		return True

	return customer_type(customer) in allowed


def may_be_messaged(customer, settings=None) -> bool:
	"""Types the automatic messages are allowed to write to."""
	settings = settings if settings is not None else _settings()
	allowed = csv_types(settings.get(MESSAGE_TYPES_FIELD), DEFAULT_MESSAGE_TYPES)
	return type_allowed(customer, allowed)


def may_see_vehicle(customer, settings=None) -> bool:
	"""Types that are shown their vehicle number.

	Read by the Chatwoot templates and by the public feedback page, so the
	message and the page a customer opens from it always agree about whether
	their plate is mentioned at all.
	"""
	settings = settings if settings is not None else _settings()
	allowed = csv_types(settings.get(VEHICLE_TYPES_FIELD), DEFAULT_VEHICLE_TYPES)
	return type_allowed(customer, allowed)
