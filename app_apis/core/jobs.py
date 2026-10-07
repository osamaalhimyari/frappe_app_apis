"""Whitelisted entry points for the app's scheduled jobs.

The schedule itself is no longer in hooks.py: each job is triggered by a
Scheduler Event Server Script ("App Apis - Subscription Reminders",
"App Apis - Stale Ticket Reminders"), so it can be paused, re-timed or
replaced from the desk. Those scripts call these functions.

Each one still decides for itself whether it is due (hour, weekday, enabled
flags on the settings Single) and never raises -- see the module each wraps.
"""

import frappe


@frappe.whitelist(methods=["POST"])
def subscription_reminders_hourly():
	frappe.only_for("System Manager")
	from app_apis import subscription_reminders

	return subscription_reminders.hourly()


@frappe.whitelist(methods=["POST"])
def stale_reminders_hourly():
	frappe.only_for("System Manager")
	from app_apis import stale_reminders

	return stale_reminders.hourly()
