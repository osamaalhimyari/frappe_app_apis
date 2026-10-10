import json
import os
import unittest

from app_apis.core import scripts

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")


class TestCarrySettings(unittest.TestCase):
	def test_switch_values_are_kept_but_text_comes_from_the_new_script(self):
		old = "var SHOW_A = 0;   // old comment\nvar SHOW_B = 1;\nold_code();\n"
		new = "var SHOW_A = 1;   // new comment\nvar SHOW_B = 1;\nvar SHOW_C = 1;\nnew_code();\n"
		out = scripts.carry_settings(old, new, prefix="SHOW_")
		self.assertIn("var SHOW_A = 0;   // new comment", out)
		self.assertIn("var SHOW_B = 1;", out)
		self.assertIn("var SHOW_C = 1;", out)      # not in the old script: stays as shipped
		self.assertIn("new_code();", out)
		self.assertNotIn("old_code", out)

	def test_named_python_setting_is_kept(self):
		old = 'LIVE_TARGETS = ("pilot_wsl",)\nx = 1\n'
		new = '# comment\nLIVE_TARGETS = ("pilot_wsl", "im")   # armed\nx = 2\n'
		out = scripts.carry_settings(old, new, names=["LIVE_TARGETS"])
		self.assertIn('LIVE_TARGETS = ("pilot_wsl",)   # armed', out)
		self.assertIn("x = 2", out)

	def test_nothing_to_carry_returns_the_new_script_untouched(self):
		new = "var SHOW_A = 1;\n"
		self.assertEqual(scripts.carry_settings("", new, prefix="SHOW_"), new)
		self.assertEqual(scripts.carry_settings("other = 1\n", new, names=["LIVE_TARGETS"]), new)

	def test_line_endings_are_preserved(self):
		out = scripts.carry_settings("var SHOW_A = 0;\r\n", "var SHOW_A = 1;\r\nz\r\n", prefix="SHOW_")
		self.assertEqual(out, "var SHOW_A = 0;\r\nz\r\n")

	def test_a_similarly_named_setting_is_not_confused(self):
		old = "var SHOW_IM = 0;\n"
		new = "var SHOW_IM_LONG = 1;\nvar SHOW_IM = 1;\n"
		out = scripts.carry_settings(old, new, prefix="SHOW_")
		self.assertEqual(out, "var SHOW_IM_LONG = 1;\nvar SHOW_IM = 0;\n")


class TestShippedScripts(unittest.TestCase):
	def test_every_manifest_file_exists(self):
		with open(os.path.join(SCRIPTS, "server_scripts.json")) as f:
			manifest = json.load(f)
		for meta in manifest:
			self.assertTrue(os.path.exists(os.path.join(SCRIPTS, "server", meta["file"])), meta["file"])

	def test_vehicle_upload_api_is_managed_and_compiles_in_the_sandbox(self):
		from frappe.utils.safe_exec import FrappeTransformer
		from RestrictedPython import compile_restricted

		with open(os.path.join(SCRIPTS, "server_scripts.json")) as f:
			meta = next(m for m in json.load(f) if m["name"] == "vehicle_upload_api")
		self.assertTrue(meta["managed"])
		self.assertIn("LIVE_TARGETS", meta["carry"])
		with open(os.path.join(SCRIPTS, "server", meta["file"])) as f:
			source = f.read()
		# the sandbox refuses tuple unpacking in some forms, `+=` on dict items, underscores...
		compile_restricted(source, "vehicle_upload_api", "exec", policy=FrappeTransformer)

	def test_vehicle_upload_client_script_is_managed_with_switches(self):
		with open(os.path.join(SCRIPTS, "client_script.json")) as f:
			rows = {r["name"]: r for r in json.load(f)}
		row = rows["customer-vehicle-upload"]
		self.assertTrue(row["managed"])
		self.assertEqual(row["view"], "Form")
		self.assertEqual(row["carry_prefix"], "SHOW_")
		# Delete is its own button on the vehicle form (beside Upload), one switch per item ...
		self.assertIn('var DELETE_GROUP = "Delete";', row["script"])
		# ... and WASL-only / Suspend SIM sit in a third button of their own, not in Delete
		self.assertIn('var OTHER_GROUP = "WASL / SIM";', row["script"])
		self.assertIn("__(OTHER_GROUP)", row["script"])
		# Check SIM goes into the existing WASL button (beside Link vehicle), not the new one
		self.assertIn("var SHOW_CHECK_SIM =", row["script"])
		self.assertIn('var WASL_GROUP = "WASL";', row["script"])
		self.assertIn("}, __(WASL_GROUP));", row["script"])
		for switch in ("SHOW_DELETE_PILOT_WSL", "SHOW_DELETE_PILOT2", "SHOW_DELETE_IM",
		               "SHOW_DELETE_WASL", "SHOW_SUSPEND_SIM"):
			self.assertIn("var " + switch + " =", row["script"])
		# ... and not a separate list-view script
		self.assertNotIn("customer-vehicle-delete", rows)


