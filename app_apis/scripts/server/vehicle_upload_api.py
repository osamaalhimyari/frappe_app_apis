# =============================================================================
# vehicle_upload_api  --  Server Script (API), SEPARATE from app_apis
# =============================================================================
#
# Install as: Server Script · Script Type "API" · API Method  vehicle_upload_api
# Nothing in app_apis is changed. This script reads app_apis' settings and talks
# to the Administrator API itself, exactly the way `expired_devices_api` does.
#
# ACTIONS
#   action=plan     resolve every id and return the payload. Sends nothing.
#   action=verify   cmd=vehinfo -- ask Pilot whether the IMEI is registered, and
#                   stamp the ticket from the answer. Writes nothing to Pilot.
#   action=delete_plan / delete   remove ONE vehicle from ONE platform (see the delete section)
#   action=im_sync  fill the Customer IM company/reseller/branch ids from IM (key = email)
#   action=upload   cmd=vehadd -- register the vehicle. Guarded by LIVE.
#
#   target  = pilot_wsl | pilot2 | im | all
#   vehicle = Customer Vehicle name
#
# THE PILOT COMMANDS (docs.pilot-gps.com, Administrator API, verified live)
#   vehadd        add a vehicle          POST/GET /backend/api.php
#   vehinfo       one vehicle by imei    GET      /backend/api.php
#   vehtypelist   the `type` ids         GET      -> [{"id":2,"name":"Truck"},...]
#   vehmodellist  the `vehmodel` ids     GET      -> [{"id":464,"name":"Teltonika FMC920"},...]
#   accountslist  the `account_id`s      GET      -> [{"id":1774,"bill_email":"..."},...]
#
#   The docs call the list key `identifier`; the live server returns `id`.
#   Both are read, so a future flip does not break this.
#
# HOW THE FIVE REQUIRED vehadd VALUES ARE FOUND
#   imei        Customer Vehicle.device_serial
#   name        license_plate / e_license_plate / plate_num
#   account_id  accountslist row whose bill_email == the vehicle's platform email
#   vehmodel    vehmodellist row matching Customer Vehicle.devices_type
#   type        vehtypelist  row matching type_new / type
#   msisdn      app_apis_fleet_audit.sim_msisdn for this IMEI  (sim_serial is an
#               ICCID, not a phone number, so it cannot be used here)
#   node        app_apis settings for the chosen admin account
#
# ON SUCCESS the newest Installation xticket for this vehicle gets
# `server_status` = uploaded. Refusals write duplicate / vehicle_not_found /
# failure -- the four records that already exist in the `server status` doctype.
# A broken connection on OUR side writes NOTHING; see SPEAKS below.
#
# IM (Trakzee, gps.im2m.ws) has no create-vehicle API, so the upload goes in
# through the web app's own "Add Vehicle" form, the way /home/im.md traces it:
# sign in (RSA-encrypted password), check the name and IMEI are free, POST
# /jsp/ProcessDetails.jsp with the vehicle + device + SIM + sensors, then read
# the IMEI back to confirm. See the IM SETTINGS block below -- the company and
# branch per platform email, and the device model ids, must be filled in.
# IM upload needs a System Manager (it uses app_apis.core.http).
# =============================================================================

# Which targets may really call cmd=vehadd. Anything not listed here resolves
# the payload, shows it, and sends nothing. An empty tuple arms nothing at all.
#
#   ()               nothing sends            <- safe default
#   ("pilot_wsl",)   only Pilot (WSL) sends
#   ("pilot_wsl", "pilot2")
#
# PILOT HAS NO DELETE. The Administrator API adds, edits, moves and
# blocks/unblocks -- there is no command that removes a vehicle. A vehicle added
# by mistake can only be blocked or moved to another account, by hand, on the
# platform. Treat the first real send as permanent.
LIVE_TARGETS = ("pilot_wsl", "im")

# How `folder` goes out with vehadd. The docs call it optional and a NAME; the
# live WSL server disagrees on the first and, going by what it answered, on the
# second. Measured, for an account whose root folder is named exactly like it:
#
#   "omit"   folder not sent at all          -> HTTP 400 "Missing parameter folder"
#   "name"   folder=<the root folder's name> -> HTTP 200 "You do not have access
#            for this folder", nothing created
#   "empty"  folder=  (present, blank)       -> the same refusal
#   "id"     folder=<the root folder's ID, from cmd=getfolders>
#
# A name and a blank drawing the SAME refusal is what a numeric lookup does with
# anything that is not a number -- so the ID is what is sent.
FOLDER_MODE = "name"

# Which door the vehicle goes in through (the user chose "panel", 2026-10-01).
#
#   "panel"   the admin panel's own create (backend/app/fitter.php,
#             cmd=create_vehicle) -- the same call its "Mechanic -> Add" button
#             makes. Signs in with the same admin account (backend/login.php)
#             and lands the vehicle in the account's root folder, so no folder,
#             node or type is sent.
#   "vehadd"  the documented Administrator API. On the WSL estate it refuses
#             every folder value for this admin login (see FOLDER_MODE).
UPLOAD_VIA = "panel"

STAMP_ALL = 0            # 0 = stamp only the newest Installation ticket

ROLES = ("System Manager", "Technical")
SETTINGS = "app_apis"
SCRIPT_NAME = "vehicle_upload_api"
BACKEND = "/backend/api.php"
VEH_DT = "Customer Vehicle"
TICKET_DT = "xticket"
AUDIT_DT = "app_apis_fleet_audit"
INSTALL_TYPE = "Installation"

TARGETS = {
    "pilot_wsl": {"label": "Pilot (WSL)", "account": 1, "email_field": "email_pilot"},
    "pilot2": {"label": "Pilot 2", "account": 2, "email_field": "email_pilot2"},
    "im": {"label": "IM (Trakzee)", "account": 0, "email_field": "im_platform"},
}

# The cluster node vehadd creates the vehicle on.
#
#   WASL is always node 5.
#   The ordinary Pilot estate spans node_id 2..5 and differs PER ACCOUNT, so it
#   cannot be guessed -- set pilot_admin_node_2 in app_apis, or the payload is
#   reported as incomplete rather than sent to the wrong node.
#
# A wrong node puts the vehicle on the wrong cluster, and Pilot has no delete.
DEFAULT_NODE = {1: "5", 2: ""}

# Device types the ERP uses that vehmodellist has no obvious name match for.
# Fill in the Pilot model id once and it is used from then on.
MODEL_OVERRIDES = {
    "TZ-RD07": 0,
    "TZ-RD06": 0,
    "M300S": 0,
    "W18L": 0,
    "JC261P": 0,
    "FMC003": 0,
}

# ERP vehicle type -> Pilot vehtypelist name, where the words differ.
TYPE_ALIASES = {
    "Single Cab Pickup": "Pickup",
    "Double Cab Pickup": "Pickup",
    "Goods Vehicle": "Light Truck",
    "Trailer Head": "Road tractor",
    "Dump Truck": "Lorry",
    "Passenger Vehicle": "Car",
    "Warehouse": "Static",
    "Motorcycle": "Motocycle",
    "Truck": "Truck",
    "Trailer": "Trailer",
    "Tractor": "Tractor",
    "Excavator": "Excavator",
    "Bus": "Bus",
    "Van": "Van",
}

# ---------------------------------------------------------------- IM SETTINGS
# Add "im" to LIVE_TARGETS (top of this script) only after the first test.
IM_WEB = "https://gps.im2m.ws"
IM_JAR = "im_web"                 # the cookie jar the IM session lives in
IM_APPLICATION_ID = "37"

# Who owns the vehicle on IM. Key = the vehicle's `im_platform` email.
# company / branch are object_company_id / object_location_id from the IM
# Add Vehicle form (Company and Branch dropdowns).
# Format: "<im_platform email>": {"company": "<id>", "branch": "<id>", "reseller": "<id, only if not IM_RESELLER_ID>"}
IM_ACCOUNTS = {
    # Al Jazeera Winds Logistics Services Est. (admin 1213, reseller 3266 Global Staff Company).
    # Swap in this customer's `im_platform` email from ERPNext, then un-comment.
    # "EMAIL_FROM_ERPNEXT": {"company": "48839", "branch": "54192"},
}
# Used for every email that is not in IM_ACCOUNTS. Leave empty to refuse.
IM_DEFAULT_ACCOUNT = {"company": "", "branch": ""}

IM_ADMIN_ID = "1213"              # object_admin_entity_id
IM_RESELLER_ID = "3237"           # object_reseller_entity_id when the company's own reseller is unknown
IM_RESELLERS = ["3237", "3267"]   # resellers scanned for companies (im2m, IM2M-B2B)
IM_SIM_PROVIDER = "2"             # sim_provide

# ERP Device Type -> IM gps_device_model_id ("Device Type" dropdown on IM).
IM_MODELS = {
    "FMC130": "1436",
    "FMC130 Static": "2736",
    "FMC130-Trace": "2439",
    "FMB120": "345",
    "FMB125": "369",
    "FMC920": "1787",
}

