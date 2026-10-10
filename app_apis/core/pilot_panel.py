"""The Pilot admin panel, per estate: find, create, delete, sensors.

Pilot's Administrator API has no way to delete a vehicle and refuses every folder on the WSL estate, so
the integration does what the admin site itself does: it signs in to the panel
(`<admin host>/backend/login.php`, which hands out a JWT) and calls the same `app/*.php` endpoints the
panel's own buttons call. This module is that, as plain functions. Nothing here knows about ERPNext's
Customer Vehicle: every function takes an IMEI / account / name. ERP-aware code is in
app_apis.core.platforms.

`account` is the app_apis settings account: 1 = Pilot (WSL), 2 = Pilot 2 (the `pilot_admin_*` and
`pilot_admin2_*` fields). Everything here is read from those settings.

Return shapes are plain dicts and these functions do not raise for a Pilot-side failure:

    find()    {"state": "yes" | "no" | "unknown", "row": {...}, "detail": str, "agent_id": str, ...}
    create()  {"verdict": "uploaded" | "duplicate" | "failure" | "error", "ok": bool, "result": str, ...}
    delete()  {"verdict": "deleted" | "not_deleted" | "failure" | "error", "ok": bool, "result": str, ...}

    "unknown" always means "could not tell" -- the panel did not answer. It is never "no".
"""

import json

import frappe
import requests

from app_apis import pilot_admin

TIMEOUT = 60
TOKEN_TTL = 600          # seconds a panel JWT is reused before signing in again
ADD_SENSORS = True       # False = create vehicles without the standard sensors

# Device types the ERP uses that the panel's list has no obvious name for. Put the panel's model id here
# once and it is used from then on.
MODEL_OVERRIDES = {"TZ-RD07": 0, "TZ-RD06": 0, "M300S": 0, "W18L": 0, "JC261P": 0, "FMC003": 0}

