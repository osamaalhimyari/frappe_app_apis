# =============================================================================
# App Apis - Stale Ticket Reminders
# -----------------------------------------------------------------------------
# Server Script -- Script Type: Scheduler Event, Event Frequency: Hourly Long
#
# Triggers the app's "nudge stuck tickets" pass (it used to be a line in the
# app's hooks.py). The pass still decides whether this hour is the one to scan:
# app_apis > Chatwoot > Nudge Stuck Tickets and Run At Hour. Tick Disabled here
# to stop the trigger entirely.
# =============================================================================

result = frappe.call("app_apis.core.jobs.stale_reminders_hourly")
