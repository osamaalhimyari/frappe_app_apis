"""HTTP with a persistent, encrypted cookie jar -- for websites with no API.

Some providers (Lebara B2B) have no real API: the integration replays the
requests their own web UI makes, and those only work inside a logged-in
browser session. The Server Script sandbox can make HTTP calls
(`frappe.make_post_request`) but cannot keep cookies between them, let alone
between scheduler runs, so it cannot hold a session. This module can.

A *jar* is a name ("lebara"). Its cookies are stored encrypted in `__Auth`
against the app_apis Single, so a session survives worker restarts and
`bench migrate` -- which is the whole point: a login that needed an OTP once
keeps working for as long as something keeps the session warm.

Every function here is a primitive. None of them knows anything about any
particular website; the Server Scripts decide what to request and how to read
the answer.
"""

import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin

import frappe
import requests
from frappe.utils.password import (
	get_decrypted_password,
	remove_encrypted_password,
	set_encrypted_password,
)

SETTINGS = "app_apis"
ALLOWED_ROLES = ("System Manager",)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
MAX_TEXT = 200_000


# --------------------------------------------------------------------------
# Jar storage
# --------------------------------------------------------------------------


def _check():
	# Scheduler-run Server Scripts execute as Administrator, which has every
	# role, so this only ever stops a desk user calling the endpoint directly.
	frappe.only_for(ALLOWED_ROLES)


def _jar_field(jar: str) -> str:
	jar = (jar or "").strip()
	if not re.fullmatch(r"[A-Za-z0-9_\-]{1,60}", jar):
		frappe.throw(f"Invalid cookie jar name: {jar!r}")
	return f"cookie_jar:{jar}"


def _load(jar: str) -> requests.Session:
	s = requests.Session()
	s.headers["User-Agent"] = USER_AGENT
	raw = get_decrypted_password(SETTINGS, SETTINGS, _jar_field(jar), raise_exception=False)
	for c in json.loads(raw) if raw else []:
		s.cookies.set_cookie(
			requests.cookies.create_cookie(
				name=c["name"],
				value=c["value"],
				domain=c.get("domain") or "",
				path=c.get("path") or "/",
				secure=bool(c.get("secure")),
				expires=c.get("expires"),
				rest=c.get("rest") or {},
			)
		)
	return s


def _save(jar: str, s: requests.Session):
	cookies = [
		{
			"name": c.name,
			"value": c.value,
			"domain": c.domain,
			"path": c.path,
			"secure": c.secure,
			"expires": c.expires,
			"rest": {k: v for k, v in (c._rest or {}).items()},
		}
		for c in s.cookies
	]
	set_encrypted_password(SETTINGS, SETTINGS, json.dumps(cookies), _jar_field(jar))
	# A scheduled job that saves the jar and then dies before its own commit
	# would lose the refreshed cookie, so the jar is committed on its own.
	frappe.db.commit()


def _parse(value, default=None):
	"""Arguments arrive as Python objects from a Server Script's frappe.call,
	but as JSON strings over HTTP -- accept both."""
	if value in (None, ""):
		return default
	if isinstance(value, str):
		try:
			return json.loads(value)
		except ValueError:
			return value
	return value


def _result(r: requests.Response) -> dict:
	ctype = r.headers.get("content-type", "")
	out = {
		"ok": r.status_code < 400,
		"status": r.status_code,
		"url": r.url,
		"history": [h.status_code for h in r.history],
		"content_type": ctype,
		"headers": dict(r.headers),
		"json": None,
		"text": "",
	}
	if "json" in ctype or r.text[:1] in ("{", "["):
		try:
			out["json"] = r.json()
		except ValueError:
			pass
	if out["json"] is None:
		out["text"] = r.text[:MAX_TEXT]
	return out


# --------------------------------------------------------------------------
# Whitelisted primitives
# --------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def request(
	jar,
	url,
	method="GET",
	body=None,
	data=None,
	headers=None,
	params=None,
	timeout=30,
	allow_redirects=1,
	csrf_cookie=None,
	csrf_header="X-CSRF-TOKEN",
):
	"""One HTTP request inside the named cookie jar; the jar is saved after.

	body            JSON body (dict/list)
	data            form body (dict) -- sent as x-www-form-urlencoded
	csrf_cookie     name of a cookie whose URL-decoded value is sent in
	                `csrf_header` (Serenity / ASP.NET double-submit pattern)

	Returns {ok, status, url, history, content_type, headers, json, text}:
	`json` is the parsed body when it is JSON, otherwise `text` holds the body.
	"""
	_check()
	s = _load(jar)
	hdrs = dict(_parse(headers, {}) or {})
	if csrf_cookie:
		token = s.cookies.get(csrf_cookie)
		if token:
			hdrs[csrf_header or "X-CSRF-TOKEN"] = unquote(token)
	try:
		r = s.request(
			(method or "GET").upper(),
			url,
			json=_parse(body),
			data=_parse(data),
			params=_parse(params),
			headers=hdrs,
			timeout=frappe.utils.cint(timeout) or 30,
			allow_redirects=bool(frappe.utils.cint(allow_redirects)),
		)
	except requests.RequestException as e:
		return {"ok": False, "status": 0, "url": url, "error": str(e)[:500], "json": None, "text": ""}
	_save(jar, s)
	return _result(r)