class TestExpiredSubscriptions(unittest.TestCase):
	def _manifest(self, name):
		with open(os.path.join(SCRIPTS, "server_scripts.json")) as f:
			return next(m for m in json.load(f) if m["name"] == name)

	def test_api_is_managed_and_compiles_in_the_sandbox(self):
		from frappe.utils.safe_exec import FrappeTransformer
		from RestrictedPython import compile_restricted

		meta = self._manifest("expired_devices_api")
		self.assertTrue(meta["managed"])
		self.assertIn("LIVE", meta["carry"])
		with open(os.path.join(SCRIPTS, "server", meta["file"])) as f:
			source = f.read()
		compile_restricted(source, "expired_devices_api", "exec", policy=FrappeTransformer)
		# individuals only: Customer Type Individual and Paying not Company, in BOTH queries
		self.assertEqual(source.count("+ INDIVIDUAL_ONLY +"), 2)
		self.assertIn("!= 'company'", source)

	def test_block_is_shipped_managed_with_links_and_row_tools(self):
		folder = os.path.join(SCRIPTS, "html_blocks", "expired_subscriptions")
		with open(os.path.join(folder, "meta.json")) as f:
			meta = json.load(f)
		self.assertEqual(meta["name"], "Expired Subscriptions")
		self.assertTrue(meta["managed"])
		with open(os.path.join(folder, "block.js")) as f:
			js = f.read()
		for needle in ("function vehicle_url", "function customer_url", "function tool_buttons", "function run_tool",
		               "function check_sim", '"del_p1"', '"del_p2"', '"del_im"', '"wasl"', '"suspend"'):
			self.assertIn(needle, js)

	def test_carry_keeps_the_live_switch(self):
		out = scripts.carry_settings("LIVE = 1   # armed here\n", "LIVE = 0   # shipped\nx = 2\n", names=["LIVE"])
		self.assertEqual(out, "LIVE = 1   # shipped\nx = 2\n")


class TestFleetAuditWasteScript(unittest.TestCase):
	def test_script_is_managed_carries_its_settings_and_compiles_in_the_sandbox(self):
		from frappe.utils.safe_exec import FrappeTransformer
		from RestrictedPython import compile_restricted

		with open(os.path.join(SCRIPTS, "server_scripts.json")) as f:
			meta = next(m for m in json.load(f) if m["name"] == "fleet_audit_waste")
		self.assertTrue(meta["managed"])
		self.assertEqual(meta["api_method"], "fleet_audit_waste")
		for name in ("SUSPENDED_PRICE", "WASTE_ERP_STATUSES", "WASTE_EXPIRED_DAYS", "WASTE_VERDICTS"):
			self.assertIn(name, meta["carry"])
		with open(os.path.join(SCRIPTS, "server", meta["file"])) as f:
			source = f.read()
		compile_restricted(source, "fleet_audit_waste", "exec", policy=FrappeTransformer)
		old = "WASTE_EXPIRED_DAYS = 30\nWASTE_ERP_STATUSES = [\"Deleted\", \"Scrap\"]\n"
		out = scripts.carry_settings(old, source, names=meta["carry"])
		self.assertIn("WASTE_EXPIRED_DAYS = 30", out)
		self.assertIn('WASTE_ERP_STATUSES = ["Deleted", "Scrap"]', out)


