"""
app_apis.pilot_admin -- the Pilot GPS **admin** connection (REST API v3).

Not to be confused with `app_apis.connector`
--------------------------------------------
There are now two Pilot connections in this app, and they are different
integrations that happen to share a vendor:

    app_apis.connector      Legacy `api.php?cmd=status`, HTTP Basic auth.
                            ONE ACCOUNT PER CUSTOMER -- the email is read off
                            the xticket or the Customer, and the module retries
                            each candidate until one is entitled to the IMEI.
                            Answers a single question: "what is this device
                            doing right now".

    THIS MODULE             REST API v3, Bearer token. UP TO TWO ADMIN
                            ACCOUNTS FOR THE WHOLE SITE, held in `app_apis`
                            (Pilot Admin Connection / Pilot Admin Connection
                            2) -- two separate estates, not a primary and a
                            backup. Reaches the partner-level surface the
                            customer accounts cannot see: the account list,
                            the users inside an account, the full vehicle list,
                            plate -> IMEI resolution. `_settings(account=1|2)`
                            picks which; most callers loop over
                            `enabled_admin_accounts()` instead of hard-coding
                            one.

They are kept apart rather than merged because the credential model is the
opposite way round in each. Folding the admin login into `connector._settings`
would mean one field whose meaning flips depending on which function read it,
which is exactly the collision the `pilot_` / `im_` prefixes were introduced to
prevent.

Architecture
------------
    Desk button / server code
          |  frappe.call("app_apis.pilot_admin.test_connection")
          v
    THIS MODULE  -- mints and caches a Bearer token, adds the node header,
                    issues the request, decodes the body, returns a clean dict
          |  HTTPS (requests)
          v
    https://<server>/api/v3/...

What the v3 API's shape forces on this module
---------------------------------------------
1. TOKEN, NOT BASIC AUTH. `POST /api/v3/auth/token` takes `{username,
   password}` as JSON and answers `{token, expires_in, node_id}`. Every later
   call sends `Authorization: Bearer {token}`. Minting one per request would be
   both wasteful and an easy way to get throttled, so the token is cached (see
   `pilot_admin_token_ttl_minutes`).

2. THE NODE HEADER IS AMBIGUOUS IN THE SPEC, so this module sends BOTH
   spellings. v3.en.yaml names the header `X-Node` in `components.parameters`
   (which is what Swagger actually sends) but calls it `X-Node-Id` in the prose
   of `info.description` and of the token endpoint. Sending an extra header a
   server does not read costs nothing; sending the wrong one of the two costs a
   silent route to the wrong cluster node. So both go out, always.

3. `/api/v3/ping` IS NOT AN UNAUTHENTICATED HEALTH CHECK, whatever the spec
   says. The spec marks it `security: []`; the live KSA and production servers
   both answer it with HTTP 401 `{"code":1,"msg":"Unauthorized"}`. Verified by
   hand before this module was written. So there is no anonymous reachability
   probe available, and `test_connection` proves reachability by signing in --
   which is the only thing the server will actually confirm.

4. TWO ERROR SHAPES. The spec documents `{"code","msg","error"}` with `error` a
   string; the live server sends `{"code","msg","errors":[],"process_time"}`
   with an `errors` ARRAY. `_explain` reads either, so a future flip between
   them does not turn a useful message into "code 1".

5. THE TOKEN EXPIRES ON PILOT'S CLOCK, NOT OURS. `expires_in` is a hint, and a
   token can also be revoked server-side (`DELETE /api/v3/auth/token`) or die
   with a node failover. So a 401 on an ordinary request is not reported to the
   caller: the cached token is dropped, one fresh sign-in happens, and the
   request is replayed exactly once. Only a second 401 is a real failure.

Public surface
--------------
Whitelisted (reachable from the browser), all READ-ONLY and role-gated:

    test_connection        get_accounts          get_account_users
    get_vehicles           find_imei_by_plate    get_vehicle_status
    clear_cache_from_desk

Everything the v3 API can WRITE -- create/delete users, change a user's login
and password, replace a user's vehicle or folder access, block a vehicle -- is
deliberately NOT whitelisted and has no wrapper here. Those endpoints are one
line away through `request()`, from `bench console`, by someone who meant it.
An admin token that can delete a customer's user accounts does not get a URL.

This app defines no business doctypes. This module owns only its own half of
the `app_apis` Single (the `pilot_admin_` fields).
"""

import json
import time

import frappe
from frappe import _

API = "/api/v3"

CACHE_PREFIX = "pilot_admin"

# Pilot's own `role_id` values, from UserCreateRequest in v3.en.yaml. Used only
# to label what `auth/me` reports, never to make a decision.
ROLE_NAMES = {0: "Administrator", 1: "User", 2: "Logistic"}