# Every vehicle created here gets the same six sensors: External Voltage, Ignition, GSM Signal,
# Temperature (BLE_T1), Humidity (BLE_Humidity1) and weight (IN1, with its calibration table). It is the
# panel's own "Export sensors" of vehicle 239355 with each sensor's id/plugged_id removed (they belong to
# that vehicle) and the tag links cleared (tags are per account). It goes in through the panel's own
# "Import sensors" (vehicles.php cmd=importSensors).
SENSOR_TEMPLATE = r'''[{"sensortype":"Dedicated","sensortypeid":1,"sensorinterpret":"External power supply","sensorinterpretid":6,"info":"External Voltage","sensorfieldname":"Vsourse","fieldname":"Vsourse","sensorminvalue":"0.000","sensormaxvalue":"30000.000","minvalue":"0.000","maxvalue":"30000.000","semanticid":6,"typeid":1,"sensordefaultdesc":"Voltage","defaultdescription":"Voltage","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"V","defaultmeasureunit":"V","checkbox":0,"history":1,"formula":null,"filter":1,"moto":0,"tags":null,"tooltips":0,"maxspeed":5,"bufferlen":null,"json_config":"{\"frame\": \"\", \"smooth\": \"\"}","calibration":[],"multi_calibration":[]},{"sensortype":"Dedicated","sensortypeid":1,"sensorinterpret":"Special sensor","sensorinterpretid":9,"info":"weight","sensorfieldname":"IN1","fieldname":"IN1","sensorminvalue":"1.000","sensormaxvalue":"30000.000","minvalue":"1.000","maxvalue":"30000.000","semanticid":9,"typeid":1,"sensordefaultdesc":"weight","defaultdescription":"weight","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"kg","defaultmeasureunit":"kg","checkbox":0,"history":1,"formula":null,"filter":0,"moto":0,"tags":null,"tooltips":3,"maxspeed":5,"bufferlen":null,"json_config":null,"calibration":[{"value1":"0","value2":"4179"},{"value1":"1984","value2":"5572"},{"value1":"2123","value2":"6965"},{"value1":"2266","value2":"8358"},{"value1":"2507","value2":"25074"},{"value1":"2532","value2":"11144"},{"value1":"2673","value2":"22288"},{"value1":"2725","value2":"16716"}],"multi_calibration":[]},{"sensortype":"Dip","sensortypeid":3,"sensorinterpret":"Ignition sensor","sensorinterpretid":1,"info":"Ignition sensor","sensorfieldname":"Roaming_Ignition1125","fieldname":"Roaming_Ignition1125","sensorminvalue":"1.000","sensormaxvalue":"1.000","minvalue":"1.000","maxvalue":"1.000","semanticid":1,"typeid":3,"sensordefaultdesc":"Ignition sensor","defaultdescription":"Ignition sensor","defaultactivemodename":"On","activemodename":"On","passivemodename":"Off","defaultpassivemodename":"Off","measureunit":"-","defaultmeasureunit":"-","checkbox":1,"history":1,"formula":null,"filter":1,"moto":1,"tags":null,"tooltips":3,"maxspeed":null,"bufferlen":null,"json_config":null,"calibration":[],"multi_calibration":[]},{"sensortype":"Selector","sensortypeid":8,"sensorinterpret":"GPS antenna sensor","sensorinterpretid":5,"info":"GSM Signal","sensorfieldname":"GSMVal","fieldname":"GSMVal","sensorminvalue":null,"sensormaxvalue":null,"minvalue":null,"maxvalue":null,"semanticid":5,"typeid":8,"sensordefaultdesc":"Value in range 1-5","defaultdescription":"Value in range 1-5","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"","defaultmeasureunit":"","checkbox":0,"history":1,"formula":null,"filter":1,"moto":0,"tags":null,"tooltips":3,"maxspeed":null,"bufferlen":null,"json_config":null,"calibration":[],"multi_calibration":[{"min":null,"max":"1","semantic":"Very low"},{"min":null,"max":"2","semantic":"Low"},{"min":null,"max":"3","semantic":"Medium"},{"min":null,"max":"4","semantic":"Normal"},{"min":null,"max":"5","semantic":"High"}]},{"sensortype":"Dedicated","sensortypeid":1,"sensorinterpret":"Temperature sensor","sensorinterpretid":7,"info":"Temperature","sensorfieldname":"BLE_T1","fieldname":"BLE_T1","sensorminvalue":"-50.000","sensormaxvalue":"50.000","minvalue":"-50.000","maxvalue":"50.000","semanticid":7,"typeid":1,"sensordefaultdesc":"Temperature","defaultdescription":"Temperature","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"C","defaultmeasureunit":"C","checkbox":0,"history":1,"formula":null,"filter":1,"moto":0,"tags":null,"tooltips":3,"maxspeed":5,"bufferlen":null,"json_config":"{\"frame\": \"6\", \"smooth\": \"\", \"max_treshold\": 0, \"min_treshold\": 0}","calibration":[],"multi_calibration":[]},{"sensortype":"Dedicated","sensortypeid":1,"sensorinterpret":"Humidity sensor","sensorinterpretid":24,"info":"Humidity","sensorfieldname":"BLE_Humidity1","fieldname":"BLE_Humidity1","sensorminvalue":"1.000","sensormaxvalue":"100.000","minvalue":"1.000","maxvalue":"100.000","semanticid":24,"typeid":1,"sensordefaultdesc":"Humidity","defaultdescription":"Humidity","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"%","defaultmeasureunit":"%","checkbox":0,"history":1,"formula":null,"filter":1,"moto":0,"tags":null,"tooltips":3,"maxspeed":5,"bufferlen":null,"json_config":"{\"frame\": \"6\", \"smooth\": \"\"}","calibration":[],"multi_calibration":[]}]'''


def sensor_fields() -> list:
	return [str(s.get("fieldname") or "") for s in json.loads(SENSOR_TEMPLATE)]


def norm(text) -> str:
	return str(text or "").upper().replace(" ", "").replace("-", "").replace("_", "")


# ------------------------------------------------------------------ settings and the panel session
def settings(account: int = 1) -> dict:
	"""{"base": ".../backend/", "user", "password", "node", "configured": bool} for one estate."""
	s = pilot_admin._settings(frappe.utils.cint(account) or 1)
	password = pilot_admin._password(s) or ""
	base = (s.get("base_url") or "").rstrip("/")
	return {
		"base": base + "/backend/" if base else "",
		"user": s.get("username") or "",
		"password": password,
		"node": s.get("node") or ("5" if (frappe.utils.cint(account) or 1) == 1 else ""),
		"enabled": bool(s.get("enabled")),
		"configured": bool(base and s.get("username") and password),
		"api_settings": s,
	}


