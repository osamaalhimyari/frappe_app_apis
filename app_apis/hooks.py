app_name = "app_apis"
app_title = "App Apis"
app_publisher = "osama"
app_description = "this app used to fetch data from pilot tracking system "
app_email = "osama@im2m.ws"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "app_apis",
# 		"logo": "/assets/app_apis/logo.png",
# 		"title": "App Apis",
# 		"route": "/app_apis",
# 		"has_permission": "app_apis.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/app_apis/css/app_apis.css"
# app_include_js = "/assets/app_apis/js/app_apis.js"

# include js, css files in header of web template
# web_include_css = "/assets/app_apis/css/app_apis.css"
# web_include_js = "/assets/app_apis/js/app_apis.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "app_apis/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# Customize Form normally refuses every Single; the app lets it open app_apis
# (see app_apis/core/customize.py and public/js/customize_form.js).
doctype_js = {"Customize Form": "public/js/customize_form.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "app_apis/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "app_apis.utils.jinja_methods",
# 	"filters": "app_apis.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "app_apis.install.before_install"
# Seed the app's Server Scripts and Client Scripts (app_apis/scripts/) into
# the desk -- only the ones that do not exist yet, so desk edits are never
# overwritten. See app_apis/core/scripts.py.
after_install = "app_apis.core.scripts.after_install"
after_migrate = "app_apis.core.scripts.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "app_apis.uninstall.before_uninstall"
# after_uninstall = "app_apis.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "app_apis.utils.before_app_install"
# after_app_install = "app_apis.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "app_apis.utils.before_app_uninstall"
# after_app_uninstall = "app_apis.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "app_apis.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "app_apis.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# `xticket` is a UI-created custom doctype, so it has no controller file to hold
# an on_update method -- doc_events is the only place this hook can live. See
# app_apis/xticket_events.py for why the trigger is `workflow_state`.
#
# Two handlers, listed separately rather than chained: they answer different
# questions ("tell the team" vs "ask the customer"), they are gated by
# different settings, and neither should be able to stop the other from
# running. Both swallow their own exceptions -- a handler in this list runs
# inside the operator's save, so anything that escapes would roll back their
# status change.
doc_events = {
    "xticket": {
        "on_update": [
            "app_apis.xticket_events.on_status_change",
            "app_apis.auto_messages.on_ticket_update",
        ],
    },
}

# Scheduled Tasks
# ---------------

# Scheduler Events
# ----------------
#
# Intentionally empty. The scheduled jobs are Scheduler Event Server Scripts
# now, visible and editable in the desk (Server Script list):
#   "App Apis - Subscription Reminders"  -> app_apis.core.jobs.subscription_reminders_hourly
#   "App Apis - Stale Ticket Reminders"  -> app_apis.core.jobs.stale_reminders_hourly
#   "Lebara Keepalive"                   -> the "lebara" API Server Script
# The passes still read their hour/weekday/enabled settings from the app_apis
# Single, as before.
# scheduler_events = {}

# Testing
# -------

# before_tests = "app_apis.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "app_apis.custom.task.CustomTaskMixin"
# }

# Customize Form: same class, but app_apis (a Single) may be customized.
override_doctype_class = {
	"Customize Form": "app_apis.core.customize.CustomizeForm",
}

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "app_apis.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "app_apis.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Fixtures
# --------
# Only desk-built records that are not scripts. The Client Scripts and Server
# Scripts are *seeded* from app_apis/scripts/ instead (after_install /
# after_migrate above): fixtures are re-imported with force=True on every
# migrate, which would overwrite any edit made to a script in the desk.
#
# The Fleet Audit / Fuel Efficiency dashboards are Custom HTML Blocks.
# Regenerate after editing them in the desk:
#     bench --site <site> export-fixtures --app app_apis
fixtures = [
    {
        "dt": "Custom HTML Block",
        "filters": [["name", "in", ["Fleet Audit", "Fuel Efficiency"]]],
    },
    {
        "dt": "Workspace",
        "filters": [["name", "in", ["Fuel Efficiency"]]],
    },
]

# Request Events
# ----------------
# Intentionally empty. An earlier design hooked before_request to monkeypatch
# the Server Script sandbox's HTTP helpers, so no Server Script could reach the
# network behind the app's back. That is now moot: there are no Server Scripts,
# and `server_script_enabled` is off. See app_apis/connector.py.
# before_request = []
# after_request = []

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"app_apis.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