# The servers v3.en.yaml lists under `servers:`. Not enforced -- a deployment
# may sit behind its own hostname -- but shown in the settings field
# description so nobody has to guess the regional endpoint.
KNOWN_SERVERS = (
	"https://pilot-gps.com",
	"https://ksa.pilot-gps.com",
	"https://global.pilot-gps.com",
	"https://pilot-gps.africa",
	"https://blade.pilot-gps.com",
	"https://boooch.pilot-gps.com",
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _settings(account: int = 1) -> dict:
	"""The `pilot_admin_` (or `pilot_admin2_`) half of the `app_apis` doctype.

	Two Pilot Administrator accounts can be configured -- two different
	estates, not a primary and a backup. `account` selects which: 1 is the
	original ("Pilot (WSL)"), 2 is the second, unrelated one. Everything that
	takes a `settings` dict downstream is account-agnostic; only this function
	and `_password` know the field-name prefix.

	`base_url` is stripped of a trailing slash AND of a trailing `/api/v3`:
	the legacy Pilot field holds a full endpoint path
	(`https://ksa.pilot-gps.com/api/api.php`), so somebody configuring this one
	by analogy will paste a path here too. Normalising costs one line and
	prevents a 404 whose cause is invisible in the message.
	"""
	account = frappe.utils.cint(account) or 1
	prefix = "pilot_admin" if account == 1 else "pilot_admin2"
	# "Pilot (WSL)" is the operator's own name for the first account, not an
	# abbreviation this module invented -- shown in error messages and in the
	# Fleet Audit's per-vehicle Pilot dialog so a finding is traceable to the
	# right estate at a glance. It is deliberately NOT used to name which App
	# APIs field is missing (see `_require_configured`) -- those messages name
	# the actual field labels ("Pilot Admin Base URL (2)", etc).
	label = "Pilot (WSL)" if account == 1 else "Pilot Admin 2"

	s = frappe.get_cached_doc("app_apis")

	base = str(s.get(prefix + "_base_url") or "").strip().rstrip("/")
	for suffix in ("/api/v3", "/api/api.php", "/api"):
		if base.lower().endswith(suffix):
			base = base[: -len(suffix)].rstrip("/")
			break

	return {
		"enabled": bool(frappe.utils.cint(s.get(prefix + "_enabled"))),
		"base_url": base,
		"username": str(s.get(prefix + "_username") or "").strip(),
		# Blank is meaningful: "use whatever node the token response issued".
		# That is why this is Data and not Int -- Int cannot tell blank from 0.
		"node": str(s.get(prefix + "_node") or "").strip(),
		"timeout": frappe.utils.cint(s.get(prefix + "_request_timeout")) or 20,
		"token_ttl": (frappe.utils.cint(s.get(prefix + "_token_ttl_minutes")) or 45) * 60,
		"doc": s,
		"account_no": account,
		"label": label,
		"pw_field": prefix + "_password",
		"conf_key": prefix + "_password",
	}


def _password(settings: dict) -> str | None:
	"""The one admin password.

	An explicit `password` on the settings dict wins over everything -- that is
	how `settings_for()` lets a caller sign in as an account other than the one
	on the form. Then `pilot_admin_password` in site_config, so a deployment can
	keep the secret out of the database entirely; otherwise the encrypted field
	on `app_apis`. Same order as IM, for the same reason.
	"""
	if settings.get("password"):
		return str(settings["password"])

	conf_pw = frappe.conf.get(settings.get("conf_key", "pilot_admin_password"))
	if conf_pw:
		return str(conf_pw)

	doc = settings.get("doc")
	if not doc:
		return None
	pw_field = settings.get("pw_field", "pilot_admin_password")
	return doc.get_password(pw_field) if doc.get(pw_field) else None


def settings_for(username: str, password: str, base_url: str = "", node: str = "") -> dict:
	"""A settings dict for ONE named account, usable anywhere `settings=` is.

	v3 is not an admin-only API: `POST /api/v3/auth/token` accepts any Pilot
	login, and `GET /api/v3/vehicles` then returns that account's own fleet.
	Verified against this deployment -- a customer account signs in and lists
	its 814 vehicles. So the same transport serves both "the admin account" and
	"walk every customer account", and only the credentials differ.

	Server and node default to whatever the Pilot Admin section holds, so a
	caller only has to supply what actually varies.

	NOT whitelisted, and it must not be: it takes a password as an argument.
	"""
	base = _settings()

	server = (base_url or base["base_url"] or "").strip().rstrip("/")
	if not server:
		# The Pilot Admin section may be blank while the legacy Pilot Connection
		# is fully configured -- and a sweep of CUSTOMER accounts depends on the
		# legacy section's credentials, not on the admin one. Derive the v3
		# server from the legacy endpoint rather than refusing to run.
		legacy = str(frappe.get_cached_doc("app_apis").pilot_base_url or "").strip()
		for suffix in ("/api/api.php", "/api/v3", "/api", "/"):
			if legacy.endswith(suffix):
				legacy = legacy[: -len(suffix)]
				break
		server = legacy.rstrip("/")

	return {
		"enabled": True,
		"base_url": server,
		"username": str(username or "").strip(),
		"password": str(password or ""),
		"node": str(node or base["node"] or "").strip(),
		"timeout": base["timeout"],
		"token_ttl": base["token_ttl"],
		"doc": None,
	}


def _require_configured(settings: dict) -> None:
	"""Refuse early, and say which field is missing.

	A whitelisted method that reaches Pilot with no username produces a 401,
	and a 401 reads as "wrong password" to everyone who sees it. Naming the
	empty field here is the difference between a five-second fix and an hour
	spent re-typing a password that was never wrong.
	"""
	label = settings.get("label", "Pilot Admin")
	account_no = settings.get("account_no", 1)
	# The field LABELS in App APIs, which is what a "go set this" message has
	# to name -- distinct from `label`, the operator's own nickname for the
	# account, which names WHICH estate a finding or error is about.
	checkbox = "Pilot Admin Enabled" if account_no == 1 else "Pilot Admin 2 Enabled"
	field_suffix = "" if account_no == 1 else " (2)"

	if not settings["enabled"]:
		frappe.throw(
			_("The {0} connection is switched off. Tick '{1}' in App APIs.").format(label, checkbox),
			title=_(label),
		)

	missing = []
	if not settings["base_url"]:
		missing.append(_("Pilot Admin Base URL{0}").format(field_suffix))
	if not settings["username"]:
		missing.append(_("Pilot Admin Login{0}").format(field_suffix))
	if not _password(settings):
		missing.append(_("Pilot Admin Password{0}").format(field_suffix))

	if missing:
		frappe.throw(
			_("{0} is not configured. Missing: {1}.").format(label, ", ".join(missing)),
			title=_(label),
		)


# --------------------------------------------------------------------------
# Cache -- the token, and nothing else
# --------------------------------------------------------------------------


def _cache_key(*parts) -> str:
	return "::".join([CACHE_PREFIX, *[str(p) for p in parts]])


def _token_key(settings: dict) -> str:
	"""Keyed by account AND server.

	Both belong in the key: a token minted against the KSA server is not valid
	on production, and a token minted for one login is not the other login's.
	Including both means changing either field cannot serve a stale token, even
	if `on_update` never fired.
	"""
	return _cache_key("token", settings["username"], settings["base_url"])


def _cache_write(key: str, value: str, ttl: int) -> None:
	"""Write a value with a TTL, and undo Frappe's negative caching of the miss.

	This exists because of a trap in `frappe.cache` that silently defeats every
	TTL'd cache in Frappe, verified against this bench:

	    RedisWrapper.get_value()  on a MISS does `frappe.local.cache[key] = None`
	    RedisWrapper.set_value()  writes `frappe.local.cache[key]` ONLY when no
	                              `expires_in_sec` was given

	So the sequence "miss, then store with a TTL" leaves the process-local dict
	holding None for that key, and `get_value` short-circuits on it for the rest
	of the process's life -- returning None while Redis holds a perfectly good
	value. The cache appears to work (Redis really has the token) and never
	hits, so every call re-signs-in. Popping the local entry after the write is
	the whole fix.
	"""
	frappe.cache.set_value(key, value, expires_in_sec=ttl)
	frappe.local.cache.pop(frappe.cache.make_key(key), None)


def _cache_drop(key: str) -> None:
	"""Forget one key in Redis AND in this process. See `_cache_write`."""
	frappe.cache.delete_value(key)
	frappe.local.cache.pop(frappe.cache.make_key(key), None)


def clear_cache() -> None:
	"""Drop the cached token.

	Called from `app_apis.on_update`: once the login, password or server
	changes, a cached token belongs to the previous configuration.
	`delete_keys` takes a prefix and handles the site-key mangling itself; the
	process-local sweep afterwards is the same point `_cache_write` makes.
	"""
	frappe.cache.delete_keys(CACHE_PREFIX)

	for key in [k for k in frappe.local.cache if CACHE_PREFIX in str(k)]:
		frappe.local.cache.pop(key, None)


# --------------------------------------------------------------------------
# Layer 1: HTTP. Internal -- the browser cannot reach any of this.
# --------------------------------------------------------------------------


def _fail(code: int, msg: str, meta: dict) -> dict:
	"""A failure shaped like a success, so callers never branch on exceptions.

	Pilot's own failures keep Pilot's positive `code` (v3 uses 0 for OK and 1
	for everything else). Transport and auth failures use NEGATIVE codes, so
	they can never collide with a value Pilot itself might send:

	    -401 credentials rejected      -404 endpoint/record not found
	    -403 forbidden                 -408 timed out
	    -502 could not connect         -500 unexpected / undecodable response
	"""
	return {"code": code, "msg": msg, "_pilot_admin": meta}


def _decode(resp) -> tuple[object, str | None]:
	"""(body, error). Never trusts the content-type header.

	The legacy Pilot endpoint answers `text/json;charset=UTF-8`, which is what
	made `frappe.integrations.utils.make_get_request` hand back a raw string
	there (see `app_apis.connector`). v3 currently answers `application/json`,
	but decoding explicitly costs nothing and means a vendor-side header change
	cannot resurrect that failure mode here.
	"""
	try:
		return resp.json(), None
	except ValueError:
		pass

	text = (resp.text or "").strip().lstrip("﻿")
	try:
		return json.loads(text), None
	except ValueError:
		return None, f"Pilot returned non-JSON ({len(resp.text or '')} bytes): {text[:160]!r}"


def _explain(body, fallback: str) -> str:
	"""The most useful sentence available out of a v3 error body.

	Two shapes in the wild -- the spec's `error` string and the live server's
	`errors` array -- so read whichever is present rather than picking one and
	being wrong half the time.
	"""
	if not isinstance(body, dict):
		return fallback

	msg = str(body.get("msg") or "").strip()

	extra = body.get("error") or body.get("errors")
	if isinstance(extra, list):
		extra = "; ".join(str(e) for e in extra if e)
	extra = str(extra or "").strip()

	if msg and extra:
		return f"{msg} -- {extra}"
	return msg or extra or fallback


def _auth_headers(token: str, node: str | None) -> dict:
	headers = {
		"Authorization": f"Bearer {token}",
		"Accept": "application/json",
	}
	if node:
		# Both spellings, deliberately. See note 2 in the module docstring.
		headers["X-Node"] = str(node)
		headers["X-Node-Id"] = str(node)
	return headers


def _mint_token(settings: dict) -> tuple[str | None, str | None, dict]:
	"""Sign in. Returns (token, node, meta). NEVER raises for a bad credential.

	The node returned by Pilot is overridden by `pilot_admin_node` when that
	field is set, because a deployment can be pinned to a node the token
	response reports as 0.
	"""
	meta = {
		"account": settings["username"],
		"base_url": settings["base_url"],
		"http_status": None,
		"elapsed_ms": 0,
		"source": "fresh",
	}

	password = _password(settings)
	if not settings["username"] or not password:
		meta["error"] = (-500, "Pilot Admin login or password is not set in app_apis settings.")
		return None, None, meta

	import requests

	url = settings["base_url"] + API + "/auth/token"
	started = time.monotonic()
	try:
		resp = requests.post(
			url,
			json={"username": settings["username"], "password": password},
			headers={"Accept": "application/json"},
			timeout=settings["timeout"],
		)
		meta["http_status"] = resp.status_code
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)

	except requests.exceptions.Timeout:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		meta["error"] = (-408, f"timed out after {settings['timeout']}s signing in to Pilot.")
		return None, None, meta

	except requests.exceptions.RequestException as e:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		meta["error"] = (-502, f"could not reach {url}: {e.__class__.__name__}: {str(e)[:160]}")
		return None, None, meta

	body, decode_error = _decode(resp)
	if decode_error:
		meta["error"] = (-500, decode_error)
		return None, None, meta

	if resp.status_code == 401:
		meta["error"] = (
			-401,
			_explain(body, "401 Unauthorized")
			+ " -- Pilot rejected the admin login. This is an account problem at "
			"Pilot's end, not a bug in this integration. Note that v3 signs in "
			"with the account LOGIN, which is not necessarily the email address "
			"the legacy api.php connection uses.",
		)
		return None, None, meta

	if resp.status_code >= 400 or not isinstance(body, dict):
		meta["error"] = (-500, _explain(body, f"Pilot returned HTTP {resp.status_code} from {url}."))
		return None, None, meta

	token = str(body.get("token") or "").strip()
	if not token:
		meta["error"] = (-500, _explain(body, "Pilot accepted the request but returned no token."))
		return None, None, meta

	# `node_id: 0` means "no routing needed", so it must not become the string
	# "0" in a header. A configured node always wins over the issued one.
	issued = str(body.get("node_id") or "").strip()
	node = settings["node"] or (issued if issued not in ("", "0") else "")

	meta["expires_in"] = frappe.utils.cint(body.get("expires_in"))
	meta["issued_node"] = issued or None
	meta["node"] = node or None
	meta["token_length"] = len(token)
	return token, node or None, meta


