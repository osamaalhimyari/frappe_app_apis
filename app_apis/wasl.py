"""
WASL linking for Customer Vehicle -- custom app module.

Put this file in any custom Frappe app, e.g.  apps/<your_app>/<your_app>/wasl.py
then:  bench --site erb.im2m.ws migrate   (or just restart)  ->  method path:
    <your_app>.wasl.link_vehicle

Why a custom app and not a Server Script: Pilot's WASL form only works inside a
signed-in *user* session, and that session lives in a cookie. Server Scripts
cannot keep cookies between calls; a requests.Session here can.

Flow (all on Pilot, admin account 1 from the app_apis settings):
  1. admin panel login            <admin>/backend/login.php        -> JWT
  2. find the vehicle by IMEI     backend/app/fitter.php get_agents -> agent_id, account_id
  3. owner user + login link      backend/app/accounts.php getUsers / getUsersFormAuth
  4. "log in as account"          <user site>/ax/user/login.php    -> session cookie
  5. save the WASL form           <user site>/ax/mod/wasl/wasl.php?cmd=vehiclesave&agentid=..
  6. register with WASL           <user site>/ax/mod/wasl/wasl.php  cmd=vehicleregister
Steps 5-6 are the same calls Pilot's own WASL window makes (Save, then Register).
Nothing is logged that contains a password or token.
"""

import json

import frappe
import requests

ROLES = ("System Manager", "Technical")
VEH_DT = "Customer Vehicle"
TIMEOUT = 60
REGISTER_TIMEOUT = 600   # Pilot's own UI waits up to 10 minutes for WASL

PLATE_TYPES = {str(i) for i in range(1, 12)}
TELECOMS = {"BEYOND ONE", "BULL", "eSIM", "FREEDOM", "GO", "LEBARA", "MOBILY",
            "RED", "SALAM", "STC", "ZAIN"}
SOURCES = {"RS", "ONE_WIRE", "BLUETOOTH"}


def _s(v):
    return str(v or "").strip()


def _check_roles():
    if frappe.session.user == "Administrator":
        return
    if not set(frappe.get_roles()) & set(ROLES):
        frappe.throw("Not permitted", frappe.PermissionError)


def _plain(v):
    """Pilot's msg is HTML ("<b>Result:</b> Success<br>Result Code: ..."); the
    dialog escapes it, so turn it into one plain line."""
    t = str(v or "").replace("<br>", " · ").replace("<br/>", " · ").replace("<br />", " · ")
    return " ".join(frappe.utils.strip_html_tags(t).split())


def _json(resp):
    try:
        return resp.json()
    except Exception:
        return {"_text": (resp.text or "")[:300]}


# ---------------------------------------------------------------- admin panel
def _admin(sess):
    st = frappe.get_doc("app_apis")
    base = _s(st.get("pilot_admin_base_url")).rstrip("/") + "/backend/"
    user = _s(st.get("pilot_admin_username"))
    pwd = st.get_password("pilot_admin_password", raise_exception=False)
    if not (base.startswith("http") and user and pwd):
        frappe.throw("Pilot admin account 1 is not configured in app_apis.")

    who = _json(sess.post(base + "login.php", timeout=TIMEOUT, data={
        "cmd": "get_user_without_auth", "username": user, "password": pwd}))
    if not isinstance(who, dict):
        who = {}
    form = {"cmd": "login", "time_zone": "0", "username": user, "password": pwd,
            "email": _s(who.get("email")) or user}
    if not who.get("isadmin") and (who.get("ispartner") or who.get("adm_user_id")) and who.get("id"):
        form["partner_id"] = str(who.get("id"))
    ans = _json(sess.post(base + "login.php", data=form, timeout=TIMEOUT))
    tok = _s(ans.get("jwt_token")) if isinstance(ans, dict) else ""
    if not tok:
        frappe.throw("Could not sign in to the Pilot admin panel.")
    return {"base": base, "hdr": {"Authorization": "Bearer " + tok}}


