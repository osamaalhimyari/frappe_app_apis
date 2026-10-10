import unittest
from unittest import mock

import frappe

from app_apis.core import events, im, messages, pilot_panel, platforms


class TestEvents(unittest.TestCase):
	def hooks(self, mapping):
		return mock.patch.object(events.frappe, "get_hooks", return_value=mapping)

	def test_unknown_event_is_refused(self):
		with self.assertRaises(ValueError):
			events.emit("after_everything")

	def test_handlers_are_read_from_hooks_and_called_with_the_payload(self):
		seen = []

		def handler(**data):
			seen.append(data)
			return "ok"

		with self.hooks({"after_delete": ["x.y.handler"]}), mock.patch.object(events.frappe, "get_attr", return_value=handler):
			out = events.emit("after_delete", platform="im", vehicle="V-1", imei="111", result={"ok": True})
		self.assertEqual(out, ["ok"])
		self.assertEqual(seen[0]["platform"], "im")
		self.assertEqual(seen[0]["event"], "after_delete")
		self.assertEqual(seen[0]["source"], "core")

	def test_a_before_handler_can_veto(self):
		def veto(**data):
			raise frappe.ValidationError("no")

		with self.hooks({"before_delete": ["x.veto"]}), mock.patch.object(events.frappe, "get_attr", return_value=veto):
			with self.assertRaises(frappe.ValidationError):
				events.emit("before_delete", platform="im")

	def test_an_after_handler_can_never_break_the_action(self):
		def boom(**data):
			raise RuntimeError("bad handler")

		ok = []
		with self.hooks({"after_delete": ["x.boom", "x.ok"]}), \
				mock.patch.object(events.frappe, "get_attr", side_effect=lambda p: boom if p == "x.boom" else (lambda **d: ok.append(1))), \
				mock.patch.object(events.frappe, "log_error") as log:
			events.emit("after_delete", platform="im")
		self.assertEqual(ok, [1])                      # the second handler still ran
		log.assert_called_once()

	def test_a_list_of_hook_dicts_and_duplicates_are_tolerated(self):
		with self.hooks([{"after_create": ["a.b"]}, {"after_create": ["a.b", "c.d"]}]):
			self.assertEqual(events.handlers("after_create"), ["a.b", "c.d"])


class TestImPureHelpers(unittest.TestCase):
	def test_tag_text_and_input_value(self):
		self.assertEqual(im.tag_text("<root><status>Match</status><iVehicleID>294497</iVehicleID></root>", "iVehicleID"), "294497")
		self.assertEqual(im.tag_text("<root></root>", "iVehicleID"), "")
		self.assertEqual(im.input_value('<input type="text" name="object_imei_no" value="123">', "object_imei_no"), "123")
		self.assertEqual(im.input_value("<input name='x'>", "object_imei_no"), "")

	def test_model_for(self):
		models = {"FMC920": "1787", "FMC130 Static": "2736"}
		self.assertEqual(im.model_for("FMC920", models), "1787")
		self.assertEqual(im.model_for("fmc-920", models), "1787")
		self.assertEqual(im.model_for("Teltonika FMC920", models), "1787")      # suffix match
		self.assertEqual(im.model_for("Smart (gps)", models), "")
		self.assertEqual(im.model_for("", models), "")

	def test_build_form_carries_the_ids_and_the_defaults(self):
		form = im.build_form(name="4324", imei="123", company="49291", branch="54742", model="1787",
		                     sim_number="+966 55-12", reseller="3237", admin_id="1213", sim_provider="2",
		                     sensors={"rows": [{"property_name": "Ignition"}], "calib": "", "screen": {}}, user="osama2@im2m.ws")
		self.assertEqual(form["object_company_id"], "49291")
		self.assertEqual(form["gps_device_location_id"], "54742")
		self.assertEqual(form["object_reseller_entity_id"], "3237")
		self.assertEqual(form["object_sim_card_no"], "96655" + "12")           # digits only
		self.assertEqual(form["vehicle_no"], "4324")
		self.assertEqual(form["mode"], "insert")                                # a default is still there
		self.assertIn("Ignition", form["iframedata1"])
		self.assertEqual(form["vehicle_model_id"], form["vehicle_model"])

	def test_default_sensor_templates_are_present_for_the_known_models(self):
		self.assertEqual(len(im.DEFAULT_SENSORS["1787"]), 2)
		self.assertEqual(im.TEMPLATE_VEHICLE["1436"], "255059")