def _get_token(settings: dict, force: bool = False) -> tuple[str | None, str | None, dict]:
	"""Cached token, or a fresh one. Returns (token, node, meta).

	`expires_in` is trusted only as an upper bound and shortened by 60s, so a
	token is never presented in the last minute of its life -- that window is
	where a "worked a second ago" 401 comes from. The configured TTL caps it in
	the other direction for a server that reports an implausibly long life.
	"""
	key = _token_key(settings)

	if not force:
		cached = frappe.cache.get_value(key)
		if cached:
			try:
				payload = json.loads(cached) if isinstance(cached, str) else cached
			except ValueError:
				payload = None
			if isinstance(payload, dict) and payload.get("token"):
				return (
					str(payload["token"]),
					payload.get("node"),
					{"source": "cache", "elapsed_ms": 0, "account": settings["username"]},
				)

	token, node, meta = _mint_token(settings)
	if not token:
		return None, None, meta

	ttl = settings["token_ttl"]
	expires_in = frappe.utils.cint(meta.get("expires_in"))
	if expires_in > 0:
		ttl = min(ttl, max(expires_in - 60, 60))

	_cache_write(key, json.dumps({"token": token, "node": node}), ttl)
	meta["cached_for"] = ttl
	return token, node, meta


def request(
	path: str,
	method: str = "GET",
	params: dict | None = None,
	body: dict | None = None,
	settings: dict | None = None,
	_retry: bool = True,
) -> dict:
	"""One v3 request, with sign-in, the node header and one 401 replay.

	THE door every other function in this module goes through, and the door to
	the endpoints that have no wrapper here. From `bench console`:

	    from app_apis import pilot_admin
	    pilot_admin.request("/vehicles/by-plate", params={"vehnum": "1234-ABC"})

	`path` is relative to `/api/v3` -- pass "/accounts", not "/api/v3/accounts".

	NOT whitelisted, and it must stay that way: this signature can reach
	`DELETE /users` with an admin token behind it.

	Always returns a dict. Never raises for a Pilot-side failure; see `_fail`
	for the negative-code convention.
	"""
	settings = settings or _settings()

	token, node, tmeta = _get_token(settings)
	if not token:
		code, msg = tmeta.get("error", (-500, "could not obtain a Pilot admin token."))
		return _fail(code, msg, tmeta)

	meta = {
		"account": settings["username"],
		"path": path,
		"method": method.upper(),
		"node": node,
		"token_source": tmeta.get("source"),
		"http_status": None,
		"elapsed_ms": 0,
		"retried": not _retry,
	}

	import requests

	url = settings["base_url"] + API + "/" + path.lstrip("/")
	started = time.monotonic()
	try:
		resp = requests.request(
			method.upper(),
			url,
			headers=_auth_headers(token, node),
			params=params or None,
			json=body if body is not None else None,
			timeout=settings["timeout"],
		)
		meta["http_status"] = resp.status_code
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)

	except requests.exceptions.Timeout:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		return _fail(-408, f"timed out after {settings['timeout']}s waiting for {path}.", meta)

	except requests.exceptions.RequestException as e:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		return _fail(-502, f"could not reach {url}: {e.__class__.__name__}: {str(e)[:160]}", meta)

	# A 401 here is far more often an expired or revoked token than a bad
	# password -- the password already worked when the token was minted. So
	# drop it, sign in once, and replay. Only the second 401 is reported.
	if resp.status_code == 401 and _retry:
		_cache_drop(_token_key(settings))
		return request(path, method, params, body, settings, _retry=False)

	body_out, decode_error = _decode(resp)
	if decode_error:
		return _fail(-500, decode_error, meta)

	if resp.status_code == 401:
		return _fail(-401, _explain(body_out, "401 Unauthorized") + " (after one re-sign-in).", meta)
	if resp.status_code == 403:
		return _fail(
			-403, _explain(body_out, f"403 Forbidden for {path} -- partner access required."), meta
		)
	if resp.status_code == 404:
		return _fail(-404, _explain(body_out, f"404 Not Found for {url}."), meta)
	if resp.status_code >= 400:
		return _fail(-500, _explain(body_out, f"Pilot returned HTTP {resp.status_code}."), meta)

	if not isinstance(body_out, dict):
		return _fail(-500, f"Pilot returned {type(body_out).__name__}, expected an object.", meta)

	body_out["_pilot_admin"] = meta
	return body_out


