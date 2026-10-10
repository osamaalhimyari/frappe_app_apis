"""IM (Trakzee) web console, as plain functions: sign in, companies, branches, find, create, delete.

app_apis.im_connector already wraps IM's OFFICIAL webservice (token + live data, one call a minute at most).
That API cannot add or remove a vehicle. This module is the other half: it drives the console's own web
calls (GenerateJSON / GenerateXML / ProcessDetails) through the cookie-jar HTTP helper, exactly as the
browser does, so a vehicle can be created and removed from code. There is no inbound hook or webhook in IM
that this app could subscribe to (checked: no webhook / integration screen in the console, and the only
callback URLs belong to its SMS gateway settings), so everything here is polling and request/response.

The functions do not raise for an IM-side failure. Signing in needs a System Manager (the HTTP helper in
app_apis.core.http is limited to that role); anything else gets {"ok": False, "why": ...} back.

Return shapes:

    login()          {"ok": bool, "why": str, "user": str}
    has_imei(imei)   {"state": "yes" | "no" | "unknown", "detail": str}
    find_by_name()   {"state": "yes" | "no" | "mismatch" | "unknown", "vehicle_id", "imei", "detail"}
    create(...)      {"verdict": "uploaded" | "duplicate" | "failure" | "error", "ok": bool, "result": str, ...}
    delete(...)      {"verdict": "deleted" | "not_deleted" | "failure" | "error", "ok": bool, "result": str, ...}

Everything IM-specific that an operator may need to change is in the app_apis settings (IM section): the
admin id, the default reseller, the resellers to scan, the SIM provider and the device-model table.
"""

import json

import frappe

from app_apis.core import crypto, http

WEB = "https://gps.im2m.ws"
JAR = "im_web"                       # the cookie jar the IM session lives in
APPLICATION_ID = "37"
SETTINGS = "app_apis"

XML_CLASS = "com.uffizio.tools.projectmanager.GenerateXmlUsingAjax"
JSON_CLASS = "com.uffizio.tools.projectmanager.GenerateJSONAjax"
JSON_TZ_CLASS = "com.uffizio.tools.projectmanager.GenerateJSONAjaxTrakzee"

OVERVIEW_VEHICLES = "/jsp/Overview.jsp?screenid=2253&level=1&popup=false"
OVERVIEW_COMPANIES = "/jsp/Overview.jsp?screenid=2286&level=1&popup=false"
ADD_VEHICLE_REFERER = ("/jsp/DetailScreen.jsp?entityid=0&screenid=2249&mode=insert&popup=false"
                       "&view=L&overviewscreenid=2253&modulename=")

# Defaults. The app_apis settings (IM section) override the first five and extend the model table.
DEFAULTS = {"admin_id": "1213", "reseller_id": "3237", "resellers": ["3237", "3267"], "sim_provider": "2"}

# ERP Device Type -> IM gps_device_model_id (the Device Type dropdown on the Add Vehicle form).
MODELS = {"FMC130": "1436", "FMC130 Static": "2736", "FMC130-Trace": "2439", "FMB120": "345",
          "FMB125": "369", "FMC920": "1787"}

# Sensors are copied from an existing IM vehicle of the same model (the form's own "Copy From"):
# model id -> IM vehicle id.
TEMPLATE_VEHICLE = {"1436": "255059"}

# Used when a model has no template vehicle: the minimum the Add Vehicle form needs.
DEFAULT_SENSORS = {
	"1787": [
		{"property_name": "Ignition", "property_category": "Digital", "model_port_specification_id": "39250",
		 "port_allocation": "10", "device_port_specification_id": "0", "reading_type": "DIRECT",
		 "work_hour_calculation": "true"},
		{"property_name": "Power", "property_category": "Digital", "model_port_specification_id": "39446",
		 "port_allocation": "26", "device_port_specification_id": "0", "reading_type": "DIRECT",
		 "work_hour_calculation": "false"},
	],
	"1436": [
		{"property_name": "Ignition", "property_category": "Digital", "model_port_specification_id": "21475",
		 "port_allocation": "10", "device_port_specification_id": "0", "reading_type": "DIRECT",
		 "work_hour_calculation": "true"},
		{"property_name": "Power", "property_category": "Digital", "model_port_specification_id": "35647",
		 "port_allocation": "26", "device_port_specification_id": "0", "reading_type": "DIRECT",
		 "work_hour_calculation": "false"},
	],
}

# Vehicle profile when the template vehicle does not give one.
PROFILE = {"vehicle_type": "2129", "object_brand": "423", "vehicle_model": "914"}

PUBKEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCgQTb0CjymoOrYWr6HY+Bdkb6IS7dDTXXCupZC3WAPYCtD/JM5ejIE/+dS28KMffV70HLd2O1JLYeT6blsSZsv1cY+DID0G9h0ikjhzkCJmEPQ59MT+Ob+i1P4OFU88wrWLbRbgXJCIodB9BItBRiL36SB7P0B9PdytjaYga3TzwIDAQAB
-----END PUBLIC KEY-----"""

# The rest of the Add Vehicle form, exactly as a fresh form serialises it.
FORM_DEFAULTS = {
	"mode": "insert", "screenId_help": "2249", "level": "2", "rights": "3_A", "view": "L",
	"lang_name": "English", "iframeids": "105498,125706,171224,175693,",
	"iframescreenids": "2252,3045,4270,4388,", "iframedeletedata": "",
	"calib_json": "", "copy_sensors_hidden": "", "copy_temperature_hidden": "",
	"vehicle_specification": "primary", "vehicle_category": "movable", "copy_from": "-1",
	"secondary_sim_tariff_plan": "5", "accuracy_input_timezone": "0",
	"accuracy_distance_calculation": "latlng", "accuracy_speed_calculation": "Direct",
	"vehicle_output_scaling": "Kilometer", "distance_variation": "+", "distance_variation_value": "0",
	"distance_variation_value_h": "+0", "latlng_odometer": "", "vehicle_odometer": "", "can_odometer": "",
	"obd_odometer": "", "tachograph_odometer": "",
	"fuel_tank": "single", "fuel_tank_h": "single", "no_of_tank": "1", "fuel_calculation": "individual",
	"fuel_calculation_h": "individual", "avg_mileage": "0.00", "mileage_duration": "0",
	"tfDistanceMileageConsumptionValue": "1", "tfDurationMileageConsumptionValue": "1",
	"distanceMileageLiquidUnit": "liter", "durationMileageLiquidUnit": "liter", "fuelIdleLiquidUnit": "liter",
	"fuel_idling_consumption": "1.89", "mileage_variation": "0.00", "battery_voltage": "24",
	"rfid_timeout_duration": "120", "sleep_mode_duration": "0", "min_working_hr": "0", "min_distance_travel": "0",
	"battery_full_charge_time": "0", "rangee": "0", "load_calculation_by": "sum", "weight_sensor_hidden": "false",
	"vehicle_empty_weight": "0", "vehicle_full_weight": "0", "overweight_tolerance": "0", "underweight_tolerance": "0",
	"load_capacity": "0", "loading_unloading_tolerance": "0", "max_load_capacity": "0", "loading_area": "0",
	"vehicle_load_type": "solid", "loading_unit": "kg", "vehicle_carriage_type": "partial", "no_of_sheets": "0",
	"cost_duration": "day", "duration_based_on": "day", "duration_cost": "0", "distance_cost": "0",
	"trailer_allocation_mode": "multiple", "trailer_beacon_rssi_threshold": "0", "work_hour_calculation_based_on": "0",
	"mdvr": "0", "itcintegration": "0", "canbus": "0", "camera_timezone": "Asia/Riyadh", "no_of_channel_hidden": "0",
	"inactive_duration": "0", "device_accuracy_tolerance": "0", "ifram_update_data": "NODATA", "bNewConsumer": "false",
	"vehicle_axle": "0", "dvir_template": "0", "fuel_type": "0", "permit": "-1",
	"distance_mileage_based_consumption_checked": "false", "duration_mileage_based_consumption_checked": "false",
	"distance_mileage_consumption_value": "1", "duration_mileage_consumption_value": "1",
	"distance_mileage_consumption_liquid_unit": "liter", "duration_mileage_duration_unit": "mm",
	"duration_mileage_liquid_unit": "liter", "fuel_idle_consumption_unit": "liter",
	"special_bus_hidden": "0", "is_active_hidden": "0", "backup_vehicle_hidden": "0", "plate_type_hidden": "private",
	"focusFlag": "true",
}


# ------------------------------------------------------------------ pure helpers (tested without a site)
def tag_text(text, tag: str) -> str:
	"""The text between <tag> and </tag> (the console answers XML)."""
	text = str(text or "")
	a = text.find("<%s>" % tag)
	if a < 0:
		return ""
	a += len(tag) + 2
	b = text.find("</%s>" % tag, a)
	return text[a: b if b > a else a + 80].strip()


def input_value(html, name: str) -> str:
	"""value="..." of the first <input name="NAME" ...> in a console page ('' when absent)."""
	html = str(html or "")
	at = html.find('name="%s"' % name)
	if at < 0:
		return ""
	end = html.find(">", at)
	tag = html[at: end if end > at else at + 600]
	v = tag.find('value="')
	if v < 0:
		return ""
	return tag[v + 7:].split('"')[0].strip()


def strip_tags(html) -> str:
	import re

	return " ".join(re.sub(r"<[^>]+>", " ", str(html or "")).split())


def model_for(devices_type: str, models: dict | None = None) -> str:
	"""The IM model id for an ERP Device Type: exact name, then a suffix match. '' when unknown."""
	models = models if models is not None else config()["models"]

	def norm(t):
		return str(t or "").upper().replace(" ", "").replace("-", "").replace("_", "")

	n = norm(devices_type)
	if not n:
		return ""
	for k, v in models.items():
		if norm(k) == n:
			return str(v)
	for k, v in models.items():
		if n.endswith(norm(k)) or norm(k).endswith(n):
			return str(v)
	return ""


def build_form(*, name: str, imei: str, company: str, branch: str, model: str, sim_number: str = "",
               reseller: str = "", admin_id: str = "", sim_provider: str = "", obd: bool = False,
               sensors: dict | None = None, user: str = "") -> dict:
	"""The full Add Vehicle form as it is posted. Pure: nothing is read or sent."""
	sensors = sensors or {}
	screen = sensors.get("screen") or {}
	cfg = DEFAULTS
	form = dict(FORM_DEFAULTS)
	form.update({
		"applicationid": APPLICATION_ID,
		"object_admin_entity_id": admin_id or cfg["admin_id"],
		"object_reseller_entity_id": reseller or cfg["reseller_id"],
		"object_company_id": company, "object_location_id": branch,
		"gps_device_company_id": company, "sim_company_id": company,
		"gps_device_location_id": branch, "sim_location_id": branch,
		"object_device_name": name, "vehicle_name": name, "vehicle_no": name,
		"object_gps_device_model_id": model, "object_imei_no": imei,
		"object_sim_card_no": "".join(c for c in str(sim_number or "") if c in "0123456789"),
		"sim_provide": sim_provider or cfg["sim_provider"],
		"Is_obd_device": "true" if obd else "false",
		"vehicle_type": str(screen.get("vehicle_type") or PROFILE["vehicle_type"]),
		"object_brand": str(screen.get("vehicle_brand_id") or PROFILE["object_brand"]),
		"vehicle_model": str(screen.get("vehicle_model_id") or PROFILE["vehicle_model"]),
		"iframedata1": json.dumps(sensors.get("rows") or []),
		"save_calib_json": sensors.get("calib") or "",
		"user": user,
	})
	form["vehicle_model_id"] = form["vehicle_model"]
	return form


# ------------------------------------------------------------------ configuration
def _cache() -> dict:
	"""One small cache per request or job, so a list is read from IM once."""
	if not hasattr(frappe.local, "app_apis_im"):
		frappe.local.app_apis_im = {}
	return frappe.local.app_apis_im


def web_base() -> str:
	return str(frappe.conf.get("app_apis_im_web") or WEB).rstrip("/")


def config() -> dict:
	"""admin_id, reseller_id, resellers, sim_provider and the model table, with the app_apis settings
	(IM section) applied over the defaults in this module."""
	c = _cache()
	if "config" in c:
		return c["config"]
	out = {"admin_id": DEFAULTS["admin_id"], "reseller_id": DEFAULTS["reseller_id"],
	       "resellers": list(DEFAULTS["resellers"]), "sim_provider": DEFAULTS["sim_provider"], "models": dict(MODELS)}
	try:
		doc = frappe.get_doc(SETTINGS)
		for key, field in (("admin_id", "im_admin_id"), ("reseller_id", "im_default_reseller_id"),
		                   ("sim_provider", "im_sim_provider")):
			if str(doc.get(field) or "").strip():
				out[key] = str(doc.get(field)).strip()
		ids = [x.strip() for x in str(doc.get("im_reseller_ids") or "").split(",") if x.strip()]
		if ids:
			out["resellers"] = ids
		for row in doc.get("im_models") or []:
			if str(row.get("device_type") or "").strip() and str(row.get("im_model_id") or "").strip():
				out["models"][str(row.get("device_type")).strip()] = str(row.get("im_model_id")).strip()
	except Exception:
		pass
	c["config"] = out
	return out


# ------------------------------------------------------------------ session
def post(path: str, data: dict, referer: str | None = None, timeout: int = 60) -> dict:
	try:
		res = http.request(jar=JAR, url=web_base() + path, method="POST", data=data,
		                   headers={"Referer": referer or (web_base() + "/jsp/index.html")}, timeout=timeout)
	except Exception as e:
		return {"ok": False, "status": 0, "text": "", "json": None, "error": str(e)[:300]}
	if res.get("error"):
		res["ok"] = False
	return res


def get(path: str, timeout: int = 90) -> str:
	try:
		res = http.request(jar=JAR, url=web_base() + path, method="GET",
		                   headers={"Referer": web_base() + "/jsp/index.html"}, timeout=timeout)
	except Exception:
		return ""
	return str(res.get("text") or "")


def login(force: bool = False) -> dict:
	"""Sign in with the IM username / password in app_apis. Once per request; the cookie stays in the jar."""
	c = _cache()
	if "login" in c and not force:
		return c["login"]
	got = {"ok": False, "why": "", "user": ""}
	doc = frappe.get_doc(SETTINGS)
	user = str(doc.get("im_username") or "").strip()
	pwd = doc.get_password("im_password", raise_exception=False) if doc.get("im_password") else ""
	got["user"] = user
	if not user or not pwd:
		got["why"] = "IM Username / Password are not set in app_apis > IM Connection."
		c["login"] = got
		return got
	try:
		enc = crypto.rsa_encrypt(public_key=PUBKEY, text=pwd)
		http.clear_jar(jar=JAR)
		res = post("/UserLogin", {"username": user, "password": enc, "projectName": "VTS",
		                          "projectentity": "EMC", "webclient": "false"})
	except Exception as e:
		why = "IM sign-in failed -- " + str(e)[:200].replace(str(pwd), "***")
		if "ermission" in str(e) or not str(e).strip():
			why += " -- IM calls need a System Manager login (app_apis.core.http is limited to that role)"
		got["why"] = why
		c["login"] = got
		return got
	landed = str(res.get("url") or "")
	if res.get("error"):
		got["why"] = "IM sign-in failed -- " + str(res.get("error"))[:200]
	elif "/jsp/index" in landed:
		got["ok"] = True
	else:
		got["why"] = "IM did not accept the sign-in (landed on %s): %s" % (landed, strip_tags(res.get("text"))[:160])
	c["login"] = got
	return got


def xml(method: str, params: dict) -> dict:
	data = {"javaclassname": XML_CLASS, "javaclassmethodname": method}
	data.update(params)
	res = post("/GenerateXML", data)
	return {"ok": bool(res.get("ok")), "status": res.get("status"),
	        "text": str(res.get("text") or res.get("json") or "")}


def json_call(method: str, params: dict, cls: str | None = None) -> dict:
	data = {"javaclassname": cls or JSON_CLASS, "javaclassmethodname": method}
	data.update(params)
	res = post("/GenerateJSON?method=" + method, data)
	if res.get("json") is not None:
		return {"ok": True, "data": res.get("json"), "text": ""}
	text = str(res.get("text") or "")
	try:
		parsed = json.loads(text)
	except ValueError:
		parsed = None
	return {"ok": bool(res.get("ok")), "data": parsed, "text": text, "status": res.get("status")}


def rows(method: str, params: dict) -> list:
	"""The rows of one dropdown. IM answers `{root : [[{"values":"DATA"}],[row, ...]]}` (an unquoted
	`root`, fixed here before parsing). [] when empty."""
	got = json_call(method, dict(params, applicationid=APPLICATION_ID))
	text = str(got.get("text") or "").strip()
	data = got.get("data")
	if not isinstance(data, dict):
		try:
			data = json.loads(text.replace("{root :", '{"root":', 1))
		except ValueError:
			data = None
	root = (data or {}).get("root") or []
	if len(root) < 2 or not isinstance(root[1], list):
		return []
	return [r for r in root[1] if isinstance(r, dict)]


# ------------------------------------------------------------------ companies and branches
def companies(resellers: list | None = None) -> list:
	"""[{"id", "name", "reseller"}] for every company under the given resellers (default: the settings)."""
	c = _cache()
	key = "companies:" + ",".join(resellers or config()["resellers"])
	if key not in c:
		found = []
		for rid in resellers or config()["resellers"]:
			for r in rows("getCompanyDropdown", {"object_reseller_entity_id": rid}):
				if r.get("company_id"):
					found.append({"id": str(r["company_id"]), "name": str(r.get("short_name") or ""), "reseller": rid})
		c[key] = found
	return c[key]


def branches(company_id) -> list:
	"""[{"id", "name"}] in IM's own dropdown order."""
	c = _cache()
	key = "branches:%s" % company_id
	if key not in c:
		c[key] = [{"id": str(r.get("location_id") or ""), "name": str(r.get("location_name") or "")}
		          for r in rows("getBranchDropdown", {"object_company_id": company_id}) if r.get("location_id")]
	return c[key]


