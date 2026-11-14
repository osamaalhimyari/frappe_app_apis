# =============================================================================
# wasl_link_api -- Server Script (API), used by the Client Script
# "customer-vehicle-wasl" on Customer Vehicle.
# =============================================================================
#
# ACTIONS
#   action=plan   build the WASL form defaults from the Customer Vehicle.
#                 Sends nothing.
#   action=link   validate the form the user confirmed, then (only when
#                 WASL_LIVE = 1) save it on Pilot and register it with WASL.
#   action=status read-only: what the Pilot admin panel's WASL list says.
#
#   connection = the Pilot connection to use: pilot_wsl (row 1 of the app_apis
#                Pilot Connections table), pilot2 (row 2), pilot3, ... It must
#                have Uses WASL ticked. Blank = the first row that does.
#
# THE PILOT SIDE (read from the Pilot web app itself, not the docs -- the docs
# say there is no API for WASL, and there is none in api.php):
#   <user site>/ax/mod/wasl/wasl.php
#     cmd=vehicle            agentid      -> the saved form
#     cmd=vehiclesave        agentid (query) + the form fields (POST)
#     cmd=vehicleregister    agentid      -> {"result": true/false, "msg": ...}
#     cmd=vehiclecheck       agentid      (Inquiry)
#     cmd=vehicleimeiupdate  agentid
#     cmd=log                id=agentid
#   These calls run in a signed-in USER session of the customer's Pilot
#   account (not the admin JWT). HOW that session is opened is still to be
#   decided -- see wasl_session() below. Until then WASL_LIVE stays 0 and
#   action=link only validates and returns the exact payload.
# =============================================================================

WASL_LIVE = 0   # sending is done by app_apis.wasl.link_vehicle (custom app), not here
USER_SITE = "https://ksa.pilot-gps.com/"
VEH_DT = "Customer Vehicle"
AUDIT_DT = "app_apis_fleet_audit"

# ---- static defaults (can be changed in the dialog) -------------------------
DEFAULT_PLATE_TYPE = "2"            # Public Transport
DEFAULT_TELECOM = "LEBARA"
STATIC_WEIGHT = {
    "weightSource": "ANALOG",
    "weightSensorAnalogNumber": "1",
    "weightInKilogramsEmptyVehicle": 4500,
    "mlVoltageEmptyVehicle": 500,
    "weightInKilogramsHalfLoad": 0,
    "mlVoltageHalfLoad": "",
    "weightInKilogramsFullLoad": 20000,
    "mlVoltageFullLoad": 3500,
}

# ERP device type prefix -> brand shown on WASL
BRANDS = (
    ("FMC", "Teltonika"), ("FMB", "Teltonika"), ("FMM", "Teltonika"),
    ("FMT", "Teltonika"), ("TAT", "Teltonika"),
    ("JC", "Jimi"), ("TZ-", "Trakzee"),
)

PLATE_TYPES = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11")
TELECOMS = ("BEYOND ONE", "BULL", "eSIM", "FREEDOM", "GO", "LEBARA", "MOBILY",
            "RED", "SALAM", "STC", "ZAIN")
SOURCES = ("RS", "ONE_WIRE", "BLUETOOTH")

args = frappe.form_dict
action = str(args.get("action") or "plan").strip().lower()
vehicle_name = str(args.get("vehicle") or "").strip()
out = {"ok": False, "action": action, "live": WASL_LIVE}


def allowed():
    # staff logins only (Guest and portal logins are refused), and the action's group must be open to the
    # user in app_apis settings > Access
    if not (str(frappe.session.user) == "Administrator" or frappe.db.get_value("User", str(frappe.session.user), "user_type") == "System User"):
        return False
    group = "WASL link and delete" if action == "link" else "View platform data"
    return bool(frappe.call("app_apis.access.can", group=group))


def s(v):
    return str(v or "").strip()


def brand_and_model(devices_type):
    dt = s(devices_type)
    if not dt:
        return {"brand": "", "model": ""}
    up = dt.upper()
    for pair in BRANDS:
        if up.startswith(pair[0]):
            model = dt if dt.lower().startswith(pair[1].lower()) else pair[1] + " " + dt
            return {"brand": pair[1], "model": model}
    return {"brand": "", "model": dt}


