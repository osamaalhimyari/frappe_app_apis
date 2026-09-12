# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Message the customer or the engineer as a ticket moves through its statuses.

Which status sends what, to whom, and in what words is entirely a table on the
settings Single (`auto_message_rules`), not a constant here: that pairing is a
decision about how the business runs, and changing it should be a row in a
grid rather than a deploy. A row is just three things -- a status, a Customer
or Technician choice, and its own Message text -- so adding a fourth stage, or
a message that only exists for one particular status, is filling in a row, not
naming a new kind of message in Python first.

The two audiences are gated separately and completely, because they are asking
different questions. A customer message needs `auto_message_enabled` AND the
customer-type filter -- most customers here are companies, and a company does
not want a "rate your visit" text. A technician message needs only
`technician_message_enabled`: the engineer is being sent to do a job, and who
owns the vehicle has no bearing on whether they should be told. Either switch
can be on with the other off. Which side a row is for is the row's own Send To
field -- nothing here infers it from wording or guesses from the status.

The whole design follows from one rule the operator set: the status change is
the real work and the message is a courtesy that happens afterwards. Every
decision here is made in favour of the transition.

  * `on_ticket_update` has a bare `except` around everything. A bug in this
    file, a dead redis, a rule somebody typed wrong -- none of them can raise
    into the operator's save and roll their transition back.
  * Nothing is sent inline. The handler decides and queues; the HTTP calls
    happen in a background worker on the `long` queue, the one meant for work
    nobody is waiting on. Chatwoot being slow costs the operator nothing.
  * Jobs are queued with `enqueue_after_commit`, so they are only pushed once
    the transition has actually committed. A save that fails further down the
    hook chain sends nothing at all.
  * The worker re-reads the ticket and re-checks the rule before sending. The
    queue is not instant and a transition can be reverted in the meantime, and
    "rate your visit" for a visit that is no longer done is worse than silence.
  * On the sync path a failure is written to the log file, never to Error Log.
    An Error Log row is a database write inside the operator's still-open
    transaction, and adding writes to their save is what this must never do.

Nothing here writes to the ticket. It reads the ticket, and it writes rows to
App Apis Message Log, which is its own table.

