# =============================================================================
# expired_devices_api  --  Server Script (API), separate from app_apis
# =============================================================================
#
# What the "Expired Subscriptions" HTML block talks to. Two actions:
#
#   action=list    the vehicles whose subscription ran out more than `days`
#                  ago (default: "Days After Expiry" from app_apis), each with
#                  its IMEI, plate, customer, days expired and what Pilot and
#                  IM say about it.
#                  with_pilot=1 also reads Pilot's estate, which is what
#                  carries the agentid and node a block needs. That read takes
#                  around half a minute.
#
#   action=block   block one device on Pilot: cmd=setvehblock&status=0.
#   action=unblock the same with status=1 -- how a mistake is undone.
#
# ONLY INDIVIDUALS: a vehicle is listed when its Customer Type is Individual and Paying is not
# "Company" (vehicle's value, else the customer's). Companies are left out of the list and of the counts.
#
# LIVE = 0 HERE: block and unblock answer with what they would have sent and
# call nothing. Set LIVE = 1 to let the buttons really reach Pilot.
#
# Pilot cannot DELETE a vehicle over its API -- the Administrator API only
# adds, edits, moves and blocks/unblocks -- and IM publishes no method that
# turns a vehicle off at all, so IM is reported, never written.
#
# Install as: Server Script, Script Type "API", API Method expired_devices_api.
# =============================================================================

LIVE = 0                     # 0 = say what would happen, touch nothing
GRACE_DAYS = 0               # added to the app's "Days After Expiry"
ROW_LIMIT = 3000             # rows returned in one list call
ROLES = ("System Manager", "Technical")

SETTINGS = "app_apis"

# Only individuals are listed. A vehicle is a company -- and left out -- when EITHER its Customer
# Type is not Individual OR Paying is "Company" (the vehicle's own value, else the customer's).
# This is the same rule the subscription reminders use (subscription_reminders.is_individual).
INDIVIDUAL_ONLY = (
    "and lower(ifnull(c.customer_type, '')) = 'individual' "
    "and lower(ifnull(nullif(v.paying, ''), ifnull(c.paying, ''))) != 'company' "
)
DNC_DT = "App Apis Do Not Contact"
SCRIPT_NAME = "expired_devices_api"
SCOPES = ("Subscription reminders", "All messages", "All", "")
BACKEND = "/backend/api.php"

args = frappe.form_dict
action = str(args.get("action") or "list").strip().lower()
out = {"ok": False, "action": action}

allowed = str(frappe.session.user) == "Administrator"
for r in frappe.db.sql("select role from `tabHas Role` where parent = %(u)s", {"u": frappe.session.user}, as_dict=True):
    if str(r.role) in ROLES:
        allowed = True

# ---------------------------------------------------------------- settings
raw = {}
for r in frappe.db.sql(
    "select field, value from `tabSingles` where doctype = %(dt)s "
    "and field like 'subscription_reminder%%'",
    {"dt": SETTINGS},
    as_dict=True,
):
    raw[r.field] = str(r.value if r.value is not None else "").strip()

days_after = frappe.utils.cint(raw.get("subscription_reminder_days_after")) \
    if raw.get("subscription_reminder_days_after", "") != "" else 90
default_days = days_after + GRACE_DAYS
days = frappe.utils.cint(args.get("days")) if str(args.get("days") or "").strip() != "" else default_days
if days < 1:
    days = default_days

allowed_types = [t.strip().lower() for t in (raw.get("subscription_reminder_customer_types") or "").split(",") if t.strip()]
all_types = (not allowed_types) or ("all" in allowed_types) or ("*" in allowed_types)


def pilot_settings():
    doc = frappe.get_doc(SETTINGS)
    base = str(doc.get("pilot_admin_base_url") or "").strip().rstrip("/")
    return {
        "url": base + BACKEND,
        "user": str(doc.get("pilot_admin_username") or "").strip(),
        "password": doc.get_password("pilot_admin_password", raise_exception=False),
    }


