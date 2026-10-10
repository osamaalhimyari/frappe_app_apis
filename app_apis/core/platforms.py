"""One set of verbs for every platform the app talks to: Pilot (WSL), Pilot 2, IM and WASL.

    find(platform, imei, vehicle="")        is the device there?
    create(platform, vehicle)               put a Customer Vehicle on the platform
    delete(platform, vehicle, pin="")       take it off (and set the vehicle's Device Statues if nothing is left)
    block(platform, imei, blocked=True)     Pilot's reversible off switch
    live(platform, imei, vehicle="")        the live snapshot (the existing connectors)
    wasl_status / wasl_link / wasl_delete   the WASL side of a Pilot vehicle
    summary(vehicle)                        what the app has already stored about where it is

`platform` is one of "pilot_wsl", "pilot2", "im" (see PLATFORMS). `vehicle` is a Customer Vehicle name.

These are plain Python: no role check, so code in another app can call them directly:

    from app_apis.core import platforms
    result = platforms.delete("pilot2", "4324 - أ أ أ")

The whitelisted, role-checked versions for Server Scripts and the browser are in app_apis.core.api.

Every create / delete fires the events in app_apis.core.events (before_* may veto, after_* observe), so
another app can react without changing this one.

Nothing raises for a platform-side failure: each function returns a dict with `verdict` / `ok` /
`result`. `state` "unknown" always means "the platform did not answer", never "no".
"""

import frappe

from app_apis.core import events, im, pilot_panel

PLATFORMS = {
	"pilot_wsl": {"label": "Pilot (WSL)", "kind": "pilot", "account": 1, "email_field": "email_pilot", "box": "ch_pilot_wsl"},
	"pilot2": {"label": "Pilot 2", "kind": "pilot", "account": 2, "email_field": "email_pilot2", "box": None},
	"im": {"label": "IM (Trakzee)", "kind": "im", "account": 0, "email_field": "im_platform", "box": "ch_trakzee"},
}

VEHICLE_DT = "Customer Vehicle"
AUDIT_DT = "app_apis_fleet_audit"

# The Platforms section tick boxes. The four Pilot boxes are flavours of ONE system (Pilot); the other four
# are systems of their own.
PILOT_BOXES = [["ch_pilot_wsl", "Pilot WSL"], ["ch_pilot_tow", "Pilot Towing"], ["ch_pilot_sfda", "Pilot SFDA"],
               ["ch_pilot_tracking_only", "Pilot Tracking Only"]]
OTHER_BOXES = [["ch_trakzee", "IM Tracking"], ["ch_sarp", "SARP"], ["ch_fmsi_medicine", "FMSI Medicine"],
               ["ch_fmsi_balady", "FMSI Balady"]]
STATUS_DELETED = "Deleted"


def _s(v) -> str:
	return str(v or "").strip()


def spec(platform: str) -> dict:
	if platform not in PLATFORMS:
		raise ValueError("Unknown platform %r. Use one of: %s" % (platform, ", ".join(PLATFORMS)))
	return PLATFORMS[platform]


# ------------------------------------------------------------------ the ERP vehicle
def vehicle_row(name: str) -> dict:
	"""The Customer Vehicle fields every platform call needs, or {} when there is no such vehicle."""
	rows = frappe.db.sql(
		"select name, customer, device_serial, sim_serial, devices_type, license_plate, e_license_plate, "
		"plate_num, device_statues, email_pilot, email_pilot2, im_platform, ch_pilot_wsl, ch_trakzee "
		"from `tab%s` where name = %%(n)s limit 1" % VEHICLE_DT, {"n": name}, as_dict=True)
	return rows[0] if rows else {}


def plate_of(v: dict) -> str:
	for f in ("license_plate", "e_license_plate", "plate_num"):
		if _s(v.get(f)):
			return _s(v.get(f))
	return _s(v.get("name"))


def msisdn_for(imei: str) -> str:
	"""The SIM number the Fleet Audit holds for an IMEI ('' when it has none)."""
	if not frappe.db.exists("DocType", AUDIT_DT) or not imei:
		return ""
	rows = frappe.db.sql("select sim_msisdn from `tab%s` where imei = %%(i)s and ifnull(sim_msisdn,'') <> '' limit 1" % AUDIT_DT,
	                     {"i": imei})
	return _s(rows[0][0]) if rows else ""