def _ok(result: dict) -> bool:
	"""v3 says 0 for OK. A transport failure carries a negative code."""
	return frappe.utils.cint(result.get("code")) == 0


def _throw_if_failed(result: dict, what: str) -> dict:
	if _ok(result):
		return result

	meta = result.get("_pilot_admin") or {}
	frappe.throw(
		_("Pilot Admin could not {0}.").format(what)
		+ "\n\n"
		+ f"[{result.get('code')}] {result.get('msg')}"
		+ "\n\n"
		+ _("Account: {0} · Node: {1} · HTTP: {2} · {3} ms").format(
			meta.get("account") or "-",
			meta.get("node") or "-",
			meta.get("http_status") if meta.get("http_status") is not None else "-",
			meta.get("elapsed_ms") or 0,
		),
		title=_("Pilot Admin"),
	)


# --------------------------------------------------------------------------
# Layer 2: the whitelisted, READ-ONLY surface
# --------------------------------------------------------------------------

# Accounts and their users are partner-level data about other companies, so
# they are kept to the two roles that administer this site. Vehicle reads are
# the same thing Support already sees on a ticket, so Support Team keeps those.
ADMIN_ROLES = ["System Manager", "Technical"]
READ_ROLES = ["System Manager", "Technical", "Support Team"]


