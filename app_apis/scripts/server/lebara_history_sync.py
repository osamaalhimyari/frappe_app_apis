# =============================================================================
# Lebara History Sync
# -----------------------------------------------------------------------------
# Server Script -- Script Type: Scheduler Event, Event Frequency: Hourly Long
#
# Keeps the two Lebara datasets a cost / loss dashboard needs:
#
#   transactions   every activate / suspend / resume / deactivate Lebara performed, who asked, and how it
#                  ended -> "Lebara Transaction". Incremental: only what is new since the last run.
#   invoice        the bill of the month just ended, one line per SIM (plan, status, amount)
#                  -> "Lebara Invoice" + "Lebara Invoice Line". Looked for once a day, at INVOICE_HOUR,
#                  and skipped when that month is already stored.
#
# The work is app_apis.core.lebara (sync_transactions, sync_latest_invoice). Skips quietly while Lebara is
# disabled or not logged in. Tick Disabled on this Server Script to stop it.
# =============================================================================

INVOICE_HOUR = 7   # site time; the invoice is dated the 1st of the month after the one it bills

hour = frappe.utils.now_datetime().hour

try:
    frappe.call("app_apis.core.api.lebara_sync_scheduled", what="transactions")
    if hour == INVOICE_HOUR:
        frappe.call("app_apis.core.api.lebara_sync_scheduled", what="invoice")
except Exception as e:
    frappe.log_error(title="Lebara History Sync", message=str(e)[:1000])