# ------------------------------------------------------------------ find
def find(platform: str, imei: str, vehicle: str = "") -> dict:
	"""-> {"platform", "state": "yes" | "no" | "unknown", "found": bool, "id": str, "name": str, "detail": str}

	For IM the IMEI alone says whether the device exists; to also get its vehicle id, give `vehicle` (a
	Customer Vehicle name) so the company and plate can be resolved."""
	sp = spec(platform)
	imei = _s(imei)
	if sp["kind"] == "pilot":
		got = pilot_panel.find(imei, sp["account"])
		return {"platform": platform, "state": got["state"], "found": got["state"] == "yes",
		        "id": got.get("agent_id", ""), "name": got.get("name", ""), "detail": got["detail"], "row": got.get("row") or {}}
	got = im.has_imei(imei)
	out = {"platform": platform, "state": got["state"], "found": got["state"] == "yes", "id": "", "name": "",
	       "detail": got["detail"], "row": {}}
	if got["state"] == "yes" and vehicle:
		v = vehicle_row(vehicle)
		acc = im.account_for_email(v.get("im_platform"), refresh=False) if v else {}
		if acc.get("company"):
			fb = im.find_by_name(plate_of(v), acc["company"], imei)
			if fb["state"] == "yes":
				out.update({"id": fb["vehicle_id"], "name": plate_of(v), "detail": fb["detail"],
				            "row": {"company": acc["company"], "branch": acc.get("branch") or fb.get("branch", ""),
				                    "reseller": acc.get("reseller", "")}})
	return out


# ------------------------------------------------------------------ create
def build(platform: str, vehicle: str) -> dict:
	"""Everything create() would send, resolved, without sending it. -> {"payload": dict, "gaps": [str]}.
	A payload with gaps is never sent."""
	sp = spec(platform)
	v = vehicle_row(vehicle)
	if not v:
		return {"payload": {}, "gaps": ["no such Customer Vehicle: %s" % vehicle]}
	imei = _s(v.get("device_serial"))
	name = plate_of(v)
	email = _s(v.get(sp["email_field"]))
	gaps = []
	if not imei:
		gaps.append("imei (Device Serial)")
	if not name:
		gaps.append("name (plate)")

	if sp["kind"] == "pilot":
		acct = pilot_panel.account_by_email(email, sp["account"])
		if not acct["id"]:
			gaps.append("account_id -- " + acct["why"])
		model = pilot_panel.resolve_model(v.get("devices_type"), sp["account"])
		if not model["name"]:
			gaps.append("configuration -- " + model["why"])
		if acct["id"] and not acct["org_name"]:
			gaps.append("account_name -- the Pilot account has no org_name")
		msisdn = msisdn_for(imei)
		payload = {"imei": imei, "name": name, "account_id": acct["id"], "account_name": acct["org_name"],
		           "configuration": model["name"], "msisdn": msisdn or "0", "account_email": email}
		if not msisdn:
			payload["msisdn_note"] = "no SIM number in the Fleet Audit -- sending 0, Pilot's no-SIM value"
		return {"payload": payload, "gaps": gaps}

	acc = im.account_for_email(email)
	model = im.model_for(v.get("devices_type"))
	if imei and not imei.isdigit():
		gaps.append("imei -- IM takes digits only, got " + imei)
	if not email:
		gaps.append("im_platform (the vehicle's IM account email)")
	if not (acc["company"] and acc["branch"]):
		gaps.append("company / branch -- " + acc["why"])
	if not model:
		gaps.append("device model -- no IM model for Device Type %s; add it in app_apis settings > IM > IM Device Models"
		            % (_s(v.get("devices_type")) or "(empty)"))
	sim = msisdn_for(imei)
	payload = {"imei": imei, "name": name, "company": acc["company"], "branch": acc["branch"],
	           "reseller": acc["reseller"], "model": model, "sim_number": "".join(c for c in sim if c.isdigit()),
	           "account_email": email, "company_found_by": acc["how"]}
	return {"payload": payload, "gaps": gaps}