def _json(text):
	try:
		return json.loads(text)
	except (TypeError, ValueError):
		return {"_text": str(text or "")[:400]}


def _login(cfg: dict) -> dict:
	"""-> {"token": str, "why": str}. Signs in the way the panel's own form does: get_user_without_auth says
	whether this login is a partner, and a partner must send its own id as partner_id with cmd=login."""
	url = cfg["base"] + "login.php"
	try:
		who = _json(requests.post(url, data={"cmd": "get_user_without_auth", "username": cfg["user"],
		                                     "password": cfg["password"]}, timeout=TIMEOUT).text)
		who = who if isinstance(who, dict) else {}
		form = {"cmd": "login", "time_zone": "0", "username": cfg["user"], "password": cfg["password"],
		        "email": str(who.get("email") or cfg["user"])}
		if not who.get("isadmin") and (who.get("ispartner") or who.get("adm_user_id")) and who.get("id"):
			form["partner_id"] = str(who.get("id"))
		ans = _json(requests.post(url, data=form, timeout=TIMEOUT).text)
		ans = ans if isinstance(ans, dict) else {}
	except requests.RequestException as e:
		return {"token": "", "why": "the panel sign-in failed -- %s" % str(e).replace(cfg["password"], "***")[:160]}
	token = str(ans.get("jwt_token") or "")
	if not token:
		return {"token": "", "why": "the panel sign-in gave no token -- " +
		        str(ans.get("auth_message") or ans.get("error") or ans.get("_text") or "")[:160]}
	return {"token": token, "why": ""}


def token(account: int = 1, force: bool = False) -> dict:
	"""-> {"token", "why"}. Reused for TOKEN_TTL seconds across requests."""
	cfg = settings(account)
	if not cfg["configured"]:
		return {"token": "", "why": "Pilot admin account %s is not configured in app_apis." % account}
	key = "app_apis_panel_token_%s_%s" % (frappe.local.site, account)
	if not force:
		cached = frappe.cache.get_value(key)
		if cached:
			return {"token": cached, "why": ""}
	got = _login(cfg)
	if got["token"]:
		frappe.cache.set_value(key, got["token"], expires_in_sec=TOKEN_TTL)
	return got


def call(path: str, data=None, account: int = 1, method: str = "GET", headers: dict | None = None) -> dict:
	"""One panel request. -> {"ok", "status", "body", "text"}; never raises.

	`path` is relative to /backend/, e.g. "app/fitter.php". `data` is the query (GET) or the form (POST);
	a str/bytes `data` is sent as the raw body (the sensor import is multipart)."""
	cfg = settings(account)
	for attempt in (1, 2):
		tok = token(account, force=attempt == 2)
		if not tok["token"]:
			return {"ok": False, "status": 0, "body": None, "text": tok["why"]}
		hdr = {"Authorization": "Bearer " + tok["token"]}
		hdr.update(headers or {})
		try:
			if str(method).upper() == "POST":
				r = requests.post(cfg["base"] + path, headers=hdr, data=data, timeout=TIMEOUT)
			else:
				r = requests.get(cfg["base"] + path, headers=hdr, params=data, timeout=TIMEOUT)
		except requests.RequestException as e:
			return {"ok": False, "status": 0, "body": None, "text": str(e)[:300]}
		if r.status_code in (401, 403) and attempt == 1:
			continue                     # the cached token expired; sign in once more
		body = _json(r.text)
		text = ""
		if isinstance(body, dict):
			text = str(body.get("_text") or body.get("error") or body.get("msg") or "")
		if r.status_code >= 400:
			return {"ok": False, "status": r.status_code, "body": body, "text": text or r.text[:300]}
		return {"ok": True, "status": r.status_code, "body": body, "text": text}
	return {"ok": False, "status": 0, "body": None, "text": "the panel refused the sign-in"}