class TestDeleteSetsStatus(unittest.TestCase):
	def test_status_rule_is_in_the_shipped_script(self):
		with open(os.path.join(SCRIPTS, "server", "vehicle_upload_api.py")) as f:
			source = f.read()
		self.assertIn("def after_platform_delete", source)
		# the four Pilot boxes are ONE system, judged by a live look at the other Pilot estate
		self.assertIn("def pilot_still_there", source)
		self.assertIn('["ch_pilot_tracking_only", "Pilot Tracking Only"]', source)
		self.assertNotIn("PLATFORM_CHECKS", source)
		# every box in the Platforms section is counted
		for box in ("ch_pilot_wsl", "ch_pilot_tow", "ch_pilot_sfda", "ch_pilot_tracking_only", "ch_trakzee",
		            "ch_sarp", "ch_fmsi_medicine", "ch_fmsi_balady"):
			self.assertIn('"' + box + '"', source)
		# only a platform delete triggers it, never the WASL-only delete or the SIM suspend
		self.assertIn('target in ("pilot_wsl", "pilot2", "im")', source)
		# written with db.set_value: a document save would run the Customer Vehicle save scripts
		self.assertIn("frappe.db.set_value(VEH_DT, vehicle_name, updates)", source)
		# the Deletion Date goes in with the status, and is filled even for a vehicle that is already Deleted
		self.assertEqual(source.count('updates["deletion_date"] = frappe.utils.nowdate()'), 2)


class TestInstallHooks(unittest.TestCase):
	"""A patch does not run on a fresh install, so install AND migrate must both do the DB setup."""

	def test_install_and_migrate_both_seed_and_ensure_site_data(self):
		from unittest import mock

		for hook in (scripts.after_install, scripts.after_migrate):
			with mock.patch.object(scripts, "seed", return_value=["x"]) as seed, \
					mock.patch.object(scripts, "ensure_site_data") as ensure, \
					mock.patch.object(scripts.frappe, "set_user"), \
					mock.patch.object(scripts.frappe, "db", mock.MagicMock()):
				hook()
			seed.assert_called_once()
			ensure.assert_called_once()

	def test_one_failing_step_does_not_stop_the_other(self):
		from unittest import mock

		from app_apis.patches import add_im_customer_cache_fields, seed_im_upload_settings

		with mock.patch.object(add_im_customer_cache_fields, "execute", side_effect=RuntimeError("boom")), \
				mock.patch.object(seed_im_upload_settings, "execute") as second, \
				mock.patch.object(scripts.frappe, "log_error"), \
				mock.patch.object(scripts.frappe, "conf", {"server_script_enabled": 1}):
			scripts.ensure_site_data()
		second.assert_called_once()


class TestSystemsSection(unittest.TestCase):
	def test_hourly_wasl_job_and_form_section_are_shipped_and_managed(self):
		with open(os.path.join(SCRIPTS, "server_scripts.json")) as f:
			job = next(m for m in json.load(f) if m["name"] == "App Apis - WASL Status Sync")
		self.assertEqual(job["script_type"], "Scheduler Event")
		self.assertEqual(job["event_frequency"], "Hourly Long")
		self.assertTrue(job["managed"])
		self.assertTrue(os.path.exists(os.path.join(SCRIPTS, "server", job["file"])))
		with open(os.path.join(SCRIPTS, "client_script.json")) as f:
			rows = {r["name"]: r for r in json.load(f)}
		cs = rows["customer-vehicle-systems"]
		self.assertTrue(cs["managed"])
		self.assertEqual((cs["dt"], cs["view"]), ("Customer Vehicle", "Form"))
		self.assertIn("app_apis.vehicle_systems.get_systems", cs["script"])
		self.assertIn("var SHOW_SYSTEMS_SECTION =", cs["script"])

	def test_the_section_never_calls_an_outside_system(self):
		# it must only read what the hourly jobs stored: no platform URL, no live-check method
		with open(os.path.join(os.path.dirname(SCRIPTS), "vehicle_systems.py")) as f:
			source = f.read()
		for forbidden in ("requests.", "make_get_request", "make_post_request", "panel_find", "get_vehicle_live"):
			self.assertNotIn(forbidden, source)