def create(platform: str, vehicle: str, dry_run: bool = False, source: str = "core") -> dict:
	"""Put a Customer Vehicle on a platform. Reads the device back before it says "uploaded".

	dry_run=True resolves everything and sends nothing (verdict "dry_run").
	-> {"platform", "vehicle", "verdict", "ok", "result", "payload", "gaps", ...}"""
	sp = spec(platform)
	built = build(platform, vehicle)
	payload, gaps = built["payload"], built["gaps"]
	base = {"platform": platform, "label": sp["label"], "vehicle": vehicle, "payload": payload, "gaps": gaps,
	        "imei": payload.get("imei", "")}
	if gaps:
		return dict(base, verdict="failure", ok=False,
		            result="cannot build a complete create for %s: %s" % (sp["label"], "; ".join(gaps)))
	if dry_run:
		return dict(base, verdict="dry_run", ok=True, result="ready to send to %s; nothing was sent" % sp["label"])

	events.emit("before_create", platform=platform, vehicle=vehicle, imei=payload["imei"], source=source, payload=payload)
	if sp["kind"] == "pilot":
		res = pilot_panel.create(payload["imei"], payload["name"], payload["account_id"], payload["account_name"],
		                         payload["configuration"], payload["msisdn"], sp["account"])
	else:
		existing = im.has_imei(payload["imei"])
		if existing["state"] == "yes":
			res = {"verdict": "uploaded", "ok": True, "already": True,
			       "result": "already registered on IM, not created again -- " + existing["detail"]}
		elif existing["state"] == "unknown":
			res = {"verdict": "error", "ok": False,
			       "result": "did not send: could not establish whether it is already there -- " + existing["detail"]}
		else:
			res = im.create(name=payload["name"], imei=payload["imei"], company=payload["company"],
			                branch=payload["branch"], model=payload["model"], sim_number=payload["sim_number"],
			                reseller=payload["reseller"])
	out = dict(base, **res)
	events.emit("after_create", platform=platform, vehicle=vehicle, imei=payload["imei"], source=source, result=out)
	return out


# ------------------------------------------------------------------ delete + the vehicle's status
def status_decision(boxes: dict, deleted_box: str | None, pilot_state: str, pilot_where: str,
                    current_status: str, deletion_date, today: str) -> dict:
	"""What to do to the vehicle record after a platform delete. PURE (no database), so it is tested.

	boxes          {fieldname: 0/1} for every Platforms tick box
	deleted_box    the box of the platform just deleted from ("" / None for Pilot 2, which has none)
	pilot_state    "yes" | "no" | "unknown": is the vehicle still on the OTHER Pilot estate (a live look)
	pilot_where    which estate, for the note
	-> {"updates": {...}, "changed": bool, "note": str, "unticked": [fieldnames]}

	Pilot Tow / SFDA / Tracking Only are flavours of the one Pilot system: they are cleared only when the
	vehicle is gone from every Pilot estate. "unknown" is treated as still there -- never guess a deletion.
	"""
	updates, unticked = {}, []
	if deleted_box and frappe.utils.cint(boxes.get(deleted_box)):
		updates[deleted_box] = 0
		unticked.append(deleted_box)
	if pilot_state == "no":
		for fieldname, _label in PILOT_BOXES:
			if frappe.utils.cint(boxes.get(fieldname)) and fieldname not in updates:
				updates[fieldname] = 0
				unticked.append(fieldname)
	left = []
	if pilot_state != "no":
		left.append("Pilot (%s)" % pilot_where)
	for fieldname, label in OTHER_BOXES:
		if frappe.utils.cint(boxes.get(fieldname)) and fieldname != deleted_box:
			left.append(label)
	cleared = ("unticked %s; " % ", ".join(unticked)) if unticked else ""
	if left:
		return {"updates": updates, "changed": False, "unticked": unticked,
		        "note": cleared + "Device Statues left as it is -- still on: " + ", ".join(left)}
	if _s(current_status) == STATUS_DELETED:
		if deletion_date:
			return {"updates": updates, "changed": False, "unticked": unticked,
			        "note": cleared + "Device Statues was already Deleted (Deletion Date %s)" % deletion_date}
		updates["deletion_date"] = today
		return {"updates": updates, "changed": False, "unticked": unticked,
		        "note": cleared + "Device Statues was already Deleted; Deletion Date was empty, set to today"}
	updates["device_statues"] = STATUS_DELETED
	date = deletion_date or today
	if not deletion_date:
		updates["deletion_date"] = today
	return {"updates": updates, "changed": True, "unticked": unticked,
	        "note": cleared + "Device Statues set to Deleted, Deletion Date %s (no other system is left)" % date}


