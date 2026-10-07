"""The app's core: whitelisted primitives that Server Scripts build on.

Everything under this package is meant to be called from a Server Script with
`frappe.call("app_apis.core.<module>.<function>", ...)`. The business logic
(what to call, when, and what to do with the answer) lives in the Server
Scripts themselves, which are editable from the desk -- see
app_apis/scripts/ for the copies the app seeds on install and migrate.
"""
