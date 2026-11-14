# Changelog

## 0.0.8 (2026-11-14)

Access
- Portal (customer) logins are refused everywhere; every staff login gets in (`app_apis.access`).
- app_apis settings > Access: which roles may use each group of actions (View platform data, Upload and
  edit vehicles, Delete and block vehicles, WASL link and delete, Lebara SIM changes, Send messages, Run
  reports and syncs, Admin tools). A group with no rows is open to all staff, so an untouched site works
  as before. System Manager always passes. The rules are site data, never code, so an update never
  changes them. Server Scripts ask `app_apis.access.can`.
- Settings changes are tracked (Version history on app_apis).

Action log
- New App Apis Action Log: who uploaded, edited, deleted, blocked, WASL-linked or changed a Lebara SIM,
  on which platform, and what the platform answered. Written by core.platforms, app_apis.wasl,
  core.lebara and the shipped scripts (vehicle_upload_api, expired_devices_api, Lebara API).

Health and alerts
- New page /app/app-apis-health: connections, hourly jobs, app_apis errors and failed messages of the
  last 24 hours, with a Test button per connection and "Test all connections".
- Hourly check (hooks.scheduler_events): new problems and fixed ones go to the Alert Users
  (settings > Health and Alerts) as a bell notification. No email or WhatsApp. "Test Connections
  Hourly" also signs in to every enabled Pilot / IM / Chatwoot connection.

Fixes
- IM track history: 12-hour times ("05-11-2026 02:15:33 PM") are read, so track points have a time.
- Pilot Admin get_vehicle_status: this Pilot server does not serve the v3 API (404); the lookup now
  falls back to the panel's own search.
- Lebara API script: carries the SimDetail CSRF fix that was made on the desk copy on 2026-11-05.

Messages
- New placeholders {where} (customer site + place / our branch / remote) and {map} (Google Maps link
  to the ticket's service-location pin). Both drop out when a ticket has none. The shipped technician
  In Hand wording uses them; a site's own Status Rules need {where} / {map} added by hand.