def first_branch(company_id) -> dict:
	"""IM's first branch for a company -- always the one vehicles go to. {"id", "note"}; the note is set
	when the first dropdown row is not also the lowest id."""
	bs = branches(company_id)
	if not bs:
		return {"id": "", "note": "company %s has no branch on IM" % company_id}
	ids = [frappe.utils.cint(b["id"]) for b in bs]
	first = bs[0]["id"]
	note = "" if frappe.utils.cint(first) == min(ids) else \
		"first dropdown branch %s is not the lowest id %s" % (first, min(ids))
	return {"id": first, "note": note}


def company_login(company_id) -> str:
	"""The username of one company (the email it is known by), read off its edit page. IM serves that page
	only after the Company overview was opened in the same session."""
	c = _cache()
	if "overview_companies" not in c:
		get(OVERVIEW_COMPANIES, 60)
		c["overview_companies"] = True
	page = get("/jsp/DetailScreen.jsp?entityid=%s&screenid=2286&mode=update&level=1&popup=false"
	           "&view=L&overviewscreenid=2286" % company_id, 60)
	return input_value(page, "username").lower()


# ------------------------------------------------------------------ is it there?
def has_imei(imei: str) -> dict:
	"""IM's own duplicate-IMEI check (no rate limit). "unknown" means IM did not answer, never "no"."""
	log = login()
	if not log["ok"]:
		return {"state": "unknown", "detail": log["why"]}
	r = xml("CheckDuplicateWeaponID", {"Imeino": str(imei), "sMode": "insert", "sGpsDeviceId": ""})
	if "<status>Match</status>" in r["text"]:
		return {"state": "yes", "detail": "IM's duplicate-IMEI check finds it"}
	if "<status>" in r["text"]:
		return {"state": "no", "detail": "IM's duplicate-IMEI check does not find it"}
	return {"state": "unknown", "detail": "IM's duplicate-IMEI check gave no answer -- HTTP %s %s" % (r["status"], r["text"][:160])}


