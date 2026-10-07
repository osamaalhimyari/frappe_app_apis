# =============================================================================
# App Apis - Subscription Reminders
# -----------------------------------------------------------------------------
# Server Script -- Script Type: Scheduler Event, Event Frequency: Hourly Long
#
# Triggers the app's subscription reminder pass (it used to be a line in the
# app's hooks.py). The pass itself still decides whether this hour is the one
# to send in: app_apis > Subscription Reminders > Enabled, Run At Hour, Run On,
# Dry Run. Tick Disabled here to stop the trigger entirely.
# =============================================================================

result = frappe.call("app_apis.core.jobs.subscription_reminders_hourly")