def pilot_still_there(vehicle: str, imei: str, deleted_platform: str) -> dict:
	"""Is the vehicle still on a Pilot estate other than the one just deleted from? Live lookups.
	-> {"state": "yes" | "no" | "unknown", "where": str}"""
	row = frappe.db.get_value(VEHICLE_DT, vehicle, ["email_pilot2"], as_dict=True) or {}
	for key in ("pilot_wsl", "pilot2"):
		if key == deleted_platform:
			continue
		if key == "pilot2" and not _s(row.get("email_pilot2")):
			continue                              # no Pilot 2 account on the vehicle: it is not there
		got = pilot_panel.find(imei, PLATFORMS[key]["account"])
		if got["state"] == "yes":
			return {"state": "yes", "where": PLATFORMS[key]["label"]}
		if got["state"] != "no":
			return {"state": "unknown", "where": "%s could not be checked: %s" % (PLATFORMS[key]["label"], got["detail"][:90])}
	return {"state": "no", "where": ""}


def apply_vehicle_status(vehicle: str, platform: str, imei: str) -> dict:
	"""After a successful platform delete: untick what is gone and set Device Statues (and the Deletion Date)
	to Deleted when no other system is left. Written with db.set_value on purpose -- saving the document
	would run the Customer Vehicle save scripts, which rewrite subscription expiry and Serial No warranty
	dates. -> {"changed": bool, "note": str}"""
	names = [c[0] for c in PILOT_BOXES + OTHER_BOXES] + ["device_statues", "deletion_date"]
	now = frappe.db.get_value(VEHICLE_DT, vehicle, names, as_dict=True)
	if not now:
		return {"changed": False, "note": "the vehicle record could not be read, so its status was not touched"}
	pilot = pilot_still_there(vehicle, imei, platform)
	dec = status_decision(dict(now), spec(platform)["box"], pilot["state"], pilot["where"],
	                      now.get("device_statues"), now.get("deletion_date"), frappe.utils.nowdate())
	if dec["updates"]:
		frappe.db.set_value(VEHICLE_DT, vehicle, dec["updates"])
	return {"changed": dec["changed"], "note": dec["note"]}


def delete(platform: str, vehicle: str, pin: str = "", update_status: bool = True, source: str = "core") -> dict:
	"""Take a Customer Vehicle off a platform. Cannot be undone on the platform.

	The device is looked up fresh and its IMEI checked before anything is removed, and read back after.
	On a successful delete, Device Statues is set per status_decision (set update_status=False to skip).
	-> {"platform", "vehicle", "verdict", "ok", "result", "status_changed", "status_note", ...}"""
	sp = spec(platform)
	v = vehicle_row(vehicle)
	base = {"platform": platform, "label": sp["label"], "vehicle": vehicle, "status_changed": False, "status_note": ""}
	if not v:
		return dict(base, verdict="not_deleted", ok=False, result="no such Customer Vehicle: %s" % vehicle)
	imei = _s(v.get("device_serial"))
	base["imei"] = imei
	if not imei:
		return dict(base, verdict="not_deleted", ok=False, result="the vehicle has no Device Serial")

	events.emit("before_delete", platform=platform, vehicle=vehicle, imei=imei, source=source)
	if sp["kind"] == "pilot":
		res = pilot_panel.delete(imei, sp["account"])
	else:
		acc = im.account_for_email(v.get("im_platform"))
		if not (acc["company"] and acc["branch"]):
			res = {"verdict": "not_deleted", "ok": False, "result": "nothing deleted -- " + acc["why"]}
		else:
			fb = im.find_by_name(plate_of(v), acc["company"], imei)
			if fb["state"] != "yes":
				res = {"verdict": "not_deleted", "ok": False, "result": "nothing deleted -- " + fb["detail"]}
			else:
				res = im.delete(vehicle_id=fb["vehicle_id"], company=acc["company"], branch=acc["branch"], imei=imei, pin=pin)
	out = dict(base, **res)
	if res.get("verdict") == "deleted" and update_status:
		try:
			st = apply_vehicle_status(vehicle, platform, imei)
			out["status_changed"] = st["changed"]
			out["status_note"] = st["note"]
			out["result"] = "%s | %s" % (out["result"], st["note"])
			if st["changed"]:
				events.emit("after_status", platform=platform, vehicle=vehicle, imei=imei, source=source,
				            status=STATUS_DELETED, note=st["note"])
		except Exception as e:
			out["status_note"] = "deleted on the platform, but the vehicle's Device Statues could not be updated: %s" % str(e)[:160]
			out["result"] = "%s | %s" % (out["result"], out["status_note"])
	events.emit("after_delete", platform=platform, vehicle=vehicle, imei=imei, source=source, result=out)
	return out


