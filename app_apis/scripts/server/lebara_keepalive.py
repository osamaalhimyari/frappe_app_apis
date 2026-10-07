# =============================================================================
# Lebara Keepalive
# -----------------------------------------------------------------------------
# Server Script -- Script Type: Scheduler Event, Event Frequency: Cron
#
# Lebara's login needs an SMS OTP, but the session it creates slides: as long
# as it is used it stays alive. This pings the portal on the cron below so the
# session never goes idle long enough to expire, and you only type an OTP
# again if it does (you then get an alert, see "Notify on Expiry").
#
# Change how often it runs with "Cron Format" on this record (default every
# 5 minutes). Tick Disabled to stop it.
# =============================================================================

SETTINGS = "app_apis"

enabled = frappe.utils.cint(frappe.db.get_single_value(SETTINGS, "lebara_enabled"))
status = frappe.db.get_single_value(SETTINGS, "lebara_session_status")

if enabled and status == "Logged In":
    try:
        frappe.call("lebara", action="ping")
    except Exception as e:
        # "lebara" has already marked the session Expired and alerted; this
        # only records why, without a traceback every five minutes.
        frappe.log_error(title="Lebara keepalive", message=str(e)[:1000])
