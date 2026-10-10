# =============================================================================
# Lebara API
# -----------------------------------------------------------------------------
# Server Script -- Script Type: API, API Method: lebara
#
# Every Lebara B2B call goes through this one script. Lebara has no public API:
# these are the requests the portal's own web UI makes (https://b2b.lebara.sa,
# Serenity / ASP.NET MVC), replayed inside a logged-in session. The session
# cookies live in the app's encrypted cookie jar "lebara", so a login that
# needed an SMS OTP once keeps working while "Lebara Keepalive" keeps it warm.
#
# Call it from the desk / a Client Script:
#     frappe.call("lebara", {action: "find_sim", msisdn: "830033599574"})
#       -> r.message.result
# or from another Server Script:
#     frappe.call("lebara", action="ping").result
#
# ACTIONS (args besides `action`)
#   status                                   settings + session state, no HTTP
#   request_otp                              step 0+1: Lebara SMSes a 4-digit code
#   submit_otp       code                    step 2: finishes the login
#   ping                                     cheap call; marks the session Expired if it is
#   logout
#   find_sim         msisdn | iccid | imsi | id
#   sim_full         msisdn                  list record + live status, profile, location
#   sim_ajax         id, name                AjaxBssStatus | AjaxLiveProfile | AjaxCharts | AjaxLocation
#   list_sims        status (opt), page (opt, 5000)   every SIM; ~7 s per 5000
#   sims_page        skip, take (100), status (opt), q (opt: part of MSISDN/ICCID/IMSI)
#                                            one page for a screen -> {total, rows}
#   suspend / resume / activate   id         activate is untested on Lebara's side
#   send_sms         id, msisdn, message, sender (opt, "0" = Lebara)
#   sms_for_sim      msisdn, take (opt), only_replies (opt)
#   sim_history      msisdn                  every SMS to and from one SIM, newest first
#   sent_check       msisdn, after_id, message   has the SMS just sent reached Lebara's list yet?
#                                            -> {sent: row|None, reply: row|None}
#   sync_sims                                every SIM into the "Lebara SIM" list (hourly job;
#                                            runs app_apis.core.lebara.sync_sims)
#   sync_now                                 sync_sims in the background, for a button
#   latest_sms       take (opt, 50)
#   replies_after    msisdn, after_id        device replies newer than a sent SMS Id
#   list_sms         since_id (opt, 0)       incremental SMS sync
#
# Only System Managers may call it (the core HTTP functions enforce it too).
# =============================================================================

SETTINGS = "app_apis"
JAR = "lebara"
HTTP = "app_apis.core.http.request"
FORM = "app_apis.core.http.form_post"
CLEAR = "app_apis.core.http.clear_jar"

SIM_COLS = ["Id", "ICCID", "Msisdn", "IMSI", "SubscriberStatusM2M1", "GroupName", "SubCustomerName",
            "ActivationDate", "LastConnectionDate", "ThisMonthUsage", "LastMonthUsage", "InDataSession", "IMEI"]
SMS_COLS = ["Id", "FromMsisdn", "ToMsisdn", "Message", "MessageType", "SmsDate", "SmsStatus",
            "SendResult", "SentByUserName"]
SIM_STATUS = {1: "Idle", 2: "Active", 3: "Bar", 4: "Suspend", 9: "Deactivated"}
SMS_STATUS = {0: "Pending", 1: "Under Processing", 2: "Success", 3: "Failed", 5: "Exceed Limit",
              7: "Rejected By Lebara"}

cint = frappe.utils.cint
args = frappe.form_dict


def is_manager():
    if frappe.session.user == "Administrator":
        return True
    return bool(frappe.db.exists("Has Role", {"parent": frappe.session.user, "role": "System Manager",
                                               "parenttype": "User"}))


def setting(field):
    return frappe.db.get_single_value(SETTINGS, field)


def base():
    return str(setting("lebara_base_url") or "https://b2b.lebara.sa").rstrip("/")


def set_state(status, error=None):
    values = {"lebara_session_status": status}
    if status == "Logged In":
        values["lebara_last_refresh"] = frappe.utils.now_datetime()
        values["lebara_last_error"] = ""
    if error is not None:
        values["lebara_last_error"] = str(error)[:1000]
    for k in values:
        frappe.db.set_value(SETTINGS, SETTINGS, k, values[k], update_modified=False)
    # Committed at once: the frappe.throw that usually follows would otherwise
    # roll the new state back.
    frappe.db.commit()


