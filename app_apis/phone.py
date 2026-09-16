# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Turning whatever somebody typed into a number you can actually send to.

Lifted out of chatwoot_connector once a second caller appeared: the customer's
number comes off the ticket and the technician's comes off their User or
Employee record, and both have to end up in the same shape before Chatwoot sees
them. Two copies of the country-code rule would eventually disagree about a
leading zero, and the symptom would be a message going to the wrong person.

A leaf module: frappe is not even imported, so this can be reasoned about and
tested on its own.
"""

import re

# Saudi. Applied only to a local number that clearly has no country code.
DEFAULT_COUNTRY_CODE = "966"

# Shortest string of digits that can be a real mobile number. Half this site's
# `driver_mobile` values are a bare "+966" with no number after it -- 3 digits,
# which looks filled to a NOT NULL check and is not a destination.
MIN_PHONE_DIGITS = 9

# Longest, and it is not a judgement call: E.164 caps a full international
# number at 15 digits including the country code, so anything longer is not a
# number that could be dialled anywhere on earth.
#
# This is not hypothetical here. 50 `Customer Vehicle` rows and 99 tickets hold
# TWO numbers pasted into one field -- "+966598222588 / 0562261442",
# "530214785 - 535347448", sometimes separated by a newline -- and stripping the
# punctuation glued them into a single 21- or 22-digit string that looked
# perfectly well-formed. Every real number on this site is 12 digits; nothing
# legitimate sits above 15.
#
# Rejected rather than split at the separator. Which of the two people in that
# field should receive the message is a question about a human, and a guess
# would be a message delivered to the wrong one. A caller that has other numbers
# to try will try them; one that does not gets "no usable number", which is the
# true statement and points at the record that needs fixing.
MAX_PHONE_DIGITS = 15


def digits(value) -> str:
	"""Everything that is not a digit, removed."""
	return re.sub(r"\D", "", str(value or ""))


def normalise(raw) -> str | None:
	"""Return an E.164 number, or None if `raw` is not one.

	None rather than a best guess: a number this cannot make sense of must not
	reach Chatwoot, where the failure would look like a delivery problem rather
	than the data problem it is.
	"""
	value = digits(raw)
	if len(value) < MIN_PHONE_DIGITS:
		return None

	# 00966… and 966… are already international; 05xxxxxxxx and 5xxxxxxxx are local.
	if value.startswith("00"):
		value = value[2:]
	elif not value.startswith(DEFAULT_COUNTRY_CODE):
		value = DEFAULT_COUNTRY_CODE + value.lstrip("0")

	# Checked after the country code is added, not before: a local number is
	# only over the limit once it is written the way it would be sent.
	if len(value) > MAX_PHONE_DIGITS:
		return None

	return "+" + value
