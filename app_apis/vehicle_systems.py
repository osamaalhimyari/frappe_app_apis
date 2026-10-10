# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Where a vehicle is, according to what the app has ALREADY stored from the systems.

The Customer Vehicle form shows a Summary section at the top built from this. It never calls Pilot, IM,
WASL or Lebara: it only joins three tables that other jobs keep filled from those systems:

    app_apis_fleet_audit  the Fleet Audit report: what the Pilot and IM estates said about each device
    App Apis WASL State   refreshed hourly by "App Apis - WASL Status Sync" -> is the vehicle linked to WASL
    Lebara SIM            refreshed hourly by "Lebara SIM Sync" -> the SIM, its status, the IMEI it last sat in

Nothing about WHERE a device is is taken from the vehicle record itself (its Platforms tick boxes and its
account emails are what the ERP user typed, not what a system reported). The vehicle supplies only the
keys: its IMEI (device serial) and its SIM serial.

Every part is returned with the time it is as of, so the form can say how fresh it is. The Fleet Audit
only holds what its last run read: if that run did not read an estate (Pilot 2 on this site), that estate
is reported as "not read" rather than "not found".

`build_summary` is pure (no database), so it is unit tested; `get_systems` only gathers its inputs."""

import re

import frappe

VEHICLE_DT = "Customer Vehicle"
AUDIT_DT = "app_apis_fleet_audit"
WASL_DT = "App Apis WASL State"
SIM_DT = "Lebara SIM"

# a snapshot older than this many days is flagged as stale on the form
STALE_DAYS = 2

WASL_LABELS = {
	"linked": "Linked to WASL",
	"registered_inactive": "Registered with WASL, not active",
	"saved_not_registered": "WASL form saved, not registered",
	"not_listed": "Not linked to WASL",
	"unknown": "WASL status not read yet",
}


def _s(v) -> str:
	return str(v or "").strip()


def _flag(v):
	return bool(int(v or 0))


def _digits(v) -> str:
	return re.sub(r"\D", "", _s(v))


def _stamp(v):
	return str(v)[:16] if v else ""


def _age_days(v, now):
	if not v or not now:
		return None
	try:
		return (now - v).days
	except TypeError:
		return None


def build_summary(veh: dict, audit, wasl, sim, wasl_synced_at, now, pilot2_read: bool = True) -> dict:
	"""Everything the form's section shows.

	veh             the Customer Vehicle keys only (device_serial, sim_serial, device_statues)
	audit           the Fleet Audit row for the IMEI, or None
	wasl            the App Apis WASL State row for the IMEI, or None
	sim             the Lebara SIM row for the vehicle's SIM, or None
	wasl_synced_at  when the WASL list was last copied (None = never)
	now             the current datetime
	pilot2_read     did the Fleet Audit read the Pilot 2 estate at all?
	"""
	imei = _s(veh.get("device_serial"))
	issues = []
	a = audit or {}

	# ---- where it is: only what the systems reported to the Fleet Audit
	def detail_pilot():
		bits = []
		if _s(a.get("pilot_account")):
			bits.append(_s(a.get("pilot_account")))
		if _s(a.get("pilot_folder")):
			bits.append(_s(a.get("pilot_folder")))
		return bits

	def detail_im():
		bits = []
		if _s(a.get("im_company")):
			bits.append(_s(a.get("im_company")))
		if _s(a.get("im_status")):
			bits.append(_s(a.get("im_status")))
		return bits

	where = []
	for key, label, flag in (("pilot_wsl", "Pilot (WSL)", "on_pilot_1"), ("pilot2", "Pilot 2", "on_pilot_2"),
	                         ("im", "IM (Trakzee)", "on_im")):
		if not audit:
			state = None                        # this device is not in the audit at all
		elif key == "pilot2" and not pilot2_read:
			state = None                        # the audit never read that estate: no claim either way
		else:
			state = _flag(a.get(flag))
		row = {"key": key, "label": label, "on": state, "detail": []}
		if state:
			row["detail"] = detail_im() if key == "im" else detail_pilot()
			if key == "im":
				row["last_seen"] = _stamp(a.get("im_last_seen"))
			else:
				row["last_seen"] = _stamp(a.get("pilot_last_seen"))
				row["active"] = _flag(a.get("pilot_active"))
		if audit and key == "pilot2" and not pilot2_read:
			row["note"] = "not read by the last audit"
		where.append(row)

	age = _age_days(a.get("audited_at"), now)
	audit_part = {"found": bool(audit), "as_of": _stamp(a.get("audited_at")),
	              "stale": bool(age is not None and age > STALE_DAYS), "age_days": age,
	              "pilot2_read": bool(pilot2_read)}

	# ---- WASL
	if wasl:
		wasl_part = {"state": wasl.get("state") or "unknown", "referencekey": _s(wasl.get("referencekey")),
		             "wasl_status": _s(wasl.get("wasl_status")), "changed_at": _stamp(wasl.get("wasl_ts")),
		             "as_of": _stamp(wasl_synced_at)}
	elif wasl_synced_at:
		wasl_part = {"state": "not_listed", "referencekey": "", "wasl_status": "", "changed_at": "",
		             "as_of": _stamp(wasl_synced_at)}
	else:
		wasl_part = {"state": "unknown", "referencekey": "", "wasl_status": "", "changed_at": "", "as_of": ""}
	wasl_part["label"] = WASL_LABELS.get(wasl_part["state"], wasl_part["state"])
	wasl_age = _age_days(wasl_synced_at, now)
	wasl_part["stale"] = bool(wasl_age is not None and wasl_age > STALE_DAYS)

	# a cross-check between two SYSTEM reports (not the vehicle's own boxes)
	on_pilot_1 = where[0]["on"]
	if on_pilot_1 and wasl_part["state"] == "not_listed":
		issues.append("The audit found it on Pilot (WSL) but it is not on the WASL list")
	if on_pilot_1 is False and wasl_part["state"] == "linked":
		issues.append("It is linked to WASL but the audit did not find it on Pilot (WSL)")

	# ---- the SIM, and whether it is in THIS vehicle's device
	if sim:
		sim_imei = _s(sim.get("imei"))
		matches = "unknown" if not sim_imei else ("yes" if sim_imei == imei else "no")
		sim_part = {"found": True, "msisdn": _s(sim.get("msisdn")), "iccid": _s(sim.get("iccid")),
		            "status": _s(sim.get("status")), "last_connection": _stamp(sim.get("last_connection")),
		            "sim_imei": sim_imei, "imei_matches": matches, "as_of": _stamp(sim.get("modified")),
		            "in_data_session": _flag(sim.get("in_data_session"))}
		if matches == "no":
			issues.append("The SIM was last seen in a different device (%s) than this vehicle's (%s)" % (sim_imei, imei))
		if sim_part["status"] in ("Suspend", "Bar", "Deactivated"):
			issues.append("The SIM is %s" % sim_part["status"])
	else:
		sim_part = {"found": False, "msisdn": "", "iccid": _digits(veh.get("sim_serial")), "status": "",
		            "last_connection": "", "sim_imei": "", "imei_matches": "unknown", "as_of": "",
		            "in_data_session": False,
		            "why": "no Lebara SIM matches this vehicle's SIM serial (the SIM may be on another operator)"
		            if _s(veh.get("sim_serial")) else "the vehicle has no SIM serial"}

	return {"imei": imei, "device_status": _s(veh.get("device_statues")), "where": where, "audit": audit_part,
	        "wasl": wasl_part, "sim": sim_part, "issues": issues}


@frappe.whitelist()
def get_systems(vehicle: str) -> dict:
	"""The section for one Customer Vehicle. Read-only; no outside system is called."""
	frappe.has_permission(VEHICLE_DT, "read", vehicle, throw=True)
	veh = frappe.db.get_value(VEHICLE_DT, vehicle, ["name", "device_serial", "sim_serial", "device_statues"],
	                          as_dict=True) or {}
	imei = _s(veh.get("device_serial"))
	audit = wasl = sim = None
	if imei:
		audit = frappe.db.get_value(
			AUDIT_DT, {"imei": imei},
			["audited_at", "on_pilot_1", "on_pilot_2", "on_im", "pilot_active", "pilot_last_seen", "pilot_account",
			 "pilot_folder", "im_last_seen", "im_status", "im_company", "sim_msisdn"], as_dict=True)
		if frappe.db.exists("DocType", WASL_DT):
			wasl = frappe.db.get_value(WASL_DT, {"imei": imei}, ["state", "referencekey", "wasl_status", "wasl_ts"],
			                          as_dict=True)
		iccid = _digits(veh.get("sim_serial"))
		fields = ["msisdn", "iccid", "status", "last_connection", "imei", "modified", "in_data_session"]
		if iccid:
			sim = frappe.db.get_value(SIM_DT, {"iccid": iccid}, fields, as_dict=True)
		if not sim and audit and _digits(audit.get("sim_msisdn")):
			sim = frappe.db.get_value(SIM_DT, {"msisdn": _digits(audit.get("sim_msisdn"))}, fields, as_dict=True)
	synced = None
	if frappe.db.exists("DocType", WASL_DT):
		synced = frappe.db.sql("select max(synced_at) from `tab%s`" % WASL_DT)[0][0]
	# Pilot 2 was only read if the last audit saw at least one device there
	pilot2_read = bool(frappe.db.exists(AUDIT_DT, {"on_pilot_2": 1}))
	return build_summary(veh, audit, wasl, sim, synced, frappe.utils.now_datetime(), pilot2_read)
