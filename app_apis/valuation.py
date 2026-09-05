# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Customer-facing valuation links for `xticket`.

The customer rates three things: the installation engineer who did the work,
how fast the installation was, and the company overall.

The customer has no ERPNext account, so the link has to work for Guest. Rather
than open the doctype up to Guest -- which would expose every ticket to anyone
who can type a URL -- each link carries an HMAC over the ticket name *and* an
expiry stamp. The token is the permission: it proves the bearer was given this
exact link, it grants nothing beyond that one ticket, and it stops working on
its own after `_TOKEN_TTL`.

Expiry is carried in the URL and signed, not stored. Nothing has to be written
when a link is minted and nothing has to be swept when it dies, so a link that
is forwarded, screenshotted, or left sitting in an inbox expires without any
server-side bookkeeping.

To rotate every live link at once (e.g. after a leak), set
`valuation_link_secret` in site_config.json to any new string. Without it the
site's encryption_key is used, which is stable for the life of the site.
"""

import hashlib
import hmac
import time
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt, get_url, now_datetime, strip_html_tags

# Five stars in the UI, stored as the 0-1 fraction frappe's Rating field wants.
_MAX_STARS = 5

# A comment box on a public endpoint is an invitation. Long enough for a real
# complaint, short enough that nobody is storing a novel in it.
_MAX_COMMENT = 2000

# Namespacing the signed payload keeps these tokens from being replayable
# against any other feature that might sign with the same site secret later.
_SCOPE = "xticket-valuation"

# 128 bits of hex. Long enough that guessing is hopeless, short enough that the
# link still fits in an SMS next to the rest of the message.
_TOKEN_LENGTH = 32

# One day. The customer is asked to rate a job that just finished, so a link
# that outlives the day it was sent is only a liability.
_TOKEN_TTL = 24 * 60 * 60

# Outcomes of check_token(). Kept distinct so the page can say "this expired,
# ask for a new one" instead of showing the same dead end as a forged link.
TOKEN_OK = "ok"
TOKEN_EXPIRED = "expired"
TOKEN_INVALID = "invalid"


def _secret() -> bytes:
	conf = frappe.local.conf
	secret = conf.get("valuation_link_secret") or conf.get("encryption_key")
	if not secret:
		frappe.throw(_("No site secret is available to sign valuation links."))
	return str(secret).encode()


def make_token(ticket: str, expires: int) -> str:
	"""Return the signature for one ticket, valid until `expires` (unix time).

	`expires` is part of the signed payload, so moving the deadline in the URL
	invalidates the token -- the customer cannot extend their own link.
	"""
	payload = f"{_SCOPE}:{ticket}:{cint(expires)}".encode()
	return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()[:_TOKEN_LENGTH]


def check_token(ticket: str, token: str, expires) -> str:
	"""Validate a link. Returns TOKEN_OK / TOKEN_EXPIRED / TOKEN_INVALID.

	The signature is checked *before* the clock: reporting "expired" for a
	stamp we never signed would confirm a guessed ticket name. Only a link we
	genuinely issued is ever told it is merely too old.

	`compare_digest` rather than `==` so the comparison cannot be timed
	character-by-character to reconstruct a valid token.
	"""
	if not ticket or not token or not expires:
		return TOKEN_INVALID

	expires = cint(expires)
	if not expires:
		return TOKEN_INVALID

	if not hmac.compare_digest(make_token(ticket, expires), str(token)):
		return TOKEN_INVALID

	if expires <= int(time.time()):
		return TOKEN_EXPIRED

	return TOKEN_OK


def verify_token(ticket: str, token: str, expires) -> bool:
	"""True only for a link that is both genuine and still live."""
	return check_token(ticket, token, expires) == TOKEN_OK


def build_url(ticket: str, ttl: int = _TOKEN_TTL) -> str:
	"""Absolute link to the public valuation page for one ticket.

	Every call mints a fresh deadline, so re-sending a ticket's link gives the
	customer a full day from that moment rather than resurrecting an old one.
	"""
	expires = int(time.time()) + cint(ttl or _TOKEN_TTL)
	query = urlencode({"t": ticket, "k": make_token(ticket, expires), "e": expires})
	return get_url(f"/valuation?{query}")


def build_link(ticket: str) -> dict:
	"""A fresh link plus when it dies, for anything that tells a human.

	Plain function, not whitelisted: callers that already hold the ticket --
	the desk button, the Chatwoot renderer -- use this so the URL and the
	expiry it reports are guaranteed to describe the same token.
	"""
	return {
		"url": build_url(ticket),
		# Shown next to the link so whoever sends it knows what they are
		# promising the customer.
		"expires_on": frappe.utils.format_datetime(
			frappe.utils.add_to_date(now_datetime(), seconds=_TOKEN_TTL)
		),
		"valid_hours": _TOKEN_TTL // 3600,
	}


@frappe.whitelist()
def get_valuation_link(ticket):
	"""Return the customer's link, for the button on the ticket form.

	Whitelisted for staff only: an operator needs read access on the ticket to
	be handed its link. The *page* the link points at is the public part.
	"""
	doc = frappe.get_doc("xticket", ticket)
	doc.check_permission("read")

	return {
		"ticket": doc.name,
		"state": doc.workflow_state,
		"customer": doc.get("customer"),
		**build_link(doc.name),
	}


def get_valuation(ticket: str):
	"""Return the existing valuation for a ticket, or None.

	Plain function, not whitelisted: the public page calls it server-side after
	the token check. Exposing it directly would let anyone read any customer's
	comments by ticket name.
	"""
	name = frappe.db.exists("Valuation", {"ticket": ticket})
	if not name:
		return None

	doc = frappe.get_doc("Valuation", name)
	return {
		"stars": round(flt(doc.engineer_rating) * _MAX_STARS),
		"description": doc.engineer_description,
		"speed_stars": round(flt(doc.speed_rating) * _MAX_STARS),
		"company_stars": round(flt(doc.company_rating) * _MAX_STARS),
		"company_description": doc.company_description,
		"submitted_on": doc.submitted_on,
	}


def _stars(value, label):
	"""Read one star field off the request, or throw."""
	stars = cint(value)
	if stars < 1 or stars > _MAX_STARS:
		frappe.throw(_("Please choose between 1 and {0} stars for {1}.").format(_MAX_STARS, label))
	return stars


@frappe.whitelist(allow_guest=True)
@rate_limit(key="ticket", limit=10, seconds=60 * 60)
def submit_valuation(
	ticket,
	token,
	expires,
	rating,
	description=None,
	speed_rating=None,
	company_rating=None,
	company_description=None,
):
	"""Record the customer's ratings: engineer, installation speed, company.

	Everything this accepts is attacker-controlled, so: the link is checked
	first and nothing else runs without it, every rating is bounded, comments
	are stripped of markup and truncated, and the engineer/customer are read
	off the ticket rather than taken from the caller. The rate limit is keyed on
	the ticket so hammering one link cannot lock out every other customer.
	"""
	status = check_token(ticket, token, expires)
	if status == TOKEN_EXPIRED:
		frappe.throw(
			_("This feedback link has expired. Please ask us for a new one."),
			frappe.PermissionError,
		)
	if status != TOKEN_OK:
		raise frappe.PermissionError(_("This valuation link is not valid."))

	stars = _stars(rating, _("the installation engineer"))
	speed_stars = _stars(speed_rating, _("the installation speed"))
	company_stars = _stars(company_rating, _("the company"))

	ticket_row = frappe.db.get_value(
		"xticket",
		ticket,
		["assigned_to", "assigned_full_name", "technician", "customer"],
		as_dict=True,
	)
	if not ticket_row:
		raise frappe.PermissionError(_("This valuation link is not valid."))

	# Named after the ticket, so a customer who submits twice edits their own
	# rating instead of creating a second one.
	name = frappe.db.exists("Valuation", {"ticket": ticket})
	doc = frappe.get_doc("Valuation", name) if name else frappe.new_doc("Valuation")

	doc.ticket = ticket
	doc.assigned_to = ticket_row.assigned_to
	# `technician` on the RIGHT is the xticket's own fieldname -- that doctype
	# belongs to the site, not to this app, so it keeps its name.
	doc.engineer_name = ticket_row.assigned_full_name or ticket_row.technician
	doc.engineer_driver = ticket_row.technician
	doc.customer = ticket_row.customer
	doc.engineer_rating = stars / _MAX_STARS
	doc.engineer_description = _clean(description)
	doc.speed_rating = speed_stars / _MAX_STARS
	doc.company_rating = company_stars / _MAX_STARS
	doc.company_description = _clean(company_description)
	doc.submitted_on = now_datetime()

	# The token authorised this write; Guest has no roles to satisfy.
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()

	return {
		"ok": True,
		"stars": stars,
		"speed_stars": speed_stars,
		"company_stars": company_stars,
	}


def _clean(text):
	"""Markup out, length capped. Applied to anything a stranger typed."""
	return strip_html_tags(text or "").strip()[:_MAX_COMMENT]