def msisdn_for(imei):
    if not imei:
        return ""
    rows = frappe.get_all(AUDIT_DT, filters={"imei": imei},
                          fields=["sim_msisdn", "pilot_msisdn"], limit=1)
    if not rows:
        return ""
    num = s(rows[0].get("sim_msisdn")) or s(rows[0].get("pilot_msisdn"))
    num = num.replace(" ", "").replace("+", "")
    num = num.lstrip("0")
    if num and not num.startswith("966"):
        num = "966" + num
    return num


def plan(doc):
    bm = brand_and_model(doc.get("devices_type"))
    imei = s(doc.get("device_serial"))
    form = {
        "plateType": DEFAULT_PLATE_TYPE,
        "plate_number": s(doc.get("plate_num")),
        # ERP char1 is the first letter read right-to-left -> the RIGHT letter
        "right_letter": s(doc.get("char1")),
        "middle_letter": s(doc.get("char2")),
        "left_letter": s(doc.get("char3")),
        "sequenceNumber": s(doc.get("serial")),
        "imeiNumber": imei,
        "telecomCompanyName": DEFAULT_TELECOM,
        "simNumber": msisdn_for(imei),
        "deviceBrand": bm["brand"],
        "deviceModel": bm["model"],
        "hasTemperatureSensor": 0,
        "temperatureSource": "",
        "hasHumiditySensor": 0,
        "humiditySource": "",
    }
    for k in STATIC_WEIGHT.keys():
        form[k] = STATIC_WEIGHT[k]
    return form


def validate(f):
    gaps = []
    if s(f.get("plateType")) not in PLATE_TYPES:
        gaps.append("Plate Type")
    for pair in (("plate_number", "Plate Number"), ("left_letter", "Left Letter"),
                     ("middle_letter", "Middle Letter"), ("right_letter", "Right Letter"),
                     ("sequenceNumber", "Sequence Number"), ("imeiNumber", "Imei Number"),
                     ("deviceBrand", "Tracking device Brand"),
                     ("deviceModel", "Tracking device Model")):
        if not s(f.get(pair[0])):
            gaps.append(pair[1])
    if s(f.get("telecomCompanyName")) not in TELECOMS:
        gaps.append("Telecom service provider")
    sim = s(f.get("simNumber")).replace(" ", "")
    if not (sim.startswith("966") and sim.isdigit() and 4 <= len(sim) <= 18):
        gaps.append("Data SIM number (must start with 966, digits only)")
    for trio in (("hasTemperatureSensor", "temperatureSource", "Temperature source"),
                             ("hasHumiditySensor", "humiditySource", "Humidity source")):
        if frappe.utils.cint(f.get(trio[0])) and s(f.get(trio[1])) not in SOURCES:
            gaps.append(trio[2])
    if s(f.get("weightSource")) not in ("ANALOG", "CAN"):
        gaps.append("Weight source")
    for pair in (("weightInKilogramsEmptyVehicle", "Empty weight"),
                     ("mlVoltageEmptyVehicle", "Empty voltage"),
                     ("weightInKilogramsFullLoad", "Full weight"),
                     ("mlVoltageFullLoad", "Full voltage")):
        if s(f.get(pair[0])) == "":
            gaps.append(pair[1])
    return gaps


def pilot_payload(f):
    """The exact field names the Pilot WASL form posts (cmd=vehiclesave)."""
    p = {
        "plateType": s(f.get("plateType")),
        "vehiclePlate[number]": s(f.get("plate_number")),
        "vehiclePlate[leftLetter]": s(f.get("left_letter")),
        "vehiclePlate[middleLetter]": s(f.get("middle_letter")),
        "vehiclePlate[rightLetter]": s(f.get("right_letter")),
        "sequenceNumber": s(f.get("sequenceNumber")),
        "imeiNumber": s(f.get("imeiNumber")),
        "trackingDevice[telecomCompanyName]": s(f.get("telecomCompanyName")),
        "trackingDevice[simNumber]": s(f.get("simNumber")).replace(" ", ""),
        "trackingDevice[deviceBrand]": s(f.get("deviceBrand")),
        "trackingDevice[deviceModel]": s(f.get("deviceModel")),
        "trackingDevice[hasTemperatureSensor]": frappe.utils.cint(f.get("hasTemperatureSensor")),
        "trackingDevice[hasHumiditySensor]": frappe.utils.cint(f.get("hasHumiditySensor")),
        "trackingDevice[weightSource]": s(f.get("weightSource")),
        "trackingDevice[weightInKilogramsEmptyVehicle]": s(f.get("weightInKilogramsEmptyVehicle")),
        "trackingDevice[mlVoltageEmptyVehicle]": s(f.get("mlVoltageEmptyVehicle")),
        "trackingDevice[weightInKilogramsHalfLoad]": s(f.get("weightInKilogramsHalfLoad")),
        "trackingDevice[mlVoltageHalfLoad]": s(f.get("mlVoltageHalfLoad")),
        "trackingDevice[weightInKilogramsFullLoad]": s(f.get("weightInKilogramsFullLoad")),
        "trackingDevice[mlVoltageFullLoad]": s(f.get("mlVoltageFullLoad")),
    }
    if p["trackingDevice[hasTemperatureSensor]"]:
        p["trackingDevice[temperatureSource]"] = s(f.get("temperatureSource"))
    if p["trackingDevice[hasHumiditySensor]"]:
        p["trackingDevice[humiditySource]"] = s(f.get("humiditySource"))
    if p["trackingDevice[weightSource]"] == "ANALOG":
        p["trackingDevice[weightSensorAnalogNumber]"] = s(f.get("weightSensorAnalogNumber"))
    return p