def enabled_admin_accounts() -> list[int]:
	"""Every admin account (1, 2 or both) that is enabled AND has credentials.

	Falls back to [1] when neither is usable, so every caller that loops over
	this list still produces the ORIGINAL "not configured" error against
	account 1 rather than silently doing nothing when nothing is set up yet.
	"""
	out = [n for n in (1, 2) if _is_usable(_settings(n))]
	return out or [1]


def _is_usable(settings: dict) -> bool:
	return bool(settings["enabled"] and settings["username"] and _password(settings))


@frappe.whitelist()
def test_connection(account: int = 1) -> dict:
	"""Sign in, then ask Pilot who we are. Drives the button on the settings form.

	Forces a fresh token rather than reporting on a cached one: "the token we
	minted an hour ago still parses" is not the question anybody presses this
	button to answer.
	"""
	frappe.only_for(ADMIN_ROLES)

	settings = _settings(account)
	_require_configured(settings)

	started = time.monotonic()
	token, node, meta = _get_token(settings, force=True)
	elapsed = int((time.monotonic() - started) * 1000)

	if not token:
		code, msg = meta.get("error", (-500, _("Pilot did not return a token.")))
		return {
			"ok": False,
			"account": settings["username"],
			"base_url": settings["base_url"],
			"node": settings["node"] or None,
			"elapsed_ms": elapsed,
			"message": f"[{code}] {msg}",
		}

	# The token is proof the credentials are good; auth/me is proof the token
	# is actually usable on this node, which is the failure the node header
	# exists to prevent -- and the only way to see it is to make one real call.
	me = request("/auth/me", settings=settings)
	elapsed = int((time.monotonic() - started) * 1000)

	if not _ok(me):
		return {
			"ok": False,
			"account": settings["username"],
			"base_url": settings["base_url"],
			"node": node,
			"elapsed_ms": elapsed,
			"message": _("Signed in ({0}-character token), but /auth/me failed: [{1}] {2}").format(
				meta.get("token_length") or 0, me.get("code"), me.get("msg")
			),
		}

	user = me.get("user") or {}
	role_id = user.get("role_id")
	role = ROLE_NAMES.get(frappe.utils.cint(role_id), f"role {role_id}")
	return {
		"ok": True,
		"account": settings["username"],
		"base_url": settings["base_url"],
		"node": node,
		"elapsed_ms": elapsed,
		"user": {
			"name": user.get("name"),
			"username": user.get("username"),
			"email": user.get("email"),
			"usr_id": user.get("usr_id"),
			"account_id": user.get("account_id"),
			"partner_id": user.get("partner_id"),
			"node_id": user.get("node_id"),
			"role_id": role_id,
			"role": role,
			"ip_filter": bool(frappe.utils.cint(user.get("ips_on"))),
		},
		"message": _("Signed in as {0} ({1}), account {2}, node {3}.").format(
			user.get("name") or user.get("username") or settings["username"],
			role,
			user.get("account_id"),
			node or user.get("node_id") or "-",
		),
	}


@frappe.whitelist()
def get_accounts() -> dict:
	"""`GET /accounts` -- every account under this partner."""
	frappe.only_for(ADMIN_ROLES)

	settings = _settings()
	_require_configured(settings)

	result = _throw_if_failed(request("/accounts", settings=settings), _("list accounts"))
	accounts = result.get("accounts") or []
	return {
		"count": len(accounts),
		"accounts": accounts,
		"diagnostics": result.get("_pilot_admin"),
	}


@frappe.whitelist()
def get_account_users(account_id) -> dict:
	"""`GET /accounts/users` -- the users inside one account."""
	frappe.only_for(ADMIN_ROLES)

	account_id = frappe.utils.cint(account_id)
	if not account_id:
		frappe.throw(_("account_id is required."), title=_("Pilot Admin"))

	settings = _settings()
	_require_configured(settings)

	result = _throw_if_failed(
		request("/accounts/users", params={"account_id": account_id}, settings=settings),
		_("list the users of account {0}").format(account_id),
	)
	users = result.get("users") or []
	return {
		"account_id": account_id,
		"count": len(users),
		"users": users,
		"diagnostics": result.get("_pilot_admin"),
	}


@frappe.whitelist()
def get_vehicles() -> dict:
	"""`GET /vehicles` -- every vehicle the admin account can see."""
	frappe.only_for(READ_ROLES)

	settings = _settings()
	_require_configured(settings)

	result = _throw_if_failed(request("/vehicles", settings=settings), _("list vehicles"))
	vehicles = result.get("data") or []
	return {
		"count": len(vehicles),
		"vehicles": vehicles,
		"diagnostics": result.get("_pilot_admin"),
	}


@frappe.whitelist()
def find_imei_by_plate(plate: str) -> dict:
	"""`GET /vehicles/by-plate` -- plate -> IMEI, admin-wide.

	Pilot matches `vehnum` as it is stored on Pilot's side, which is not
	necessarily how the plate is spelled in the ERP. A miss is reported as a
	miss rather than thrown, because "no vehicle with that plate" is a normal
	answer to a lookup, not a broken connection.
	"""
	frappe.only_for(READ_ROLES)

	plate = str(plate or "").strip()
	if not plate:
		frappe.throw(_("plate is required."), title=_("Pilot Admin"))

	settings = _settings()
	_require_configured(settings)

	result = request("/vehicles/by-plate", params={"vehnum": plate}, settings=settings)

	if frappe.utils.cint(result.get("code")) == -404:
		return {"plate": plate, "found": False, "imei": None, "message": result.get("msg")}

	_throw_if_failed(result, _("look up plate {0}").format(plate))

	imei = str(result.get("imei") or "").strip()
	return {
		"plate": plate,
		"found": bool(imei),
		"imei": imei or None,
		"message": result.get("msg"),
		"diagnostics": result.get("_pilot_admin"),
	}