class TestPilotPanelPure(unittest.TestCase):
	def types(self, names):
		return {"rows": [{"id": i + 1, "name": n} for i, n in enumerate(names)], "err": ""}

	def test_resolve_model_prefers_the_exact_or_shortest_match(self):
		t = self.types(["Teltonika FMC920", "Teltonika FMC920 Long", "Concox JC261"])
		self.assertEqual(pilot_panel.resolve_model("FMC920", 1, t)["name"], "Teltonika FMC920")
		self.assertEqual(pilot_panel.resolve_model("JC261", 1, t)["name"], "Concox JC261")
		self.assertEqual(pilot_panel.resolve_model("Smart (gps)", 1, t)["name"], "")
		self.assertIn("MODEL_OVERRIDES", pilot_panel.resolve_model("Smart (gps)", 1, t)["why"])
		self.assertEqual(pilot_panel.resolve_model("", 1, t)["why"], "the vehicle has no Device Type")

	def test_a_failed_type_list_is_reported_not_guessed(self):
		got = pilot_panel.resolve_model("FMC920", 1, {"rows": [], "err": "get_vehicle_types failed"})
		self.assertEqual(got["name"], "")
		self.assertEqual(got["why"], "get_vehicle_types failed")

	def test_create_refuses_incomplete_input_without_calling_the_panel(self):
		with mock.patch.object(pilot_panel, "call") as call:
			out = pilot_panel.create("", "name", 1, "org", "Teltonika FMC920")
		self.assertEqual(out["verdict"], "failure")
		call.assert_not_called()

	def test_create_reads_the_vehicle_back_before_it_says_uploaded(self):
		finds = [{"state": "no", "row": {}, "detail": "not there", "agent_id": ""},
		         {"state": "yes", "row": {}, "detail": "found as X", "agent_id": "77"}]
		with mock.patch.object(pilot_panel, "find", side_effect=finds), \
				mock.patch.object(pilot_panel, "call", return_value={"ok": True, "status": 200, "body": {"agent_id": 77}, "text": ""}), \
				mock.patch.object(pilot_panel, "ensure_sensors", return_value={"ok": True, "msg": "sensors: 6 added"}):
			out = pilot_panel.create("111", "X", 5, "Org", "Teltonika FMC920")
		self.assertEqual(out["verdict"], "uploaded")
		self.assertEqual(out["agent_id"], "77")

	def test_a_panel_that_says_yes_but_is_not_there_is_a_failure(self):
		finds = [{"state": "no", "row": {}, "detail": "", "agent_id": ""}, {"state": "no", "row": {}, "detail": "", "agent_id": ""}]
		with mock.patch.object(pilot_panel, "find", side_effect=finds), \
				mock.patch.object(pilot_panel, "call", return_value={"ok": True, "status": 200, "body": {"agent_id": 77}, "text": ""}):
			out = pilot_panel.create("111", "X", 5, "Org", "Teltonika FMC920")
		self.assertEqual(out["verdict"], "failure")
		self.assertFalse(out["ok"])

	def test_delete_of_an_unknown_state_deletes_nothing(self):
		with mock.patch.object(pilot_panel, "find", return_value={"state": "unknown", "detail": "no answer", "agent_id": ""}), \
				mock.patch.object(pilot_panel, "call") as call:
			out = pilot_panel.delete("111", 1)
		self.assertEqual(out["verdict"], "not_deleted")
		call.assert_not_called()


class TestStatusDecision(unittest.TestCase):
	def dec(self, boxes=None, deleted="ch_pilot_wsl", pilot="no", status="Installed", date=None):
		return platforms.status_decision(boxes or {}, deleted, pilot, "Pilot 2", status, date, "2026-10-10")

	def test_nothing_left_sets_deleted_and_the_date(self):
		d = self.dec({"ch_pilot_wsl": 1})
		self.assertTrue(d["changed"])
		self.assertEqual(d["updates"]["device_statues"], "Deleted")
		self.assertEqual(d["updates"]["deletion_date"], "2026-10-10")
		self.assertEqual(d["updates"]["ch_pilot_wsl"], 0)

	def test_pilot_flavour_boxes_go_when_pilot_is_gone(self):
		d = self.dec({"ch_pilot_tracking_only": 1}, deleted=None)
		self.assertTrue(d["changed"])
		self.assertIn("ch_pilot_tracking_only", d["unticked"])

	def test_another_system_keeps_the_vehicle(self):
		for box in ("ch_trakzee", "ch_sarp", "ch_fmsi_medicine", "ch_fmsi_balady"):
			d = self.dec({box: 1}, deleted="ch_pilot_wsl")
			self.assertFalse(d["changed"], box)
			self.assertNotIn("device_statues", d["updates"])

	def test_still_on_the_other_pilot_estate_keeps_it_and_unknown_counts_as_there(self):
		self.assertFalse(self.dec({"ch_pilot_wsl": 1}, pilot="yes")["changed"])
		self.assertFalse(self.dec({"ch_pilot_wsl": 1}, pilot="unknown")["changed"])
		self.assertNotIn("ch_pilot_tracking_only", self.dec({"ch_pilot_tracking_only": 1}, pilot="yes")["updates"])

	def test_the_deletion_date_is_never_left_empty_and_never_overwritten(self):
		self.assertEqual(self.dec(status="Deleted", date=None)["updates"], {"deletion_date": "2026-10-10"})
		self.assertEqual(self.dec(status="Deleted", date="2026-01-26")["updates"], {})
		self.assertNotIn("deletion_date", self.dec(date="2025-05-05")["updates"])