# ------------------------------------------------------------------ find
def find(imei: str, account: int = 1) -> dict:
	"""The panel's own search (the Mechanic grid), matched on the EXACT IMEI."""
	want = str(imei or "").strip()
	if not want:
		return {"state": "unknown", "row": {}, "detail": "no IMEI given", "agent_id": ""}
	res = call("app/fitter.php", {"cmd": "get_agents", "query": want, "page": 1, "start": 0, "limit": 25}, account)
	if not res["ok"] or not isinstance(res["body"], dict) or "data" not in res["body"]:
		return {"state": "unknown", "row": {}, "agent_id": "",
		        "detail": "the panel search did not answer -- HTTP %s %s" % (res["status"], str(res["text"])[:160])}
	for r in res["body"].get("data") or []:
		if isinstance(r, dict) and str(r.get("uniqid") or "").strip() == want:
			return {"state": "yes", "row": r, "agent_id": str(r.get("agent_id") or ""),
			        "name": str(r.get("vehiclenumber") or ""), "account_id": str(r.get("account_id") or ""),
			        "detail": "found as %s, agent_id %s, account %s" % (
				        r.get("vehiclenumber") or "?", r.get("agent_id") or "?", r.get("account_id") or "?")}
	return {"state": "no", "row": {}, "agent_id": "", "detail": "the panel finds no vehicle with this IMEI"}


# ------------------------------------------------------------------ accounts, models
def accounts(account: int = 1) -> dict:
	"""-> {"rows": [...], "err": str}: every Pilot account under this administrator (Administrator API)."""
	res = pilot_admin.backend_accounts(settings(account)["api_settings"])
	good = frappe.utils.cint(res.get("code")) == 0
	return {"rows": (res.get("data") or []) if good else [], "err": "" if good else str(res.get("msg") or "")}


def account_by_email(email: str, account: int = 1) -> dict:
	"""The Pilot account whose bill_email is `email`. -> {"id", "org_name", "why"}."""
	want = str(email or "").strip().lower()
	if not want:
		return {"id": 0, "org_name": "", "why": "no account email given"}
	got = accounts(account)
	if got["err"]:
		return {"id": 0, "org_name": "", "why": "accountslist failed: " + got["err"]}
	for r in got["rows"]:
		if str(r.get("bill_email") or "").strip().lower() == want:
			ident = r.get("id") if r.get("id") is not None else r.get("identifier")
			return {"id": frappe.utils.cint(ident), "org_name": str(r.get("org_name") or "").strip(), "why": ""}
	return {"id": 0, "org_name": "", "why": "no Pilot account has bill_email %s (%d accounts read)" % (want, len(got["rows"]))}


def vehicle_types(account: int = 1) -> dict:
	"""-> {"rows": [{"id", "name"}...], "err": str}: the panel's own device-type list."""
	res = call("app/fitter.php", {"cmd": "get_vehicle_types"}, account)
	body = res["body"]
	rows = body.get("data") if isinstance(body, dict) else (body if isinstance(body, list) else [])
	rows = rows or []
	return {"rows": rows, "err": "" if rows else "get_vehicle_types failed -- %s" % str(res["text"])[:120]}


def resolve_model(devices_type: str, account: int = 1, types: dict | None = None) -> dict:
	"""The panel's device-type NAME for an ERP Device Type, e.g. "Teltonika FMC920" for "FMC920"."""
	want = str(devices_type or "").strip()
	if not want:
		return {"name": "", "why": "the vehicle has no Device Type"}
	got = types if types is not None else vehicle_types(account)
	if got["err"]:
		return {"name": "", "why": got["err"]}
	pinned = frappe.utils.cint(MODEL_OVERRIDES.get(want) or 0)
	n = norm(want)
	best = ""
	for r in got["rows"]:
		name = str(r.get("name") or "") if isinstance(r, dict) else str(r)
		if pinned and isinstance(r, dict) and frappe.utils.cint(r.get("id")) == pinned:
			return {"name": name, "why": ""}
		nn = norm(name)
		if n and (nn == n or nn.endswith(n)) and (not best or len(name) < len(best)):
			best = name
	if not best:
		for r in got["rows"]:
			name = str(r.get("name") or "") if isinstance(r, dict) else str(r)
			if n and n in norm(name) and (not best or len(name) < len(best)):
				best = name
	if best:
		return {"name": best, "why": ""}
	return {"name": "", "why": "no panel device type matches %s -- put its Pilot model id in "
	        "app_apis.core.pilot_panel.MODEL_OVERRIDES" % want}