def notify_expired(reason):
    user = setting("lebara_notify_user")
    if not user:
        return
    frappe.get_doc({
        "doctype": "Notification Log",
        "for_user": user,
        "type": "Alert",
        "document_type": SETTINGS,
        "document_name": SETTINGS,
        "subject": "Lebara session expired -- request a new OTP from App Apis > Lebara",
        "email_content": str(reason or "")[:500],
    }).insert(ignore_permissions=True)


def looks_logged_out(res):
    """The portal answers an expired session with its login page or a
    redirect to it instead of JSON."""
    if not res:
        return True
    if "/Account/Login" in str(res.get("url") or ""):
        return True
    if res.get("status") in [401, 403]:
        return True
    # Serenity services answer a dead session with JSON, not a redirect:
    # HTTP 400 {"Error": {"Code": "NotLoggedIn", ...}}
    data = res.get("json")
    if isinstance(data, dict) and (data.get("Error") or {}).get("Code") == "NotLoggedIn":
        return True
    if res.get("json") is None and "Account/Login" in str(res.get("text") or "")[:20000]:
        return True
    return False


def expired(res):
    # Only flip Logged In -> Expired once, so the alert is sent once.
    if setting("lebara_session_status") == "Logged In":
        set_state("Expired", "Session expired at %s (%s)" % (frappe.utils.now(), res.get("url") if res else ""))
        notify_expired(res.get("url") if res else "")
        frappe.db.commit()
    frappe.throw("Lebara session expired. Request a new OTP from App Apis > Lebara.")


def refresh_csrf():
    """Load a portal page. Lebara hands out a NEW CSRF-TOKEN cookie once the
    session is logged in, and only with a page load; every service call made
    with the pre-login token answers HTTP 500 {"Error": {"Code": "Exception"}}."""
    frappe.call(HTTP, jar=JAR, url=base() + "/Main/Subscriber", method="GET", timeout=60)


def service_post(path, body):
    return frappe.call(HTTP, jar=JAR, url=base() + path, method="POST", body=body,
                       headers={"X-Requested-With": "XMLHttpRequest"},
                       csrf_cookie="CSRF-TOKEN", timeout=180)


def generic_error(res):
    data = res.get("json")
    return isinstance(data, dict) and (data.get("Error") or {}).get("Code") == "Exception"


def service(path, body):
    """Serenity JSON service: POST JSON with the CSRF-TOKEN cookie as header.
    A stale CSRF token gets one page load and one retry."""
    res = service_post(path, body)
    if generic_error(res) and not looks_logged_out(res):
        refresh_csrf()
        res = service_post(path, body)
    if res.get("error"):
        frappe.throw("Lebara unreachable: %s" % res.get("error"))
    if looks_logged_out(res):
        expired(res)
    data = res.get("json")
    if isinstance(data, dict) and data.get("Error"):
        frappe.throw("Lebara: %s" % (data["Error"].get("Message") or data["Error"].get("Code")))
    if data is None:
        frappe.throw("Lebara returned HTTP %s with no JSON" % res.get("status"))
    return data


def ajax(sub_id, name):
    if name not in ["AjaxBssStatus", "AjaxLiveProfile", "AjaxCharts", "AjaxLocation"]:
        frappe.throw("Unknown SIM detail call: %s" % name)
    res = frappe.call(HTTP, jar=JAR, url="%s/SimDetail/%s?id=%s" % (base(), name, cint(sub_id)),
                      method="POST", headers={"X-Requested-With": "XMLHttpRequest"})
    if looks_logged_out(res):
        expired(res)
    return res.get("json") or {}


def login_post(body, otp):
    headers = {"X-Requested-With": "XMLHttpRequest"}
    if otp:
        headers["X-OTP-Request"] = "true"
    res = frappe.call(HTTP, jar=JAR, url=base() + "/Account/Login", method="POST", body=body,
                      headers=headers, csrf_cookie="CSRF-TOKEN")
    if res.get("error"):
        frappe.throw("Lebara unreachable: %s" % res.get("error"))
    return res


def credentials():
    doc = frappe.get_doc(SETTINGS)
    user = str(doc.get("lebara_username") or "").strip()
    pwd = doc.get_password("lebara_password", raise_exception=False) if doc.get("lebara_password") else ""
    if not user or not pwd:
        frappe.throw("Set the Lebara Username and Password in App Apis > Lebara first.")
    # A dict, not a pair: the Server Script sandbox has no tuple unpacking.
    return {"user": user, "pwd": pwd}


