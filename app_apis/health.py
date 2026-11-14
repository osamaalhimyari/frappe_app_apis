"""Health of everything app_apis connects to, and alerts when something breaks.

`snapshot()` answers from what already happened -- no outside call: the hourly jobs' runs, the Lebara
session, failed customer logins, recent Error Log entries and failed messages. `test_now()` (and the
hourly check when "Test Connections Hourly" is on) also signs in to every enabled Pilot / IM / Chatwoot
connection.

`hourly()` (hooks.scheduler_events) compares the problems it finds with the ones it found last time and
sends a bell notification to the Alert Users (app_apis settings > Health and Alerts) for each new
problem and each one that cleared. Nothing is ever sent by email or WhatsApp.

The page is /app/app-apis-health.
"""

import json

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime, pretty_date
from frappe.utils.scheduler import is_scheduler_inactive

from app_apis import access

SETTINGS = "app_apis"
STATE_KEY = "app_apis_health_problems"
SCRIPT_MANIFEST = "scripts/server_scripts.json"

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"

# how long an hourly / cron job may stay silent before it counts as stopped
OVERDUE_HOURS = {"Hourly": 3, "Hourly Long": 3, "Daily": 30, "Daily Long": 30, "Cron": 1}


def _check(key, area, name, state, detail="", when=None, test=None) -> dict:
	return {"key": key, "area": area, "name": name, "state": state, "detail": detail or "",
	        "when": str(when or ""), "ago": pretty_date(when) if when else "", "test": test}


def _setting(field):
	return frappe.db.get_single_value(SETTINGS, field)


# ------------------------------------------------------------------ jobs
def _script_names() -> list:
	path = frappe.get_app_path("app_apis", *SCRIPT_MANIFEST.split("/"))
	with open(path) as f:
		return [m["name"] for m in json.load(f) if m.get("script_type") == "Scheduler Event"]


def job_checks() -> list:
	out = []
	scheduler_on = not is_scheduler_inactive()
	names = _script_names()
	jobs = frappe.get_all(
		"Scheduled Job Type",
		filters=[["server_script", "in", names]],
		fields=["name", "server_script", "frequency", "stopped", "last_execution"],
	) if names else []
	jobs += frappe.get_all("Scheduled Job Type", filters=[["method", "like", "app_apis.%"]],
	                       fields=["name", "method as server_script", "frequency", "stopped", "last_execution"])
	disabled = set(frappe.get_all("Server Script", filters={"name": ["in", names], "disabled": 1}, pluck="name")) if names else set()
	since = add_to_date(now_datetime(), hours=-24)
	for j in jobs:
		label = j.server_script
		key = "job:" + j.name
		if j.server_script in disabled or cint(j.stopped):
			out.append(_check(key, "Hourly jobs", label, OFF, "disabled", j.last_execution))
			continue
		last = frappe.get_all("Scheduled Job Log", filters={"scheduled_job_type": j.name},
		                      fields=["status", "creation", "details"], order_by="creation desc", limit=1)
		failed = frappe.db.count("Scheduled Job Log", {"scheduled_job_type": j.name, "status": "Failed",
		                                               "creation": [">", since]})
		limit = OVERDUE_HOURS.get(j.frequency, 30)
		overdue = (scheduler_on and j.last_execution
		           and get_datetime(j.last_execution) < add_to_date(now_datetime(), hours=-limit))
		if last and last[0].status == "Failed":
			detail = (last[0].details or "").strip().splitlines()
			out.append(_check(key, "Hourly jobs", label, FAIL,
			                  "last run failed" + (": " + detail[-1][:200] if detail else ""), last[0].creation))
		elif overdue:
			out.append(_check(key, "Hourly jobs", label, FAIL,
			                  "has not run for more than %d hours" % limit, j.last_execution))
		elif failed:
			out.append(_check(key, "Hourly jobs", label, WARN, "%d failed runs in the last 24 hours" % failed,
			                  j.last_execution))
		elif not last and not j.last_execution:
			out.append(_check(key, "Hourly jobs", label, OK, "not run yet"))
		else:
			out.append(_check(key, "Hourly jobs", label, OK, "last run fine", j.last_execution))
	if not scheduler_on:
		out.insert(0, _check("scheduler", "Hourly jobs", "Scheduler", FAIL,
		                     "the site's scheduler is off, so no hourly job runs"))
	return out