# ------------------------------------------------------------------ sensors
def sensors_of(agent_id, account: int = 1) -> dict:
	res = call("app/vehicles.php", {"cmd": "sensors", "agent_id": agent_id, "page": 1, "start": 0, "limit": 200}, account)
	body = res["body"]
	if not res["ok"] or not isinstance(body, dict) or "data" not in body:
		return {"ok": False, "fields": [], "why": "HTTP %s %s" % (res["status"], str(res["text"])[:160])}
	return {"ok": True, "fields": [str(r.get("fieldname") or "") for r in body.get("data") or [] if isinstance(r, dict)],
	        "why": ""}


def ensure_sensors(agent_id, account: int = 1) -> dict:
	"""Import the standard sensors unless the vehicle already has some. A vehicle with SOME sensors is
	left alone: importing on top would duplicate them. -> {"ok": bool, "msg": str}"""
	if not ADD_SENSORS:
		return {"ok": True, "msg": "sensors: switched off (ADD_SENSORS = False)"}
	if not agent_id:
		return {"ok": False, "msg": "sensors: no agent_id to attach them to"}
	want = sensor_fields()
	have = sensors_of(agent_id, account)
	if not have["ok"]:
		return {"ok": False, "msg": "sensors: could not read the vehicle's sensors -- " + have["why"]}
	missing = [f for f in want if f not in have["fields"]]
	if not missing:
		return {"ok": True, "msg": "sensors: all %d already there" % len(want)}
	if have["fields"]:
		return {"ok": False, "msg": "sensors: the vehicle already has %d sensor(s) but is missing %s -- not "
		        "importing on top; add them in the panel" % (len(have["fields"]), ", ".join(missing))}

	boundary = "----appApisSensors7f3a9c"
	body = (
		"--%s\r\nContent-Disposition: form-data; name=\"cmd\"\r\n\r\nimportSensors\r\n"
		"--%s\r\nContent-Disposition: form-data; name=\"agent_id\"\r\n\r\n%s\r\n"
		"--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sensors_template.json\"\r\n"
		"Content-Type: application/json\r\n\r\n%s\r\n--%s--\r\n"
	) % (boundary, boundary, agent_id, boundary, SENSOR_TEMPLATE, boundary)
	res = call("app/vehicles.php", body.encode("utf-8"), account, "POST",
	           {"Content-Type": "multipart/form-data; boundary=" + boundary})
	after = sensors_of(agent_id, account)
	got = [f for f in want if f in after["fields"]]
	if len(got) == len(want):
		return {"ok": True, "msg": "sensors: %d added" % len(want)}
	said = ("HTTP %s %s" % (res["status"], str(res["text"])[:200])) if not res["ok"] else str(res["body"])[:200]
	return {"ok": False, "msg": "sensors: import did not take (%d/%d present) -- panel said: %s" % (len(got), len(want), said)}


# ------------------------------------------------------------------ create / delete
def _create_verdict(res: dict) -> dict:
	"""What the panel's create said. 'Limit create reached' = this login's quota is used up; anything
	else on a refusal reads as 'This device id exist in the system'."""
	body = res.get("body")
	if res.get("ok"):
		if isinstance(body, dict) and body.get("agent_id"):
			return {"verdict": "uploaded", "msg": "the panel created agent_id %s" % body.get("agent_id")}
		return {"verdict": "uploaded", "msg": "the panel answered: %s" % str(body)[:200]}
	text = str(res.get("text") or "")
	low = text.lower()
	status = frappe.utils.cint(res.get("status"))
	msg = ("HTTP %s -- " % status if status else "") + text[:300]
	if "limit" in low:
		return {"verdict": "failure", "msg": msg + " (this admin login's create limit is used up -- raise it in the panel)"}
	if "exist" in low or "dublicat" in low or "duplicat" in low:
		return {"verdict": "duplicate", "msg": msg}
	if status in (0, 502, 503, 504):
		return {"verdict": "error", "msg": msg}
	return {"verdict": "failure", "msg": msg}