def wasl_session(doc):
    """TODO -- open a signed-in user session on USER_SITE for the customer's
    Pilot account. Not built yet: the way to sign in is the open decision."""
    return {"ok": False, "why": "the WASL connection to Pilot is not configured yet"}

# ---------------------------------------------------------------- the Pilot connection
# Read-only. Uses the Pilot ADMIN panel of one row of the app_apis Pilot
# Connections table (the same rows vehicle_upload_api uses): find the vehicle by
# IMEI, then read the admin panel's own WASL vehicle list (backend/app/wasl.php,
# cmd=get_vehicles).
def connection_rows():
    rows = list(frappe.get_doc("app_apis").get("pilot_connections") or [])
    return sorted(rows, key=lambda r: frappe.utils.cint(r.get("idx")))


def connection_key(n):
    return "pilot_wsl" if n == 1 else "pilot" + str(n)


def pick_connection(key):
    """-> {"no": row number, "row": row} of the connection named by `key`, or of the first one using
    WASL; {"no": 0, "row": None} when there is none."""
    rows = connection_rows()
    for i in range(len(rows)):
        n = i + 1
        if frappe.utils.cint(rows[i].get("uses_wasl")) and (not key or key == connection_key(n)):
            return {"no": n, "row": rows[i]}
    return {"no": 0, "row": None}