# ---------------------------------------------------------------- auth
def request_otp():
    cred = credentials()
    user = cred["user"]
    pwd = cred["pwd"]
    frappe.call(CLEAR, jar=JAR)  # a fresh session, so no half-dead cookie survives
    frappe.call(HTTP, jar=JAR, url=base() + "/Account/Login", method="GET")
    res = login_post({"Username": user, "Password": pwd}, False)
    data = res.get("json") or {}
    if res.get("status") == 200 and data == {}:
        refresh_csrf()
        set_state("Logged In")
        return {"logged_in": True, "message": "Logged in without an OTP."}
    err = data.get("Error") or {}
    if err.get("Code") != "TwoFactorAuthenticationRequired":
        set_state("Login Failed", err.get("Message") or ("HTTP %s" % res.get("status")))
        frappe.throw("Lebara login failed: %s" % (err.get("Message") or err.get("Code") or res.get("status")))
    parts = str(err.get("Arguments") or "").rsplit("|", 1)
    if len(parts) != 2:
        frappe.throw("Lebara asked for an OTP but sent no TwoFactorGuid: %s" % err.get("Arguments"))
    frappe.db.set_value(SETTINGS, SETTINGS, "lebara_two_factor_guid", parts[1].strip(), update_modified=False)
    set_state("Waiting for OTP", "")
    hint = parts[0].split("<")[0].strip()
    return {"logged_in": False, "message": hint or "Lebara sent a code by SMS.", "seconds": 60}


def submit_otp(code):
    code = "".join([c for c in str(code or "") if c in "0123456789"])
    if not code:
        frappe.throw("Enter the code Lebara sent by SMS.")
    guid = setting("lebara_two_factor_guid")
    if not guid:
        frappe.throw("No OTP request is pending. Click Request OTP first.")
    cred = credentials()
    user = cred["user"]
    pwd = cred["pwd"]
    res = login_post({"Username": user, "Password": pwd, "TwoFactorGuid": guid,
                      "TwoFactorCode": cint(code)}, True)
    data = res.get("json") or {}
    if res.get("status") != 200 or data.get("Error"):
        msg = (data.get("Error") or {}).get("Message") or ("HTTP %s" % res.get("status"))
        set_state("Login Failed", msg)
        frappe.throw("Lebara rejected the code: %s. Request a new OTP and try again." % msg)
    frappe.db.set_value(SETTINGS, SETTINGS, "lebara_two_factor_guid", "", update_modified=False)
    refresh_csrf()
    set_state("Logged In")
    return {"logged_in": True, "message": data.get("PasswordExpiryMessage") or "Logged in to Lebara."}


def ping():
    """The keepalive: the smallest real service call. Refreshes the sliding
    session and proves it still works."""
    service("/Services/Main/Subscriber/List", {"Take": 1, "Sort": ["Id"], "IncludeColumns": ["Id"]})
    set_state("Logged In")
    return {"logged_in": True, "at": frappe.utils.now()}


def logout():
    frappe.call(HTTP, jar=JAR, url=base() + "/Account/Signout", method="GET")
    frappe.call(CLEAR, jar=JAR)
    set_state("Logged Out", "")
    return {"logged_in": False}


# ---------------------------------------------------------------- SIMs
def with_status(sim):
    sim["StatusText"] = SIM_STATUS.get(sim.get("SubscriberStatusM2M1"), "")
    return sim


def find_sim():
    for pair in [["Msisdn", "msisdn"], ["ICCID", "iccid"], ["IMSI", "imsi"], ["Id", "id"]]:
        field = pair[0]
        value = args.get(pair[1])
        if value:
            value = cint(value) if field == "Id" else str(value).strip()
            res = service("/Services/Main/Subscriber/List", {
                "Take": 1, "IncludeColumns": SIM_COLS + ["IMEI", "Apn", "CellId", "TechnologyUsed"],
                "Criteria": [[field], "=", value]})
            rows = res.get("Entities") or []
            return with_status(rows[0]) if rows else None
    frappe.throw("Give msisdn, iccid, imsi or id.")


def sim_full():
    sim = find_sim()
    if not sim:
        return None
    sim["live_status"] = ajax(sim["Id"], "AjaxBssStatus")
    sim["live_profile"] = ajax(sim["Id"], "AjaxLiveProfile")
    loc = ajax(sim["Id"], "AjaxLocation")
    sim["location"] = {"lat": loc.get("latitude"), "lng": loc.get("longitude"),
                       "map": loc.get("locLink")} if loc.get("hasMap") else None
    return sim