def name_taken(name: str, company_id) -> dict:
	"""-> {"taken": bool, "vehicle_id": str}: is there a vehicle with this name in this company?"""
	r = xml("getDuplicateVehicle", {"vehiclenumber": name, "companyId": company_id, "sMode": "insert", "sVehicleId": ""})
	taken = "<status>Match</status>" in r["text"]
	return {"taken": taken, "vehicle_id": tag_text(r["text"], "iVehicleID") if taken else ""}


def vehicle_fields(vehicle_id) -> dict:
	"""{"name", "imei", "branch"} read from the vehicle's edit page; '' for an id that is not there."""
	c = _cache()
	if "overview_vehicles" not in c:
		get(OVERVIEW_VEHICLES, 60)
		c["overview_vehicles"] = True
	page = get("/jsp/DetailScreen.jsp?entityid=%s&screenid=2249&mode=update&level=1&popup=false"
	           "&view=L&overviewscreenid=2253" % vehicle_id)
	branch = ""
	at = page.find("DependentDropDown('object_location_id'")
	if at >= 0:
		parts = page[at: at + 400].split("'")
		# DependentDropDown('object_location_id','105373','false','<filter>','<selected>', ...
		if len(parts) > 9:
			branch = parts[9]
	return {"name": input_value(page, "object_device_name"), "imei": input_value(page, "object_imei_no"),
	        "branch": branch}