def past_cutoff(imei):
    """The device must belong to a vehicle that really is past the cutoff."""
    rows = frappe.db.sql(
        "select name, customer, license_plate, subscription_expiry_date, "
        "datediff(curdate(), subscription_expiry_date) as days "
        "from `tabCustomer Vehicle` "
        "where ifnull(device_serial, '') = %(imei)s "
        "and ifnull(subscription_expiry_date, '') != '' "
        "and subscription_expiry_date < date_sub(curdate(), interval %(d)s day) "
        "limit 1",
        {"imei": imei, "d": days},
        as_dict=True,
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------- list
if action == "list":
    vehicles = frappe.db.sql(
        "select v.name, v.customer, v.license_plate, v.e_license_plate, v.plate_num, "
        "v.device_serial as imei, v.subscription_expiry_date as expiry, "
        "datediff(curdate(), v.subscription_expiry_date) as days, v.device_type, "
        "c.customer_name, c.customer_type, "
        "ifnull(nullif(v.paying, ''), ifnull(c.paying, '')) as paying, "
        "a.plate as audit_plate, a.device_model, a.erp_status, a.sim_status, a.sim_msisdn, "
        "a.on_pilot_1, a.on_pilot_2, a.on_im, a.pilot_active, a.im_status, a.audited_at "
        "from `tabCustomer Vehicle` v "
        "left join `tabCustomer` c on c.name = v.customer "
        "left join `tabapp_apis_fleet_audit` a on a.imei = v.device_serial "
        "where v.device_statues = 'Installed' "
        "and ifnull(v.subscription_expiry_date, '') != '' "
        "and ifnull(v.device_serial, '') != '' "
        "and ifnull(v.deletion_date, '') = '' "
        + INDIVIDUAL_ONLY +
        "and v.subscription_expiry_date < date_sub(curdate(), interval %(d)s day) "
        "order by v.subscription_expiry_date asc "
        "limit %(lim)s",
        {"d": days, "lim": ROW_LIMIT},
        as_dict=True,
    )

    total = frappe.db.sql(
        "select count(*) from `tabCustomer Vehicle` v "
        "left join `tabCustomer` c on c.name = v.customer "
        "where v.device_statues = 'Installed' "
        "and ifnull(v.subscription_expiry_date, '') != '' "
        "and ifnull(v.device_serial, '') != '' "
        "and ifnull(v.deletion_date, '') = '' "
        + INDIVIDUAL_ONLY +
        "and v.subscription_expiry_date < date_sub(curdate(), interval %(d)s day)",
        {"d": days},
    )[0][0]

    # the same conditions the reminders and the nightly script use
    excluded_customers = set()
    blocks = frappe.get_all(DNC_DT, filters={"enabled": 1}, fields=["customer", "scope"], limit_page_length=0)
    for row in frappe.get_doc(SETTINGS).get("excluded_customers") or []:
        if frappe.utils.cint(row.get("enabled")):
            blocks.append({"customer": row.get("customer"), "scope": row.get("scope")})
    for b in blocks:
        if str(b.get("scope") or "") in SCOPES and str(b.get("customer") or "").strip():
            excluded_customers.add(str(b.get("customer")).strip())

    paid_vehicles = set()
    if frappe.db.exists("DocType", "Subscription Renewal"):
        for r in frappe.db.sql(
            "select rv.vehicle as v from `tabSubscription Renewal` r "
            "join `tabSubscription Renewal Vehicle` rv on rv.parent = r.name "
            "where r.docstatus < 2 and ifnull(r.update_completed, 0) = 0",
            as_dict=True,
        ):
            if r.v:
                paid_vehicles.add(r.v)
    for r in frappe.db.sql(
        "select distinct i.customer_vehicle as v from `tabSales Invoice Item` i "
        "join `tabSales Invoice` inv on inv.name = i.parent "
        "where inv.docstatus = 1 and inv.posting_date >= date_sub(curdate(), interval %(back)s day) "
        "and i.item_code in ('Subscription renewal', 'subscription') "
        "and ifnull(i.customer_vehicle, '') != ''",
        {"back": days + 365},
        as_dict=True,
    ):
        if r.v:
            paid_vehicles.add(r.v)

    # Pilot's estate carries the agentid and node a block needs
    estate = {}
    pilot_note = "Pilot ids were not read (with_pilot was not asked for)."
    if frappe.utils.cint(args.get("with_pilot")):
        cfg = pilot_settings()
        if not cfg["user"] or not cfg["password"]:
            pilot_note = "Pilot Admin is not configured in app_apis."
        else:
            body = None
            why = ""
            for attempt in (1, 2, 3):
                try:
                    answer = frappe.make_get_request(
                        cfg["url"],
                        auth=(cfg["user"], cfg["password"]),
                        params={"cmd": "vehicles", "account_id": "1", "node": "1", "is_show_deleted": "1"},
                    )
                    try:
                        body = json.loads(answer)
                    except Exception:
                        body = answer
                    break
                except Exception as e:
                    why = str(e)[:150]
            rows = (body or {}).get("data") or []
            for row in rows:
                uid = str(row.get("uniqid") or "").strip()
                if uid and uid != "0":
                    estate[uid] = row
            pilot_note = "Pilot estate: " + str(len(estate)) + " devices." if estate \
                else ("Could not read the Pilot estate" + ((" -- " + why) if why else "") + ".")

    data = []
    for v in vehicles:
        imei = str(v.imei or "").strip()
        row = estate.get(imei) or {}
        plate = str(v.license_plate or v.e_license_plate or v.plate_num or v.audit_plate or "").strip()
        wrong_type = (not all_types) and str(v.customer_type or "").strip().lower() not in allowed_types
        data.append({
            "vehicle": v.name,
            "imei": imei,
            "plate": plate,
            "customer": v.customer,
            "customer_name": v.customer_name or v.customer,
            "customer_type": v.customer_type,
            "paying": v.paying,
            "model": v.device_model or v.device_type,
            "expiry": str(v.expiry or "")[:10],
            "days": frappe.utils.cint(v.days),
            "sim": v.sim_msisdn,
            "sim_status": v.sim_status,
            "erp_status": v.erp_status,
            "on_pilot_1": frappe.utils.cint(v.on_pilot_1),
            "on_pilot_2": frappe.utils.cint(v.on_pilot_2),
            "on_im": frappe.utils.cint(v.on_im),
            "im_status": v.im_status,
            "pilot_active": frappe.utils.cint(row.get("active")) if row else frappe.utils.cint(v.pilot_active),
            "agentid": row.get("agentid"),
            "node": row.get("node_id"),
            "paid": 1 if v.name in paid_vehicles else 0,
            "excluded": 1 if (v.customer in excluded_customers or wrong_type) else 0,
        })

    out = {
        "ok": True,
        "action": "list",
        "days": days,
        "days_after_setting": days_after,
        "grace": GRACE_DAYS,
        "customer_types": "Individual only (Customer Type Individual and Paying not Company)",
        "total": total,
        "returned": len(data),
        "limit": ROW_LIMIT,
        "live": LIVE,
        "pilot_note": pilot_note,
        "can_act": allowed,
        "rows": data,
    }

# ---------------------------------------------------------------- block / unblock
elif action in ("block", "unblock"):
    imei = str(args.get("imei") or "").strip()
    agentid = str(args.get("agentid") or "").strip()
    node = str(args.get("node") or "").strip()
    status = "0" if action == "block" else "1"

    vehicle = past_cutoff(imei) if imei else None

    if not allowed:
        out["error"] = "Only System Manager or Technical can do this."
    elif not imei:
        out["error"] = "No device given."
    elif not vehicle:
        out["error"] = "That device does not belong to a vehicle expired more than " + str(days) + " days."
    elif not agentid and not node:
        out["error"] = "Read the Pilot ids first (Refresh with Pilot)."
    else:
        params = {"cmd": "setvehblock", "status": status}
        if agentid:
            params["agentid"] = agentid
        else:
            params["imei"] = imei
        if node:
            params["node"] = node

        if not LIVE:
            out["ok"] = True
            out["result"] = "would " + action
            out["sent"] = params
        else:
            cfg = pilot_settings()
            try:
                answer = frappe.make_post_request(
                    cfg["url"], auth=(cfg["user"], cfg["password"]), params=params)
                try:
                    answer = json.loads(answer)
                except Exception:
                    pass
                code = frappe.utils.cint((answer or {}).get("code")) if answer else -1
                out["ok"] = code == 0
                out["result"] = action + "ed" if code == 0 else str((answer or {}).get("msg") or answer)[:120]
                out["sent"] = params
            except Exception as e:
                out["result"] = "failed: " + str(e)[:120]

            # every real change leaves a trail
            frappe.get_doc({
                "doctype": "Comment",
                "comment_type": "Comment",
                "reference_doctype": "Server Script",
                "reference_name": SCRIPT_NAME,
                "content": (str(frappe.session.user) + " -- " + action + " " + imei +
                            " (agentid " + str(agentid) + ", node " + str(node) + ", " +
                            str(vehicle.get("license_plate")) + ", " + str(vehicle.get("days")) +
                            " days expired) -- " + str(out.get("result"))),
            }).insert(ignore_permissions=True)

else:
    out["error"] = "Unknown action."

# An API Server Script hands its answer back through frappe.flags: whatever is
# set there becomes the "message" of the call.
for key in out.keys():
    frappe.flags[key] = out[key]