def list_sims():
    page = cint(args.get("page")) or 5000
    out = []
    skip = 0
    while True:
        body = {"Skip": skip, "Take": page, "Sort": ["Id"], "IncludeColumns": SIM_COLS}
        if args.get("status") not in [None, ""]:
            body["EqualityFilter"] = {"SubscriberStatusM2M1": cint(args.get("status"))}
        res = service("/Services/Main/Subscriber/List", body)
        rows = res.get("Entities") or []
        for e in rows:
            with_status(e)
        out.extend(rows)
        skip = skip + page
        if skip >= cint(res.get("TotalCount")) or not rows:
            return out


def sims_page():
    take = min(cint(args.get("take")) or 100, 1000)
    body = {"Skip": cint(args.get("skip")), "Take": take, "Sort": ["Id"], "IncludeColumns": SIM_COLS}
    if args.get("status") not in [None, ""]:
        body["EqualityFilter"] = {"SubscriberStatusM2M1": cint(args.get("status"))}
    q = "".join([c for c in str(args.get("q") or "") if c in "0123456789"])
    if q:
        like = "%" + q + "%"
        body["Criteria"] = [[[["Msisdn"], "like", like], "or", [["ICCID"], "like", like]], "or",
                            [["IMSI"], "like", like]]
    res = service("/Services/Main/Subscriber/List", body)
    rows = res.get("Entities") or []
    for e in rows:
        with_status(e)
    return {"total": cint(res.get("TotalCount")), "rows": rows}


def sim_history():
    sim = str(args.get("msisdn") or "").strip()
    if not sim:
        frappe.throw("Give the msisdn.")
    crit = [[["FromMsisdn"], "=", sim], "or", [["ToMsisdn"], "=", sim]]
    out = []
    skip = 0
    while True:
        res = service("/Services/Main/SmsMessges/List", {
            "Skip": skip, "Take": 5000, "Sort": ["Id DESC"], "Criteria": crit, "IncludeColumns": SMS_COLS})
        rows = res.get("Entities") or []
        for r in rows:
            r["StatusText"] = SMS_STATUS.get(r.get("SmsStatus"), "")
            r["TypeText"] = "Receive" if r.get("MessageType") == 2 else "Send"
        out.extend(rows)
        skip = skip + 5000
        if skip >= cint(res.get("TotalCount")) or not rows:
            return out


def sim_action(action):
    sub_id = cint(args.get("id"))
    if not sub_id:
        frappe.throw("Give the subscriber id (find_sim -> Id).")
    res = frappe.call(FORM, jar=JAR, url="%s/SimDetail/%s?Id=%s" % (base(), action, sub_id),
                      fields={"Id": sub_id})
    if looks_logged_out(res):
        expired(res)
    messages = [a.get("message") for a in res.get("alerts") or []]
    if not res.get("ok"):
        frappe.log_error(title="Lebara %s %s failed" % (action, sub_id),
                         message="By %s: %s" % (frappe.session.user, "; ".join(messages) or res.get("error")))
    return {"ok": res.get("ok"), "message": "; ".join(messages) or res.get("error") or "",
            "http": res.get("status")}


# ---------------------------------------------------------------- SMS
def sms_query(criteria, take, sort, msg_type):
    body = {"Take": cint(take) or 1, "Sort": [sort], "IncludeColumns": SMS_COLS}
    if criteria:
        body["Criteria"] = criteria
    if msg_type:
        body["EqualityFilter"] = {"MessageType": msg_type}
    rows = service("/Services/Main/SmsMessges/List", body).get("Entities") or []
    for r in rows:
        r["StatusText"] = SMS_STATUS.get(r.get("SmsStatus"), "")
        r["TypeText"] = "Receive" if r.get("MessageType") == 2 else "Send"
    return rows


def send_sms():
    text = str(args.get("message") or "")
    if not text or len(text) > 160:
        frappe.throw("The SMS must be 1-160 characters.")
    sub_id = cint(args.get("id"))
    msisdn = str(args.get("msisdn") or "").strip()
    if not sub_id or not msisdn:
        frappe.throw("Give id and msisdn of the destination SIM.")
    # SendSMS answers {} only -- no message Id. The newest row to this SIM
    # BEFORE sending is the marker: the sent SMS is the first row after it
    # (see sent_check).
    before = sms_query([["ToMsisdn"], "=", msisdn], 1, "Id DESC", None)
    before_id = cint(before[0].get("Id")) if before else 0
    service("/SimDetail/SendSMS", {"Id": sub_id, "Source_MSISDN": str(args.get("sender") or "0"),
                                   "Destination_MSISDN": msisdn, "Message": text})
    return {"ok": True, "before_id": before_id, "message": text}