class _Forms(HTMLParser):
	"""Collects every <form> with its action and named inputs."""

	def __init__(self):
		super().__init__()
		self.forms = []

	def handle_starttag(self, tag, attrs):
		a = dict(attrs)
		if tag == "form":
			self.forms.append({"action": a.get("action") or "", "method": a.get("method") or "get", "fields": {}})
		elif tag in ("input", "select", "textarea") and self.forms and a.get("name"):
			if a.get("type") in ("checkbox", "radio") and "checked" not in a:
				return
			if a.get("type") in ("submit", "button", "image"):
				return
			self.forms[-1]["fields"].setdefault(a["name"], a.get("value") or "")


def _alerts(html: str) -> list[dict]:
	out = []
	for kind, inner in re.findall(r'class="[^"]*\balert-(\w+)\b[^"]*"[^>]*>(.*?)</div>', html, re.S):
		msg = re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&times;|×", " ", inner)).strip()
		out.append({"kind": kind, "message": msg})
	for inner in re.findall(r'class="[^"]*validation-summary-errors[^"]*"[^>]*>(.*?)</div>', html, re.S):
		msg = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
		if msg:
			out.append({"kind": "danger", "message": msg})
	return out


@frappe.whitelist(methods=["POST"])
def form_post(jar, url, fields=None, form_index=0, timeout=60):
	"""Submit an HTML form the way a browser would.

	GETs `url` (following redirects), reads the form at `form_index` -- hidden
	anti-forgery tokens included -- overlays `fields`, and POSTs it to the
	form's action (the final URL when the form has none).

	Returns {ok, status, url, posted_to, fields_sent, alerts, text}: `alerts`
	is every Bootstrap `.alert-*` / validation summary on the result page as
	{kind, message}, and `ok` is True only when one of them is `success`.
	"""
	_check()
	s = _load(jar)
	timeout = frappe.utils.cint(timeout) or 60
	try:
		page = s.get(url, timeout=timeout)
		parser = _Forms()
		parser.feed(page.text)
		forms = [f for f in parser.forms if f["method"].lower() == "post"] or parser.forms
		idx = frappe.utils.cint(form_index)
		if not forms or idx >= len(forms):
			_save(jar, s)
			return {"ok": False, "status": page.status_code, "url": page.url, "alerts": _alerts(page.text),
					"error": "No form found on the page", "text": page.text[:MAX_TEXT]}
		form = forms[idx]
		payload = dict(form["fields"])
		payload.update({k: "" if v is None else str(v) for k, v in (_parse(fields, {}) or {}).items()})
		target = urljoin(page.url, form["action"]) if form["action"] else page.url
		r = s.post(target, data=payload, timeout=timeout)
	except requests.RequestException as e:
		return {"ok": False, "status": 0, "url": url, "error": str(e)[:500], "alerts": [], "text": ""}
	_save(jar, s)
	alerts = _alerts(r.text)
	return {
		"ok": any(a["kind"] == "success" for a in alerts),
		"status": r.status_code,
		"url": r.url,
		"posted_to": target,
		"fields_sent": sorted(k for k in payload if "token" not in k.lower()),
		"alerts": alerts,
		"text": r.text[:MAX_TEXT],
	}


@frappe.whitelist()
def jar_info(jar):
	"""Cookie names, domains and expiry -- never the values."""
	_check()
	return [
		{"name": c.name, "domain": c.domain, "expires": c.expires, "secure": c.secure}
		for c in _load(jar).cookies
	]


@frappe.whitelist(methods=["POST"])
def clear_jar(jar):
	"""Forget every cookie in the jar (a logout that does not ask the server)."""
	_check()
	remove_encrypted_password(SETTINGS, SETTINGS, _jar_field(jar))
	frappe.db.commit()
	return True
