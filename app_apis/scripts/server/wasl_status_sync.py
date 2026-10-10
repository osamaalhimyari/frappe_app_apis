# =============================================================================
# App Apis - WASL Status Sync
# -----------------------------------------------------------------------------
# Server Script -- Script Type: Scheduler Event, Event Frequency: Hourly Long
#
# Copies Pilot's WASL registration list (app/wasl.php get_vehicles, about a second per 1,000 vehicles)
# into the "App Apis WASL State" list once an hour, keyed by IMEI, so the Customer Vehicle form can say
# whether a vehicle is linked to WASL without calling Pilot every time it is opened.
#
# The work is in app_apis.wasl.sync_wasl_states. It needs the Pilot admin account (app_apis > Pilot
# Admin). Tick Disabled on this Server Script to stop it.
# =============================================================================

try:
    frappe.call("app_apis.wasl.sync_wasl_states")
except Exception as e:
    frappe.log_error(title="App Apis - WASL Status Sync", message=str(e)[:1000])