def sent_check():
    sim = str(args.get("msisdn") or "").strip()
    after = cint(args.get("after_id"))
    text = str(args.get("message") or "").strip()
    rows = sms_query([[["ToMsisdn"], "=", sim], "and", [["Id"], ">", after]], 20, "Id", 1)
    sent = None
    for r in rows:
        if not text or str(r.get("Message") or "").strip() == text:
            sent = r
            break
    reply = None
    if sent:
        got = sms_query([[["FromMsisdn"], "=", sim], "and", [["Id"], ">", cint(sent.get("Id"))]], 1, "Id", 2)
        reply = got[0] if got else None
    return {"sent": sent, "reply": reply}


def sync_sims():
    """The work is app_apis.core.lebara.sync_sims (every SIM, the extra usage / IMEI-date columns, the ERP
    match, a "seen in this sync" stamp). Kept here only so the action name, the hourly job and the
    Sync now button keep working."""
    return frappe.call("app_apis.core.api.lebara_sync", what="sims")


def list_sms():
    out = []
    skip = 0
    since = cint(args.get("since_id"))
    while True:
        res = service("/Services/Main/SmsMessges/List", {
            "Skip": skip, "Take": 5000, "Sort": ["Id"], "Criteria": [["Id"], ">", since],
            "IncludeColumns": SMS_COLS})
        rows = res.get("Entities") or []
        out.extend(rows)
        skip = skip + 5000
        if skip >= cint(res.get("TotalCount")) or not rows:
            return out


# ---------------------------------------------------------------- dispatch
if not is_manager():
    frappe.throw("Only a System Manager can use the Lebara integration.", frappe.PermissionError)

action = str(args.get("action") or "status")
if action != "status" and not cint(setting("lebara_enabled")):
    frappe.throw("Lebara is disabled in App Apis > Lebara.")

if action == "status":
    result = {"enabled": cint(setting("lebara_enabled")), "status": setting("lebara_session_status"),
              "sims_synced_at": str(setting("lebara_sims_synced_at") or "").replace("0001-01-01 00:00:00", ""),
              "sims_count": cint(setting("lebara_sims_count")),
              "sims_sync_note": setting("lebara_sims_sync_note"),
              "last_refresh": str(setting("lebara_last_refresh") or "").replace("0001-01-01 00:00:00", ""),
              "last_error": setting("lebara_last_error"),
              "cookies": frappe.call("app_apis.core.http.jar_info", jar=JAR)}
elif action == "request_otp":
    result = request_otp()
elif action == "submit_otp":
    result = submit_otp(args.get("code"))
elif action == "ping":
    result = ping()
elif action == "logout":
    result = logout()
elif action == "find_sim":
    result = find_sim()
elif action == "sim_full":
    result = sim_full()
elif action == "sim_ajax":
    result = ajax(args.get("id"), args.get("name"))
elif action == "list_sims":
    result = list_sims()
elif action == "sims_page":
    result = sims_page()
elif action == "sim_history":
    result = sim_history()
elif action == "suspend":
    result = sim_action("SuspendSIM")
elif action == "resume":
    result = sim_action("ResumeSIM")
elif action == "activate":
    result = sim_action("ActivateSIM")
elif action == "send_sms":
    result = send_sms()
elif action == "sms_for_sim":
    sim = str(args.get("msisdn") or "").strip()
    result = sms_query([[["FromMsisdn"], "=", sim], "or", [["ToMsisdn"], "=", sim]],
                       args.get("take") or 100, "Id DESC", 2 if cint(args.get("only_replies")) else None)
elif action == "latest_sms":
    result = sms_query([], args.get("take") or 50, "Id DESC", None)
elif action == "replies_after":
    sim = str(args.get("msisdn") or "").strip()
    result = sms_query([[["FromMsisdn"], "=", sim], "and", [["Id"], ">", cint(args.get("after_id"))]],
                       20, "Id", 2)
elif action == "sent_check":
    result = sent_check()
elif action == "sync_sims":
    result = sync_sims()
elif action == "sync_now":
    frappe.enqueue("lebara", queue="long", timeout=1800, action="sync_sims")
    result = {"queued": True}
elif action == "list_sms":
    result = list_sms()
else:
    frappe.throw("Unknown Lebara action: %s" % action)

frappe.flags.result = result
