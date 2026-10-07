# =============================================================================
# Lebara SIM Sync
# -----------------------------------------------------------------------------
# Server Script -- Script Type: Scheduler Event, Event Frequency: Hourly Long
#
# Copies every Lebara SIM (about 30,000, ~1 minute) into the "Lebara SIM" list
# once an hour, so the Lebara SIMs page and the desk search the copy instead
# of calling Lebara each time. Only rows that changed are written. Afterwards
# the Fleet Audit's SIM columns are refreshed from the new list.
# Skips quietly while Lebara is disabled or not logged in.
# Quiet hours: no scheduled sync from 23:00 to 06:00 (site time zone); the
# 06:00 run is the first of the day. "Sync now" is not affected.
# =============================================================================

SETTINGS = "app_apis"
QUIET_FROM = 23   # first hour with no sync
QUIET_TO = 6      # first hour that syncs again

hour = frappe.utils.now_datetime().hour
quiet = hour >= QUIET_FROM or hour < QUIET_TO

enabled = frappe.utils.cint(frappe.db.get_single_value(SETTINGS, "lebara_enabled"))
status = frappe.db.get_single_value(SETTINGS, "lebara_session_status")

if enabled and status == "Logged In" and not quiet:
    try:
        frappe.call("lebara", action="sync_sims")
        frappe.call("app_apis.fleet_audit.refresh_sim_data")
    except Exception as e:
        frappe.db.set_value(SETTINGS, SETTINGS, "lebara_sims_sync_note",
                            "Last sync failed at %s: %s" % (frappe.utils.now(), str(e)[:300]),
                            update_modified=False)
        frappe.log_error(title="Lebara SIM Sync", message=str(e)[:1000])