# ------------------------------------------------------------------ connections (no outside call)
def connection_checks() -> list:
	from app_apis import im_connector, pilot_admin

	out = []
	logins = frappe.get_all("App Apis Pilot Login", fields=["account_key", "failures", "last_ok", "last_failed"])
	for c in pilot_admin.connections():
		key = "pilot:%s" % c["account"]
		if not c.get("enabled"):
			out.append(_check(key, "Connections", c["label"], OFF, "not enabled", test=None))
			continue
		mine = [r for r in logins if (r.account_key or "").startswith(c["key"])]
		failing = [r for r in mine if cint(r.failures) and (not r.last_ok or (r.last_failed and r.last_failed > r.last_ok))]
		last_ok = max([r.last_ok for r in mine if r.last_ok] or [None])
		if failing:
			out.append(_check(key, "Connections", c["label"], WARN,
			                  "%d customer live-tracking logins keep failing (wrong saved password?)" % len(failing),
			                  last_ok, test="pilot:%s" % c["account"]))
		else:
			out.append(_check(key, "Connections", c["label"], OK, "no failing logins on record", last_ok,
			                  test="pilot:%s" % c["account"]))
	for c in im_connector.connections():
		key = "im:%s" % c["account"]
		if not c.get("enabled"):
			out.append(_check(key, "Connections", c["label"], OFF, "not enabled"))
		else:
			out.append(_check(key, "Connections", c["label"], OK, "press Test to sign in", test="im:%s" % c["account"]))

	from app_apis.core import lebara

	st = lebara.status()
	if not st.get("enabled"):
		out.append(_check("lebara", "Connections", "Lebara", OFF, "not enabled"))
	elif (st.get("status") or "") != "Logged In":
		out.append(_check("lebara", "Connections", "Lebara", FAIL,
		                  "session is %s -- sign in again from app_apis settings > Lebara (OTP)%s" % (
		                      st.get("status") or "unknown", (": " + st["last_error"][:150]) if st.get("last_error") else ""),
		                  st.get("last_refresh") or None))
	else:
		out.append(_check("lebara", "Connections", "Lebara", OK,
		                  "signed in; %s SIMs, last SIM sync %s" % (st.get("sims_count"),
		                                                            pretty_date(st["sims_synced_at"]) if st.get("sims_synced_at") else "never"),
		                  st.get("last_refresh") or None))

	if cint(_setting("chatwoot_enabled")) or cint(_setting("chatwoot2_enabled")):
		out.append(_check("chatwoot", "Connections", "Chatwoot (WhatsApp)", OK, "press Test to check", test="chatwoot"))
	else:
		out.append(_check("chatwoot", "Connections", "Chatwoot (WhatsApp)", OFF, "not enabled"))
	return out


# ------------------------------------------------------------------ errors and messages
ERROR_TITLES = ("app_apis%", "App Apis%", "Lebara%", "WASL%", "Pilot%", "IM %", "Chatwoot%", "auto-messages%")


def error_checks(hours: int = 24) -> list:
	since = add_to_date(now_datetime(), hours=-hours)
	cond = " or ".join(["method like %s"] * len(ERROR_TITLES))
	rows = frappe.db.sql(
		"select method, count(*) as n, max(creation) as last from `tabError Log` "
		"where creation > %s and (" + cond + ") group by method order by n desc limit 15",
		[since, *ERROR_TITLES], as_dict=True)
	out = []
	for r in rows:
		out.append(_check("error:" + (r.method or "")[:100], "Errors (24h)", r.method or "(no title)", WARN,
		                  "%d in the last %d hours" % (r.n, hours), r.last))
	if not rows:
		out.append(_check("errors", "Errors (24h)", "Error Log", OK, "no app_apis errors in the last %d hours" % hours))
	return out


def message_checks(hours: int = 24) -> list:
	since = add_to_date(now_datetime(), hours=-hours)
	out = []
	for dt, label in (("App Apis Message Log", "Ticket messages"), ("App Apis Reminder Log", "Subscription reminders")):
		if not frappe.db.exists("DocType", dt):
			continue
		failed = frappe.db.count(dt, {"status": "Failed", "creation": [">", since]})
		sent = frappe.db.count(dt, {"status": "Sent", "creation": [">", since]})
		state = WARN if failed else OK
		out.append(_check("messages:" + dt, "Messages (24h)", label, state, "%d sent, %d failed" % (sent, failed)))
	return out