@frappe.whitelist()
def get_vehicle_status(imei: str, account: int = 0) -> dict:
	"""`GET /vehicles/status` -- current status, from the ADMIN account(s).

	The v3 replacement for the legacy `cmd=status` that `app_apis.connector`
	calls, and the reason it is worth having twice: the legacy path can only
	see a device if one of the CUSTOMER's own accounts is entitled to it, so a
	device whose customer email is wrong in the ERP is invisible there and
	visible here. Useful for exactly that diagnosis, not as a replacement --
	Check Pilot on a ticket stays the customer-facing read.

	`account` picks one estate (1 or 2). Left at 0 (the default), every
	configured account is tried in turn and the first that has the device
	wins -- the caller (a click on a Fleet Audit row) has no way to know in
	advance which of the two estates a device lives on.
	"""
	frappe.only_for(READ_ROLES)

	imei = str(imei or "").strip()
	if not imei:
		frappe.throw(_("imei is required."), title=_("Pilot Admin"))

	accounts = [frappe.utils.cint(account)] if frappe.utils.cint(account) else enabled_admin_accounts()

	tried = []
	last_result = None
	for acc in accounts:
		settings = _settings(acc)
		if not _is_usable(settings):
			continue
		result = request("/vehicles/status", params={"imei": imei}, settings=settings)
		last_result = (acc, settings, result)
		tried.append(settings.get("label"))

		if _ok(result):
			devices = result.get("data") or []
			if devices:
				return {
					"imei": imei,
					"found": True,
					"device": devices[0],
					"count": len(devices),
					"account_no": acc,
					"account_label": settings.get("label"),
					"tried": tried,
					"diagnostics": result.get("_pilot_admin"),
				}

	if last_result is None:
		frappe.throw(
			_("No Pilot Admin connection is configured. Set one up in App APIs."),
			title=_("Pilot Admin"),
		)

	acc, settings, result = last_result
	if not _ok(result):
		_throw_if_failed(result, _("read the status of {0}").format(imei))

	return {
		"imei": imei,
		"found": False,
		"device": None,
		"count": 0,
		"tried": tried,
		"diagnostics": result.get("_pilot_admin"),
	}


@frappe.whitelist()
def clear_cache_from_desk(account: int = 1) -> dict:
	frappe.only_for(ADMIN_ROLES)
	# clear_cache() sweeps every key under CACHE_PREFIX, which covers both
	# accounts' tokens -- there is no per-account cache worth keeping separate.
	clear_cache()
	return {"ok": True}


# --------------------------------------------------------------------------
# Introspection -- NOT whitelisted; `bench console` only, by design.
# --------------------------------------------------------------------------


def status(account: int = 1) -> dict:
	"""What is configured, without revealing any of it."""
	cfg = _settings(account)
	cached = frappe.cache.get_value(_token_key(cfg))
	return {
		"enabled": cfg["enabled"],
		"base_url": cfg["base_url"],
		"username": cfg["username"],
		"node": cfg["node"] or "(as issued by Pilot)",
		"timeout": cfg["timeout"],
		"token_ttl_seconds": cfg["token_ttl"],
		"password_set": bool(_password(cfg)),
		"password_from_site_config": bool(frappe.conf.get("pilot_admin_password")),
		"token_cached": bool(cached),
		"known_servers": list(KNOWN_SERVERS),
		"api_prefix": API,
	}


def logout() -> dict:
	"""`DELETE /auth/token` -- revoke the current token at Pilot, then locally.

	Not whitelisted and not wired to a button: revoking is a thing to do
	deliberately, and `clear_cache_from_desk` already covers "stop using the
	token I have" without asking Pilot to invalidate a token other workers may
	still be holding.
	"""
	settings = _settings()
	result = request("/auth/token", method="DELETE", settings=settings)
	clear_cache()
	return result


# ==========================================================================
# The Administrator API  --  <server>/backend/api.php
# ==========================================================================
#
# A SECOND protocol on the same connection, and the important one.
#
#     v3 REST     ksa.pilot-gps.com/api/v3/...          Bearer token
#                 Scope: the signed-in account only. Proven five ways --
#                 /vehicles ignores account_id, /vehicles/status 404s on an
#                 IMEI from another account, and the legacy cmd=list is
#                 account-scoped too. It CANNOT enumerate the estate.
#
#     Admin API   admksa.pilot-gps.com/backend/api.php  HTTP Basic
#                 Scope: EVERYTHING. `cmd=vehicles` returns all 24,085
#                 devices in one request, `cmd=accountslist` all 1,490
#                 accounts. This is what the Fleet Audit uses.
#
# They share the credentials in the Pilot Admin Connection section and nothing
# else -- different host, different auth, different command vocabulary. Hence
# a separate client rather than a flag on the v3 one.
#
# Documented at docs.pilot-gps.com under "Administrator API".

BACKEND_PATH = "/backend/api.php"


