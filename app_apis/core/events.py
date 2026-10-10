"""Extension points: let another app (or a site script) react to what app_apis does, without editing app_apis.

The platform layer (app_apis.core.platforms) fires an event around each thing it does to Pilot, IM or
WASL. Anything registered for that event is called. Register handlers from ANOTHER app's hooks.py:

    app_apis_events = {
        "after_delete": ["my_app.handlers.vehicle_deleted"],
        "before_create": ["my_app.handlers.check_before_create"],
    }

A handler is a plain function that takes keyword arguments (it should accept **data so new keys never
break it):

    def vehicle_deleted(event, platform, vehicle, imei, result, source, user, **data):
        ...

Events
------
    before_create   about to create the vehicle on `platform`.   A handler may RAISE to veto it.
    after_create    the create finished; `result` says how (verdict, ok, result text).
    before_delete   about to delete.                              A handler may RAISE to veto it.
    after_delete    the delete finished; `result` says how.
    after_block     a Pilot device was blocked / unblocked (`blocked` is True/False).
    after_status    the vehicle's Device Statues was changed by a platform delete (`status`, `note`).
    after_wasl      a WASL action finished (`action` is link / delete / check).

Every event carries: event, platform, vehicle (Customer Vehicle name or ""), imei, source ("core" when
fired by app_apis.core.platforms, "script" when reported by a Server Script through app_apis.core.api),
user, and the event's own keys.

`before_*` handlers run in order and the first one to raise stops the action (the exception reaches
the caller). `after_*` handlers can never break the action that already happened: their errors are
logged to the Error Log and the next handler still runs.

`emit` returns the list of what each handler returned, so a caller can see who answered.
"""

import frappe

HOOK = "app_apis_events"

EVENTS = (
	"before_create", "after_create", "before_delete", "after_delete",
	"after_block", "after_status", "after_wasl",
)


def handlers(event: str) -> list:
	"""The dotted paths registered for `event`, in the order the apps declare them."""
	declared = frappe.get_hooks(HOOK) or {}
	paths = []
	# frappe merges a dict hook across apps into {event: [path, ...]}; tolerate a list of such dicts too
	sources = declared if isinstance(declared, list) else [declared]
	for src in sources:
		if isinstance(src, dict):
			value = src.get(event) or []
			paths.extend([value] if isinstance(value, str) else list(value))
	seen = []
	for p in paths:
		if p and p not in seen:
			seen.append(p)
	return seen


def emit(event: str, **data) -> list:
	"""Call every handler registered for `event`. See the module docstring for the veto rule."""
	if event not in EVENTS:
		raise ValueError("Unknown app_apis event %r. Known: %s" % (event, ", ".join(EVENTS)))
	payload = {"event": event, "platform": "", "vehicle": "", "imei": "", "source": "core",
	           "user": frappe.session.user if getattr(frappe.local, "session", None) else "Administrator"}
	payload.update(data)
	answers = []
	for path in handlers(event):
		try:
			fn = frappe.get_attr(path)
		except Exception:
			frappe.log_error(title="app_apis event %s: cannot load %s" % (event, path))
			continue
		if event.startswith("before_"):
			answers.append(fn(**payload))        # a raise here is the veto: let it travel
			continue
		try:
			answers.append(fn(**payload))
		except Exception:
			frappe.log_error(title="app_apis event %s: handler %s failed" % (event, path))
	return answers