def adm_cfg(row):
    base = s(row.get("base_url")).rstrip("/")
    for suffix in ("/api/v3", "/api/api.php", "/api"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    return {"base": base + "/backend/",
            "user": s(row.get("username")),
            "password": row.get_password("password", raise_exception=False)}


def as_json(ans):
    if ans is None or isinstance(ans, dict) or isinstance(ans, list):
        return ans
    try:
        return json.loads(ans)
    except Exception:
        return {"_text": str(ans)[:600]}


def adm_token(cfg):
    url = cfg["base"] + "login.php"
    who = as_json(frappe.make_post_request(url, data={
        "cmd": "get_user_without_auth", "username": cfg["user"], "password": cfg["password"]}))
    if not isinstance(who, dict):
        who = {}
    form = {"cmd": "login", "time_zone": "0", "username": cfg["user"],
            "password": cfg["password"], "email": str(who.get("email") or cfg["user"])}
    if not who.get("isadmin") and (who.get("ispartner") or who.get("adm_user_id")) and who.get("id"):
        form["partner_id"] = str(who.get("id"))
    ans = as_json(frappe.make_post_request(url, data=form))
    if not isinstance(ans, dict):
        return ""
    return str(ans.get("jwt_token") or "")


def adm_get(cfg, tok, path, params):
    try:
        return as_json(frappe.make_get_request(cfg["base"] + path,
                       headers={"Authorization": "Bearer " + tok}, params=params))
    except Exception as e:
        return {"_error": str(e)[:300]}


def wasl_status(doc, debug, row):
    imei = s(doc.get("device_serial"))
    res = {"imei": imei, "state": "unknown", "detail": ""}
    if not imei:
        res["detail"] = "no IMEI on this vehicle"
        return res
    cfg = adm_cfg(row)
    tok = adm_token(cfg)
    if not tok:
        res["detail"] = "could not sign in to the Pilot admin panel"
        return res
    found = adm_get(cfg, tok, "app/fitter.php", {"cmd": "get_agents", "query": imei,
                                                   "page": 1, "start": 0, "limit": 25})
    row = {}
    for r in (found.get("data") or []) if isinstance(found, dict) else []:
        if isinstance(r, dict) and s(r.get("uniqid")) == imei:
            row = r
    if not row:
        res["state"] = "not_on_pilot"
        res["detail"] = "this IMEI is not on Pilot"
        return res
    agent_id = s(row.get("agent_id"))
    res["agent_id"] = agent_id
    res["pilot_name"] = s(row.get("vehiclenumber"))
    res["account_id"] = s(row.get("account_id"))
    # The admin grid filters like Ext: [{"property", "value", "operator": "eq"}].
    # query=<agent_id> is the fallback (it searches loosely, so rows are re-checked).
    got = None
    for t in ({"filter": json.dumps([{"property": "object_id", "value": agent_id, "operator": "eq"}])},
              {"query": agent_id}):
        p = {"cmd": "get_vehicles", "page": 1, "start": 0, "limit": 50}
        for k in t.keys():
            p[k] = t[k]
        g = adm_get(cfg, tok, "app/wasl.php", p)
        if isinstance(g, dict) and isinstance(g.get("data"), list):
            got = g
            if [r for r in g["data"] if isinstance(r, dict) and s(r.get("object_id")) == agent_id]:
                break
    if got is None:
        res["detail"] = "the admin panel WASL list did not answer"
        return res
    if debug:
        res["row_keys"] = list(row.keys())
        res["raw"] = [r for r in (got.get("data") or []) if isinstance(r, dict) and s(r.get("object_id")) == agent_id]
    rows = (got.get("data") or got.get("items") or []) if isinstance(got, dict) else (got or [])
    mine = [r for r in rows if isinstance(r, dict) and s(r.get("object_id")) == agent_id]
    res["checked_rows"] = len(rows)
    if mine:
        w = mine[-1]
        res["wasl_status"] = s(w.get("status"))
        res["referencekey"] = s(w.get("referencekey"))
        res["ts"] = s(w.get("ts"))
        res["object_json"] = s(w.get("object_json"))
        # status 1 + a WASL reference key is what Pilot itself treats as registered
        # (it locks the WASL form in that state).
        if s(w.get("referencekey")) and s(w.get("status")) == "1":
            res["state"] = "linked"
        elif s(w.get("referencekey")):
            res["state"] = "registered_inactive"
        else:
            res["state"] = "saved_not_registered"
    else:
        res["state"] = "not_linked"
    return res



picked = pick_connection(str(args.get("connection") or "").strip())
con_no = picked["no"]
con_row = picked["row"]
out["connection"] = connection_key(con_no) if con_no else ""
out["account"] = con_no
if con_row is not None:
    out["connection_label"] = s(con_row.get("label")) or ("Pilot " + str(con_no))

if not allowed():
    out["error"] = "Not permitted"
elif not vehicle_name or not frappe.db.exists(VEH_DT, vehicle_name):
    out["error"] = "Customer Vehicle not found: " + vehicle_name
elif con_row is None:
    out["error"] = ("No Pilot connection uses WASL" + (" under the name " + str(args.get("connection")) if args.get("connection") else "") +
                    ". Tick Uses WASL on its row in app_apis > Pilot Connections.")
else:
    doc = frappe.get_doc(VEH_DT, vehicle_name)
    if action == "plan":
        out["ok"] = True
        out["form"] = plan(doc)
    elif action == "link":
        try:
            form = json.loads(args.get("form") or "{}")
        except Exception:
            form = {}
        gaps = validate(form)
        out["gaps"] = gaps
        out["payload"] = pilot_payload(form)
        if gaps:
            out["msg"] = "Missing or invalid: " + ", ".join(gaps)
        elif not WASL_LIVE:
            out["ok"] = True
            out["dry_run"] = True
            out["msg"] = ("Checked. Nothing was sent -- WASL_LIVE is 0 in the Server Script "
                          "wasl_link_api.")
        else:
            sess = wasl_session(doc)
            out["msg"] = "Not sent: " + sess["why"]
    elif action == "status":
        out["ok"] = True
        out["status"] = wasl_status(doc, frappe.utils.cint(args.get("debug")), con_row)
    else:
        out["error"] = "Unknown action: " + action

frappe.response["message"] = out