def find_by_name(name: str, company_id, imei: str = "") -> dict:
	"""Find a vehicle by its name inside a company, and prove it is the right device by its IMEI."""
	log = login()
	if not log["ok"]:
		return {"state": "unknown", "vehicle_id": "", "imei": "", "detail": log["why"]}
	taken = name_taken(name, company_id)
	if not taken["taken"] or not taken["vehicle_id"]:
		return {"state": "no", "vehicle_id": "", "imei": "",
		        "detail": "IM has no vehicle named %s in company %s" % (name, company_id)}
	fields = vehicle_fields(taken["vehicle_id"])
	if imei and fields["imei"] != str(imei):
		return {"state": "mismatch", "vehicle_id": taken["vehicle_id"], "imei": fields["imei"],
		        "detail": "IM vehicle %s is named %s but its IMEI is %s, not %s" % (
			        taken["vehicle_id"], name, fields["imei"] or "(unreadable)", imei)}
	return {"state": "yes", "vehicle_id": taken["vehicle_id"], "imei": fields["imei"], "branch": fields["branch"],
	        "detail": "IM vehicle %s (%s), IMEI %s" % (taken["vehicle_id"], name, fields["imei"])}


# ------------------------------------------------------------------ models and sensors
def sensors_for(model: str) -> dict:
	"""The sensor rows (iframedata1), calibrations (save_calib_json) and vehicle profile for a model:
	copied from its template vehicle when it has one, else the built-in minimum.
	-> {"rows", "calib", "screen", "from", "why"}"""
	tpl = str(TEMPLATE_VEHICLE.get(str(model)) or "")
	if tpl:
		got = json_call("getDataOfCopyObject", {"sVehicleID": tpl}, JSON_TZ_CLASS)
		data = got["data"]
		if isinstance(data, dict) and data.get("device_port_specification"):
			names = {str(p.get("model_port_specification_hidden_id") or ""): str(p.get("property_name") or "")
			         for p in data.get("device_port_specification") or []}
			save, by_spec = {}, {}
			for ck, value in (data.get("calibration_json") or {}).items():
				items = [value] if isinstance(value, dict) else (value or [])
				named = []
				for item in items:
					if not isinstance(item, dict):
						continue
					c2 = dict(item)
					sid = str(c2.get("selected_model_specification_id") or "")
					c2["property_name"] = names.get(sid, "")
					named.append(c2)
					by_spec.setdefault(sid, []).append(c2)
				save[ck] = named[0] if isinstance(value, dict) and named else named
			out_rows = []
			for p in data.get("device_port_specification") or []:
				sid = str(p.get("model_port_specification_hidden_id") or "")
				row = {"property_name": str(p.get("property_name") or ""),
				       "property_category": str(p.get("property_category") or ""),
				       "model_port_specification_id": sid, "port_allocation": str(p.get("port_allocation") or ""),
				       "device_port_specification_id": "0", "reading_type": str(p.get("reading_type") or "DIRECT"),
				       "work_hour_calculation": str(p.get("sWorkHourCalculation") or "false")}
				extra = p.get("analog_config")
				if extra and not isinstance(extra, dict):
					try:
						extra = json.loads(extra)
					except ValueError:
						extra = None
				if isinstance(extra, dict):
					row.update(extra)
				if sid in by_spec:
					row["calibration_json"] = json.dumps(by_spec[sid])
				out_rows.append(row)
			screen = (data.get("screen_data") or [{}])[0] or {}
			return {"rows": out_rows, "calib": json.dumps(save) if save else "", "screen": screen,
			        "from": "IM vehicle " + tpl, "why": ""}
		return {"rows": [], "calib": "", "screen": {}, "from": "",
		        "why": "could not read the sensors of template vehicle %s -- %s" % (tpl, str(got.get("text") or "")[:160])}
	rows_ = DEFAULT_SENSORS.get(str(model)) or []
	if rows_:
		return {"rows": rows_, "calib": "", "screen": {}, "from": "the built-in list", "why": ""}
	return {"rows": [], "calib": "", "screen": {}, "from": "",
	        "why": "no sensor template for IM model %s -- add it to app_apis.core.im.TEMPLATE_VEHICLE" % model}