def backend_request(cmd: str, params: dict | None = None, settings: dict | None = None,
                    timeout: int | None = None) -> dict:
	"""One Administrator API call. Never raises for a Pilot-side failure.

	Basic auth, not Bearer: this endpoint predates v3 and authenticates the
	admin account the way the other api.php endpoints do. An unauthenticated
	request answers `401 Need authentication` in PLAIN TEXT, not JSON, which is
	why the body is decoded defensively.

	Returns the usual shape: `code` 0 on success with the payload under `data`,
	a negative code on transport failure, `_pilot_admin` carrying diagnostics.
	"""
	settings = settings or _settings()

	meta = {
		"account": settings["username"],
		"cmd": cmd,
		"api": "administrator",
		"http_status": None,
		"elapsed_ms": 0,
	}

	password = _password(settings)
	if not settings["username"] or not password:
		return _fail(-500, "Pilot Admin login or password is not set in app_apis settings.", meta)

	base = settings["base_url"]
	if not base:
		return _fail(-500, "Pilot Admin Base URL is not set in app_apis settings.", meta)

	import requests

	url = base + BACKEND_PATH
	query = {"cmd": cmd}
	query.update(params or {})

	started = time.monotonic()
	try:
		resp = requests.get(
			url,
			auth=(settings["username"], password),  # HTTP Basic
			params=query,
			headers={"Accept": "application/json"},
			timeout=timeout or max(settings["timeout"], 240),
		)
		meta["http_status"] = resp.status_code
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)

	except requests.exceptions.Timeout:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		return _fail(-408, f"timed out waiting for the Administrator API (cmd={cmd}).", meta)

	except requests.exceptions.RequestException as e:
		meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
		return _fail(-502, f"could not reach {url}: {e.__class__.__name__}: {str(e)[:160]}", meta)

	if resp.status_code == 401:
		# Plain text, not JSON -- decoding first would lose the message.
		return _fail(
			-401,
			"401 %s -- the Administrator API rejected these credentials. Note this "
			"endpoint lives on the ADMIN host (admksa...), not the tracking host."
			% (resp.text or "Unauthorized").strip()[:80],
			meta,
		)

	body, decode_error = _decode(resp)
	if decode_error:
		if resp.status_code == 404:
			return _fail(
				-404,
				f"404 for {url} -- the Administrator API is not served here. It lives "
				"on the admin host (e.g. https://admksa.pilot-gps.com), not on the "
				"tracking host.",
				meta,
			)
		return _fail(-500, decode_error, meta)

	if resp.status_code >= 400:
		return _fail(-500, _explain(body, f"Pilot returned HTTP {resp.status_code}."), meta)

	if not isinstance(body, dict):
		return _fail(-500, f"Pilot returned {type(body).__name__}, expected an object.", meta)

	if frappe.utils.cint(body.get("code")) != 0:
		return _fail(
			frappe.utils.cint(body.get("code")) or -500,
			str(body.get("msg") or "the Administrator API refused the request"),
			meta,
		)

	body["_pilot_admin"] = meta
	body.setdefault("data", [])
	return body


def backend_accounts(settings: dict | None = None) -> dict:
	"""`cmd=accountslist` -- every account under this administrator.

	`node` is required by the documentation but the server ignores it here:
	nodes 1..5 all return the same 1,490 rows. Sent anyway, because "documented and
	ignored" can become "documented and enforced" in any release.
	"""
	return backend_request("accountslist", {"node": "1"}, settings)


def backend_vehicles(settings: dict | None = None, include_deleted: bool = True) -> dict:
	"""`cmd=vehicles` -- EVERY device on the estate, in ONE request.

	The documentation marks `account_id` as required and describes the result
	as one account's vehicles. On this deployment the parameter is ignored and
	the whole estate comes back: account 3012 (veh_count 1) and account 66651
	(veh_count 1) both answer with the same 24,085 rows. One is sent anyway so
	the request stays valid against the documented contract.

	`uniqid` is the device identifier and the join key. It is NOT always an
	IMEI: of 24,085 rows, 14,211 are 15-digit IMEIs and the rest are 10-digit
	registration ids, 23-25 digit SIM identifiers and a few oddities. The audit
	joins on the raw value and lets the non-IMEIs simply fail to match, which
	is correct -- they are devices the ERP does not track by serial.
	"""
	params = {"account_id": "1", "node": "1"}
	if include_deleted:
		params["is_show_deleted"] = "1"

	# This one request builds ~24,000 rows server-side and Pilot does not always
	# finish it. Measured behaviour: a success streams for 24-26 seconds; a
	# failure gives up FAST -- HTTP 500 with an empty body after 3-7 seconds --
	# and roughly half of attempts fail depending on how busy the admin host is.
	#
	# So the call is retried rather than reported as broken. The backoff is
	# deliberately generous: hammering a server that is already failing to
	# assemble the response is what turned a working endpoint into a
	# consistently failing one earlier.
	#
	# Retrying is the RIGHT fix here, not a workaround for a fetch that should
	# have been split up. `node` does not partition the result (node=2 returns
	# all 24,085 rows, spanning node_id 2..5) and `account_id` is ignored, so
	# there is no smaller unit to ask for short of one account at a time --
	# 1,346 requests to replace one.
	last = None
	for attempt, wait in enumerate((0, 10, 25, 45, 60), start=1):
		if wait:
			time.sleep(wait)
		last = backend_request("vehicles", params, settings, timeout=300)
		if frappe.utils.cint(last.get("code")) == 0:
			meta = last.get("_pilot_admin") or {}
			meta["attempts"] = attempt
			return last

	meta = (last or {}).get("_pilot_admin") or {}
	meta["attempts"] = 5
	return last


def fetch_estate(settings: dict | None = None) -> dict:
	"""Every Pilot device, normalised, keyed by its `uniqid`.

	Returns {"code": 0, "rows": [...], "_pilot_admin": {...}} or a failure in
	the usual negative-code shape.
	"""
	result = backend_vehicles(settings)
	if frappe.utils.cint(result.get("code")) != 0:
		return result

	out = []
	for row in result.get("data") or []:
		if not isinstance(row, dict):
			continue
		uid = str(row.get("uniqid") or "").strip()
		if not uid or uid == "0":
			continue
		out.append({
			"imei": uid,
			"vehicle_no": str(row.get("vehiclenumber") or "").strip(),
			"folder": str(row.get("folder") or "").strip(),
			"veh_id": row.get("agentid"),
			"type": row.get("type"),
			"model": str(row.get("model") or row.get("configuration") or "").strip(),
			"active": frappe.utils.cint(row.get("active")),
			"node_id": row.get("node_id"),
			"msisdn": str(row.get("msisdn") or "").strip(),
			"account": None,  # cmd=vehicles does not say which account owns the row
			"last_seen_epoch": frappe.utils.cint(row.get("ts")) or None,
		})

	meta = result.get("_pilot_admin") or {}
	meta["returned"] = len(result.get("data") or [])
	meta["with_id"] = len(out)
	return {"code": 0, "msg": "OK", "rows": out, "_pilot_admin": meta}