def create(imei: str, name: str, account_id, account_name: str, configuration: str, msisdn: str = "0",
           account: int = 1) -> dict:
	"""Create one vehicle through the panel's own create, then read it back. Never trusts the answer alone.

	`configuration` is the panel's device-type NAME (see resolve_model). `msisdn` is "0" for no SIM, which
	is Pilot's own convention. An IMEI that is already there is not created again (its sensors are topped up).
	"""
	imei = str(imei or "").strip()
	missing = [k for k, v in (("imei", imei), ("name", name), ("account_id", account_id),
	                          ("account_name", account_name), ("configuration", configuration)) if not v]
	if missing:
		return {"verdict": "failure", "ok": False, "result": "cannot create: missing %s" % ", ".join(missing)}

	before = find(imei, account)
	if before["state"] == "yes":
		sen = ensure_sensors(before["agent_id"], account)
		return {"verdict": "uploaded", "ok": True, "already": True, "agent_id": before["agent_id"],
		        "detail": before["detail"], "sensors": sen["msg"], "sensors_ok": sen["ok"],
		        "result": "already registered, not created again -- %s. %s" % (before["detail"], sen["msg"])}
	if before["state"] == "unknown":
		return {"verdict": "error", "ok": False,
		        "result": "did not send: could not establish whether it is already there -- " + before["detail"]}

	form = {"cmd": "create_vehicle", "agent_id": "", "device_name": configuration, "initial_mileage": "0",
	        "current_mileage": "0", "sum_mileage": "0", "vehiclenumber": str(name), "uniqid": imei,
	        "msisdn": str(msisdn or "0"), "configuration": configuration, "account_id": str(account_id),
	        "account_name": str(account_name)}
	res = call("app/fitter.php", form, account, "POST")
	said = _create_verdict(res)
	after = find(imei, account)
	out = {"platform_said": said["msg"], "sent": form}
	if after["state"] == "yes":
		sen = ensure_sensors(after["agent_id"], account)
		out.update({"verdict": "uploaded", "ok": True, "agent_id": after["agent_id"], "detail": after["detail"],
		            "sensors": sen["msg"], "sensors_ok": sen["ok"],
		            "result": "created -- %s. %s. Panel said: %s" % (after["detail"], sen["msg"], said["msg"][:160])})
	elif after["state"] == "no":
		verdict = said["verdict"] if said["verdict"] != "uploaded" else "failure"
		out.update({"verdict": verdict, "ok": False, "result": "Pilot did not create it. Panel said: " + said["msg"][:400]})
	else:
		out.update({"verdict": "error", "ok": False, "detail": after["detail"],
		            "result": "sent, but it cannot be confirmed right now -- %s. Verify before sending again." % after["detail"]})
	return out


def delete(imei: str, account: int = 1, update_sim_status: bool = False) -> dict:
	"""Remove the vehicle with the panel's own Delete (vehicles.php cmd=remove): it takes all stored data
	and sensors with it and cannot be undone. The IMEI is looked up fresh and read back afterwards."""
	imei = str(imei or "").strip()
	found = find(imei, account)
	if found["state"] != "yes":
		return {"verdict": "not_deleted", "ok": False, "state": found["state"],
		        "result": "nothing deleted -- " + found["detail"]}
	res = call("app/vehicles.php", {"cmd": "remove", "agent_id": found["agent_id"],
	                                "is_update_sim_status": "true" if update_sim_status else "false"}, account, "POST")
	said = str(res.get("text") or res.get("body") or "")[:200]
	after = find(imei, account)
	if after["state"] == "no":
		return {"verdict": "deleted", "ok": True, "state": "deleted", "agent_id": found["agent_id"],
		        "result": "deleted -- the panel no longer finds it. " + said}
	if after["state"] == "yes":
		return {"verdict": "failure", "ok": False, "state": "yes", "agent_id": found["agent_id"],
		        "result": "the panel is still showing the vehicle after the delete. Panel said: " + (said or "(nothing)")}
	return {"verdict": "error", "ok": False, "state": "unknown", "agent_id": found["agent_id"],
	        "result": "sent, but cannot confirm -- " + after["detail"]}