# ------------------------------------------------------------------ create / delete
def create(*, name: str, imei: str, company: str, branch: str, model: str, sim_number: str = "",
           reseller: str = "") -> dict:
	"""Add a vehicle exactly as the Add Vehicle form's Save does, then read the IMEI back.

	Not created again if the name is already used in the company. The result is only ever "uploaded" when IM
	itself says the IMEI is now there."""
	missing = [k for k, v in (("name", name), ("imei", imei), ("company", company), ("branch", branch),
	                          ("model", model)) if not v]
	if missing:
		return {"verdict": "failure", "ok": False, "result": "cannot create: missing %s" % ", ".join(missing)}
	if not str(imei).isdigit():
		return {"verdict": "failure", "ok": False, "result": "IM takes digits only in the IMEI, got %s" % imei}
	log = login()
	if not log["ok"]:
		return {"verdict": "error", "ok": False, "result": "did not send: " + log["why"]}

	if name_taken(name, company)["taken"]:
		return {"verdict": "duplicate", "ok": False,
		        "result": "IM already has a vehicle named %s in company %s; not sent" % (name, company)}
	already = has_imei(imei)
	if already["state"] == "yes":
		return {"verdict": "duplicate", "ok": False, "result": "IM already has this IMEI; not sent"}

	sensors = sensors_for(model)
	if not sensors["rows"]:
		return {"verdict": "failure", "ok": False, "result": "did not send: " + sensors["why"]}
	info = json_call("getModelPortName", {"gps_device_model_id": model})
	flat = str(info.get("text") or info.get("data") or "").replace(" ", "").lower()
	obd = '"obd":"yes"' in flat or '"can":"yes"' in flat

	form = build_form(name=name, imei=imei, company=company, branch=branch, model=model, sim_number=sim_number,
	                  reseller=reseller, admin_id=config()["admin_id"], sim_provider=config()["sim_provider"],
	                  obd=obd, sensors=sensors, user=log["user"])
	form["object_reseller_entity_id"] = reseller or config()["reseller_id"]
	# IM serves the form only to a session that opened it the way a browser does
	get(OVERVIEW_VEHICLES, 60)
	get(ADD_VEHICLE_REFERER, 60)
	res = post("/jsp/ProcessDetails.jsp", form, web_base() + ADD_VEHICLE_REFERER, 120)
	raw = str(res.get("text") or "")
	said = strip_tags(raw)[:300] or str(res.get("error") or "")
	out = {"platform_said": "HTTP %s -- %s" % (res.get("status"), said or raw.strip()[:300]),
	       "sensors": "%d sensors from %s" % (len(sensors["rows"]), sensors["from"])}

	after = has_imei(imei)
	if after["state"] == "yes":
		found = find_by_name(name, company, imei)
		out.update({"verdict": "uploaded", "ok": True, "vehicle_id": found.get("vehicle_id", ""),
		            "detail": after["detail"],
		            "result": "created on IM -- %s. %s. IM said: %s" % (after["detail"], out["sensors"], said[:160])})
	elif after["state"] == "no":
		out.update({"verdict": "failure", "ok": False, "result": "IM did not create it. IM said: " + out["platform_said"]})
	else:
		out.update({"verdict": "error", "ok": False,
		            "result": "sent, but it cannot be confirmed right now -- %s. Verify before sending again." % after["detail"]})
	return out