# ------------------------------------------------------------------ block, live
def block(platform: str, imei: str, blocked: bool = True, agent_id: str = "", node: str = "", source: str = "core") -> dict:
	"""Block / unblock a device on Pilot (reversible). IM has no method to switch a vehicle off."""
	sp = spec(platform)
	if sp["kind"] != "pilot":
		return {"ok": False, "result": "IM publishes no method that switches a vehicle off; do it in IM's own screens"}
	if not agent_id:
		found = pilot_panel.find(imei, sp["account"])
		agent_id = found.get("agent_id", "")
	res = pilot_panel.set_block(imei, blocked, sp["account"], agent_id, node)
	events.emit("after_block", platform=platform, imei=_s(imei), source=source, blocked=bool(blocked), result=res)
	return dict(res, platform=platform, agent_id=agent_id)


def live(platform: str, imei: str, vehicle: str = "") -> dict:
	"""The live snapshot from the existing connectors (Pilot: app_apis.connector, IM: app_apis.im_connector)."""
	sp = spec(platform)
	if sp["kind"] == "im":
		from app_apis import im_connector

		return im_connector.get_vehicle_live(imei)
	from app_apis import connector

	customer = frappe.db.get_value(VEHICLE_DT, vehicle, "customer") if vehicle else ""
	return connector.get_vehicle_live(imei, customer or "", None, sp["account"])


# ------------------------------------------------------------------ WASL
def wasl_status(imei: str) -> dict:
	"""Live WASL status of a Pilot (WSL) vehicle, read through the Pilot admin panel.
	state: not_on_pilot | not_linked | saved_not_registered | registered_inactive | linked | unknown"""
	imei = _s(imei)
	res = {"imei": imei, "state": "unknown", "detail": ""}
	if not imei:
		res["detail"] = "no IMEI"
		return res
	found = pilot_panel.find(imei, 1)
	if found["state"] == "unknown":
		res["detail"] = found["detail"]
		return res
	if found["state"] == "no":
		res.update({"state": "not_on_pilot", "detail": "this IMEI is not on Pilot"})
		return res
	res.update({"agent_id": found["agent_id"], "pilot_name": found.get("name", ""), "account_id": found.get("account_id", "")})
	got = pilot_panel.wasl_row(found["agent_id"], 1)
	if not got["ok"]:
		res["detail"] = got["why"]
		return res
	row = got["row"]
	res["checked_rows"] = got["rows_checked"]
	if not row:
		res["state"] = "not_linked"
		return res
	res.update({"wasl_status": _s(row.get("status")), "referencekey": _s(row.get("referencekey")), "ts": _s(row.get("ts"))})
	if _s(row.get("referencekey")) and _s(row.get("status")) == "1":
		res["state"] = "linked"
	elif _s(row.get("referencekey")):
		res["state"] = "registered_inactive"
	else:
		res["state"] = "saved_not_registered"
	return res


def wasl_link(vehicle: str, form: dict, register: bool = True, source: str = "core") -> dict:
	"""Save the WASL form on Pilot and (register=True) register the vehicle with WASL."""
	from app_apis import wasl

	res = wasl.link_vehicle(vehicle, form, 1 if register else 0)
	events.emit("after_wasl", platform="pilot_wsl", vehicle=vehicle, imei=_s((res or {}).get("imei")), source=source,
	            action="link", result=res)
	return res


def wasl_delete(vehicle: str, source: str = "core") -> dict:
	"""Delete the vehicle from WASL ONLY (the Pilot vehicle stays)."""
	from app_apis import wasl

	res = wasl.wasl_delete(vehicle, confirm=wasl.DELETE_WORD, dry_run=0)
	events.emit("after_wasl", platform="pilot_wsl", vehicle=vehicle, imei=_s((res or {}).get("imei")), source=source,
	            action="delete", result=res)
	return res


def wasl_check(vehicle: str) -> dict:
	"""WASL's own inquiry for a vehicle, nothing changed (the dry run of wasl_delete)."""
	from app_apis import wasl

	return wasl.wasl_delete(vehicle, dry_run=1)


# ------------------------------------------------------------------ stored picture
def summary(vehicle: str) -> dict:
	"""What the hourly jobs and the Fleet Audit have already stored about where the vehicle is -- no
	platform is called. See app_apis.vehicle_systems."""
	from app_apis import vehicle_systems

	return vehicle_systems.get_systems(vehicle)