# ------------------------------------------------------------------ the Administrator API (POST commands)
def api_post(cmd: str, params: dict | None = None, account: int = 1, timeout: int = 60) -> dict:
	"""One Administrator API command sent as a POST with the parameters in the query string (the way
	`vehadd` and `setvehblock` are documented). Basic auth. -> {"code": int, "msg": str, "data": ...};
	a negative code is a transport failure or a refusal. Never raises."""
	cfg = settings(account)
	if not cfg["configured"]:
		return {"code": -500, "msg": "Pilot admin account %s is not configured in app_apis." % account, "data": []}
	api = cfg["api_settings"]
	url = (api.get("base_url") or "").rstrip("/") + pilot_admin.BACKEND_PATH
	query = {"cmd": cmd}
	if cfg["node"] and "node" not in (params or {}):
		query["node"] = cfg["node"]
	query.update(params or {})
	try:
		r = requests.post(url, auth=(cfg["user"], cfg["password"]), params=query, timeout=timeout,
		                  headers={"Accept": "application/json"})
	except requests.RequestException as e:
		return {"code": -502, "msg": "Administrator API did not answer: %s" % str(e)[:200], "data": []}
	try:
		body = r.json()
	except ValueError:
		body = None
	if body is None:
		return {"code": 0 if r.status_code < 400 else -1, "msg": r.text[:200] or "no data", "data": []}
	if isinstance(body, list):
		return {"code": 0, "msg": "OK", "data": body}
	if body.get("Fail"):
		return {"code": -1, "msg": str(body.get("Fail")), "data": []}
	body.setdefault("data", [])
	body.setdefault("code", 0 if r.status_code < 400 else -1)
	return body


def set_block(imei: str = "", blocked: bool = True, account: int = 1, agent_id: str = "", node: str = "") -> dict:
	"""Block (or unblock) a device on Pilot -- the platform's own, reversible off switch (`setvehblock`,
	status 0 = blocked, 1 = unblocked). Give the agent id when you have it, else the IMEI.
	-> {"ok": bool, "result": str, "sent": dict}"""
	params = {"status": "0" if blocked else "1"}
	if agent_id:
		params["agentid"] = str(agent_id)
	elif imei:
		params["imei"] = str(imei)
	else:
		return {"ok": False, "result": "give an agent_id or an IMEI", "sent": {}}
	if node:
		params["node"] = str(node)
	res = api_post("setvehblock", params, account)
	ok = frappe.utils.cint(res.get("code")) == 0
	return {"ok": ok, "result": ("blocked" if blocked else "unblocked") if ok else str(res.get("msg") or res)[:160],
	        "sent": dict(params, cmd="setvehblock")}


# ------------------------------------------------------------------ WASL, as the panel reports it
def wasl_row(agent_id, account: int = 1) -> dict:
	"""The WASL registration row for one Pilot agent (admin panel list, filtered on object_id).
	-> {"ok": bool, "row": dict | None, "rows_checked": int, "why": str}"""
	got = None
	for extra in ({"filter": json.dumps([{"property": "object_id", "value": agent_id, "operator": "eq"}])},
	              {"query": str(agent_id)}):
		p = {"cmd": "get_vehicles", "page": 1, "start": 0, "limit": 50}
		p.update(extra)
		res = call("app/wasl.php", p, account)
		body = res["body"]
		if res["ok"] and isinstance(body, dict) and isinstance(body.get("data"), list):
			got = body
			if [r for r in body["data"] if isinstance(r, dict) and str(r.get("object_id")) == str(agent_id)]:
				break
	if got is None:
		return {"ok": False, "row": None, "rows_checked": 0, "why": "the admin panel WASL list did not answer"}
	mine = [r for r in got["data"] if isinstance(r, dict) and str(r.get("object_id")) == str(agent_id)]
	return {"ok": True, "row": mine[-1] if mine else None, "rows_checked": len(got["data"]), "why": ""}