def pin_needed() -> bool:
	"""Does this IM account protect deletes with a Security PIN?"""
	r = xml("getUserSessionData", {"rights": "UserLevelRights,subaccount_of,projectid"})
	return tag_text(r["text"], "isEnabledSecurity").lower() == "true"


def check_pin(pin: str) -> bool:
	got = json_call("isCheckSecurityPin", {"security_pin": str(pin or "").strip()})
	data = got.get("data")
	return isinstance(data, dict) and bool(data.get("result"))


def delete(*, vehicle_id, company, branch, imei: str = "", pin: str = "") -> dict:
	"""Remove a vehicle with IM's BulkVehicleRemove. Cannot be undone. When the account has a Security PIN
	it must be given and is checked with IM first (never bypassed). Read back afterwards."""
	log = login()
	if not log["ok"]:
		return {"verdict": "error", "ok": False, "result": "did not send: " + log["why"]}
	if not (vehicle_id and company and branch):
		return {"verdict": "failure", "ok": False, "result": "cannot delete: vehicle_id, company and branch are all needed"}
	if pin_needed():
		if not str(pin or "").strip():
			return {"verdict": "not_deleted", "ok": False, "result": "nothing deleted -- this IM account needs its Security PIN"}
		if not check_pin(pin):
			return {"verdict": "not_deleted", "ok": False, "result": "nothing deleted -- IM did not accept the Security PIN"}
	res = json_call("bulkObjectRemove", {"vehicle_ids": str(vehicle_id), "companyid": str(company), "branchid": str(branch)})
	data = res.get("data")
	said = str(res.get("text") or data or "")[:200]
	accepted = isinstance(data, dict) and frappe.utils.cint(data.get("result")) == 1
	gone = not vehicle_fields(vehicle_id)["imei"]
	if imei:
		gone = gone and has_imei(imei)["state"] == "no"
	if accepted and gone:
		return {"verdict": "deleted", "ok": True, "result": "deleted from IM -- it is no longer there. " + said}
	if accepted:
		return {"verdict": "error", "ok": False, "result": "IM accepted the delete but still finds the vehicle -- check IM. " + said}
	return {"verdict": "failure", "ok": False, "result": "IM did not delete it. IM said: " + (said or "(nothing)")}


# ------------------------------------------------------------------ which company does an email belong to?
def customers_with(email: str) -> list:
	return [r.name for r in frappe.db.sql(
		"select name from `tabCustomer` where lower(trim(email_im_platform)) = %(e)s",
		{"e": str(email or "").strip().lower()}, as_dict=True)]