class TestMessages(unittest.TestCase):
	def rows(self, *rows):
		doc = frappe._dict(custom_messages=[frappe._dict(r) for r in rows])
		doc.get = lambda k, d=None: getattr(doc, k, d)
		return mock.patch.object(messages.frappe, "get_cached_doc", return_value=doc)

	def test_lookup_is_case_insensitive_and_tidy(self):
		with self.rows({"code": " bs ", "message": "Hello {customer}", "whatsapp_template": "", "template_variables": ""}):
			self.assertEqual(messages.get("BS")["code"], "BS")
			self.assertEqual(messages.get("bs")["message"], "Hello {customer}")
			self.assertIsNone(messages.get("ZZ"))

	def test_render_fills_placeholders_and_names_the_known_codes_on_a_miss(self):
		with self.rows({"code": "BS", "message": "Hello {customer}, plate {plate}"}):
			self.assertEqual(messages.render("BS", {"customer": "Ali", "plate": "1234"}), "Hello Ali, plate 1234")
			with self.assertRaises(LookupError) as e:
				messages.render("NOPE")
			self.assertIn("BS", str(e.exception))

	def test_dry_run_sends_nothing_and_says_how_it_would_go(self):
		with self.rows({"code": "BS", "message": "Hi {customer}", "whatsapp_template": "Renew 1"}):
			with mock.patch("app_apis.chatwoot_connector._send") as send:
				out = messages.send("BS", "+966500000000", {"customer": "Ali"}, dry_run=True)
			send.assert_not_called()
		self.assertTrue(out["ok"])
		self.assertTrue(out["dry_run"])
		self.assertEqual(out["text"], "Hi Ali")
		self.assertIn("WhatsApp template Renew 1", out["msg"])

	def test_unknown_code_is_a_clean_failure(self):
		with self.rows():
			out = messages.send("NOPE", "+966500000000")
		self.assertFalse(out["ok"])


class TestApiFrontDoor(unittest.TestCase):
	def setUp(self):
		from app_apis.core import api

		class Raw:
			"""The api functions without the whitelist's argument validation, which wants a live frappe.local."""

			def __getattr__(_, name):
				fn = getattr(api, name)
				return getattr(fn, "__wrapped__", fn)

		self.api = Raw()
		self.api.OPERATORS = api.OPERATORS
		self.api.frappe, self.api.platforms, self.api.events = api.frappe, api.platforms, api.events
		self.only = mock.patch.object(api.frappe, "only_for")
		self.only.start()
		# frappe.throw needs a live frappe.local (message log); a bare unit test has none
		self.throw = mock.patch.object(api.frappe, "throw", side_effect=frappe.ValidationError)
		self.throw.start()

	def tearDown(self):
		self.throw.stop()
		self.only.stop()

	def test_create_is_a_dry_run_unless_told_otherwise(self):
		with mock.patch.object(self.api.platforms, "create", return_value={}) as create:
			self.api.create("pilot2", "V-1")
			self.api.create("pilot2", "V-1", dry_run=0)
		self.assertTrue(create.call_args_list[0].kwargs["dry_run"])
		self.assertFalse(create.call_args_list[1].kwargs["dry_run"])

	def test_delete_needs_the_typed_word(self):
		with mock.patch.object(self.api.platforms, "delete") as delete:
			with self.assertRaises(frappe.ValidationError):
				self.api.delete("im", "V-1", confirm="delete")
			delete.assert_not_called()
			self.api.delete("im", "V-1", confirm="DELETE")
			delete.assert_called_once()

	def test_wasl_delete_needs_the_typed_word(self):
		with mock.patch.object(self.api.platforms, "wasl_delete") as wd:
			with self.assertRaises(frappe.ValidationError):
				self.api.wasl_delete("V-1", confirm="")
			wd.assert_not_called()

	def test_roles_are_checked_before_anything_runs(self):
		self.only.stop()
		with mock.patch.object(self.api.frappe, "only_for", side_effect=frappe.PermissionError) as only, \
				mock.patch.object(self.api.platforms, "delete") as delete:
			with self.assertRaises(frappe.PermissionError):
				self.api.delete("im", "V-1", confirm="DELETE")
			delete.assert_not_called()
			only.assert_called_with(self.api.OPERATORS)
		self.only.start()

	def test_notify_accepts_only_after_events_and_cannot_overwrite_the_event(self):
		seen = {}
		with mock.patch.object(self.api.frappe, "session", mock.MagicMock(user="a@b.c")), \
				mock.patch.object(self.api.events, "emit", side_effect=lambda e, **d: seen.update(d) or [1]):
			with self.assertRaises(frappe.ValidationError):
				self.api.notify("before_delete", platform="im")
			out = self.api.notify("after_delete", platform="im", vehicle="", imei="1",
			                      detail={"platform": "SPOOF", "source": "core", "result": {"ok": True}})
		self.assertEqual(out["handlers_called"], 1)
		self.assertEqual(seen["platform"], "im")                 # not "SPOOF"
		self.assertEqual(seen["source"], "script")               # not "core"
		self.assertEqual(seen["result"], {"ok": True})