# Sensors are copied from an existing IM vehicle of the same model (the Add
# Vehicle form's own "Copy From"): model id -> IM vehicle id. Vehicle 255059
# (1328-LXA) is the FMC130 example from im.md.
IM_TEMPLATE_VEHICLE = {
    "1436": "255059",
}
# Used when a model has no template vehicle: the minimum im.md shows.
IM_DEFAULT_SENSORS = {
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
IM_PROFILE = {"vehicle_type": "2129", "object_brand": "423", "vehicle_model": "914"}

# "api" = Verify asks IM's API (app_apis.im_connector, ~1 call a minute).
# "web" = Verify asks the web app's duplicate-IMEI check (no rate limit).
IM_VERIFY_VIA = "api"

IM_PUBKEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCgQTb0CjymoOrYWr6HY+Bdkb6IS7dDTXXCupZC3WAPYCtD/JM5ejIE/+dS28KMffV70HLd2O1JLYeT6blsSZsv1cY+DID0G9h0ikjhzkCJmEPQ59MT+Ob+i1P4OFU88wrWLbRbgXJCIodB9BItBRiL36SB7P0B9PdytjaYga3TzwIDAQAB
-----END PUBLIC KEY-----"""

IM_HTTP = "app_apis.core.http.request"
IM_CLEAR = "app_apis.core.http.clear_jar"
IM_RSA = "app_apis.core.crypto.rsa_encrypt"
IM_XML_CLASS = "com.uffizio.tools.projectmanager.GenerateXmlUsingAjax"
IM_JSON_CLASS = "com.uffizio.tools.projectmanager.GenerateJSONAjax"
IM_JSON_TZ_CLASS = "com.uffizio.tools.projectmanager.GenerateJSONAjaxTrakzee"
IM_DETAIL_REFERER = (IM_WEB + "/jsp/DetailScreen.jsp?entityid=0&screenid=2249&mode=insert&popup=false"
                     "&view=L&overviewscreenid=2253&modulename=")

# The rest of the Add Vehicle form, exactly as a fresh form serialises it (im.md 4.4).
IM_FORM_DEFAULTS = {
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

args = frappe.form_dict
action = str(args.get("action") or "plan").strip().lower()
target = str(args.get("target") or "").strip().lower()
vehicle_name = str(args.get("vehicle") or "").strip()

out = {"ok": False, "action": action, "target": target,
       "live_targets": list(LIVE_TARGETS), "results": []}

allowed = str(frappe.session.user) == "Administrator"
for r in frappe.db.sql("select role from `tabHas Role` where parent = %(u)s",
                       {"u": frappe.session.user}, as_dict=True):
    if str(r.role) in ROLES:
        allowed = True

# one resolved-lookup cache per request, so a multi-target run reads each list once
LOOKUPS = {}

# The IM section of the app_apis settings (IM Upload / IM Device Models)
# overrides the values written at the top of this script. A server whose app_apis does
# not have those fields yet simply keeps the values above.
try:
    im_cfg = frappe.get_doc(SETTINGS)
    if str(im_cfg.get("im_admin_id") or "").strip():
        IM_ADMIN_ID = str(im_cfg.get("im_admin_id")).strip()
    if str(im_cfg.get("im_default_reseller_id") or "").strip():
        IM_RESELLER_ID = str(im_cfg.get("im_default_reseller_id")).strip()
    if str(im_cfg.get("im_sim_provider") or "").strip():
        IM_SIM_PROVIDER = str(im_cfg.get("im_sim_provider")).strip()
    im_ids = [x.strip() for x in str(im_cfg.get("im_reseller_ids") or "").split(",") if x.strip()]
    if im_ids:
        IM_RESELLERS = im_ids
    for im_row in im_cfg.get("im_models") or []:
        if str(im_row.get("device_type") or "").strip() and str(im_row.get("im_model_id") or "").strip():
            IM_MODELS[str(im_row.get("device_type")).strip()] = str(im_row.get("im_model_id")).strip()
except Exception:
    pass


# ---------------------------------------------------------------- the platform
def pilot_settings(account):
    doc = frappe.get_doc(SETTINGS)
    acc_no = frappe.utils.cint(account)
    # The settings fields are pilot_admin_* for account 1 and pilot_admin2_* for account 2
    # (NOT a "_2" suffix -- reading pilot_admin_base_url_2 made Pilot 2 look unconfigured).
    prefix = "pilot_admin" if acc_no == 1 else "pilot_admin2"
    base = str(doc.get(prefix + "_base_url") or "").strip().rstrip("/")

    # The setting wins; otherwise the known default for that estate. Left empty
    # on purpose for the ordinary Pilot estate, because its node varies per
    # account and a wrong node cannot be undone.
    node = str(doc.get(prefix + "_node") or "").strip()
    if not node:
        node = str(DEFAULT_NODE.get(acc_no) or "")

    return {
        "url": base + BACKEND,
        "user": str(doc.get(prefix + "_username") or "").strip(),
        "password": doc.get_password(prefix + "_password", raise_exception=False),
        "node": node,
        "label": "",
    }


def backend(cmd, params, account, method="GET"):
    """One Administrator API call. Basic auth, same as expired_devices_api.

    The read commands (vehinfo, vehtypelist, vehmodellist, accountslist) are GET.
    `vehadd` is documented as POST -- sending it as GET answers 400. Either way
    the parameters go in the QUERY STRING, not a body; that is how this API is
    built and how `setvehblock` is called elsewhere on this site.

    Returns {"code":0,"data":[...]} or a negative code with `msg`.
    """
    cfg = pilot_settings(account)
    if not cfg["user"] or not cfg["password"] or not cfg["url"].startswith("http"):
        return {"code": -500, "msg": "Pilot admin account " + str(account) +
                " is not configured in app_apis.", "data": []}

    query = {"cmd": cmd, "node": cfg["node"]}
    for k in (params or {}).keys():
        query[k] = params[k]

    try:
        if str(method).upper() == "POST":
            answer = frappe.make_post_request(cfg["url"], auth=(cfg["user"], cfg["password"]),
                                              params=query)
        else:
            answer = frappe.make_get_request(cfg["url"], auth=(cfg["user"], cfg["password"]),
                                             params=query)
    except Exception as e:
        # frappe.make_* calls raise_for_status(), whose message drops Pilot's own
        # explanation. The HTTPError still carries the response, and on a 400 the
        # body is the only thing that says why. (`frappe.flags` in here is
        # safe_exec's own empty dict, so the response cannot be read from there.)
        # A Response is falsy on a 4xx, so it is tested against None, not truth.
        status = 0
        body = ""
        resp = e.response
        if resp is not None:
            status = frappe.utils.cint(resp.status_code)
            body = str(resp.text or "").strip()[:400]
        if status in (400, 404, 409, 422):
            # Pilot heard the call and refused it -- an answer, not an outage.
            return {"code": -1, "http": status, "data": [],
                    "msg": "HTTP " + str(status) + " -- " + (body or str(e)[:200])}
        detail = ("HTTP " + str(status) + " ") if status else ""
        if body:
            detail = detail + "-- Pilot said: " + body + " "
        return {"code": -502, "http": status, "data": [],
                "msg": "Administrator API did not answer properly. " + detail + str(e)[:200]}

    # Pilot answers a bare `null` -- not an object, not an empty list -- when
    # vehinfo has no match for the IMEI. Reading it as "no rows" is what it
    # means. Without this, `answer.setdefault` below hits None, and because
    # safe_exec yields None for an attribute of None rather than raising, the
    # script died with "'NoneType' object is not callable".
    if answer is None:
        return {"code": 0, "msg": "no data", "data": []}

    if not isinstance(answer, dict) and not isinstance(answer, list):
        try:
            answer = json.loads(answer)
        except Exception:
            return {"code": -500, "msg": "Pilot answered with something that is not JSON: " +
                    str(answer)[:160], "data": []}
        if answer is None:
            return {"code": 0, "msg": "no data", "data": []}

    # vehinfo answers with a bare list; the list commands answer with an object
    if isinstance(answer, list):
        return {"code": 0, "msg": "OK", "data": answer}
    # A missing or bad input comes back as HTTP 200 {"Fail": "..."} with no
    # `code` at all -- which would otherwise read as code 0, a success.
    if answer.get("Fail"):
        return {"code": -1, "http": 200, "msg": str(answer.get("Fail")), "data": []}
    answer.setdefault("data", [])
    return answer


# ---------------------------------------------------------------- the admin panel
# The panel at <base>/backend/app/*.php is what the Pilot admin site itself
# runs on. It takes a JWT (Authorization: Bearer ...) that backend/login.php
# hands out for the same username and password the Administrator API uses.
def panel_base(account):
    return pilot_settings(account)["url"].replace(BACKEND, "") + "/backend/"


def panel_json(answer):
    if answer is None or isinstance(answer, dict) or isinstance(answer, list):
        return answer
    try:
        return json.loads(answer)
    except Exception:
        return {"_text": str(answer)[:400]}


def http_failure(e):
    """Status and body of a failed call. A Response is falsy on a 4xx, so it
    is tested against None, not truth."""
    status = 0
    text = ""
    try:
        resp = e.response
        if resp is not None:
            status = frappe.utils.cint(resp.status_code)
            text = str(resp.text or "").strip()[:400]
    except Exception:
        status = 0
    return {"status": status, "text": text or str(e)[:200]}


def panel_token(account):
    """Sign in once per request, the way the panel's login form does:
    get_user_without_auth tells whether this login is a partner, and a partner
    must send its own id as partner_id with cmd=login."""
    key = "panel-token@" + str(account)
    if key in LOOKUPS:
        return LOOKUPS[key]
    cfg = pilot_settings(account)
    got = {"token": "", "why": ""}
    if not cfg["user"] or not cfg["password"] or not cfg["url"].startswith("http"):
        got["why"] = "Pilot admin account " + str(account) + " is not configured in app_apis."
        LOOKUPS[key] = got
        return got
    url = panel_base(account) + "login.php"
    try:
        who = panel_json(frappe.make_post_request(url, data={
            "cmd": "get_user_without_auth", "username": cfg["user"], "password": cfg["password"]}))
        if not isinstance(who, dict):
            who = {}
        form = {"cmd": "login", "time_zone": "0", "username": cfg["user"],
                "password": cfg["password"], "email": str(who.get("email") or cfg["user"])}
        if not who.get("isadmin") and (who.get("ispartner") or who.get("adm_user_id")) and who.get("id"):
            form["partner_id"] = str(who.get("id"))
        ans = panel_json(frappe.make_post_request(url, data=form))
        if not isinstance(ans, dict):
            ans = {}
        got["token"] = str(ans.get("jwt_token") or "")
        if not got["token"]:
            got["why"] = ("the panel sign-in gave no token -- " +
                          str(ans.get("auth_message") or ans.get("error") or ans.get("_text") or "")[:160])
    except Exception as e:
        f = http_failure(e)
        got["why"] = ("the panel sign-in failed -- HTTP " + str(f["status"]) + " " +
                      f["text"].replace(str(cfg["password"]), "***")[:160])
    LOOKUPS[key] = got
    return got


def panel_call(path, data, account, method="GET", extra_headers=None):
    """-> {"ok", "status", "body", "text"}; never raises."""
    tok = panel_token(account)
    if not tok["token"]:
        return {"ok": False, "status": 0, "body": None, "text": tok["why"]}
    url = panel_base(account) + path
    hdr = {"Authorization": "Bearer " + tok["token"]}
    for k in (extra_headers or {}).keys():
        hdr[k] = extra_headers[k]
    try:
        if method == "POST":
            ans = frappe.make_post_request(url, headers=hdr, data=data)
        else:
            ans = frappe.make_get_request(url, headers=hdr, params=data)
    except Exception as e:
        f = http_failure(e)
        return {"ok": False, "status": f["status"], "body": None, "text": f["text"]}
    body = panel_json(ans)
    text = ""
    if isinstance(body, dict):
        text = str(body.get("_text") or body.get("error") or body.get("msg") or "")
    return {"ok": True, "status": 200, "body": body, "text": text}


def panel_find(imei, account):
    """The panel's own search (Mechanic grid), matched on the exact IMEI."""
    want = str(imei or "").strip()
    res = panel_call("app/fitter.php", {"cmd": "get_agents", "query": want,
                                        "page": 1, "start": 0, "limit": 25}, account)
    if not res["ok"] or not isinstance(res["body"], dict) or "data" not in res["body"]:
        return {"state": "unknown", "row": {},
                "detail": ("the panel search did not answer -- HTTP " + str(res["status"]) + " " +
                           str(res["text"])[:160])}
    for r in res["body"].get("data") or []:
        if isinstance(r, dict) and str(r.get("uniqid") or "").strip() == want:
            return {"state": "yes", "row": r,
                    "detail": ("found as " + str(r.get("vehiclenumber") or "?") + ", agent_id " +
                               str(r.get("agent_id") or "?") + ", account " +
                               str(r.get("account_id") or "?"))}
    return {"state": "no", "row": {}, "detail": "the panel finds no vehicle with this IMEI"}


def resolve_panel_model(devices_type, account):
    """The panel's device type is a NAME from its own list (fitter.php
    get_vehicle_types), e.g. "Teltonika FMC920" for the ERP's "FMC920"."""
    want = str(devices_type or "").strip()
    if not want:
        return {"name": "", "why": "the vehicle has no Device Type"}
    key = "panel-types@" + str(account)
    if key not in LOOKUPS:
        res = panel_call("app/fitter.php", {"cmd": "get_vehicle_types"}, account)
        rows = []
        body = res["body"]
        if isinstance(body, dict):
            rows = body.get("data") or []
        elif isinstance(body, list):
            rows = body
        LOOKUPS[key] = {"rows": rows, "err": "" if rows else ("get_vehicle_types failed -- " +
                                                              str(res["text"])[:120])}
    got = LOOKUPS[key]
    if got["err"]:
        return {"name": "", "why": got["err"]}
    pinned = frappe.utils.cint(MODEL_OVERRIDES.get(want) or 0)
    best = ""
    n = norm(want)
    for r in got["rows"]:
        name = str(r.get("name") or "") if isinstance(r, dict) else str(r)
        if pinned and isinstance(r, dict) and frappe.utils.cint(r.get("id")) == pinned:
            return {"name": name, "why": ""}
        nn = norm(name)
        if n and (nn == n or nn.endswith(n)):
            if not best or len(name) < len(best):
                best = name
    if not best:
        for r in got["rows"]:
            name = str(r.get("name") or "") if isinstance(r, dict) else str(r)
            if n and n in norm(name) and (not best or len(name) < len(best)):
                best = name
    if best:
        return {"name": best, "why": ""}
    return {"name": "", "why": ("no panel device type matches " + want +
                                " -- put its Pilot model id in MODEL_OVERRIDES")}


# ---------------------------------------------------------------- sensors
# Every vehicle created here gets the same six sensors: External Voltage,
# Ignition, GSM Signal, Temperature (BLE_T1), Humidity (BLE_Humidity1) and
# weight (IN1, with its calibration table). The list is FIXED here: it is the
# panel's own "Export sensors" of vehicle 239355 (vehicles.php, cmd=
# exportSensors), with each sensor's id/plugged_id removed (they belong to that
# vehicle) and the tag links cleared (tags are per account). It goes in through
# the panel's own "Import sensors" (vehicles.php, cmd=importSensors), which
# reads exactly this list format -- the grouped "sensors/tags" file is refused
# with "Missing required parameter: info".
SENSOR_TEMPLATE = r'''[{"sensortype":"Dedicated","sensortypeid":1,"sensorinterpret":"External power supply","sensorinterpretid":6,"info":"External Voltage","sensorfieldname":"Vsourse","fieldname":"Vsourse","sensorminvalue":"0.000","sensormaxvalue":"30000.000","minvalue":"0.000","maxvalue":"30000.000","semanticid":6,"typeid":1,"sensordefaultdesc":"Voltage","defaultdescription":"Voltage","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"V","defaultmeasureunit":"V","checkbox":0,"history":1,"formula":null,"filter":1,"moto":0,"tags":null,"tooltips":0,"maxspeed":5,"bufferlen":null,"json_config":"{\"frame\": \"\", \"smooth\": \"\"}","calibration":[],"multi_calibration":[]},{"sensortype":"Dedicated","sensortypeid":1,"sensorinterpret":"Special sensor","sensorinterpretid":9,"info":"weight","sensorfieldname":"IN1","fieldname":"IN1","sensorminvalue":"1.000","sensormaxvalue":"30000.000","minvalue":"1.000","maxvalue":"30000.000","semanticid":9,"typeid":1,"sensordefaultdesc":"weight","defaultdescription":"weight","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"kg","defaultmeasureunit":"kg","checkbox":0,"history":1,"formula":null,"filter":0,"moto":0,"tags":null,"tooltips":3,"maxspeed":5,"bufferlen":null,"json_config":null,"calibration":[{"value1":"0","value2":"4179"},{"value1":"1984","value2":"5572"},{"value1":"2123","value2":"6965"},{"value1":"2266","value2":"8358"},{"value1":"2507","value2":"25074"},{"value1":"2532","value2":"11144"},{"value1":"2673","value2":"22288"},{"value1":"2725","value2":"16716"}],"multi_calibration":[]},{"sensortype":"Dip","sensortypeid":3,"sensorinterpret":"Ignition sensor","sensorinterpretid":1,"info":"Ignition sensor","sensorfieldname":"Roaming_Ignition1125","fieldname":"Roaming_Ignition1125","sensorminvalue":"1.000","sensormaxvalue":"1.000","minvalue":"1.000","maxvalue":"1.000","semanticid":1,"typeid":3,"sensordefaultdesc":"Ignition sensor","defaultdescription":"Ignition sensor","defaultactivemodename":"On","activemodename":"On","passivemodename":"Off","defaultpassivemodename":"Off","measureunit":"-","defaultmeasureunit":"-","checkbox":1,"history":1,"formula":null,"filter":1,"moto":1,"tags":null,"tooltips":3,"maxspeed":null,"bufferlen":null,"json_config":null,"calibration":[],"multi_calibration":[]},{"sensortype":"Selector","sensortypeid":8,"sensorinterpret":"GPS antenna sensor","sensorinterpretid":5,"info":"GSM Signal","sensorfieldname":"GSMVal","fieldname":"GSMVal","sensorminvalue":null,"sensormaxvalue":null,"minvalue":null,"maxvalue":null,"semanticid":5,"typeid":8,"sensordefaultdesc":"Value in range 1-5","defaultdescription":"Value in range 1-5","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"","defaultmeasureunit":"","checkbox":0,"history":1,"formula":null,"filter":1,"moto":0,"tags":null,"tooltips":3,"maxspeed":null,"bufferlen":null,"json_config":null,"calibration":[],"multi_calibration":[{"min":null,"max":"1","semantic":"Very low"},{"min":null,"max":"2","semantic":"Low"},{"min":null,"max":"3","semantic":"Medium"},{"min":null,"max":"4","semantic":"Normal"},{"min":null,"max":"5","semantic":"High"}]},{"sensortype":"Dedicated","sensortypeid":1,"sensorinterpret":"Temperature sensor","sensorinterpretid":7,"info":"Temperature","sensorfieldname":"BLE_T1","fieldname":"BLE_T1","sensorminvalue":"-50.000","sensormaxvalue":"50.000","minvalue":"-50.000","maxvalue":"50.000","semanticid":7,"typeid":1,"sensordefaultdesc":"Temperature","defaultdescription":"Temperature","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"C","defaultmeasureunit":"C","checkbox":0,"history":1,"formula":null,"filter":1,"moto":0,"tags":null,"tooltips":3,"maxspeed":5,"bufferlen":null,"json_config":"{\"frame\": \"6\", \"smooth\": \"\", \"max_treshold\": 0, \"min_treshold\": 0}","calibration":[],"multi_calibration":[]},{"sensortype":"Dedicated","sensortypeid":1,"sensorinterpret":"Humidity sensor","sensorinterpretid":24,"info":"Humidity","sensorfieldname":"BLE_Humidity1","fieldname":"BLE_Humidity1","sensorminvalue":"1.000","sensormaxvalue":"100.000","minvalue":"1.000","maxvalue":"100.000","semanticid":24,"typeid":1,"sensordefaultdesc":"Humidity","defaultdescription":"Humidity","defaultactivemodename":null,"activemodename":null,"passivemodename":null,"defaultpassivemodename":null,"measureunit":"%","defaultmeasureunit":"%","checkbox":0,"history":1,"formula":null,"filter":1,"moto":0,"tags":null,"tooltips":3,"maxspeed":5,"bufferlen":null,"json_config":"{\"frame\": \"6\", \"smooth\": \"\"}","calibration":[],"multi_calibration":[]}]'''

ADD_SENSORS = 1          # 0 = create vehicles without sensors


def sensor_fields():
    names = []
    for s in json.loads(SENSOR_TEMPLATE):
        names.append(str(s.get("fieldname") or ""))
    return names


def panel_sensors(agent_id, account):
    res = panel_call("app/vehicles.php", {"cmd": "sensors", "agent_id": agent_id,
                                          "page": 1, "start": 0, "limit": 200}, account)
    body = res["body"]
    if not res["ok"] or not isinstance(body, dict) or "data" not in body:
        return {"ok": False, "fields": [],
                "why": "HTTP " + str(res["status"]) + " " + str(res["text"])[:160]}
    fields = []
    for r in body.get("data") or []:
        if isinstance(r, dict):
            fields.append(str(r.get("fieldname") or ""))
    return {"ok": True, "fields": fields, "why": ""}


def ensure_sensors(agent_id, account):
    """Import the template unless the vehicle already has sensors. A vehicle
    with SOME sensors is left alone: importing on top would duplicate them."""
    if not ADD_SENSORS:
        return {"ok": True, "msg": "sensors: switched off (ADD_SENSORS = 0)"}
    if not agent_id:
        return {"ok": False, "msg": "sensors: no agent_id to attach them to"}
    want = sensor_fields()
    have = panel_sensors(agent_id, account)
    if not have["ok"]:
        return {"ok": False, "msg": "sensors: could not read the vehicle's sensors -- " + have["why"]}
    missing = [f for f in want if f not in have["fields"]]
    if not missing:
        return {"ok": True, "msg": "sensors: all " + str(len(want)) + " already there"}
    if have["fields"]:
        return {"ok": False, "msg": ("sensors: the vehicle already has " + str(len(have["fields"])) +
                                     " sensor(s) but is missing " + ", ".join(missing) +
                                     " -- not importing on top; add them in the panel")}

    boundary = "----vehicleUploadSensors7f3a9c"
    parts = []
    parts.append("--" + boundary + "\r\nContent-Disposition: form-data; name=\"cmd\"\r\n\r\nimportSensors\r\n")
    parts.append("--" + boundary + "\r\nContent-Disposition: form-data; name=\"agent_id\"\r\n\r\n" +
                 str(agent_id) + "\r\n")
    parts.append("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; "
                 "filename=\"sensors_template.json\"\r\nContent-Type: application/json\r\n\r\n" +
                 SENSOR_TEMPLATE + "\r\n")
    parts.append("--" + boundary + "--\r\n")
    res = panel_call("app/vehicles.php", "".join(parts), account, "POST",
                     {"Content-Type": "multipart/form-data; boundary=" + boundary})

    after = panel_sensors(agent_id, account)
    got = [f for f in want if f in after["fields"]]
    if len(got) == len(want):
        return {"ok": True, "msg": "sensors: " + str(len(want)) + " added"}
    said = ("HTTP " + str(res["status"]) + " " + str(res["text"])[:200]) if not res["ok"] else str(res["body"])[:200]
    return {"ok": False, "msg": ("sensors: import did not take (" + str(len(got)) + "/" + str(len(want)) +
                                 " present) -- panel said: " + said)}


def pilot_has(imei, account):
    if UPLOAD_VIA == "panel":
        return panel_find(imei, account)
    return exists_on_pilot(imei, account)


def lookup(cmd, account):
    """safe_exec forbids tuple unpacking, so every helper here answers with a
    dict instead of a pair."""
    key = cmd + "@" + str(account)
    if key not in LOOKUPS:
        res = backend(cmd, {}, account)
        good = frappe.utils.cint(res.get("code")) == 0
        LOOKUPS[key] = {"rows": (res.get("data") or []) if good else [],
                        "err": "" if good else str(res.get("msg") or "")}
    return LOOKUPS[key]


def row_id(row):
    """The docs say `identifier`, the live server says `id`."""
    for k in ("id", "identifier"):
        if row.get(k) is not None:
            return frappe.utils.cint(row.get(k))
    return 0


def norm(text):
    return str(text or "").upper().replace(" ", "").replace("-", "").replace("_", "")


def resolve_account_id(email, account):
    got = lookup("accountslist", account)
    if got["err"]:
        return {"id": 0, "why": "accountslist failed: " + got["err"], "org_name": ""}
    want = str(email or "").strip().lower()
    if not want:
        return {"id": 0, "why": "the vehicle has no platform account email", "org_name": ""}
    for r in got["rows"]:
        if str(r.get("bill_email") or "").strip().lower() == want:
            # org_name is also what `folder` holds -- verified on a Pilot vehicle
            # export: folder == org_name on every row.
            return {"id": row_id(r), "why": "",
                    "org_name": str(r.get("org_name") or "").strip()}
    return {"id": 0, "why": "no Pilot account has bill_email " + want +
            " (" + str(len(got["rows"])) + " accounts read)", "org_name": ""}


def resolve_model_id(devices_type, account):
    want = str(devices_type or "").strip()
    if not want:
        return {"id": 0, "why": "the vehicle has no Device Type"}
    if want in MODEL_OVERRIDES and frappe.utils.cint(MODEL_OVERRIDES[want]):
        return {"id": frappe.utils.cint(MODEL_OVERRIDES[want]), "why": ""}
    got = lookup("vehmodellist", account)
    if got["err"]:
        return {"id": 0, "why": "vehmodellist failed: " + got["err"]}
    n = norm(want)
    for r in got["rows"]:
        if n and n in norm(r.get("name")):
            return {"id": row_id(r), "why": ""}
    return {"id": 0, "why": "no Pilot model matches device type " + want +
            " -- put its id in MODEL_OVERRIDES at the top of this script"}


def resolve_type_id(veh, account):
    want = str(veh.get("type_new") or veh.get("type") or "").strip()
    if not want:
        return {"id": 0, "why": "the vehicle has no Type"}
    if want in TYPE_ALIASES:
        want = TYPE_ALIASES[want]
    got = lookup("vehtypelist", account)
    if got["err"]:
        return {"id": 0, "why": "vehtypelist failed: " + got["err"]}
    n = norm(want)
    for r in got["rows"]:
        if n and n == norm(r.get("name")):
            return {"id": row_id(r), "why": ""}
    for r in got["rows"]:
        if n and n in norm(r.get("name")):
            return {"id": row_id(r), "why": ""}
    return {"id": 0, "why": "no Pilot vehicle type matches " + want +
            " -- add it to TYPE_ALIASES at the top of this script"}


def resolve_folder(account_id, org_name, account):
    """The account's root folder, read from cmd=getfolders. It is usually named
    after the account's org_name but not always, so it is read, not assumed."""
    if not account_id:
        return {"id": "", "name": "", "why": "no account to read the folders of"}
    res = backend("getfolders", {"account_id": account_id}, account)
    if frappe.utils.cint(res.get("code")) != 0:
        return {"id": "", "name": "",
                "why": "getfolders failed: " + str(res.get("msg") or "")[:120]}
    roots = []
    for k in res.keys():
        f = res[k]
        if isinstance(f, dict) and "parent" in f and f.get("parent") is None:
            roots.append({"id": str(k), "name": str(f.get("name") or ""), "why": ""})
    for r in roots:
        if r["name"].strip() == str(org_name).strip():
            return r
    if roots:
        return roots[0]
    return {"id": "", "name": "",
            "why": "account " + str(account_id) + " has no root folder on Pilot"}


def resolve_msisdn(imei):
    """sim_serial on the vehicle is an ICCID, not a phone number. The Fleet
    Audit table is where the real MSISDN lives, keyed by IMEI."""
    if not frappe.db.exists("DocType", AUDIT_DT):
        return {"value": "", "why": "no Fleet Audit table on this site, so no MSISDN"}
    rows = frappe.db.sql("select sim_msisdn from `tab" + AUDIT_DT + "` "
                         "where imei = %(i)s and ifnull(sim_msisdn,'') <> '' limit 1",
                         {"i": imei}, as_dict=True)
    if rows:
        return {"value": str(rows[0].get("sim_msisdn") or "").strip(), "why": ""}
    return {"value": "", "why": "Fleet Audit has no SIM number for IMEI " + str(imei)}


# ---------------------------------------------------------------- the ERP side
def vehicle_doc(name):
    rows = frappe.db.sql(
        "select name, customer, customer2, is_sec_customer, device_serial, sim_serial, "
        "devices_type, license_plate, e_license_plate, plate_num, naming_type, "
        "`type`, type_new, licensse_type, device_statues, server_type, "
        "email_pilot, email_pilot2, im_platform, ch_pilot_wsl, ch_trakzee, "
        "installation_date, subscription_expiry_date, deletion_date "
        "from `tab" + VEH_DT + "` where name = %(n)s limit 1",
        {"n": name}, as_dict=True)
    return rows[0] if rows else None


def plate_of(v):
    for f in ("license_plate", "e_license_plate", "plate_num"):
        value = str(v.get(f) or "").strip()
        if value:
            return value
    return str(v.get("name") or "")


def install_tickets(veh):
    """xticket.license_plate is a Link to Customer Vehicle, so it holds the
    vehicle's NAME, not a plate string."""
    return frappe.db.sql(
        "select name, status, server_status from `tab" + TICKET_DT + "` "
        "where license_plate = %(v)s and issue_type = %(t)s order by creation desc",
        {"v": veh, "t": INSTALL_TYPE}, as_dict=True)


def stamp_tickets(veh, status_value, note):
    found = install_tickets(veh)
    if not STAMP_ALL:
        found = found[:1]
    touched = []
    for t in found:
        frappe.db.set_value(TICKET_DT, t.name, "server_status", status_value,
                            update_modified=True)
        touched.append(t.name)
        frappe.get_doc({
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": TICKET_DT,
            "reference_name": t.name,
            "content": (str(frappe.session.user) + " -- server status set to " +
                        str(status_value) + " -- " + str(note)[:220]),
        }).insert(ignore_permissions=True)
    return touched


# ---------------------------------------------------------------- the payload
def build_payload(v, key):
    """Everything cmd=vehadd needs, resolved. `gaps` lists what could not be
    worked out -- a payload with gaps is never sent."""
    spec = TARGETS[key]
    account = spec["account"]
    imei = str(v.get("device_serial") or "").strip()
    email = str(v.get(spec["email_field"]) or "").strip()
    gaps = []

    payload = {"imei": imei, "name": plate_of(v)}
    if not imei:
        gaps.append("imei (Device Serial)")
    if not payload["name"]:
        gaps.append("name (plate)")

    if key == "im":
        return im_build(v, payload, gaps, email)

    got = resolve_account_id(email, account)
    payload["account_id"] = got["id"]
    if not got["id"]:
        gaps.append("account_id -- " + got["why"])

    if UPLOAD_VIA == "panel":
        # Exactly the fields of the panel's create form, under its own names.
        sim = resolve_msisdn(imei)
        model = resolve_panel_model(v.get("devices_type"), account)
        panel = {"vehiclenumber": payload["name"], "uniqid": imei,
                 "msisdn": sim["value"] or "0", "configuration": model["name"],
                 "account_id": got["id"], "account_name": got.get("org_name") or ""}
        if not model["name"]:
            gaps.append("configuration -- " + model["why"])
        if got["id"] and not panel["account_name"]:
            gaps.append("account_name -- the Pilot account has no org_name")
        if not sim["value"]:
            panel["_msisdn_note"] = sim["why"] + " -- sending 0, Pilot's no-SIM value"
        panel["_account_email"] = email
        panel["_device_type"] = str(v.get("devices_type") or "")
        if ADD_SENSORS:
            panel["_sensors"] = ", ".join(sensor_fields()) + " (from the template, after create)"
        return {"payload": panel, "gaps": gaps}

    # See FOLDER_MODE at the top.
    if FOLDER_MODE in ("id", "name"):
        fol = resolve_folder(got["id"], got.get("org_name") or "", account)
        payload["folder"] = fol["id"] if FOLDER_MODE == "id" else fol["name"]
        payload["_folder_name"] = fol["name"]
        if not payload["folder"]:
            gaps.append("folder -- " + fol["why"])
    elif FOLDER_MODE == "empty":
        payload["folder"] = ""

    got = resolve_model_id(v.get("devices_type"), account)
    payload["vehmodel"] = got["id"]
    if not got["id"]:
        gaps.append("vehmodel -- " + got["why"])

    got = resolve_type_id(v, account)
    payload["type"] = got["id"]
    if not got["id"]:
        gaps.append("type -- " + got["why"])

    # These three go up as EMPTY rather than blocking. This fleet's vehicles have
    # no SIM on the ERP side (sim_serial is an ICCID, not a number, and most have
    # no Fleet Audit row at all), and the docs mark msisdn2 Required while the
    # worked example omits it -- so sending them empty satisfies both readings.
    # A missing MSISDN is reported for information, it is not fatal.
    # Pilot's own convention for "this vehicle has no SIM" is the STRING "0", not
    # an empty value -- every SIM-less row in a Pilot vehicle export carries
    # msisdn "0" with iccid null. An empty string is one of the ways vehadd
    # answers 400.
    got = resolve_msisdn(imei)
    payload["msisdn"] = got["value"] or "0"
    payload["msisdn2"] = "0"
    if not got["value"]:
        payload["_msisdn_note"] = got["why"] + " -- sending 0, Pilot's no-SIM value"

    node = pilot_settings(account)["node"]
    payload["node"] = node
    if not node:
        gaps.append("node -- this estate's node varies per account and is not set. "
                    "Put it in app_apis (Pilot Admin Node) before uploading; a wrong "
                    "node cannot be undone.")
    payload["_account_email"] = email
    payload["_device_type"] = str(v.get("devices_type") or "")
    payload["_vehicle_type"] = str(v.get("type_new") or v.get("type") or "")
    return {"payload": payload, "gaps": gaps}


# Pilot answers `code 0` even when it is REFUSING. A live vehadd came back
# code 0 with "You do not have access for this folder" and created nothing --
# and this script called that a success. So code 0 is necessary but not
# sufficient: a message that reads like a refusal overrides it, and a real
# upload is confirmed by reading the vehicle back (see send_one).
REFUSALS = ("do not have access", "don't have access", "no access", "not allowed",
            "denied", "permission", "not permitted", "forbidden", "unauthor",
            "does not exist", "doesn't exist", "not exist", "invalid", "wrong",
            "required", "missing", "cannot", "can not", "failed", "failure")


def verdict_from(answer):
    if not isinstance(answer, dict):
        return {"verdict": "failure", "msg": str(answer)[:180]}
    code = frappe.utils.cint(answer.get("code"))
    msg = str(answer.get("msg") or answer.get("message") or "")
    low = msg.lower()
    if code == 0:
        for hint in REFUSALS:
            if hint in low:
                return {"verdict": "failure",
                        "msg": "Pilot answered code 0 but refused: " + msg}
        return {"verdict": "uploaded", "msg": msg or "Pilot accepted it"}
    gone = "not exist" in low or "n't exist" in low or "no such" in low
    if ("exist" in low or "duplicate" in low or "already" in low) and not gone:
        return {"verdict": "duplicate", "msg": msg}
    if code <= -400:
        return {"verdict": "error", "msg": msg or ("transport code " + str(code))}
    return {"verdict": "failure", "msg": msg or ("code " + str(code))}


def panel_verdict(res):
    """What the panel's create said. Its own form reads the failures as:
    'Limit create reached' -> this login's create quota is used up; anything
    else -> 'This device id exist in the system'."""
    body = res.get("body")
    if res.get("ok"):
        if isinstance(body, dict) and body.get("agent_id"):
            return {"verdict": "uploaded",
                    "msg": "the panel created agent_id " + str(body.get("agent_id"))}
        return {"verdict": "uploaded", "msg": "the panel answered: " + str(body)[:200]}
    text = str(res.get("text") or "")
    low = text.lower()
    status = frappe.utils.cint(res.get("status"))
    msg = ("HTTP " + str(status) + " -- " if status else "") + text[:300]
    if "limit" in low:
        return {"verdict": "failure",
                "msg": msg + " (this admin login's create limit is used up -- raise it in the panel)"}
    if "exist" in low or "dublicat" in low or "duplicat" in low:
        return {"verdict": "duplicate", "msg": msg}
    if status in (0, 502, 503, 504):
        return {"verdict": "error", "msg": msg}
    return {"verdict": "failure", "msg": msg}


# ---------------------------------------------------------------- IM (web app)
def strip_tags(html):
    """Readable text of an HTML page (no `re` in the sandbox)."""
    out = []
    inside = False
    for ch in str(html or ""):
        if ch == "<":
            inside = True
            out.append(" ")
        elif ch == ">":
            inside = False
        elif not inside:
            out.append(ch)
    return " ".join("".join(out).split())


def im_post(path, data, referer=None, timeout=60):
    headers = {"Referer": referer or (IM_WEB + "/jsp/index.html")}
    try:
        res = frappe.call(IM_HTTP, jar=IM_JAR, url=IM_WEB + path, method="POST", data=data,
                          headers=headers, timeout=timeout)
    except Exception as e:
        return {"ok": False, "status": 0, "text": "", "json": None, "error": str(e)[:300]}
    if res.get("error"):
        res["ok"] = False
    return res


def im_login():
    """Sign in once per request; the session cookie stays in the IM_JAR jar."""
    if "im-login" in LOOKUPS:
        return LOOKUPS["im-login"]
    got = {"ok": False, "why": "", "user": ""}
    doc = frappe.get_doc(SETTINGS)
    user = str(doc.get("im_username") or "").strip()
    pwd = doc.get_password("im_password", raise_exception=False) if doc.get("im_password") else ""
    got["user"] = user
    if not user or not pwd:
        got["why"] = "IM Username / Password are not set in app_apis > IM Connection."
        LOOKUPS["im-login"] = got
        return got
    res = None
    try:
        enc = frappe.call(IM_RSA, public_key=IM_PUBKEY, text=pwd)
        frappe.call(IM_CLEAR, jar=IM_JAR)
        res = im_post("/UserLogin", {"username": user, "password": enc, "projectName": "VTS",
                                     "projectentity": "EMC", "webclient": "false"})
    except Exception as e:
        got["why"] = "IM sign-in failed -- " + str(e)[:200].replace(str(pwd), "***")
        if "ermission" in str(e) or not str(e).strip():
            got["why"] = got["why"] + " -- IM calls need a System Manager login (app_apis.core.http is limited to that role)"
        if "No module named" in str(e):
            got["why"] = got["why"] + " -- this server's app_apis is too old (it has no core module); update the app"
    if res is not None:
        landed = str(res.get("url") or "")
        if res.get("error"):
            got["why"] = "IM sign-in failed -- " + str(res.get("error"))[:200]
        elif "/jsp/index" in landed:
            got["ok"] = True
        else:
            got["why"] = ("IM did not accept the sign-in (landed on " + landed + "): " +
                          strip_tags(res.get("text"))[:160])
    LOOKUPS["im-login"] = got
    return got


def im_xml(method, params):
    data = {"javaclassname": IM_XML_CLASS, "javaclassmethodname": method}
    for k in params.keys():
        data[k] = params[k]
    res = im_post("/GenerateXML", data)
    return {"ok": bool(res.get("ok")), "status": res.get("status"),
            "text": str(res.get("text") or res.get("json") or "")}


def im_json(method, params, cls=None):
    data = {"javaclassname": cls or IM_JSON_CLASS, "javaclassmethodname": method}
    for k in params.keys():
        data[k] = params[k]
    res = im_post("/GenerateJSON?method=" + method, data)
    if res.get("json") is not None:
        return {"ok": True, "data": res.get("json"), "text": ""}
    text = str(res.get("text") or "")
    data = None
    try:
        data = json.loads(text)
    except Exception:
        data = None
    return {"ok": bool(res.get("ok")), "data": data, "text": text, "status": res.get("status")}


def im_has(imei):
    """Is this IMEI on IM?  The web app's own duplicate-IMEI check."""
    log = im_login()
    if not log["ok"]:
        return {"state": "unknown", "row": {}, "detail": log["why"]}
    r = im_xml("CheckDuplicateWeaponID", {"Imeino": str(imei), "sMode": "insert", "sGpsDeviceId": ""})
    text = r["text"]
    if "<status>Match</status>" in text:
        return {"state": "yes", "row": {}, "detail": "IM's duplicate-IMEI check finds it"}
    if "<status>" in text:
        return {"state": "no", "row": {}, "detail": "IM's duplicate-IMEI check does not find it"}
    return {"state": "unknown", "row": {},
            "detail": "IM's duplicate-IMEI check gave no answer -- HTTP " + str(r["status"]) + " " + text[:160]}


def im_name_taken(name, company):
    r = im_xml("getDuplicateVehicle", {"vehiclenumber": name, "companyId": company,
                                       "sMode": "insert", "sVehicleId": ""})
    return "<status>Match</status>" in r["text"]


def im_rows(method, params):
    """The rows of one IM dropdown. IM answers `{root : [[{"values":"DATA"}],[row, ...]]}`
    -- `root` is not quoted, so it is fixed before json reads it. [] when empty."""
    got = im_json(method, dict(params, applicationid=IM_APPLICATION_ID))
    text = str(got.get("text") or "").strip()
    data = got.get("data")
    if not isinstance(data, dict):
        try:
            data = json.loads(text.replace("{root :", '{"root":', 1))
        except Exception:
            data = None
    root = (data or {}).get("root") or []
    if len(root) < 2 or not isinstance(root[1], list):
        return []
    return [r for r in root[1] if isinstance(r, dict)]


def im_companies():
    """[{id, name, reseller}] of the companies under every reseller in IM_RESELLERS."""
    if "im-companies" not in LOOKUPS:
        found = []
        for rid in IM_RESELLERS:
            for r in im_rows("getCompanyDropdown", {"object_reseller_entity_id": rid}):
                if r.get("company_id"):
                    found.append({"id": str(r.get("company_id")), "name": str(r.get("short_name") or ""),
                                  "reseller": rid})
        LOOKUPS["im-companies"] = found
    return LOOKUPS["im-companies"]


def im_branches(company):
    key = "im-branches-" + str(company)
    if key not in LOOKUPS:
        rows = im_rows("getBranchDropdown", {"object_company_id": company})
        LOOKUPS[key] = [{"id": str(r.get("location_id") or ""), "name": str(r.get("location_name") or "")}
                        for r in rows if r.get("location_id")]
    return LOOKUPS[key]


def alnum(text):
    return "".join([c for c in str(text or "") if c.isalnum()]).lower()


def im_first_branch(company):
    """The first branch in IM's own dropdown (it is mostly also the lowest id).
    Returns {"id", "note"} -- the note is set when the first row is not the lowest id."""
    branches = im_branches(company)
    if not branches:
        return {"id": "", "note": "company " + str(company) + " has no branch on IM"}
    first = branches[0]["id"]
    ids = [frappe.utils.cint(b["id"]) for b in branches]
    note = ""
    if frappe.utils.cint(first) != min(ids):
        note = "first dropdown branch " + first + " is not the lowest id " + str(min(ids))
    return {"id": first, "note": note}


def im_company_login(company):
    """The username (= the email the customer is known by) of one IM company, read off its edit page.
    IM only serves that page after the Company overview has been opened in the same session."""
    if "im-overview" not in LOOKUPS:
        try:
            frappe.call(IM_HTTP, jar=IM_JAR, url=IM_WEB + "/jsp/Overview.jsp?screenid=2286&level=1&popup=false",
                        method="GET", headers={"Referer": IM_WEB + "/jsp/index.html"}, timeout=60)
        except Exception:
            pass
        LOOKUPS["im-overview"] = 1
    try:
        res = frappe.call(IM_HTTP, jar=IM_JAR, method="GET", timeout=60,
                          url=IM_WEB + "/jsp/DetailScreen.jsp?entityid=" + str(company) +
                              "&screenid=2286&mode=update&level=1&popup=false&view=L&overviewscreenid=2286",
                          headers={"Referer": IM_WEB + "/jsp/index.html"})
    except Exception:
        return ""
    text = str(res.get("text") or "")
    at = text.find('name="username"')
    if at < 0:
        return ""
    end = text.find(">", at)
    tag = text[at:end if end > at else at + 600]
    v = tag.find('value="')
    if v < 0:
        return ""
    return tag[v + 7:].split('"')[0].strip().lower()


def im_cache_fields():
    """Does Customer have the im_company_id / im_reseller_id / im_branch_id cache fields? (The
    main server may not.)"""
    meta = frappe.get_meta("Customer")
    return bool(meta.has_field("im_company_id") and meta.has_field("im_reseller_id") and meta.has_field("im_branch_id"))


def im_customers_with(email):
    """Names of the ERP Customers whose IM Platform email is `email` (case-insensitive)."""
    return [r.name for r in frappe.db.sql(
        "select name from `tabCustomer` where lower(trim(email_im_platform)) = %(e)s",
        {"e": str(email or "").strip().lower()}, as_dict=True)]


def im_sync(want="", limit=60, force=False):
    """Fill Customer.im_company_id / im_reseller_id / im_branch_id from IM.
    IM's company dropdown has no email, so each company's username is read off its edit page
    and matched to Customer.email_im_platform. Companies that already map to a Customer are
    skipped unless `force`. Stops after `limit` page reads (continue by calling again), or as
    soon as the email `want` is found. Returns {"found": {...} or None, ...counts}."""
    stats = {"companies": 0, "read": 0, "matched": 0, "no_customer": [], "no_login": [], "remaining": 0,
             "found": None}
    log = im_login()
    if not log["ok"]:
        stats["error"] = log["why"]
        return stats
    known = {}
    if not force:
        if im_cache_fields():
            for r in frappe.db.sql("select im_company_id from `tabCustomer` where ifnull(im_company_id, '') != ''",
                                   as_dict=True):
                known[str(r.im_company_id)] = 1
    want = str(want or "").strip().lower()
    todo = []
    for c in im_companies():
        stats["companies"] = stats["companies"] + 1
        if c["id"] not in known:
            todo.append(c)
    for c in todo:
        if stats["read"] >= limit:
            stats["remaining"] = stats["remaining"] + 1
            continue
        if stats["found"] and want:
            stats["remaining"] = stats["remaining"] + 1
            continue
        stats["read"] = stats["read"] + 1
        login = im_company_login(c["id"])
        if not login:
            stats["no_login"].append(c["id"])
            continue
        fb = im_first_branch(c["id"])
        branch = fb["id"]
        note = fb["note"]
        hit = {"company": c["id"], "reseller": c["reseller"], "branch": branch, "name": c["name"],
               "login": login, "branch_note": note}
        names = im_customers_with(login)
        for n in (names if im_cache_fields() else []):
            frappe.db.set_value("Customer", n, {"im_company_id": c["id"], "im_reseller_id": c["reseller"],
                                                "im_branch_id": branch})
            stats["matched"] = stats["matched"] + 1
        if not names:
            stats["no_customer"].append(login)
        if want and login == want:
            stats["found"] = hit
    return stats


def im_account(email, customer="", customer_doc=""):
    """Which IM company + branch owns this vehicle. The key is the vehicle's im_platform
    email -- never a name:
      1. IM_ACCOUNTS (a hand-set override keyed by that email)
      2. the Customer whose `email_im_platform` is that email and that already has
         im_company_id / im_reseller_id / im_branch_id (cached from IM by im_sync)
      3. otherwise refresh the cache once from IM (looking for that email) and use the hit
      4. otherwise refuse, saying whether the email is missing from IM or from ERP
    The branch is the first one in IM's branch dropdown.
    Returns {"company", "branch", "name", "how", "why", "reseller"}; `why` is set (and
    company/branch empty) when it could not be worked out."""
    out = {"company": "", "branch": "", "name": "", "how": "", "why": "", "reseller": ""}
    want = str(email or "").strip().lower()
    for k in IM_ACCOUNTS.keys():
        if str(k).strip().lower() == want:
            acc = IM_ACCOUNTS[k]
            out.update({"company": str(acc.get("company") or ""), "branch": str(acc.get("branch") or ""),
                        "how": "IM_ACCOUNTS override", "reseller": str(acc.get("reseller") or "")})
            return out
    if not want:
        out["why"] = "the vehicle has no im_platform email"
        return out

    for attempt in (1, 2):
        rows = []
        if im_cache_fields():
            rows = frappe.db.sql(
                "select name, im_company_id, im_reseller_id, im_branch_id from `tabCustomer` "
                "where lower(trim(email_im_platform)) = %(e)s and ifnull(im_company_id, '') != '' "
                "order by name", {"e": want}, as_dict=True)
        if rows:
            r = rows[0]
            out["company"] = str(r.im_company_id).strip()
            out["reseller"] = str(r.im_reseller_id or "").strip()
            out["branch"] = str(r.im_branch_id or "").strip()
            out["how"] = "email " + want + " -> IM company " + out["company"] + " (cached on Customer " + str(r.name) + ")"
            if not out["branch"]:
                out["branch"] = im_first_branch(out["company"])["id"]
            if not out["branch"]:
                out["why"] = "company " + out["company"] + " has no branch on IM"
            return out
        if attempt == 2:
            break
        got = im_sync(want, 400)
        if got.get("found"):
            f = got["found"]
            out.update({"company": f["company"], "reseller": f["reseller"], "branch": f["branch"],
                        "name": f["name"], "how": "email " + want + " -> IM company " + f["company"] +
                        " '" + f["name"] + "' (cache refreshed)"})
            if not im_customers_with(want):
                out["how"] = out["how"] + "; no ERP Customer has this IM Platform email"
            if not out["branch"]:
                out["why"] = "company " + out["company"] + " has no branch on IM"
            return out
        if got.get("error"):
            out["why"] = got["error"]
            return out
        if got.get("remaining"):
            out["why"] = ("refreshed 400 IM companies without finding " + want + "; " + str(got["remaining"]) +
                          " left -- run action=im_sync until remaining is 0")
            return out

    if im_customers_with(want):
        out["why"] = ("no IM company (resellers " + ", ".join(IM_RESELLERS) + ") has the username " + want +
                      " -- create it on IM or fix the email on the Customer")
    else:
        out["why"] = ("IM Platform email " + want + " is on no ERP Customer, and no IM company (resellers " +
                      ", ".join(IM_RESELLERS) + ") has it as username")
    return out


def im_model(devices_type):
    n = norm(devices_type)
    if not n:
        return ""
    for k in IM_MODELS.keys():
        if norm(k) == n:
            return str(IM_MODELS[k])
    for k in IM_MODELS.keys():
        if n.endswith(norm(k)) or norm(k).endswith(n):
            return str(IM_MODELS[k])
    # Not in IM_MODELS: ask IM for its own model list and take an exact name match.
    try:
        if "im-models" not in LOOKUPS:
            got = im_json("getGpsDeviceModel", {"applicationid": IM_APPLICATION_ID})
            data = got.get("data")
            LOOKUPS["im-models"] = (data.get("data") if isinstance(data, dict) else None) or []
        same = [r for r in LOOKUPS["im-models"] if norm(r.get("model")) == n and r.get("model_id")]
        if len(same) == 1:
            return str(same[0]["model_id"])
    except Exception:
        pass
    return ""


def im_sensors(model):
    """The sensor rows (iframedata1), the grouped calibrations (save_calib_json)
    and the vehicle profile, copied from the model's template vehicle."""
    tpl = str(IM_TEMPLATE_VEHICLE.get(str(model)) or "")
    if tpl:
        got = im_json("getDataOfCopyObject", {"sVehicleID": tpl}, IM_JSON_TZ_CLASS)
        data = got["data"]
        if isinstance(data, dict) and data.get("device_port_specification"):
            names = {}
            for p in data.get("device_port_specification") or []:
                names[str(p.get("model_port_specification_hidden_id") or "")] = str(p.get("property_name") or "")
            calibs = data.get("calibration_json") or {}
            save = {}
            by_spec = {}
            for ck in calibs.keys():
                value = calibs[ck]
                items = [value] if isinstance(value, dict) else (value or [])
                named = []
                for c in items:
                    if not isinstance(c, dict):
                        continue
                    c2 = dict(c)
                    sid = str(c2.get("selected_model_specification_id") or "")
                    c2["property_name"] = names.get(sid, "")
                    named.append(c2)
                    by_spec.setdefault(sid, []).append(c2)
                save[ck] = named[0] if isinstance(value, dict) and named else named
            rows = []
            for p in data.get("device_port_specification") or []:
                sid = str(p.get("model_port_specification_hidden_id") or "")
                row = {
                    "property_name": str(p.get("property_name") or ""),
                    "property_category": str(p.get("property_category") or ""),
                    "model_port_specification_id": sid,
                    "port_allocation": str(p.get("port_allocation") or ""),
                    "device_port_specification_id": "0",
                    "reading_type": str(p.get("reading_type") or "DIRECT"),
                    "work_hour_calculation": str(p.get("sWorkHourCalculation") or "false"),
                }
                extra = p.get("analog_config")
                if extra and not isinstance(extra, dict):
                    try:
                        extra = json.loads(extra)
                    except Exception:
                        extra = None
                if isinstance(extra, dict):
                    for k in extra.keys():
                        row[k] = extra[k]
                if sid in by_spec:
                    row["calibration_json"] = json.dumps(by_spec[sid])
                rows.append(row)
            screen = (data.get("screen_data") or [{}])[0] or {}
            return {"rows": rows, "calib": json.dumps(save) if save else "", "screen": screen,
                    "from": "IM vehicle " + tpl, "why": ""}
        return {"rows": [], "calib": "", "screen": {}, "from": "",
                "why": "could not read the sensors of template vehicle " + tpl + " -- " +
                       str(got.get("text") or "")[:160]}
    rows = IM_DEFAULT_SENSORS.get(str(model)) or []
    if rows:
        return {"rows": rows, "calib": "", "screen": {}, "from": "the built-in list", "why": ""}
    return {"rows": [], "calib": "", "screen": {}, "from": "",
            "why": "no sensor template for IM model " + str(model) + " -- add it to IM_TEMPLATE_VEHICLE"}


def im_build(v, payload, gaps, email):
    imei = payload["imei"]
    log = im_login()
    cust = ""
    if v.get("customer"):
        # IM companies are named with the Customer's Translated Name; fall back to the customer name.
        names = frappe.db.get_value("Customer", v.get("customer"), ["customer_name_in_arabic", "customer_name"], as_dict=True) or {}
        cust = str(names.get("customer_name_in_arabic") or names.get("customer_name") or v.get("customer"))
    # The settings table needs no IM sign-in; only a lookup that has to ask IM does. If it
    # could not be answered and the sign-in is why, say so.
    acc = im_account(email, cust, v.get("customer"))
    if not (acc["company"] and acc["branch"]) and not log["ok"]:
        acc["why"] = "IM sign-in failed -- " + log["why"] + (" | " + acc["why"] if acc["why"] else "")
    model = im_model(v.get("devices_type"))
    sim = resolve_msisdn(imei)
    out = {
        "object_device_name": payload["name"],
        "object_imei_no": imei,
        "object_company_id": str((acc or {}).get("company") or ""),
        "object_location_id": str((acc or {}).get("branch") or ""),
        "object_gps_device_model_id": model,
        "object_sim_card_no": "".join([c for c in str(sim["value"] or "") if c in "0123456789"]),
        "sim_provide": IM_SIM_PROVIDER,
        "_account_email": email,
        "_im_company": acc["how"],
        "_reseller": acc.get("reseller") or "",
        "_device_type": str(v.get("devices_type") or ""),
        "_sensors": ("copied from IM vehicle " + str(IM_TEMPLATE_VEHICLE.get(model)))
                    if IM_TEMPLATE_VEHICLE.get(model) else
                    (str(len(IM_DEFAULT_SENSORS.get(model) or [])) + " from the built-in list"),
    }
    if not sim["value"]:
        out["_msisdn_note"] = sim["why"] + " -- sent without a SIM number"
    if imei and not imei.isdigit():
        gaps.append("imei -- IM takes digits only, got " + imei)
    if not email:
        gaps.append("im_platform (the vehicle's IM account email)")
    if not (acc["company"] and acc["branch"]):
        gaps.append("company / branch -- " + acc["why"])
    if not model:
        gaps.append("device model -- no IM model for Device Type " + str(v.get("devices_type") or "(empty)") +
                    "; add it in app_apis settings > IM > IM Device Models")
    elif not IM_TEMPLATE_VEHICLE.get(model) and not IM_DEFAULT_SENSORS.get(model):
        gaps.append("sensors -- no template for IM model " + model + "; add it to IM_TEMPLATE_VEHICLE")
    return {"payload": out, "gaps": gaps}


def im_upload(row, payload):
    """The Add Vehicle form's Save, then a read-back of the IMEI."""
    log = im_login()
    if not log["ok"]:
        row["verdict"] = "error"
        row["result"] = "did not send: " + log["why"]
        return row
    name = payload["object_device_name"]
    company = payload["object_company_id"]
    branch = payload["object_location_id"]
    model = payload["object_gps_device_model_id"]

    if im_name_taken(name, company):
        row["verdict"] = "duplicate"
        row["result"] = "IM already has a vehicle named " + name + " in company " + company + "; not sent"
        row["headline"] = "Name already used on IM"
        return row

    sensors = im_sensors(model)
    if not sensors["rows"]:
        row["verdict"] = "failure"
        row["result"] = "did not send: " + sensors["why"]
        return row

    info = im_json("getModelPortName", {"gps_device_model_id": model})
    flat = str(info.get("text") or info.get("data") or "").replace(" ", "").lower()
    obd = '"obd":"yes"' in flat or '"can":"yes"' in flat

    screen = sensors["screen"] or {}
    form = dict(IM_FORM_DEFAULTS)
    form["applicationid"] = IM_APPLICATION_ID
    form["object_admin_entity_id"] = IM_ADMIN_ID
    form["object_reseller_entity_id"] = payload.get("_reseller") or IM_RESELLER_ID
    form["object_company_id"] = company
    form["object_location_id"] = branch
    form["gps_device_company_id"] = company
    form["sim_company_id"] = company
    form["gps_device_location_id"] = branch
    form["sim_location_id"] = branch
    form["object_device_name"] = name
    form["vehicle_name"] = name
    form["vehicle_no"] = name
    form["object_gps_device_model_id"] = model
    form["object_imei_no"] = payload["object_imei_no"]
    form["object_sim_card_no"] = payload["object_sim_card_no"]
    form["sim_provide"] = payload["sim_provide"]
    form["Is_obd_device"] = "true" if obd else "false"
    form["vehicle_type"] = str(screen.get("vehicle_type") or IM_PROFILE["vehicle_type"])
    form["object_brand"] = str(screen.get("vehicle_brand_id") or IM_PROFILE["object_brand"])
    form["vehicle_model"] = str(screen.get("vehicle_model_id") or IM_PROFILE["vehicle_model"])
    form["vehicle_model_id"] = form["vehicle_model"]
    form["iframedata1"] = json.dumps(sensors["rows"])
    form["save_calib_json"] = sensors["calib"]
    form["user"] = log["user"]

    # IM only serves a form to a session that opened it the way the browser does:
    # the vehicle overview first, then the Add Vehicle page. Do the same before posting.
    for prime in ("/jsp/Overview.jsp?screenid=2253&level=1&popup=false", IM_DETAIL_REFERER.replace(IM_WEB, "")):
        try:
            frappe.call(IM_HTTP, jar=IM_JAR, url=IM_WEB + prime, method="GET",
                        headers={"Referer": IM_WEB + "/jsp/index.html"}, timeout=60)
        except Exception:
            pass

    res = im_post("/jsp/ProcessDetails.jsp", form, IM_DETAIL_REFERER, 120)
    raw = str(res.get("text") or "")
    said = strip_tags(raw)[:300] or str(res.get("error") or "")
    row["raw_reply"] = ("url " + str(res.get("url")) + "; type " + str(res.get("content_type")) +
                        "; " + str(len(raw)) + " chars; redirects " + str(len(res.get("history") or [])) +
                        "; body: " + raw.strip()[:600])
    shown = {}
    for k in form.keys():
        if k not in IM_FORM_DEFAULTS:
            shown[k] = form[k] if k not in ("iframedata1", "save_calib_json") else (str(len(form[k])) + " chars")
    row["sent"] = shown
    row["sensors"] = str(len(sensors["rows"])) + " sensors from " + sensors["from"]
    row["platform_said"] = "HTTP " + str(res.get("status")) + " -- " + (said or row["raw_reply"])

    after = im_has(payload["object_imei_no"])
    row["confirmed"] = after["state"] == "yes"
    if after["state"] == "yes":
        row["ok"] = True
        row["verdict"] = "uploaded"
        row["result"] = ("created on IM -- " + after["detail"] + ". " + row["sensors"] +
                         ". IM said: " + said[:160])
        row["headline"] = "Created on IM (Trakzee)"
        row["detail"] = after["detail"]
    elif after["state"] == "no":
        row["verdict"] = "failure"
        row["result"] = "IM did not create it. IM said: " + row["platform_said"]
        row["headline"] = "Not created on IM (Trakzee)"
    else:
        row["verdict"] = "error"
        row["result"] = ("sent, but it cannot be confirmed right now -- " + after["detail"] +
                         ". Press Verify & Stamp before uploading again.")
        row["headline"] = "Sent — not confirmed yet"
        row["advice"] = "Press Verify & Stamp in a minute. Do not press Upload again before that."
    return row


def platform_has(key, imei, account):
    if key == "im":
        return im_has(imei)
    return pilot_has(imei, account)


# ---------------------------------------------------------------- delete
# Removes ONE vehicle from ONE platform. Nothing here is reversible: Pilot says
# "Deleting object ... will delete all stored data on it, and its sensors", and IM's
# bulk remove drops the vehicle and its history.
#
#   action=delete_plan  looks the vehicle up on the platform and says what it found. Deletes nothing.
#   action=delete       needs confirm=DELETE (typed by a person), looks the vehicle up AGAIN,
#                       refuses unless the IMEI on the platform is this vehicle's IMEI, deletes,
#                       then reads it back and only says "deleted" if it is really gone.
#
# Pilot: the panel's own Delete (app/vehicles.php, cmd=remove) -- the Administrator API has none.
# IM: BulkVehicleRemove (bulkObjectRemove), behind the Security PIN when the IM account has one
#     switched on.
#
# Two more actions share the same lookup / confirm / read-back flow:
#   wasl  Delete the vehicle from WASL ONLY -- Pilot's own WASL window > Delete (wasl.php
#         cmd=vehicledelete). The Pilot vehicle is kept. Needs a Pilot user session, which a
#         Server Script cannot hold, so it is done by app_apis.wasl.wasl_delete.
#   sim   Suspend the vehicle's SIM on Lebara (the `lebara` script's suspend action). Lebara
#         only, and that script is limited to System Managers.
DELETE_WORD = "DELETE"
SUSPEND_WORD = "SUSPEND"
DELETE_LABELS = {"pilot_wsl": "Pilot (WSL)", "pilot2": "Pilot 2", "im": "IM (Trakzee)",
                 "wasl": "WASL only (Pilot vehicle kept)", "sim": "SIM (suspend)"}
DELETE_TARGET_KEYS = ("pilot_wsl", "pilot2", "im", "wasl", "sim")

# After a vehicle is deleted from a PLATFORM (Pilot WSL, Pilot 2 or IM) its Device Statues becomes
# "Deleted", with today's Deletion Date, when no other SYSTEM is left. Systems, as the Platforms section
# ticks them:
#   Pilot  = Pilot WSL, Pilot Towing, Pilot SFDA and Pilot Tracking Only. These are flavours of the one
#            Pilot system, not four systems. Pilot WSL and Pilot 2 are its two estates.
#   IM     = IM Tracking
#   SARP, FMSI Medicine and FMSI Balady are systems of their own (this script cannot delete from those,
#            so a tick there always keeps the vehicle alive).
# Pilot is "left" only if the vehicle is still on the OTHER Pilot estate -- looked up live, because the
# Pilot ticks are often stale. When Pilot is gone from both estates the Pilot ticks are cleared with it.
# This runs for the form's Delete button and for the Expired Subscriptions block alike, because both call
# this script. WASL-only deletes and SIM suspends never change the status.
PILOT_BOXES = [["ch_pilot_wsl", "Pilot WSL"], ["ch_pilot_tow", "Pilot Towing"], ["ch_pilot_sfda", "Pilot SFDA"],
               ["ch_pilot_tracking_only", "Pilot Tracking Only"]]
OTHER_BOXES = [["ch_trakzee", "IM Tracking"], ["ch_sarp", "SARP"], ["ch_fmsi_medicine", "FMSI Medicine"],
               ["ch_fmsi_balady", "FMSI Balady"]]
DELETE_UNTICKS = {"pilot_wsl": "ch_pilot_wsl", "im": "ch_trakzee"}      # pilot2 has no box
STATUS_DELETED = "Deleted"


def pilot_still_there(vehicle_name, imei, deleted_key):
    """Is the vehicle still on a Pilot estate other than the one just deleted from?  Live lookups.
    -> {"state": "yes" | "no" | "unknown", "where": text}   ("unknown" is treated as "yes": never guess a deletion)"""
    mine = frappe.db.get_value(VEH_DT, vehicle_name, ["email_pilot2"], as_dict=True) or {}
    for k in ("pilot_wsl", "pilot2"):
        if k == deleted_key:
            continue
        if k == "pilot2" and not str(mine.get("email_pilot2") or "").strip():
            continue                      # no Pilot 2 account on the vehicle: it is not there
        got = panel_find(imei, TARGETS[k]["account"])
        if got["state"] == "yes":
            return {"state": "yes", "where": TARGETS[k]["label"]}
        if got["state"] != "no":
            return {"state": "unknown", "where": TARGETS[k]["label"] + " could not be checked: " + str(got["detail"])[:90]}
    return {"state": "no", "where": ""}


def after_platform_delete(vehicle_name, key, imei):
    """Untick what is gone; set Device Statues to Deleted (and the Deletion Date) if no system is left.
    Written with db.set_value on purpose: saving the document would run the Customer Vehicle save
    scripts, which rewrite subscription expiry dates and Serial No warranty dates.
    Returns {"changed": bool, "note": str}."""
    names = [c[0] for c in PILOT_BOXES] + [c[0] for c in OTHER_BOXES] + ["device_statues", "deletion_date"]
    now = frappe.db.get_value(VEH_DT, vehicle_name, names, as_dict=True)
    if not now:
        return {"changed": False, "note": "the vehicle record could not be read, so its status was not touched"}
    box = DELETE_UNTICKS.get(key)
    updates = {}
    unticked = []
    if box and frappe.utils.cint(now.get(box)):
        updates[box] = 0
        unticked.append(box)

    pilot = pilot_still_there(vehicle_name, imei, key)
    if pilot["state"] == "no":
        # gone from every Pilot estate: the Pilot ticks (including Towing / SFDA / Tracking Only) go with it
        for c in PILOT_BOXES:
            if frappe.utils.cint(now.get(c[0])) and c[0] not in updates:
                updates[c[0]] = 0
                unticked.append(c[0])

    left = []
    if pilot["state"] != "no":
        left.append("Pilot (" + pilot["where"] + ")")
    for c in OTHER_BOXES:
        if frappe.utils.cint(now.get(c[0])) and c[0] != box:
            left.append(c[1])

    changed = False
    cleared = ""
    if unticked:
        cleared = "unticked " + ", ".join(unticked) + "; "
    if left:
        note = cleared + "Device Statues left as it is -- still on: " + ", ".join(left)
    elif str(now.get("device_statues") or "") == STATUS_DELETED:
        # already Deleted: only make sure the Deletion Date is not left empty
        if now.get("deletion_date"):
            note = cleared + "Device Statues was already Deleted (Deletion Date " + str(now.get("deletion_date")) + ")"
        else:
            updates["deletion_date"] = frappe.utils.nowdate()
            note = cleared + "Device Statues was already Deleted; Deletion Date was empty, set to today"
    else:
        # the Deletion Date goes in together with the status, never one without the other
        updates["device_statues"] = STATUS_DELETED
        if not now.get("deletion_date"):
            updates["deletion_date"] = frappe.utils.nowdate()
        changed = True
        note = (cleared + "Device Statues set to Deleted, Deletion Date " +
                str(updates.get("deletion_date") or now.get("deletion_date")) + " (no other system is left)")
    if updates:
        frappe.db.set_value(VEH_DT, vehicle_name, updates)
    return {"changed": changed, "note": note}


def delete_word(key):
    return SUSPEND_WORD if key == "sim" else DELETE_WORD


def wasl_find(row, v):
    """Is the vehicle on Pilot (WASL is reached through it), and what does WASL's inquiry say?"""
    try:
        got = frappe.call("app_apis.wasl.wasl_delete", vehicle=str(v.get("name")), dry_run=1)
    except Exception as e:
        row["state"] = "unknown"
        row["detail"] = "WASL check failed -- " + str(e)[:240]
        return row
    row["state"] = str(got.get("state") or "unknown")
    row["found"] = bool(got.get("found"))
    row["platform_id"] = str(got.get("agent_id") or "")
    row["detail"] = str(got.get("detail") or "")
    return row


def wasl_delete_one(row, v):
    try:
        got = frappe.call("app_apis.wasl.wasl_delete", vehicle=str(v.get("name")), confirm=DELETE_WORD, dry_run=0)
    except Exception as e:
        row["verdict"] = "error"
        row["result"] = "WASL delete did not complete -- " + str(e)[:240]
        return row
    said = str(got.get("msg") or "")
    after = str(got.get("after_status") or "")
    if got.get("deleted"):
        row.update({"ok": True, "verdict": "done", "state": "deleted",
                    "result": "deleted from WASL only; the Pilot vehicle was kept. WASL said: " + said +
                              ((" | inquiry afterwards: " + after) if after else "")})
    else:
        row["verdict"] = "failure"
        row["result"] = "WASL did not delete it. WASL said: " + (said or "(nothing)")
    return row


def sim_lookup(v):
    """The vehicle's SIM on Lebara: by ICCID (sim_serial) first, then by the number Fleet Audit has."""
    imei = str(v.get("device_serial") or "").strip()
    iccid = "".join([c for c in str(v.get("sim_serial") or "") if c.isdigit()])
    msisdn = "".join([c for c in str(resolve_msisdn(imei).get("value") or "") if c.isdigit()]) if imei else ""
    if not iccid and not msisdn:
        return {"sim": None, "why": "the vehicle has no SIM serial (ICCID) and Fleet Audit has no SIM number for it"}
    try:
        res = None
        if iccid:
            res = frappe.call("lebara", action="find_sim", iccid=iccid)
        sim = res.get("result") if res is not None else None
        if not sim and msisdn:
            res = frappe.call("lebara", action="find_sim", msisdn=msisdn)
            sim = res.get("result")
    except Exception as e:
        return {"sim": None, "why": "Lebara -- " + str(e)[:240]}
    if not sim:
        return {"sim": None, "why": "Lebara has no SIM with ICCID " + (iccid or "-") + " or number " + (msisdn or "-")}
    return {"sim": sim, "why": ""}


def sim_find(row, v):
    got = sim_lookup(v)
    sim = got["sim"]
    if not sim:
        row["state"] = "missing" if not got["why"].startswith("Lebara --") else "unknown"
        row["detail"] = got["why"]
        return row
    status = str(sim.get("StatusText") or "")
    row["platform_id"] = str(sim.get("Id") or "")
    row["platform_name"] = str(sim.get("Msisdn") or "")
    row["detail"] = ("Lebara SIM " + str(sim.get("Msisdn") or "?") + " (ICCID " + str(sim.get("ICCID") or "?") +
                     "), status: " + (status or "unknown"))
    if status == "Suspend":
        row["state"] = "suspended"
        row["detail"] = row["detail"] + " -- already suspended, nothing to do"
        return row
    row["state"] = "found"
    row["found"] = True
    return row


def sim_suspend_one(row, v):
    try:
        res = frappe.call("lebara", action="suspend", id=row["platform_id"])
    except Exception as e:
        row["verdict"] = "error"
        row["result"] = "Lebara suspend did not complete -- " + str(e)[:240]
        return row
    out = res.get("result") or {}
    said = str(out.get("message") or "")
    after = sim_lookup(v)
    status = str((after["sim"] or {}).get("StatusText") or "")
    if status == "Suspend":
        row.update({"ok": True, "verdict": "done", "state": "suspended",
                    "result": "SIM " + row["platform_name"] + " is now suspended on Lebara. " + said})
    elif out.get("ok"):
        row["verdict"] = "error"
        row["result"] = ("Lebara accepted the suspend but the SIM still shows '" + (status or "unknown") +
                         "' -- it may take a moment; check Lebara. " + said)
    else:
        row["verdict"] = "failure"
        row["result"] = "Lebara did not suspend it. Lebara said: " + (said or "(nothing)")
    return row


def delete_row(key, v):
    return {"target": key, "label": DELETE_LABELS[key], "state": "", "found": False, "ok": False,
            "verdict": "", "detail": "", "platform_id": "", "platform_name": "", "pin_needed": False,
            "result": "", "imei": str(v.get("device_serial") or "").strip(), "plate": plate_of(v)}


def tag_text(text, tag):
    """The text between <tag> and </tag> (IM answers XML)."""
    a = str(text or "").find("<" + tag + ">")
    if a < 0:
        return ""
    a = a + len(tag) + 2
    b = str(text).find("</" + tag + ">", a)
    return str(text)[a:b if b > a else a + 80].strip()


def im_input_value(html, name):
    """value="..." of <input name="NAME" ...> in an IM page."""
    at = str(html or "").find('name="' + name + '"')
    if at < 0:
        return ""
    end = html.find(">", at)
    tag = html[at:end if end > at else at + 600]
    v = tag.find('value="')
    if v < 0:
        return ""
    return tag[v + 7:].split('"')[0].strip()


def im_get(path):
    try:
        res = frappe.call(IM_HTTP, jar=IM_JAR, url=IM_WEB + path, method="GET",
                          headers={"Referer": IM_WEB + "/jsp/index.html"}, timeout=90)
    except Exception as e:
        return ""
    return str(res.get("text") or "")


def im_security_pin_needed():
    """Does this IM account protect deletes with a Security PIN?  (The web app reads
    isEnabledSecurity from getUserSessionData.)"""
    r = im_xml("getUserSessionData", {"rights": "UserLevelRights,subaccount_of,projectid"})
    return tag_text(r["text"], "isEnabledSecurity").lower() == "true"


def delete_find(v, key):
    """Look the vehicle up on the platform. Fills the row; never changes anything."""
    row = delete_row(key, v)
    imei = row["imei"]
    if not imei:
        row["state"] = "skipped"
        row["detail"] = "the vehicle has no Device Serial, so there is nothing to look for"
        return row
    if key == "wasl":
        return wasl_find(row, v)
    if key == "sim":
        return sim_find(row, v)
    spec = TARGETS[key]

    if key != "im":
        got = panel_find(imei, spec["account"])
        row["detail"] = got["detail"]
        if got["state"] == "yes":
            r = got["row"]
            row.update({"state": "found", "found": True, "platform_id": str(r.get("agent_id") or ""),
                        "platform_name": str(r.get("vehiclenumber") or "")})
            if not row["platform_id"]:
                row["state"] = "unknown"
                row["found"] = False
                row["detail"] = "the panel found it but gave no agent_id"
        elif got["state"] == "no":
            row["state"] = "missing"
        else:
            row["state"] = "unknown"
        return row

    # IM
    log = im_login()
    if not log["ok"]:
        row["state"] = "unknown"
        row["detail"] = "IM sign-in failed -- " + log["why"]
        if "ermission" in log["why"]:
            row["detail"] = row["detail"] + " (IM calls need a System Manager login: app_apis.core.http is limited to that role)"
        return row
    email = str(v.get("im_platform") or "").strip()
    if not email:
        row["state"] = "unknown"
        row["detail"] = "the vehicle has no im_platform email, so the IM company cannot be worked out"
        return row
    acc = im_account(email)
    if not (acc["company"] and acc["branch"]):
        row["state"] = "unknown"
        row["detail"] = acc["why"] or "could not work out the IM company"
        return row
    name = row["plate"]
    r = im_xml("getDuplicateVehicle", {"vehiclenumber": name, "companyId": acc["company"],
                                       "sMode": "insert", "sVehicleId": ""})
    vid = tag_text(r["text"], "iVehicleID")
    if "<status>Match</status>" not in r["text"] or not vid:
        row["state"] = "missing"
        row["detail"] = "IM has no vehicle named " + name + " in company " + acc["company"]
        return row
    # The name matched; make sure it is THIS device before anything can be deleted.
    im_get("/jsp/Overview.jsp?screenid=2253&level=1&popup=false")
    page = im_get("/jsp/DetailScreen.jsp?entityid=" + vid + "&screenid=2249&mode=update&level=1&popup=false"
                  "&view=L&overviewscreenid=2253")
    on_im = im_input_value(page, "object_imei_no")
    if on_im != imei:
        row["state"] = "mismatch"
        row["platform_id"] = vid
        row["detail"] = ("IM vehicle " + vid + " is named " + name + " but its IMEI is " + (on_im or "(unreadable)") +
                         ", not " + imei + " -- not deleting")
        return row
    row.update({"state": "found", "found": True, "platform_id": vid, "platform_name": name,
                "detail": "IM vehicle " + vid + " (" + name + "), IMEI " + imei + ", company " + acc["company"] +
                          ", branch " + acc["branch"]})
    row["company"] = acc["company"]
    row["branch"] = acc["branch"]
    row["pin_needed"] = im_security_pin_needed()
    return row


def delete_one(v, key, pin):
    row = delete_find(v, key)
    if row["state"] != "found":
        row["verdict"] = "not_deleted"
        row["result"] = "nothing done -- " + (row["detail"] or row["state"])
        return row
    if key == "wasl":
        return wasl_delete_one(row, v)
    if key == "sim":
        return sim_suspend_one(row, v)
    spec = TARGETS[key]
    imei = row["imei"]

    if key != "im":
        res = panel_call("app/vehicles.php", {"cmd": "remove", "agent_id": row["platform_id"],
                                              "is_update_sim_status": "false"}, spec["account"], "POST")
        said = str(res.get("text") or res.get("body") or "")[:200]
        after = panel_find(imei, spec["account"])
        if after["state"] == "no":
            row.update({"ok": True, "verdict": "deleted", "state": "deleted",
                        "result": "deleted from " + spec["label"] + " -- the panel no longer finds it. " + said})
        elif after["state"] == "yes":
            row["verdict"] = "failure"
            row["result"] = "the panel is still showing the vehicle after the delete. Panel said: " + (said or "(nothing)")
        else:
            row["verdict"] = "error"
            row["result"] = "sent, but cannot confirm -- " + after["detail"]
        return row

    # IM
    if row["pin_needed"]:
        if not str(pin or "").strip():
            row["verdict"] = "not_deleted"
            row["result"] = "nothing deleted -- this IM account needs its Security PIN to delete"
            return row
        chk = im_json("isCheckSecurityPin", {"security_pin": str(pin).strip()})
        data = chk.get("data")
        good = isinstance(data, dict) and bool(data.get("result"))
        if not good:
            row["verdict"] = "not_deleted"
            row["result"] = "nothing deleted -- IM did not accept the Security PIN"
            return row
    res = im_json("bulkObjectRemove", {"vehicle_ids": row["platform_id"], "companyid": row["company"],
                                       "branchid": row["branch"]})
    data = res.get("data")
    said = str(res.get("text") or data or "")[:200]
    accepted = isinstance(data, dict) and frappe.utils.cint(data.get("result")) == 1
    gone = (not im_name_taken(row["plate"], row["company"])) and im_has(imei)["state"] == "no"
    if accepted and gone:
        row.update({"ok": True, "verdict": "deleted", "state": "deleted",
                    "result": "deleted from IM -- IM no longer finds vehicle " + row["platform_id"] + ". " + said})
    elif accepted:
        row["verdict"] = "error"
        row["result"] = "IM accepted the delete but it still finds the vehicle -- check IM. " + said
    else:
        row["verdict"] = "failure"
        row["result"] = "IM did not delete it. IM said: " + (said or "(nothing)")
    return row


# ---------------------------------------------------------------- verify
def exists_on_pilot(imei, account):
    """Is this IMEI registered on the estate?  -> {"state": yes|no|unknown, ...}

    Measured on this deployment, not taken from the docs:
      * unknown IMEI      -> HTTP 200, body `null`
      * registered IMEI   -> HTTP 500. Pilot's own vehinfo crashes on a missing
                             table (`relation "driver2veh" does not exist`) once
                             it has FOUND the vehicle and goes to join drivers.
    So a 500 for this IMEI means "it is there" -- but only if Pilot is otherwise
    healthy. That is checked with a control call for an IMEI nobody has: if the
    control also fails, Pilot is simply down and the answer is "unknown".
    """
    res = backend("vehinfo", {"imei": imei}, account)
    code = frappe.utils.cint(res.get("code"))
    msg = str(res.get("msg") or "")
    if code == 0:
        found = res.get("data") or []
        if found:
            first = found[0] if isinstance(found[0], dict) else {}
            return {"state": "yes", "row": first,
                    "detail": "vehinfo returned it (agentid " + str(first.get("agentid") or "?") + ")"}
        return {"state": "no", "row": {}, "detail": "vehinfo finds nothing for this IMEI"}
    low = msg.lower()
    if "no veh info" in low:
        return {"state": "no", "row": {}, "detail": "vehinfo finds nothing for this IMEI"}

    # Only a SERVER CRASH is read as "it is there". A refusal, a bad sign-in or a
    # timeout says nothing about the vehicle either way.
    crashed = (frappe.utils.cint(res.get("http")) >= 500 or "too many 500" in low or
               "500 server error" in low or "driver2veh" in low)
    if not crashed:
        return {"state": "unknown", "row": {},
                "detail": "vehinfo did not give a usable answer -- " + msg[:140]}

    control = backend("vehinfo", {"imei": "000000000000001"}, account)
    healthy = frappe.utils.cint(control.get("code")) == 0 and not (control.get("data") or [])
    if healthy:
        return {"state": "yes", "row": {},
                "detail": ("vehinfo crashes for this IMEI while answering normally for an "
                           "unknown one -- on this server that is what a registered "
                           "vehicle looks like (Pilot bug: driver2veh)")}
    return {"state": "unknown", "row": {},
            "detail": "Pilot is not answering reliably right now -- " + msg[:140]}


def verify_one(v, key):
    spec = TARGETS[key]
    imei = str(v.get("device_serial") or "").strip()
    built = build_payload(v, key)
    row = {"target": key, "label": spec["label"], "ok": False,
           "payload": built["payload"], "gaps": built["gaps"], "mode": "verify"}

    if not imei:
        row["verdict"] = "failure"
        row["result"] = "this vehicle has no Device Serial, so nothing can be looked up"
        return row

    if key == "im" and IM_VERIFY_VIA == "web":
        seen = im_has(imei)
        row["ok"] = seen["state"] == "yes"
        row["verdict"] = {"yes": "uploaded", "no": "vehicle_not_found"}.get(seen["state"], "error")
        row["result"] = seen["detail"]
        return row

    if key == "im":
        try:
            snap = frappe.call("app_apis.im_connector.get_vehicle_live", imei=imei) or {}
            row["ok"] = True
            row["verdict"] = "uploaded"
            row["result"] = "IM has this device"
        except Exception as e:
            # get_vehicle_live throws both for "not on IM" and for "IM refused
            # the call" -- rate limit, bad sign-in, timeout. Only the first is a
            # verdict about the vehicle. IM allows about one call a minute.
            why = str(e)
            low = why.lower()
            hit = ""
            for phrase in ("exceeded the limit", "sign-in failed", "sign in failed",
                           "timed out", "could not reach", "credential", "unauthor"):
                if phrase in low:
                    hit = phrase
            if hit:
                row["verdict"] = "error"
                row["result"] = "IM would not answer (" + hit + ") -- " + why[:130]
            else:
                row["verdict"] = "vehicle_not_found"
                row["result"] = "IM has no row for this IMEI -- " + why[:150]
        return row

    seen = pilot_has(imei, spec["account"])
    if seen["state"] == "yes":
        row["ok"] = True
        row["verdict"] = "uploaded"
        row["result"] = "registered on " + spec["label"] + " -- " + seen["detail"]
        row["found"] = seen["row"]
        row["headline"] = "Registered on " + spec["label"]
        row["detail"] = seen["detail"]
    elif seen["state"] == "no":
        row["verdict"] = "vehicle_not_found"
        row["result"] = "not registered on " + spec["label"]
        row["headline"] = "Not registered on " + spec["label"]
        row["detail"] = seen["detail"]
    else:
        row["verdict"] = "error"
        row["result"] = "the check itself failed -- " + seen["detail"]
    return row


# ---------------------------------------------------------------- upload
def send_one(v, key):
    spec = TARGETS[key]
    built = build_payload(v, key)
    payload = built["payload"]
    gaps = built["gaps"]
    row = {"target": key, "label": spec["label"], "ok": False,
           "payload": payload, "gaps": gaps, "mode": "upload"}

    how = "create_vehicle (admin panel)" if UPLOAD_VIA == "panel" else "vehadd"
    if key == "im":
        how = "Add Vehicle form (gps.im2m.ws)"
    if gaps:
        row["verdict"] = "failure"
        row["result"] = "cannot build a complete " + how + ": " + "; ".join(gaps)
        return row

    # Two different reasons not to send, and they must not be confused: a plan is
    # a deliberate rehearsal, while an unarmed target is a safety catch.
    armed_here = key in LIVE_TARGETS
    if action == "plan" or not armed_here:
        row["ok"] = True
        row["verdict"] = ""
        row["cmd"] = how
        if action == "plan" and armed_here:
            row["result"] = ("ready to send " + how + ". " + spec["label"] +
                             " IS armed, so Upload Now will really send this.")
        elif action == "plan":
            row["result"] = ("would send " + how + ", but " + spec["label"] +
                             " is not in LIVE_TARGETS, so Upload Now would send nothing.")
        else:
            row["result"] = (spec["label"] + " is not in LIVE_TARGETS, so nothing was sent.")
            row["blocked"] = True
        return row

    row["cmd"] = how
    imei = str(v.get("device_serial") or "").strip()

    # Already there? Then sending again could only make a duplicate.
    before = platform_has(key, imei, spec["account"])
    if before["state"] == "yes":
        row["ok"] = True
        row["verdict"] = "uploaded"
        row["result"] = ("already registered on " + spec["label"] + ", not created again -- " +
                         before["detail"])
        row["headline"] = "Already on " + spec["label"] + " — not created again"
        row["detail"] = before["detail"]
        if UPLOAD_VIA == "panel" and key != "im":
            sen = ensure_sensors((before.get("row") or {}).get("agent_id"), spec["account"])
            row["sensors"] = sen["msg"]
            row["sensors_ok"] = sen["ok"]
            row["result"] = row["result"] + ". " + sen["msg"]
        return row
    if before["state"] == "unknown":
        row["verdict"] = "error"
        row["result"] = ("did not send: could not establish whether it is already there -- " +
                         before["detail"])
        return row

    params = {}
    for k in payload.keys():
        if not str(k).startswith("_"):
            params[k] = payload[k]

    if key == "im":
        return im_upload(row, payload)

    # Create through the admin panel, then read it back.
    if UPLOAD_VIA == "panel":
        form = {"cmd": "create_vehicle", "agent_id": "",
                "device_name": params["configuration"], "initial_mileage": "0",
                "current_mileage": "0", "sum_mileage": "0"}
        for k in params.keys():
            form[k] = str(params[k])
        res = panel_call("app/fitter.php", form, spec["account"], "POST")
        said = panel_verdict(res)
        row["sent"] = form
        after = pilot_has(imei, spec["account"])
        row["confirmed"] = after["state"] == "yes"
        if after["state"] == "yes":
            row["ok"] = True
            row["verdict"] = "uploaded"
            sen = ensure_sensors((after.get("row") or {}).get("agent_id"), spec["account"])
            row["sensors"] = sen["msg"]
            row["sensors_ok"] = sen["ok"]
            row["result"] = ("created on " + spec["label"] + " -- " + after["detail"] +
                             ". " + sen["msg"] + ". Panel said: " + said["msg"][:160])
            row["headline"] = "Created on " + spec["label"]
            row["detail"] = after["detail"]
        elif after["state"] == "no":
            row["verdict"] = said["verdict"] if said["verdict"] not in ("uploaded", "") else "failure"
            row["result"] = "Pilot did not create it. Panel said: " + said["msg"][:400]
            row["headline"] = "Not created on " + spec["label"]
        else:
            row["verdict"] = "error"
            row["result"] = ("sent, but it cannot be confirmed right now -- " + after["detail"] +
                             ". Press Verify & Stamp before uploading again.")
            row["headline"] = "Sent — not confirmed yet"
            row["detail"] = after["detail"]
            row["advice"] = "Press Verify & Stamp in a minute. Do not press Upload again before that."
        row["platform_said"] = said["msg"]
        return row

    # ONE attempt per click. The answer is shown word for word.
    res = backend("vehadd", params, spec["account"], "POST")
    said = verdict_from(res)
    row["sent"] = params

    # Never take vehadd's word for it: read the vehicle back.
    after = exists_on_pilot(imei, spec["account"])
    row["confirmed"] = after["state"] == "yes"
    if after["state"] == "yes":
        row["ok"] = True
        row["verdict"] = "uploaded"
        row["result"] = ("created on " + spec["label"] + " -- confirmed by reading it back (" +
                         after["detail"] + "). Pilot said: " + said["msg"][:160])
    elif after["state"] == "no":
        row["verdict"] = said["verdict"] if said["verdict"] not in ("uploaded", "") else "failure"
        row["result"] = ("Pilot did not create it. Pilot said: " + said["msg"][:400])
    else:
        row["verdict"] = "error"
        row["result"] = ("sent, but whether it was created cannot be confirmed right now -- " +
                         after["detail"] + ". Press Verify & Stamp in a minute; do not "
                         "press Upload again before that.")
    return row


def which_targets(v, asked):
    if asked and asked != "all":
        return [asked] if asked in TARGETS else []
    picked = []
    if frappe.utils.cint(v.get("ch_pilot_wsl")):
        picked.append("pilot_wsl")
    if str(v.get("email_pilot2") or "").strip():
        picked.append("pilot2")
    if frappe.utils.cint(v.get("ch_trakzee")) or str(v.get("im_platform") or "").strip():
        picked.append("im")
    return picked


# ---------------------------------------------------------------- events for code built on the core
def notify_core(event, platform, vehicle, imei, detail):
    """Tell app_apis.core.events handlers (registered by other apps in hooks.py) what this script just
    did, so work done with the buttons is heard exactly like work done through app_apis.core.platforms.
    Reporting is best effort: it must never change the outcome of the action it reports."""
    try:
        frappe.call("app_apis.core.api.notify", event=event, platform=platform, vehicle=vehicle,
                    imei=imei, detail=detail)
    except Exception:
        pass


# ---------------------------------------------------------------- run
if not allowed and action not in ("delete_plan", "delete", "sim_check"):
    # upload / verify / im_sync stay with these roles; delete and the SIM check are open to
    # every signed-in user (their own branches refuse Guest; delete needs the typed word)
    out["error"] = "Only System Manager or Technical can do this."

elif action == "im_sync":
    # Refresh Customer.im_company_id / im_reseller_id / im_branch_id from IM (email is the key).
    # Optional: email=<one address to look for>, limit=<page reads, default 60>, force=1 to re-read mapped companies.
    got = im_sync(str(args.get("email") or ""), frappe.utils.cint(args.get("limit") or 60), bool(args.get("force")))
    frappe.db.commit()
    out["sync"] = got
    out["ok"] = not got.get("error")

elif action in ("delete_plan", "delete"):
    # One vehicle, one platform, per call (the list view loops). Open to every signed-in
    # user -- the typed DELETE is the guard. (IM itself still needs a System Manager login,
    # because its calls go through app_apis.core.http; delete_find says so.)
    veh = vehicle_doc(vehicle_name) if vehicle_name else None
    if str(frappe.session.user) == "Guest":
        out["error"] = "Sign in first."
    elif not veh:
        out["error"] = "No such Customer Vehicle: " + vehicle_name
    elif target not in DELETE_TARGET_KEYS:
        out["error"] = "Pick one target: " + ", ".join(DELETE_TARGET_KEYS) + "."
    elif action == "delete" and str(args.get("confirm") or "").strip() != delete_word(target):
        out["error"] = "Nothing done: type " + delete_word(target) + " to confirm."
    else:
        out["vehicle"] = vehicle_name
        out["plate"] = plate_of(veh)
        out["imei"] = str(veh.get("device_serial") or "")
        if action == "delete_plan":
            drow = delete_find(veh, target)
        else:
            drow = delete_one(veh, target, str(args.get("pin") or ""))
            out["device_status_changed"] = False
            if drow.get("verdict") == "deleted" and target in ("pilot_wsl", "pilot2", "im"):
                try:
                    st = after_platform_delete(vehicle_name, target, str(drow.get("imei") or ""))
                    drow["result"] = str(drow["result"]) + " | " + st["note"]
                    out["device_status_changed"] = bool(st["changed"])
                except Exception as e:
                    drow["result"] = (str(drow["result"]) + " | deleted on the platform, but the vehicle's "
                                      "Device Statues could not be updated: " + str(e)[:160])
            trail = (str(frappe.session.user) + " -- delete " + drow["label"] + " -> " +
                     str(drow["verdict"] or "no verdict") + " -- " + str(drow["result"])[:300])
            frappe.get_doc({"doctype": "Comment", "comment_type": "Comment", "reference_doctype": VEH_DT,
                            "reference_name": vehicle_name, "content": trail}).insert(ignore_permissions=True)
            frappe.get_doc({"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "Server Script",
                            "reference_name": SCRIPT_NAME,
                            "content": trail + " (" + vehicle_name + ", IMEI " + out["imei"] + ")"}
                           ).insert(ignore_permissions=True)
        drow.pop("company", None)
        drow.pop("branch", None)
        out["results"].append(drow)
        if action == "delete" and drow.get("verdict") in ("deleted", "done"):
            if target == "wasl":
                notify_core("after_wasl", "pilot_wsl", vehicle_name, out["imei"],
                            {"action": "delete", "result": str(drow.get("result"))[:300]})
            elif target in ("pilot_wsl", "pilot2", "im"):
                notify_core("after_delete", target, vehicle_name, out["imei"],
                            {"result": {"verdict": drow.get("verdict"), "ok": bool(drow.get("ok")),
                                        "result": str(drow.get("result"))[:300],
                                        "status_changed": bool(out.get("device_status_changed"))}})
        out["ok"] = bool(drow["found"]) if action == "delete_plan" else bool(drow["ok"])

elif action == "sim_check":
    # Read-only: what Lebara says about this vehicle's SIM. Open to every signed-in user (the
    # lebara script itself is limited to System Managers and says so if it refuses).
    veh = vehicle_doc(vehicle_name) if vehicle_name else None
    if str(frappe.session.user) == "Guest":
        out["error"] = "Sign in first."
    elif not veh:
        out["error"] = "No such Customer Vehicle: " + vehicle_name
    else:
        out["vehicle"] = vehicle_name
        out["plate"] = plate_of(veh)
        out["imei"] = str(veh.get("device_serial") or "")
        got = sim_lookup(veh)
        sim = got["sim"]
        out["found"] = bool(sim)
        out["why"] = got["why"]
        if sim:
            keep = [["msisdn", "Msisdn"], ["iccid", "ICCID"], ["imsi", "IMSI"], ["status", "StatusText"],
                    ["group", "GroupName"], ["customer", "SubCustomerName"], ["activated", "ActivationDate"],
                    ["last_connection", "LastConnectionDate"], ["this_month_usage", "ThisMonthUsage"],
                    ["last_month_usage", "LastMonthUsage"], ["in_data_session", "InDataSession"],
                    ["sim_imei", "IMEI"]]
            info = {}
            for pair in keep:
                info[pair[0]] = str(sim.get(pair[1]) if sim.get(pair[1]) is not None else "")
            # is this SIM in THIS vehicle's device?  (Lebara records the IMEI it last saw it in)
            info["imei_matches_vehicle"] = ("" if not info["sim_imei"] else
                                            ("yes" if info["sim_imei"] == out["imei"] else "no"))
            # the live status from Lebara's BSS, when it will give it
            try:
                full = frappe.call("lebara", action="sim_full", msisdn=info["msisdn"])
                live = (full.get("result") or {}).get("live_status") or {}
                info["live_status"] = json.dumps(live, ensure_ascii=False)[:400] if live else ""
            except Exception as e:
                info["live_status"] = ""
            out["sim"] = info
        out["ok"] = bool(sim)

elif action not in ("plan", "verify", "upload"):
    out["error"] = "Unknown action. Use plan, verify, upload, im_sync, delete_plan, delete or sim_check."

elif not vehicle_name:
    out["error"] = "No vehicle given."

else:
    veh = vehicle_doc(vehicle_name)
    if not veh:
        out["error"] = "No such Customer Vehicle: " + vehicle_name
    else:
        picked = which_targets(veh, target)
        if not picked:
            out["error"] = ("Nothing to upload to. Tick the platform on the vehicle, "
                            "or choose one target explicitly.")
        else:
            tickets = install_tickets(vehicle_name)
            out["vehicle"] = vehicle_name
            out["plate"] = plate_of(veh)
            out["imei"] = str(veh.get("device_serial") or "")
            out["install_tickets"] = [t.name for t in tickets]
            out["would_stamp"] = ([t.name for t in tickets] if STAMP_ALL
                                  else [t.name for t in tickets[:1]])
            # A vehicle with no Installation ticket is not a problem -- plenty of
            # them have none. The run carries on; there is simply no Server status
            # field to write, so the outcome is recorded on the vehicle instead.
            if not tickets:
                out["note"] = ("No Installation xticket for this vehicle, so there is no "
                               "Server status to set. The outcome is recorded on the "
                               "vehicle itself.")

            # Only these are answers FROM the platform, and only these may ever
            # be written onto a ticket. "error" means our own side broke.
            SPEAKS = ("uploaded", "duplicate", "vehicle_not_found", "failure")

            every_ok = True
            worst = ""
            heard_back = False
            not_sent = []
            for key in picked:
                if action == "verify":
                    row = verify_one(veh, key)
                else:
                    row = send_one(veh, key)
                out["results"].append(row)
                if (action == "upload" and row.get("verdict") == "uploaded" and not row.get("blocked")
                        and "already registered" not in str(row.get("result") or "")):
                    notify_core("after_create", key, vehicle_name, str(veh.get("device_serial") or ""),
                                {"result": {"verdict": "uploaded", "ok": True,
                                            "result": str(row.get("result") or "")[:300]}})
                if not row.get("ok"):
                    every_ok = False
                if row.get("blocked"):
                    not_sent.append(str(row.get("label") or key))
                if row.get("verdict") in SPEAKS:
                    heard_back = True
                    if row.get("verdict") != "uploaded":
                        worst = row.get("verdict")

            # A target that is not in LIVE_TARGETS sends nothing. That is never an upload,
            # so it must not read as success to the caller.
            out["not_sent"] = not_sent
            out["ok"] = every_ok and not (action == "upload" and not_sent)
            out["heard_back"] = heard_back
            if action == "upload" and not_sent:
                out["warning"] = ("NOT sent to " + ", ".join(not_sent) + " -- it is not in LIVE_TARGETS, "
                                  "so nothing was created there.")

            # Only complain that the platform stayed silent when something
            # actually tried to reach it. A dry run reaches nothing by design.
            armed = [k for k in picked if k in LIVE_TARGETS]
            tried = (action == "verify") or (action == "upload" and armed)
            if not heard_back and tried:
                out["warning"] = ("The platform never answered, so the ticket was left "
                                  "untouched. Fix the connection, then try again.")

            # Record the outcome whenever the platform actually answered -- with or
            # without a ticket. A missing ticket only means there is no Server
            # status field to set; it never means "do not save".
            stamp_now = ((action == "verify") or (action == "upload")) and heard_back
            if stamp_now:
                note_parts = []
                for row in out["results"]:
                    note_parts.append(str(row.get("label")) + ": " + str(row.get("result")))
                note = " | ".join(note_parts)

                verdict = "uploaded" if every_ok else worst
                if tickets and verdict:
                    out["stamped"] = stamp_tickets(vehicle_name, verdict, note)
                    out["server_status"] = verdict
                elif verdict:
                    out["server_status"] = verdict
                    out["stamped"] = []

                trail = (str(frappe.session.user) + " -- " + action + " -> " +
                         str(verdict or "no verdict") + " -- " + note[:300])

                # on the vehicle, always -- this is the record that survives when
                # there is no ticket to carry it
                frappe.get_doc({
                    "doctype": "Comment",
                    "comment_type": "Comment",
                    "reference_doctype": VEH_DT,
                    "reference_name": vehicle_name,
                    "content": trail,
                }).insert(ignore_permissions=True)

                frappe.get_doc({
                    "doctype": "Comment",
                    "comment_type": "Comment",
                    "reference_doctype": "Server Script",
                    "reference_name": SCRIPT_NAME,
                    "content": (str(frappe.session.user) + " -- " + action + " " + vehicle_name +
                                " (" + out["plate"] + ", IMEI " + out["imei"] + ") -> " +
                                str(out.get("server_status") or "no change") + " -- " + note[:300]),
                }).insert(ignore_permissions=True)

for key in out.keys():
    frappe.flags[key] = out[key]
