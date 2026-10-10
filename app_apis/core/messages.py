"""Custom messages: look one up by its short code, fill it in, send it.

The table is in the app_apis settings (Custom Messages): a CODE (BS, RENEW, ...), the MESSAGE text, and the
WhatsApp TEMPLATE it should go out as. A script of your own then needs only the code:

    from app_apis.core import messages

    messages.render("BS", {"customer": "Ali", "plate": "1234 - ABC"})      # the text, nothing sent
    messages.send("BS", "+9665XXXXXXXX", {"customer": "Ali"})              # sends it
    messages.send("BS", phone, ctx, dry_run=True)                         # says what WOULD go

so the wording and the template stay editable by whoever runs the system, with no code change.

Placeholders are {names} in the message text; they are filled from the context you pass (the same plain
replacement the ticket and reminder messages use: an unknown placeholder is left visible, a line that came
out completely empty is dropped). The template's own variables are mapped in the row's Template Variables,
one per line, e.g. `{{1}} = {customer}`.

Sending goes through the same Chatwoot sender the subscription reminders use, including its rules: the
number must be a usable Saudi mobile, a template row is sent AS its template (WhatsApp's 24-hour window is
closed for almost everyone), a row without one goes as text while the window is open.
"""

import frappe

SETTINGS = "app_apis"
TABLE = "custom_messages"


def _norm(code) -> str:
	return str(code or "").strip().upper()


def rows() -> list:
	"""Every configured custom message: [{code, message, template, variables}]."""
	doc = frappe.get_cached_doc(SETTINGS)
	out = []
	for r in doc.get(TABLE) or []:
		code = _norm(r.get("code"))
		if code:
			out.append({"code": code, "message": str(r.get("message") or ""),
			            "template": str(r.get("whatsapp_template") or "").strip(),
			            "variables": str(r.get("template_variables") or "")})
	return out


def get(code: str) -> dict | None:
	"""The row for a code (case-insensitive), or None."""
	want = _norm(code)
	for r in rows():
		if r["code"] == want:
			return r
	return None


def render(code: str, context: dict | None = None) -> str:
	"""The message text for a code with its placeholders filled. Raises LookupError for an unknown code."""
	from app_apis import chatwoot_connector as cw

	row = get(code)
	if not row:
		raise LookupError("No custom message with the code %r. Known codes: %s" % (
			code, ", ".join(r["code"] for r in rows()) or "(none yet)"))
	return cw._render(row["message"], context or {}) if row["message"] else ""


def send(code: str, phone: str, context: dict | None = None, name: str | None = None,
         dry_run: bool = False) -> dict:
	"""Send a custom message to one number.

	-> {"ok": bool, "code": str, "text": str, "template": str, "dry_run": bool, "msg": str, ...} -- the rest
	is the sender's own answer. Nothing is sent when dry_run=True, or when the code is unknown."""
	from app_apis import chatwoot_connector as cw

	row = get(code)
	if not row:
		return {"ok": False, "code": _norm(code), "msg": "No custom message with the code %r." % code, "dry_run": dry_run}
	ctx = context or {}
	text = cw._render(row["message"], ctx) if row["message"] else ""
	out = {"code": row["code"], "text": text, "template": row["template"], "dry_run": bool(dry_run)}
	if dry_run:
		route = ("as WhatsApp template %s" % row["template"]) if row["template"] else "as plain text"
		return dict(out, ok=True, msg="Dry run: nothing was sent. It would go %s." % route)

	fallback = None
	if row["template"] and frappe.utils.cint(frappe.db.get_single_value(SETTINGS, "chatwoot_use_templates")):
		fallback = {"template": row["template"], "variables": row["variables"], "ctx": ctx, "force": True}
	if not text and not row["template"]:
		return dict(out, ok=False, msg="The row has neither a message nor a template.")
	try:
		res = cw._send(text or ("Custom message %s" % row["code"]), phone=phone, name=name or "",
		               context={"customer": name or "", "template": "custom_message", "code": row["code"]},
		               template_fallback=fallback, confirm=True) or {}
	except Exception as e:
		return dict(out, ok=False, msg="Send raised an error: %s" % str(e)[:300])
	return dict(res, **out, ok=bool(res.get("ok")))