Replaces the earlier `auto_valuation` module, which did this for one message
only. Renamed rather than extended in place because "auto_valuation" stopped
being true the moment it learned to send three different things -- and later
stopped naming stages at all, once a row could be anything an operator typed.
"""

import frappe
from frappe.utils import cint, now_datetime

from app_apis.customers import may_be_messaged

# Both spellings of "the ticket's state" are watched. `workflow_state` is what
# the workflow writes; `status` is the Select the form shows and what the
# operator means when they say they changed the status. On this site the
# `Sync xticket status` server script copies one into the other on every Before
# Save, so in practice both move together -- collecting them into a set below is
# what keeps that from sending two copies of the same message.
STATE_FIELDS = ("workflow_state", "status")

# Used only when the rule table is empty, so a site that has never opened the
# settings form still behaves sensibly. Mind the apostrophe in Work's Done: it
# has to match the Select option on xticket exactly. Wording mirrors what this
# app shipped with before Send To / Message replaced the six named templates.
DEFAULT_RULES = (
	{"state": "In Hand", "to": "Customer", "send_once": 1, "message": (
		"👋 أهلاً {customer}\n\n"
		"🎫 {ticket}\n"
		"{vehicle}\n"
		"🔧 {engineer}\n\n"
		"✅ استلمنا طلبك وصار بين إيدينا! بنخبرك أول ما يبدأ العمل 🚀\n"
		"✅ Got it — your request is in good hands! We'll tell you the moment work starts 🚀\n\n"
		"شكراً لثقتك فينا / Thanks for trusting us 🤝"
	)},
	{"state": "Pending", "to": "Customer", "send_once": 1, "message": (
		"👋 أهلاً {customer}\n\n"
		"🎫 {ticket}\n"
		"{vehicle}\n"
		"🔧 {engineer}\n\n"
		"🛠️ فريقنا شغّال على طلبك الحين! ما راح ناخذ وقت طويل 💪\n"
		"🛠️ Our team is on it right now — we won't keep you long 💪\n\n"
		"بنبلغك أول ما نخلص / We'll let you know the second we're done ✅"
	)},
	{"state": "Work's Done", "to": "Customer", "send_once": 1, "message": (
		"🎉 خلصنا يا {customer}! / All done, {customer}!\n\n"
		"🎫 {ticket}\n"
		"{vehicle}\n"
		"🔧 {engineer}\n\n"
		"⭐ كيف كانت تجربتك معنا؟ رأيك يهمنا وما ياخذ دقيقة 🙏\n"
		"⭐ How did we do? Your feedback means a lot — under a minute 🙏\n\n"
		"🔗 {link}\n"
		"⏰ صالح ٢٤ ساعة / Valid for 24 hours"
	)},
	# The engineer's side of the same three stages. Same states, different
	# audience, and each is gated by its own switch -- see _consider.
	{"state": "In Hand", "to": "Technician", "send_once": 1, "message": (
		"👋 يعطيك العافية {engineer}\n\n"
		"📋 عندك طلب جديد / New job for you:\n"
		"🎫 {ticket}\n"
		"👤 {customer}\n"
		"{vehicle}\n"
		"{location}\n"
		"{phone}\n\n"
		"📞 كلّم العميل ونسّق معه الموعد، وإذا احتجت شي إحنا معك 💪\n"
		"📞 Give the customer a call and set a time — shout if you need anything 💪"
	)},
	{"state": "Pending", "to": "Technician", "send_once": 1, "message": (
		"👋 يعطيك العافية {engineer}\n\n"
		"🛠️ الطلب صار قيد التنفيذ / Job is now in progress:\n"
		"🎫 {ticket}\n"
		"👤 {customer}\n"
		"{vehicle}\n"
		"{location}\n\n"
		"✅ حدّث الطلب أول ما تخلص التركيب، وبالتوفيق 🚀\n"
		"✅ Update the ticket once the install is done — good luck out there 🚀"
	)},
	{"state": "Work's Done", "to": "Technician", "send_once": 1, "message": (
		"🎉 تمام يا {engineer}!\n\n"
		"✅ تم إغلاق الطلب / Job closed:\n"
		"🎫 {ticket}\n"
		"👤 {customer}\n"
		"{vehicle}\n\n"
		"🙏 شكراً لجهودك، شغل ممتاز! أرسلنا للعميل طلب تقييم ⭐\n"
		"🙏 Thanks for your effort — great work! We've asked the customer to rate it ⭐"
	)},
)

def _settings():
	"""The settings Single, from cache. Cheap enough for the save path."""
	return frappe.get_cached_doc("app_apis")


def _rules(settings) -> list[dict]:
	"""Enabled, fully-filled-in rules, as plain dicts.

	Falls back to DEFAULT_RULES if the table is empty, so a site that has never
	opened the settings form still behaves sensibly. A row missing a status or
	a message cannot do anything useful, so it is dropped rather than failing
	loudly -- half-filled-in rows are a normal thing to have while an operator
	is still typing (the Message field is required on the doctype, but that
	only stops a *save*, not a row somebody is mid-edit on).
	"""
	rows = []
	for row in settings.get("auto_message_rules") or []:
		state = str(row.get("state") or "").strip()
		to = str(row.get("to") or "").strip()
		message = str(row.get("message") or "").strip()
		if not cint(row.get("enabled")) or not state or to not in ("Customer", "Technician") or not message:
			continue
		rows.append({
			"state": state,
			"to": to,
			"message": message,
			"send_once": cint(row.get("send_once")),
		})
	return rows or [dict(r) for r in DEFAULT_RULES]


def _identity(state: str, to: str) -> str:
	"""How a rule identifies itself in the log and the realtime toast.

	There is no template name to fall back on any more -- a rule is a status
	and an audience, so that pair, written the way an operator reads it, is
	the whole identity. Used for `_already_sent` and Message Log's `template`
	column; kept ASCII-free of nothing special, but see `_job_key` for the
	separate, ASCII-safe form used in job ids.
	"""
	return f"{state} → {to}"


def _job_key(state: str, to: str) -> str:
	"""The same identity, safe to put in a Redis job id."""
	return f"{state.strip().lower()}::{to.strip().lower()}"


def _cw_template(to: str) -> str:
	"""Which entry in chatwoot_connector.TEMPLATES this audience renders as.

	Purely a routing key -- see chatwoot_connector.TEMPLATES["auto_customer"]
	and ["auto_technician"]. The wording sent is always the rule's own Message
	field; these two exist only so `_context`/`_render`/`recipient_of` treat
	the row as a customer or technician message, correctly and by construction.
	"""
	return "auto_technician" if to == "Technician" else "auto_customer"


# --------------------------------------------------------------------------
# The hook. Runs inside the operator's save -- keep it cheap and silent.
# --------------------------------------------------------------------------


def on_ticket_update(doc, method=None):
	"""Queue whatever messages this save's status change has earned.

	Bound to `on_update` in hooks.doc_events, alongside the older internal
	status notifier. Both are listed separately so one throwing cannot stop the
	other -- though neither throws, by construction.
	"""
	try:
		_consider(doc)
	except Exception:
		# Deliberately not frappe.log_error: see the module docstring. A file
		# write cannot touch the transaction being committed.
		try:
			frappe.logger("app_apis").error(
				"auto-messages: trigger failed for %s\n%s"
				% (getattr(doc, "name", "?"), frappe.get_traceback())
			)
		except Exception:
			pass


def _consider(doc):
	"""Decide which rules this save matches, and queue one job for each.

	The two audiences are gated separately and completely. A customer message
	needs the customer switch AND the customer-type filter; a technician message
	needs only the technician switch, because the engineer is being told to go
	and do a job and who owns the vehicle has nothing to do with whether they
	need telling. Either side can be on with the other off.
	"""
	settings = _settings()

	entered = _entered_states(doc)
	if not entered:
		return

	rules = [rule for rule in _rules(settings) if rule["state"].lower() in entered]
	if not rules:
		return

	customer_on = cint(settings.get("auto_message_enabled"))
	technician_on = cint(settings.get("technician_message_enabled"))

	# Resolved at most once per save, and only if a customer rule matched --
	# it is a database read for the Customer's type.
	customer_ok = None

	for rule in rules:
		if rule["to"] == "Technician":
			if not technician_on:
				continue
		else:
			if not customer_on:
				continue
			if customer_ok is None:
				customer_ok = _customer_allowed(doc, settings)
			if not customer_ok:
				continue

		field, state = entered[rule["state"].lower()]
		frappe.enqueue(
			"app_apis.auto_messages.run",
			queue="long",
			timeout=300,
			# The heart of "do not mess with the status change": nothing reaches
			# redis until the transaction carrying this transition has
			# committed. A rolled-back save sends no message.
			enqueue_after_commit=True,
			# A Client Script on this site saves the ticket again right after a
			# workflow action. This drops the twin while the first is still
			# queued; the log check in _run covers the case where it finished.
			job_id=f"auto-message::{doc.name}::{_job_key(rule['state'], rule['to'])}",
			deduplicate=True,
			ticket=doc.name,
			state=state,
			to=rule["to"],
			field=field,
			# Captured here and carried into the worker, because by the time the
			# message actually goes out the worker is running as Administrator
			# and has no idea who moved the ticket. This is the only thread back
			# to the person who should see the result.
			notify_user=frappe.session.user,
		)


def _entered_states(doc) -> dict:
	"""States this save *entered*, as {lowered state: (field, as written)}.

	"Entered" and not "is in": a ticket saved five more times while sitting in
	Work's Done has not done anything new. Collapsing both state fields into one
	dict is what stops a single save -- which moves `workflow_state` and then
	has `status` copied from it -- from counting as two separate arrivals.
	"""
	before = doc.get_doc_before_save()
	entered = {}

	for field in STATE_FIELDS:
		current = str(doc.get(field) or "").strip()
		if not current:
			continue
		# `before` is None on insert, which is a genuine first entry.
		previous = str(before.get(field) or "").strip() if before else ""
		if previous.lower() == current.lower():
			continue
		entered.setdefault(current.lower(), (field, current))

	return entered


def _customer_allowed(doc, settings) -> bool:
	"""Is this ticket's customer one of the types allowed to be messaged?

	A blank setting falls back to Individual rather than to "everyone". Nine out
	of ten tickets here belong to a Company, so a field somebody cleared by
	accident would otherwise start messaging thousands of business contacts --
	the failure mode of the safe reading is that nobody gets a message, which is
	the one worth having. Turning the filter off is therefore something you have
	to write down: put `All` in the field.

	An unset or unknown customer is never messaged. If the system cannot tell
	who it is about to contact, it should not contact them.

	The reading itself lives in app_apis.customers, alongside the matching rule
	for who is shown their vehicle number. Both answer "is this an individual?"
	off fields an operator types by hand, and two copies of that would eventually
	disagree about a stray capital letter.
	"""
	return may_be_messaged(doc.get("customer"), settings)


# --------------------------------------------------------------------------
# The worker. Runs after the commit, in its own process and its own transaction.
# --------------------------------------------------------------------------


def run(ticket: str, state: str, to: str, field: str | None = None,
        notify_user: str | None = None):
	"""Background entry point. Never raises -- there is nobody to raise to."""
	try:
		_run(ticket, state, to, field, notify_user)
	except Exception:
		# Here Error Log is the right home: a separate process, its own
		# transaction, and a failure nobody can see is a failure nobody fixes.
		frappe.log_error(frappe.get_traceback(), f"auto-message send failed: {ticket} / {state} -> {to}")
		# Still tell whoever moved the ticket. A crash they never see is the
		# worst outcome: they assume the customer was messaged.
		_notify(notify_user, {
			"ticket": ticket,
			"template": _identity(state, to),
			"status": "Failed",
			"reason": "Unexpected error — see Error Log.",
		})


def _run(ticket: str, state: str, to: str, field: str | None,
         notify_user: str | None = None):
	from app_apis import chatwoot_connector as cw

	settings = _settings()
	cw_template = _cw_template(to)
	to_const = cw.recipient_of(cw_template)

	# Re-checked here as well as at queue time: the switch can be turned off in
	# the seconds between the transition committing and the worker picking the
	# job up, and the switch being off has to mean nothing goes out.
	switch = "technician_message_enabled" if to_const == cw.TO_TECHNICIAN else "auto_message_enabled"
	if not cint(settings.get(switch)):
		return

	# A system decision, not a user action: the operator moved a ticket, they
	# did not press "message this customer". Running as Administrator keeps
	# delivery working whichever role made the transition, and keeps the
	# Permission Query on xticket out of the sending path.
	frappe.set_user("Administrator")

	doc = frappe.get_doc("xticket", ticket)
	trigger = f"{field or 'state'} = {state or '?'}"

	# The queue is not instant, and a transition can be undone in the meantime.
	# Re-fetched fresh (not the settings above, which were only needed for the
	# switch) so a message edited seconds ago goes out in its current wording.
	rule = next(
		(r for r in _rules(settings) if r["state"].lower() == state.lower() and r["to"] == to),
		None,
	)
	identity = _identity((rule or {}).get("state", state), to)

	if rule:
		current = [str(doc.get(f) or "").strip().lower() for f in STATE_FIELDS]
		if rule["state"].lower() not in current:
			reason = f"Ticket left {rule['state']} before the message went out (now: {doc.get('workflow_state') or '-'})."
			_log(doc, identity, "Skipped", to_const, trigger=trigger, reason=reason)
			_notify(notify_user, {
				"ticket": doc.name,
				"template": identity,
				"recipient": to_const,
				"status": "Skipped",
				"reason": reason,
			})
			return

	# Has this customer asked to be left alone? Checked on the customer side
	# only: a technician message is internal, and an engineer is not a customer
	# who can opt out of being told where their next job is.
	if to_const != cw.TO_TECHNICIAN:
		from app_apis import do_not_contact

		asked = do_not_contact.check(
			customer=doc.get("customer"),
			phone=cw.recipient_phone(doc, cw_template),
			scope=do_not_contact.TICKETS,
		)
		if asked["blocked"]:
			_log(doc, identity, "Skipped", to_const, trigger=trigger, reason=asked["reason"])
			_notify(notify_user, {
				"ticket": doc.name,
				"template": identity,
				"recipient": to_const,
				"status": "Skipped",
				"reason": asked["reason"],
			})
			return

	if cint((rule or {}).get("send_once", 1)) and _already_sent(ticket, identity):
		# Silent: the point of "once" is that the second attempt is a non-event,
		# and a row for every non-event would bury the rows that matter.
		return

	# Nobody has recorded a number for this engineer. Recorded as Skipped, not
	# Failed: nothing was attempted and nothing is broken -- somebody has to
	# type a number into a User or Employee record, and saying so plainly is
	# the difference between a fixable gap and a mystery. See
	# app_apis.technicians.missing_numbers for the full list.
	if to_const == cw.TO_TECHNICIAN and not cw.recipient_phone(doc, cw_template):
		from app_apis import technicians

		who = technicians.name(doc) or doc.get("assigned_to") or "the engineer"
		reason = f"No phone number on file for {who} — add one to their User or Employee record."
		_log(doc, identity, "Skipped", to_const, trigger=trigger, reason=reason)
		_notify(notify_user, {
			"ticket": doc.name,
			"template": identity,
			"recipient": to_const,
			"status": "Skipped",
			"reason": reason,
		})
		return

	# The wording is always the rule's own Message field; a rule that vanished
	# between the transition and the worker (deleted, or the table cleared)
	# falls back to a plain built-in line rather than sending nothing silently.
	message_text = (rule or {}).get("message") or cw.TEMPLATES[cw_template]["text"]
	body = cw._render(message_text, cw._context(doc, cw_template))
	result = cw.send_ticket_message(ticket, template=cw_template, text=body) or {}
	status = "Sent" if result.get("ok") else "Failed"

	_log(
		doc,
		identity,
		status,
		to_const,
		trigger=trigger,
		reason="" if result.get("ok") else str(result.get("msg") or "")[:500],
		result=result,
	)

	_notify(notify_user, {
		"ticket": doc.name,
		"template": identity,
		"recipient": to_const,
		"status": status,
		"phone": result.get("phone") or "",
		"customer": str(doc.get("customer") or "")[:80],
		"engineer": str(result.get("engineer") or "")[:80],
		"reason": "" if result.get("ok") else str(result.get("msg") or "")[:300],
	})


def _notify(user: str | None, payload: dict):
	"""Push the outcome to the desk of whoever moved the ticket.

	The send happens in a worker seconds after the save, long after the browser
	got its response, so there is nothing left to return a result to -- a
	realtime push is the only way this reaches a screen. Aimed at one user
	rather than broadcast: the whole team does not need a toast every time a
	colleague closes a ticket.

	Guarded and silent on failure. A notification is the least important thing
	this module does, and it must never be the reason a send is recorded as
	having failed.
	"""
	# Guest has no desk to show a toast on. Administrator is deliberately NOT
	# excluded: it is this site's normal working login, and `user` was captured
	# from the browser session that saved the ticket, not from the worker.
	if not user or user == "Guest":
		return
	try:
		frappe.publish_realtime(
			"app_apis_message_sent",
			message=payload,
			user=user,
		)
	except Exception:
		frappe.logger("app_apis").warning(f"auto-message: could not notify {user}")


def _already_sent(ticket: str, identity: str) -> bool:
	"""Has this exact message already gone out for this ticket?"""
	return bool(
		frappe.db.exists(
			"App Apis Message Log",
			{"ticket": ticket, "template": identity, "status": "Sent"},
		)
	)


def _log(doc, identity: str, status: str, to: str, trigger: str = "", reason: str = "",
          result: dict | None = None):
	"""Record one outcome. Guarded: a logging failure must not lose the send.

	`to` is chatwoot_connector.TO_CUSTOMER/TO_TECHNICIAN, passed in rather than
	derived from `identity` -- there is no TEMPLATES entry named "In Hand ->
	Technician" to look the audience back up from.
	"""
	result = result or {}
	try:
		from app_apis import chatwoot_connector as cw
		from app_apis import technicians

		entry = frappe.new_doc("App Apis Message Log")
		entry.ticket = getattr(doc, "name", None) or str(doc)
		entry.customer = str(getattr(doc, "customer", "") or "")[:140]
		# Who this row is about, so "was the engineer told?" is a filter rather
		# than a memory test about which template names start with tech_.
		entry.recipient = "Technician" if to == cw.TO_TECHNICIAN else "Customer"
		entry.engineer = str(technicians.name(doc) or "")[:140] if to == cw.TO_TECHNICIAN else ""
		entry.template = identity
		entry.status = status
		entry.trigger_source = trigger
		entry.reason = reason
		entry.phone = result.get("phone") or ""
		# `language` is left unset on purpose. A message carries Arabic and
		# English together now, so there is no answer to record; the column
		# stays for the rows written while there was one.
		entry.code = cint(result.get("code"))
		entry.message = result.get("message") or ""
		entry.conversation_id = str(result.get("conversation_id") or "")
		entry.message_id = str(result.get("message_id") or "")
		entry.sent_on = now_datetime() if status == "Sent" else None
		entry.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"auto-message log failed: {getattr(doc, 'name', doc)}")


# --------------------------------------------------------------------------
# Manual controls. For a console, a Server Script, or a button added later.
# --------------------------------------------------------------------------


@frappe.whitelist()
def send_now(ticket: str, state: str, to: str, force: int = 0) -> dict:
	"""Run one rule against one ticket by hand.

	Useful for testing without walking a real ticket through the workflow, and
	for retrying something that failed. `force` ignores the already-sent check;
	every other gate still applies, because bypassing the customer-type filter
	should take a settings change and a moment's thought rather than a keyword
	argument.
	"""
	frappe.only_for("System Manager")

	from app_apis import chatwoot_connector as cw

	if to not in ("Customer", "Technician"):
		return {"ok": False, "msg": "to must be 'Customer' or 'Technician'."}

	settings = _settings()
	doc = frappe.get_doc("xticket", ticket)
	cw_template = _cw_template(to)
	to_const = cw.recipient_of(cw_template)
	identity = _identity(state, to)

	if to_const == cw.TO_TECHNICIAN:
		if not cint(settings.get("technician_message_enabled")):
			return {"ok": False, "msg": "Technician messages are switched off in App APIs settings."}
		if not cw.recipient_phone(doc, cw_template):
			from app_apis import technicians

			who = technicians.name(doc) or doc.get("assigned_to") or "the engineer"
			return {"ok": False, "msg": f"No phone number on file for {who}."}
	else:
		if not cint(settings.get("auto_message_enabled")):
			return {"ok": False, "msg": "Automatic messages are switched off in App APIs settings."}
		if not _customer_allowed(doc, settings):
			customer_type = frappe.get_cached_value("Customer", doc.get("customer"), "customer_type")
			return {"ok": False, "msg": f"Customer type '{customer_type or '-'}' is not in the allowed list."}

	if not cint(force) and _already_sent(ticket, identity):
		return {"ok": False, "msg": f"'{identity}' has already been sent for this ticket. Pass force=1 to repeat it."}

	frappe.enqueue(
		"app_apis.auto_messages.run",
		queue="long",
		timeout=300,
		ticket=doc.name,
		state=state,
		to=to,
		field="send_now",
		notify_user=frappe.session.user,
	)
	return {"ok": True, "msg": f"Queued '{identity}' for {doc.name}."}


@frappe.whitelist()
def preview(ticket: str) -> dict:
	"""Explain, without sending anything, what this ticket would do.

	Answers "why did my customer not get a message?" in one call instead of a
	trawl through the log file.
	"""
	frappe.only_for("System Manager")

	from app_apis import chatwoot_connector as cw

	settings = _settings()
	doc = frappe.get_doc("xticket", ticket)
	ready, why = cw._ready(cw._settings())
	customer_type = frappe.get_cached_value("Customer", doc.get("customer"), "customer_type")
	current = [str(doc.get(f) or "").strip().lower() for f in STATE_FIELDS]

	from app_apis import technicians

	engineer = technicians.engineer(doc)
	customer_on = bool(cint(settings.get("auto_message_enabled")))
	technician_on = bool(cint(settings.get("technician_message_enabled")))
	customer_ok = _customer_allowed(doc, settings)

	rules = []
	for rule in _rules(settings):
		is_tech = rule["to"] == "Technician"
		identity = _identity(rule["state"], rule["to"])
		# EVERYTHING standing between this rule and a message, not just the first
		# thing. "Why did nothing go out?" is usually asked once, and answering
		# "the switch is off" only to be asked again about the missing phone
		# number wastes the trip.
		blocked = []
		if is_tech:
			if not technician_on:
				blocked.append("technician messages are off")
			if not engineer["phone"]:
				blocked.append("no phone on file for the engineer")
		else:
			if not customer_on:
				blocked.append("customer messages are off")
			if not customer_ok:
				blocked.append(f"customer type '{customer_type or '-'}' is not allowed")
			if not cw.ticket_phone(doc):
				blocked.append("no usable phone on the ticket")
		if not ready:
			blocked.append(why)

		rules.append({
			"state": rule["state"],
			"to": rule["to"],
			"identity": identity,
			"recipient": cw.TO_TECHNICIAN if is_tech else cw.TO_CUSTOMER,
			"matches_now": rule["state"].lower() in current,
			"already_sent": _already_sent(ticket, identity),
			"send_once": bool(cint(rule["send_once"])),
			"blocked_by": blocked,
			"would_send": not blocked,
		})

	return {
		"ticket": doc.name,
		"enabled": customer_on,
		"technician_enabled": technician_on,
		"workflow_state": doc.get("workflow_state"),
		"status": doc.get("status"),
		"customer": doc.get("customer"),
		"customer_type": customer_type,
		"customer_allowed": customer_ok,
		"phone": cw.ticket_phone(doc),
		"phones": [p["phone"] for p in cw.ticket_phones(doc)],
		"engineer": engineer["name"],
		"engineer_user": engineer["user"],
		"engineer_phone": engineer["phone"] or "",
		"engineer_phone_source": engineer["source"],
		"chatwoot_ready": ready,
		"chatwoot_reason": why,
		"rules": rules,
	}