def cache_fields_exist() -> bool:
	meta = frappe.get_meta("Customer")
	return all(meta.has_field(f) for f in ("im_company_id", "im_reseller_id", "im_branch_id"))


def sync_customers(email: str = "", limit: int = 60, force: bool = False, skip: int = 0) -> dict:
	"""Fill Customer.im_company_id / im_reseller_id / im_branch_id from IM. IM's company list has no email,
	so each company's username is read off its edit page and matched to Customer.email_im_platform.
	Companies that already map to a Customer are skipped unless `force`. Stops after `limit` page reads
	(call again to continue), or as soon as `email` is found."""
	stats = {"companies": 0, "read": 0, "matched": 0, "no_customer": [], "no_login": [], "remaining": 0, "found": None}
	log = login()
	if not log["ok"]:
		stats["error"] = log["why"]
		return stats
	known = set()
	has_fields = cache_fields_exist()
	if has_fields and not force:
		known = {str(r[0]) for r in frappe.db.sql("select im_company_id from `tabCustomer` where ifnull(im_company_id,'') != ''")}
	want = str(email or "").strip().lower()
	todo = []
	for c in companies():
		stats["companies"] += 1
		if c["id"] not in known:
			todo.append(c)
	for c in todo[skip:]:
		if stats["read"] >= limit or (stats["found"] and want):
			stats["remaining"] += 1
			continue
		stats["read"] += 1
		login_name = company_login(c["id"])
		if not login_name:
			stats["no_login"].append(c["id"])
			continue
		fb = first_branch(c["id"])
		hit = {"company": c["id"], "reseller": c["reseller"], "branch": fb["id"], "name": c["name"],
		       "login": login_name, "branch_note": fb["note"]}
		names = customers_with(login_name)
		if has_fields:
			for n in names:
				frappe.db.set_value("Customer", n, {"im_company_id": c["id"], "im_reseller_id": c["reseller"],
				                                    "im_branch_id": fb["id"]})
				stats["matched"] += 1
		if not names:
			stats["no_customer"].append(login_name)
		if want and login_name == want:
			stats["found"] = hit
	return stats


def account_for_email(email: str, refresh: bool = True) -> dict:
	"""The IM company + branch + reseller a vehicle with this im_platform email belongs to.

	1. the Customer whose email_im_platform is this email and that already has the cached ids;
	2. otherwise (refresh=True) look for the email on IM once and use what is found;
	3. otherwise {"company": ""...} with `why` saying whether the email is missing from IM or from ERP.
	Never matched by name."""
	out = {"company": "", "branch": "", "reseller": "", "how": "", "why": ""}
	want = str(email or "").strip().lower()
	if not want:
		out["why"] = "the vehicle has no im_platform email"
		return out
	if cache_fields_exist():
		rows_ = frappe.db.sql(
			"select name, im_company_id, im_reseller_id, im_branch_id from `tabCustomer` "
			"where lower(trim(email_im_platform)) = %(e)s and ifnull(im_company_id,'') != '' order by name",
			{"e": want}, as_dict=True)
		if rows_:
			r = rows_[0]
			out.update({"company": str(r.im_company_id).strip(), "reseller": str(r.im_reseller_id or "").strip(),
			            "branch": str(r.im_branch_id or "").strip(),
			            "how": "email %s -> IM company %s (cached on Customer %s)" % (want, r.im_company_id, r.name)})
			if not out["branch"]:
				out["branch"] = first_branch(out["company"])["id"]
			if not out["branch"]:
				out["why"] = "company %s has no branch on IM" % out["company"]
			return out
	if refresh:
		got = sync_customers(want, 400)
		if got.get("found"):
			f = got["found"]
			out.update({"company": f["company"], "reseller": f["reseller"], "branch": f["branch"],
			            "how": "email %s -> IM company %s '%s' (read from IM)" % (want, f["company"], f["name"])})
			if not out["branch"]:
				out["why"] = "company %s has no branch on IM" % out["company"]
			return out
		if got.get("error"):
			out["why"] = got["error"]
			return out
		if got.get("remaining"):
			out["why"] = "read 400 IM companies without finding %s; %d left -- call sync_customers again" % (want, got["remaining"])
			return out
	if customers_with(want):
		out["why"] = "no IM company (resellers %s) has the username %s -- create it on IM or fix the Customer's email" % (
			", ".join(config()["resellers"]), want)
	else:
		out["why"] = "IM Platform email %s is on no ERP Customer, and no IM company has it as username" % want
	return out