def _adm_get(sess, adm, path, params):
    r = sess.get(adm["base"] + path, headers=adm["hdr"], params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return _json(r)


def _find_agent(sess, adm, imei):
    got = _adm_get(sess, adm, "app/fitter.php", {"cmd": "get_agents", "query": imei,
                                                 "page": 1, "start": 0, "limit": 25})
    for r in (got.get("data") or []) if isinstance(got, dict) else []:
        if isinstance(r, dict) and _s(r.get("uniqid")) == imei:
            return r
    frappe.throw("IMEI " + imei + " is not on Pilot -- upload the vehicle first.")


def _user_session(sess, adm, account_id):
    """The admin panel's own 'log in as this account' (Users grid double-click)."""
    users = _adm_get(sess, adm, "app/accounts.php", {"cmd": "getUsers", "account_id": account_id,
                                                      "page": 1, "start": 0, "limit": 50})
    login = ""
    for u in (users.get("data") or []) if isinstance(users, dict) else []:
        if isinstance(u, dict) and frappe.utils.cint(u.get("status")) != 0:
            if u.get("is_owner_user") or not login:
                login = _s(u.get("login"))
    if not login:
        frappe.throw("Pilot account " + str(account_id) + " has no active user.")

    host = adm["base"].split("//", 1)[1].split("/", 1)[0]
    auth = _adm_get(sess, adm, "app/accounts.php", {"cmd": "getUsersFormAuth",
                                                     "account_id": account_id,
                                                     "host": host, "protocol": "https:"})
    url = _s(auth.get("url")) if isinstance(auth, dict) else ""
    if not url:
        frappe.throw("The admin panel gave no login link for account " + str(account_id) + ".")
    r = sess.post(url, timeout=TIMEOUT, data={
        "username": login, "role": _s(auth.get("role")), "id": _s(auth.get("id")),
        "token": _s(auth.get("token")), "sec_token": _s(auth.get("sec_token"))})
    r.raise_for_status()
    # .../ax/user/login.php -> site root
    return url.split("ax/user/login.php")[0]


# ---------------------------------------------------------------- form
def _validate(f):
    gaps = []
    if _s(f.get("plateType")) not in PLATE_TYPES:
        gaps.append("Plate Type")
    for k, label in (("plate_number", "Plate Number"), ("left_letter", "Left Letter"),
                     ("middle_letter", "Middle Letter"), ("right_letter", "Right Letter"),
                     ("sequenceNumber", "Sequence Number"), ("imeiNumber", "Imei Number"),
                     ("deviceBrand", "Tracking device Brand"),
                     ("deviceModel", "Tracking device Model")):
        if not _s(f.get(k)):
            gaps.append(label)
    if _s(f.get("telecomCompanyName")) not in TELECOMS:
        gaps.append("Telecom service provider")
    sim = _s(f.get("simNumber")).replace(" ", "")
    if not (sim.startswith("966") and sim.isdigit() and 4 <= len(sim) <= 18):
        gaps.append("Data SIM number")
    for flag, src, label in (("hasTemperatureSensor", "temperatureSource", "Temperature source"),
                             ("hasHumiditySensor", "humiditySource", "Humidity source")):
        if frappe.utils.cint(f.get(flag)) and _s(f.get(src)) not in SOURCES:
            gaps.append(label)
    if _s(f.get("weightSource")) not in ("ANALOG", "CAN"):
        gaps.append("Weight source")
    for k in ("weightInKilogramsEmptyVehicle", "mlVoltageEmptyVehicle",
              "weightInKilogramsFullLoad", "mlVoltageFullLoad"):
        if _s(f.get(k)) == "":
            gaps.append(k)
    return gaps


def _payload(f):
    p = {
        "plateType": _s(f.get("plateType")),
        "vehiclePlate[number]": _s(f.get("plate_number")),
        "vehiclePlate[leftLetter]": _s(f.get("left_letter")),
        "vehiclePlate[middleLetter]": _s(f.get("middle_letter")),
        "vehiclePlate[rightLetter]": _s(f.get("right_letter")),
        "sequenceNumber": _s(f.get("sequenceNumber")),
        "imeiNumber": _s(f.get("imeiNumber")),
        "trackingDevice[telecomCompanyName]": _s(f.get("telecomCompanyName")),
        "trackingDevice[simNumber]": _s(f.get("simNumber")).replace(" ", ""),
        "trackingDevice[deviceBrand]": _s(f.get("deviceBrand")),
        "trackingDevice[deviceModel]": _s(f.get("deviceModel")),
        "trackingDevice[hasTemperatureSensor]": frappe.utils.cint(f.get("hasTemperatureSensor")),
        "trackingDevice[hasHumiditySensor]": frappe.utils.cint(f.get("hasHumiditySensor")),
        "trackingDevice[weightSource]": _s(f.get("weightSource")),
        "trackingDevice[weightInKilogramsEmptyVehicle]": _s(f.get("weightInKilogramsEmptyVehicle")),
        "trackingDevice[mlVoltageEmptyVehicle]": _s(f.get("mlVoltageEmptyVehicle")),
        "trackingDevice[weightInKilogramsHalfLoad]": _s(f.get("weightInKilogramsHalfLoad")),
        "trackingDevice[mlVoltageHalfLoad]": _s(f.get("mlVoltageHalfLoad")),
        "trackingDevice[weightInKilogramsFullLoad]": _s(f.get("weightInKilogramsFullLoad")),
        "trackingDevice[mlVoltageFullLoad]": _s(f.get("mlVoltageFullLoad")),
    }
    if p["trackingDevice[hasTemperatureSensor]"]:
        p["trackingDevice[temperatureSource]"] = _s(f.get("temperatureSource"))
    if p["trackingDevice[hasHumiditySensor]"]:
        p["trackingDevice[humiditySource]"] = _s(f.get("humiditySource"))
    if p["trackingDevice[weightSource]"] == "ANALOG":
        p["trackingDevice[weightSensorAnalogNumber]"] = _s(f.get("weightSensorAnalogNumber"))
    return p


# ---------------------------------------------------------------- API
@frappe.whitelist()
def link_vehicle(vehicle, form, register=1):
    """Save the WASL form on Pilot, then (register=1) register it with WASL."""
    _check_roles()
    doc = frappe.get_doc(VEH_DT, vehicle)
    f = json.loads(form) if isinstance(form, str) else (form or {})
    imei = _s(doc.device_serial)
    if _s(f.get("imeiNumber")) != imei:
        frappe.throw("The IMEI in the form does not match this vehicle's Device Serial.")
    gaps = _validate(f)
    if gaps:
        return {"ok": False, "step": "validate", "msg": "Missing or invalid: " + ", ".join(gaps)}

    out = {"ok": False, "imei": imei}
    with requests.Session() as sess:
        adm = _admin(sess)
        row = _find_agent(sess, adm, imei)
        agent_id = _s(row.get("agent_id"))
        out["agent_id"] = agent_id
        site = _user_session(sess, adm, _s(row.get("account_id")))
        url = site + "ax/mod/wasl/wasl.php"

        # 5. Save Form (Pilot posts it as an Ext form submit)
        r = sess.post(url, params={"cmd": "vehiclesave", "agentid": agent_id},
                      data=_payload(f), timeout=TIMEOUT)
        saved = _json(r)
        if r.status_code != 200 or not (isinstance(saved, dict) and saved.get("success")):
            out["step"] = "save"
            out["msg"] = "Pilot did not save the WASL form: " + _plain(
                saved.get("msg") if isinstance(saved, dict) else r.text)[:300]
            return out
        out["saved"] = True
        if not frappe.utils.cint(register):
            out["ok"] = True
            out["step"] = "save"
            out["msg"] = "WASL form saved on Pilot (not registered)."
            return out

        # 6. Register
        r = sess.post(url, data={"cmd": "vehicleregister", "agentid": agent_id},
                      timeout=REGISTER_TIMEOUT)
        reg = _json(r)
        out["step"] = "register"
        out["ok"] = bool(isinstance(reg, dict) and reg.get("result"))
        out["msg"] = _plain(reg.get("msg") if isinstance(reg, dict) else r.text)[:500] or (
            "Registered with WASL." if out["ok"] else "WASL did not confirm the registration.")
        # WASL answers "Result Code: duplicate" (HTTP 400) when the vehicle is
        # already registered; Pilot still reports result=true.
        out["duplicate"] = "duplicate" in out["msg"].lower()
        if out["duplicate"]:
            out["msg"] = "Already registered in WASL (WASL replied: duplicate). " \
                         "Use WASL > Check status to confirm the reference key. -- " + out["msg"]

    state = ("already registered" if out.get("duplicate") else "registered") if out["ok"] else "failed"
    frappe.get_doc({"doctype": "Comment", "comment_type": "Info",
                    "reference_doctype": VEH_DT, "reference_name": vehicle,
                    "content": "WASL link: " + state + " -- " + frappe.utils.escape_html(out["msg"])
                    }).insert(ignore_permissions=True)
    return out


# ---------------------------------------------------------------- delete from WASL only
DELETE_WORD = "DELETE"


def _wasl_call(sess, url, cmd, agent_id, timeout):
    """One wasl.php command as the Pilot window sends it. -> (parsed json or {}, http status)."""
    r = sess.post(url, data={"cmd": cmd, "agentid": agent_id}, timeout=timeout)
    body = _json(r)
    return (body if isinstance(body, dict) else {}), r.status_code


@frappe.whitelist(methods=["POST"])
def wasl_delete(vehicle, confirm="", dry_run=1):
    """Delete a vehicle from the WASL database ONLY. The Pilot vehicle is not touched.

    It is Pilot's own WASL window > Delete ("Delete vehicle from wasl database"):
    wasl.php cmd=vehicledelete&agentid=<id>, inside a "log in as account" session, so the
    Pilot vehicle has to exist for WASL to be reachable. Open to any signed-in user; the
    typed word is the guard.

    dry_run=1 (default) only looks: it reports whether the vehicle is on Pilot and asks WASL's
    own Inquiry (cmd=vehiclecheck) what it knows. dry_run=0 needs confirm=DELETE.

    -> {state: found|missing|unknown, found, deleted, agent_id, detail, wasl_status, msg}
    """
    if frappe.session.user == "Guest":
        frappe.throw("Sign in first.", frappe.PermissionError)
    dry = frappe.utils.cint(dry_run)
    if not dry and _s(confirm) != DELETE_WORD:
        frappe.throw("Not deleted: type " + DELETE_WORD + " to confirm.")
    doc = frappe.get_doc(VEH_DT, vehicle)
    imei = _s(doc.device_serial)
    out = {"state": "unknown", "found": False, "deleted": False, "imei": imei, "agent_id": "",
           "detail": "", "wasl_status": "", "msg": ""}
    if not imei:
        out["detail"] = "the vehicle has no Device Serial, so there is nothing to look for"
        return out

    with requests.Session() as sess:
        try:
            adm = _admin(sess)
            row = _find_agent(sess, adm, imei)
        except Exception as e:
            text = _plain(str(e))[:300]
            out["state"] = "missing" if "not on Pilot" in text else "unknown"
            out["detail"] = ("not on Pilot, and WASL can only be reached through the Pilot vehicle -- " + text
                             if out["state"] == "missing" else text)
            return out
        out["agent_id"] = _s(row.get("agent_id"))
        try:
            site = _user_session(sess, adm, _s(row.get("account_id")))
        except Exception as e:
            out["detail"] = "could not open the Pilot account session -- " + _plain(str(e))[:300]
            return out
        url = site + "ax/mod/wasl/wasl.php"

        chk, status = _wasl_call(sess, url, "vehiclecheck", out["agent_id"], REGISTER_TIMEOUT)
        out["wasl_status"] = _plain(chk.get("msg"))[:500] or ("HTTP " + str(status))
        out["state"] = "found"
        out["found"] = True
        out["detail"] = "Pilot vehicle " + _s(row.get("vehiclenumber")) + " (agent " + out["agent_id"] + \
                        "). WASL inquiry: " + out["wasl_status"]
        if dry:
            return out

        res, status = _wasl_call(sess, url, "vehicledelete", out["agent_id"], REGISTER_TIMEOUT)
        out["deleted"] = bool(res.get("result"))
        out["msg"] = _plain(res.get("msg"))[:500] or ("HTTP " + str(status))
        after, _status = _wasl_call(sess, url, "vehiclecheck", out["agent_id"], REGISTER_TIMEOUT)
        out["after_status"] = _plain(after.get("msg"))[:500]

    frappe.get_doc({"doctype": "Comment", "comment_type": "Info",
                    "reference_doctype": VEH_DT, "reference_name": vehicle,
                    "content": "WASL delete (Pilot vehicle kept): " + ("done" if out["deleted"] else "failed") +
                               " -- " + frappe.utils.escape_html(out["msg"])}).insert(ignore_permissions=True)
    return out


# ---------------------------------------------------------------- the stored WASL snapshot
# Pilot's admin panel lists every vehicle's WASL registration in one paged call (app/wasl.php
# get_vehicles). This copies it into "App Apis WASL State" once an hour, keyed by IMEI, so the Customer
# Vehicle form can say "linked to WASL or not" without a live call per open.
WASL_STATE_DT = "App Apis WASL State"
WASL_PAGE = 5000                # the panel refuses more than about 5,000 rows per request (HTTP 500)
WASL_MAX_PASSES = 10            # a read gives up after this many passes over the list
STATE_LINKED = "linked"
STATE_REGISTERED_INACTIVE = "registered_inactive"
STATE_SAVED = "saved_not_registered"


def wasl_state_of(row: dict) -> str:
    """linked = status 1 with a WASL reference key (what Pilot itself treats as registered)."""
    key = _s(row.get("referencekey"))
    if key and _s(row.get("status")) == "1":
        return STATE_LINKED
    if key:
        return STATE_REGISTERED_INACTIVE
    return STATE_SAVED


def wasl_imei_of(row: dict) -> str:
    """The IMEI is inside the row's object_json ({"imeiNumber": ...})."""
    raw = row.get("object_json")
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return ""
    return _s(data.get("imeiNumber")) if isinstance(data, dict) else ""


def _epoch_to_datetime(ts):
    import datetime

    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    return datetime.datetime.fromtimestamp(n) if n > 0 else None


def read_wasl_list(sess, adm) -> tuple:
    """Every vehicle row on Pilot's WASL list -> ({imei: row}, complete).

    The panel pages this list with no stable order, so one pass over it can repeat some rows and skip
    others (measured: about 2% skipped per pass). Passes are therefore repeated, merged by row id, until
    every id the list reports (`totalCount`) has been seen. `complete` says whether that happened; a
    caller must not treat a missing vehicle as "gone" unless it did. When an IMEI appears on more than
    one row the newest row (highest id) wins, which is the one Pilot's own window shows."""
    by_id = {}
    total = None
    complete = False
    for _pass in range(WASL_MAX_PASSES):
        page = 0
        while True:
            page += 1
            got = _adm_get(sess, adm, "app/wasl.php", {"cmd": "get_vehicles", "page": page,
                                                        "start": (page - 1) * WASL_PAGE, "limit": WASL_PAGE})
            rows = (got.get("data") or []) if isinstance(got, dict) else []
            if isinstance(got, dict) and got.get("totalCount") is not None:
                total = int(got.get("totalCount"))
            for r in rows:
                if isinstance(r, dict) and r.get("id") is not None:
                    by_id[r["id"]] = r
            if len(rows) < WASL_PAGE:
                break
        if total is not None and len(by_id) >= total:
            complete = True
            break

    out = {}
    for r in by_id.values():
        if _s(r.get("object_type")) not in ("vehicle", ""):
            continue
        imei = wasl_imei_of(r)
        if not imei:
            continue
        old = out.get(imei)
        if old is None or int(r.get("id") or 0) >= int(old.get("id") or 0):
            out[imei] = r
    return out, complete


def _upsert_states(rows: dict, now) -> None:
    cols = ("name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "imei", "state",
            "wasl_status", "referencekey", "wasl_ts", "agent_id", "account_id", "synced_at")
    updates = ", ".join("`%s`=VALUES(`%s`)" % (c, c)
                        for c in ("modified", "state", "wasl_status", "referencekey", "wasl_ts", "agent_id",
                                  "account_id", "synced_at"))
    items = list(rows.items())
    for i in range(0, len(items), 500):
        chunk = items[i : i + 500]
        values = []
        for imei, r in chunk:
            values.extend([imei, now, now, "Administrator", "Administrator", 0, 0, imei, wasl_state_of(r),
                           _s(r.get("status")), _s(r.get("referencekey")) or None,
                           _epoch_to_datetime(r.get("ts")), _s(r.get("object_id")), _s(r.get("account_id")), now])
        marks = ", ".join(["(" + ", ".join(["%s"] * len(cols)) + ")"] * len(chunk))
        frappe.db.sql("insert into `tab%s` (%s) values %s on duplicate key update %s" % (
            WASL_STATE_DT, ", ".join("`%s`" % c for c in cols), marks, updates), values)


@frappe.whitelist()
def sync_wasl_states() -> dict:
    """Copy Pilot's WASL list into App Apis WASL State. Hourly (Server Script "App Apis - WASL Status Sync").

    Vehicles are added and updated from every read. A vehicle is REMOVED only after a read that saw the
    whole list (see read_wasl_list), so a flaky page can never make linked vehicles look unlinked."""
    frappe.only_for(("System Manager", "Technical"))
    started = frappe.utils.now_datetime()
    out = {"ok": False, "rows": 0, "removed": 0, "complete": False, "note": ""}
    try:
        with requests.Session() as sess:
            adm = _admin(sess)
            rows, complete = read_wasl_list(sess, adm)
    except Exception as e:
        out["note"] = "WASL status sync failed: " + _plain(str(e))[:300]
        frappe.log_error(title="App Apis WASL Status Sync", message=str(e)[:1000])
        return out
    _upsert_states(rows, started)
    out["rows"] = len(rows)
    out["complete"] = complete
    if complete:
        out["removed"] = frappe.db.sql(
            "select count(*) from `tab%s` where synced_at < %%s" % WASL_STATE_DT, started)[0][0]
        frappe.db.sql("delete from `tab%s` where synced_at < %%s" % WASL_STATE_DT, started)
    else:
        out["note"] = ("the list could not be read completely after %d passes; rows were added/updated, "
                       "none removed" % WASL_MAX_PASSES)
    out["ok"] = True
    frappe.db.commit()
    return out