# ------------------------------------------------------------------ live tests
def run_test(test: str) -> dict:
	"""One live sign-in. `test` is pilot:N, im:N or chatwoot. -> {ok, message}. Never raises."""
	from app_apis import chatwoot_connector, im_connector, pilot_admin

	try:
		kind, _, n = (test or "").partition(":")
		if kind == "pilot":
			res = pilot_admin.test_admin_api(cint(n) or 1)
		elif kind == "im":
			res = im_connector.test_connection(cint(n) or 1)
		elif kind == "chatwoot":
			res = chatwoot_connector.test_connection()
		else:
			return {"ok": False, "message": "unknown test %s" % test}
		res = res if isinstance(res, dict) else {"ok": bool(res), "message": str(res)}
		return {"ok": bool(res.get("ok")), "message": str(res.get("message") or res.get("msg") or "")[:300]}
	except Exception as e:
		return {"ok": False, "message": str(e)[:300]}


def live_checks(checks: list) -> list:
	"""Replace each enabled connection's passive check by its live test result."""
	out = []
	for c in checks:
		if c.get("test") and c["state"] != OFF:
			r = run_test(c["test"])
			c = dict(c, state=OK if r["ok"] else FAIL, detail=r["message"] or ("signed in" if r["ok"] else "failed"),
			         when=str(now_datetime()), ago="just now")
		out.append(c)
	return out


# ------------------------------------------------------------------ endpoints
def snapshot(live: bool = False) -> dict:
	conns = connection_checks()
	if live:
		conns = live_checks(conns)
	checks = conns + job_checks() + message_checks() + error_checks()
	counts = {s: len([c for c in checks if c["state"] == s]) for s in (OK, WARN, FAIL, OFF)}
	return {"checks": checks, "counts": counts, "at": str(now_datetime()),
	        "alert_users": [r.user for r in frappe.get_cached_doc(SETTINGS).get("alert_users") or []],
	        "live_hourly": cint(_setting("health_live_checks"))}


@frappe.whitelist()
def status() -> dict:
	"""The health page, from what already happened (no outside call)."""
	access.require(access.VIEW)
	return snapshot(live=False)


@frappe.whitelist(methods=["POST"])
def test_now(test: str = "") -> dict:
	"""Sign in to one connection (`test`), or to all of them when `test` is empty."""
	access.require(access.ADMIN)
	if test:
		return run_test(test)
	return snapshot(live=True)


# ------------------------------------------------------------------ alerts
def _problems(checks: list) -> dict:
	return {c["key"]: "%s: %s" % (c["name"], c["detail"]) for c in checks if c["state"] == FAIL}


def _notify(users: list, subject: str, body: str):
	for user in users:
		try:
			frappe.get_doc({
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"subject": subject,
				"email_content": body,
				"link": "/app/app-apis-health",
			}).insert(ignore_permissions=True)
		except Exception:
			frappe.log_error(title="app_apis health: could not notify %s" % user)


def hourly():
	"""Scheduler: find what is broken, and tell the Alert Users about what changed since last hour."""
	try:
		snap = snapshot(live=bool(cint(_setting("health_live_checks"))))
		now = _problems(snap["checks"])
		before = json.loads(frappe.db.get_global(STATE_KEY) or "{}")
		new = {k: v for k, v in now.items() if k not in before}
		cleared = {k: v for k, v in before.items() if k not in now}
		frappe.db.set_global(STATE_KEY, json.dumps(now))
		users = [u for u in snap["alert_users"] if frappe.db.get_value("User", u, "enabled")]
		if users:
			for v in new.values():
				_notify(users, "app_apis problem: " + frappe.utils.escape_html(v[:120]), frappe.utils.escape_html(v))
			for v in cleared.values():
				_notify(users, "app_apis fixed: " + frappe.utils.escape_html(v.split(":")[0][:120]), frappe.utils.escape_html(v))
		frappe.db.commit()
		return {"new": list(new), "cleared": list(cleared), "notified": users}
	except Exception:
		frappe.log_error(title="app_apis health: hourly check failed")
