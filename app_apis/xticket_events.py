# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Status-change notifications for the custom `xticket` doctype.

`xticket` was created through the UI (`"custom": 1`), so it has no controller
file anywhere on disk -- there is no `xticket.py` to put an `on_update` method
in, and creating one would not be picked up. Everything therefore hangs off
`doc_events` in hooks.py, which frappe applies to custom doctypes exactly the
way it applies to shipped ones.

The trigger is `workflow_state`, not `status`. `status` is a Select that mirrors
the workflow and is written by other code paths, so keying off it would fire on
edits that never crossed a workflow transition.
"""

import logging

import frappe

# The single place to edit which workflow states raise a notification.
# Mind the apostrophe: "Work's Done" must stay double-quoted.
TRIGGER_STATES = ["Work's Done", "Closed"]


def on_status_change(doc, method=None):
	"""Enqueue a notification when the ticket *enters* a trigger state.

	Bound to `on_update` via hooks.doc_events. Cheap on purpose: everything
	beyond the state comparison happens in a background worker, so a slow or
	broken notifier can never stretch out the user's save.
	"""
	before = doc.get_doc_before_save()

	if doc.workflow_state not in TRIGGER_STATES:
		return

	# A Client Script sets `works_done_flag` inside `after_workflow_action`,
	# which saves the ticket a second time immediately after the transition. On
	# that second save the state is already the trigger state and has not
	# changed, so bail out -- without this, every transition notifies twice.
	# `before` is None on insert, which is a genuine first entry into the state.
	if before and before.workflow_state == doc.workflow_state:
		return

	# Pass identifiers only, never the doc object: the worker is a separate
	# process, and a pickled doc would be a stale snapshot of this request's
	# in-memory state rather than what actually landed in the database.
	frappe.enqueue(
		"app_apis.xticket_events.notify",
		queue="short",
		ticket=doc.name,
		state=doc.workflow_state,
	)


def notify(ticket, state):
	"""Deliver the status-change message. Runs in a background worker.

	The whole body is guarded: this is fired off the back of a workflow
	transition, and a failure here must never surface to the user who moved the
	ticket, let alone roll their transition back.
	"""
	try:
		doc = frappe.get_doc("xticket", ticket)
		message = build_message(doc, state)

		# Nothing is actually sent yet -- the log is the delivery channel until
		# build_message() below is swapped for a real one.
		#
		# The level is pinned here on purpose: frappe defaults its loggers to
		# WARNING unless `logging` is set in site_config, so a bare .info()
		# writes an empty logs/app_apis.log and looks like the hook never ran.
		log = frappe.logger("app_apis")
		log.setLevel(logging.INFO)
		log.info(message)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "xticket notify failed")


# TODO: replace with SMS / WhatsApp / valuation link
def build_message(doc, state):
	"""Return the notification text.

	Deliberately the only place that knows what a notification *says*. Swap the
	body out and every caller -- the workflow hook and the form button -- picks
	up the new text unchanged.
	"""
	return (
		f"Ticket {doc.name} is now '{state}'. "
		f"Customer: {doc.get('customer') or '-'}. "
		f"License plate: {doc.get('license_plate') or '-'}. "
		# `technician` is the xticket's own fieldname -- that doctype belongs to
		# the site, not this app -- but the role is the installation engineer.
		f"Installation Engineer: "
		f"{doc.get('assigned_full_name') or doc.get('technician') or '-'}."
	)


# There was a `notify_now` whitelisted method here, driven by an
# `xticket-notify-now` Client Script that put a "Notify Now" button on the
# ticket form. Both are gone. It is left noted rather than silently deleted so
# the next person does not go looking for a caller that no longer exists --
# the workflow transition is now the only thing that fires a notification.