def _admin_row_to_snapshot(row: dict, account_no: int, label: str, node, sweep_meta: dict | None = None) -> dict:
	"""One `cmd=vehicles` row, shaped like `connector.get_vehicle_live`'s device/state/location.

	Verified empirically (not assumed from the docs): a `cmd=vehicles` row
	carries real live fields -- `lat`, `lon`, `dir`, `speed`, `sats`, `on`
	(ignition), `ts` -- not just static identity. So when the CUSTOMER'S own
	Pilot login is rejected, this is a genuine live-ish read, not a re-hash of
	the last stored snapshot -- just from a slower, whole-estate call instead
	of a single-device one, because the Administrator API has no
	single-device query for it.

	No `sensors_status` here -- `cmd=vehicles` does not carry probe/sensor
	data, only the fields above -- so `probes`/`others` are left empty.
	"""
	lat = row.get("lat")
	lon = row.get("lon")
	try:
		lat = float(lat) if lat not in (None, "", "0") else None
		lon = float(lon) if lon not in (None, "", "0") else None
	except (TypeError, ValueError):
		lat, lon = None, None

	ts = frappe.utils.cint(row.get("ts")) or None
	now_epoch = int(time.time())
	age_seconds = (now_epoch - ts) if ts else None

	from app_apis import connector
	stale_after = connector._settings()["stale_after_minutes"]

	return {
		"device": {
			"imei": str(row.get("uniqid") or "").strip(),
			"name": row.get("vehiclenumber"),
			"folder": row.get("folder"),
			"type": row.get("type"),
			"model": row.get("configuration") or row.get("model"),
			"agentid": row.get("agentid"),
			"driver_name": row.get("driver_name"),
			"driver_phone": row.get("driver_phone"),
			"current_mileage": row.get("current_mileage"),
		},
		"state": {
			"active": frappe.utils.cint(row.get("active")),
			"ignition": row.get("on"),
			"speed": frappe.utils.flt(row.get("speed")),
			"direction": row.get("dir"),
		},
		"last_update": {
			"epoch": ts,
			"age_seconds": age_seconds,
			"is_stale": (age_seconds > stale_after * 60) if age_seconds is not None else None,
		},
		"location": {
			"lat": lat,
			"lon": lon,
			"sats": row.get("sats"),
			"source": "live" if (lat or lon) else "none",
		},
		"stale_after_minutes": stale_after,
		"account": label,
		"account_source": "admin estate sweep -- not the customer's own login",
		"node": node,
		"diagnostics": {
			"http_status": (sweep_meta or {}).get("http_status"),
			"elapsed_ms": (sweep_meta or {}).get("elapsed_ms"),
			"accounts_tried": (sweep_meta or {}).get("attempts"),
		},
		"raw": row,
	}


@frappe.whitelist()
def get_vehicle_live_admin(imei: str) -> dict:
	"""One device, found in the whole-estate sweep -- the fallback for when
	the CUSTOMER'S OWN Pilot login is rejected (see `connector.get_vehicle_live`).

	Slow on purpose: the Administrator API has no single-device query, so this
	re-reads the ENTIRE estate (the same call `backend_vehicles` makes for the
	Fleet Audit refresh, ~25s) and picks out one row -- there is no faster
	admin-scoped path to one device's live data. Tried once per enabled admin
	account, stopping at the first that has the device.

	Raises (via frappe.throw) when no configured account's estate contains
	this IMEI at all.
	"""
	frappe.only_for(READ_ROLES)

	imei = str(imei or "").strip()
	if not imei:
		frappe.throw(_("imei is required."), title=_("Pilot"))

	accounts = enabled_admin_accounts()
	tried = []
	for acc_no in accounts:
		settings = _settings(acc_no)
		if not _is_usable(settings):
			continue
		tried.append(settings["label"])

		result = backend_vehicles(settings)
		if frappe.utils.cint(result.get("code")) != 0:
			continue

		for row in result.get("data") or []:
			if isinstance(row, dict) and str(row.get("uniqid") or "").strip() == imei:
				return _admin_row_to_snapshot(
					row, acc_no, settings["label"], settings.get("node") or row.get("node_id"),
					result.get("_pilot_admin"),
				)

	frappe.throw(
		_("This device was not found in {0}'s estate sweep either.").format(
			" or ".join(tried) if tried else _("any configured Pilot admin account")
		),
		title=_("Pilot: Not Found"),
	)


@frappe.whitelist()
def test_admin_api(account: int = 1) -> dict:
	"""Prove the Administrator API works, for the settings-form button."""
	frappe.only_for(ADMIN_ROLES)

	settings = _settings(account)
	_require_configured(settings)

	started = time.monotonic()
	accounts = backend_accounts(settings)
	elapsed = int((time.monotonic() - started) * 1000)

	if frappe.utils.cint(accounts.get("code")) != 0:
		return {
			"ok": False,
			"account": settings["username"],
			"base_url": settings["base_url"],
			"elapsed_ms": elapsed,
			"message": "[%s] %s" % (accounts.get("code"), accounts.get("msg")),
		}

	rows = accounts.get("data") or []
	vehicles = sum(frappe.utils.cint(a.get("veh_count")) for a in rows if isinstance(a, dict))
	return {
		"ok": True,
		"account": settings["username"],
		"base_url": settings["base_url"],
		"elapsed_ms": elapsed,
		"accounts": len(rows),
		"vehicles": vehicles,
		"message": _("Administrator API answered: {0} accounts, {1} vehicles between them.").format(
			len(rows), vehicles
		),
	}
