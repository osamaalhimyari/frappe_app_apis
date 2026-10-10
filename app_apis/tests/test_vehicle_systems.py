import datetime
import json
import unittest

from app_apis import vehicle_systems as vs
from app_apis import wasl

NOW = datetime.datetime(2026, 10, 10, 15, 0, 0)
FRESH = datetime.datetime(2026, 10, 10, 14, 0, 0)
OLD = datetime.datetime(2026, 9, 16, 15, 9, 0)


def veh(**kw):
	base = {"device_serial": "111", "sim_serial": "8996606099008658488", "device_statues": "Installed"}
	base.update(kw)
	return base


def audit(**kw):
	base = {"audited_at": FRESH, "on_pilot_1": 1, "on_pilot_2": 0, "on_im": 0, "pilot_active": 1,
	        "pilot_account": "Im@wasl.com", "pilot_folder": "Acme", "pilot_last_seen": FRESH,
	        "im_company": "", "im_status": "", "im_last_seen": None}
	base.update(kw)
	return base


class TestWaslParsing(unittest.TestCase):
	def test_state_from_status_and_reference_key(self):
		self.assertEqual(wasl.wasl_state_of({"status": 1, "referencekey": "ABC"}), "linked")
		self.assertEqual(wasl.wasl_state_of({"status": "1", "referencekey": "ABC"}), "linked")
		self.assertEqual(wasl.wasl_state_of({"status": 0, "referencekey": "ABC"}), "registered_inactive")
		self.assertEqual(wasl.wasl_state_of({"status": 0, "referencekey": None}), "saved_not_registered")
		self.assertEqual(wasl.wasl_state_of({"status": 1, "referencekey": ""}), "saved_not_registered")

	def test_imei_is_read_from_object_json(self):
		row = {"object_json": json.dumps({"imeiNumber": "359633108574240", "plateType": "2"})}
		self.assertEqual(wasl.wasl_imei_of(row), "359633108574240")
		self.assertEqual(wasl.wasl_imei_of({"object_json": "not json"}), "")
		self.assertEqual(wasl.wasl_imei_of({}), "")

	def test_epoch_to_datetime(self):
		self.assertIsNone(wasl._epoch_to_datetime(None))
		self.assertIsNone(wasl._epoch_to_datetime(0))
		self.assertIsInstance(wasl._epoch_to_datetime(1702884667), datetime.datetime)


class TestWhereItIs(unittest.TestCase):
	def where(self, d):
		return {w["key"]: w for w in d["where"]}

	def test_comes_from_the_audit_report_with_its_details(self):
		d = vs.build_summary(veh(), audit(), None, None, FRESH, NOW)
		w = self.where(d)
		self.assertTrue(w["pilot_wsl"]["on"])
		self.assertEqual(w["pilot_wsl"]["detail"], ["Im@wasl.com", "Acme"])
		self.assertIs(w["pilot_wsl"]["active"], True)
		self.assertFalse(w["im"]["on"])

	def test_the_vehicles_own_ticks_and_emails_are_never_used(self):
		# the vehicle says Pilot WSL + IM + Pilot 2; the report says it is only on Pilot 2's absence -> report wins
		v = veh(ch_pilot_wsl=1, ch_trakzee=1, email_pilot2="a@b.c", im_platform="x@y.z")
		d = vs.build_summary(v, audit(on_pilot_1=0, on_pilot_2=0, on_im=0), None, None, FRESH, NOW, pilot2_read=True)
		w = self.where(d)
		self.assertFalse(w["pilot_wsl"]["on"])
		self.assertFalse(w["pilot2"]["on"])
		self.assertFalse(w["im"]["on"])
		self.assertTrue(all("ticked" not in x for x in d["where"]))
		self.assertFalse(any("ticked" in i for i in d["issues"]))

	def test_an_estate_the_audit_did_not_read_is_not_reported_as_not_found(self):
		d = vs.build_summary(veh(), audit(), None, None, FRESH, NOW, pilot2_read=False)
		p2 = self.where(d)["pilot2"]
		self.assertIsNone(p2["on"])
		self.assertEqual(p2["note"], "not read by the last audit")

	def test_no_audit_row_makes_no_platform_claims(self):
		d = vs.build_summary(veh(), None, None, None, FRESH, NOW)
		self.assertFalse(d["audit"]["found"])
		self.assertTrue(all(w["on"] is None for w in d["where"]))

	def test_stale_audit_carries_its_age(self):
		d = vs.build_summary(veh(), audit(audited_at=OLD), None, None, FRESH, NOW)
		self.assertTrue(d["audit"]["stale"])
		self.assertEqual(d["audit"]["age_days"], 23)

	def test_im_details(self):
		d = vs.build_summary(veh(), audit(on_pilot_1=0, on_im=1, im_company="Mutawa", im_status="Active",
		                                  im_last_seen=FRESH), None, None, FRESH, NOW)
		im = self.where(d)["im"]
		self.assertTrue(im["on"])
		self.assertEqual(im["detail"], ["Mutawa", "Active"])


class TestWaslAndSim(unittest.TestCase):
	def sim(self, **kw):
		base = {"msisdn": "830", "iccid": "899", "status": "Active", "imei": "111", "modified": FRESH,
		        "last_connection": FRESH, "in_data_session": 1}
		base.update(kw)
		return base

	def test_everything_known_and_consistent(self):
		d = vs.build_summary(veh(), audit(), {"state": "linked", "referencekey": "K1", "wasl_status": "1",
		                                       "wasl_ts": FRESH}, self.sim(), FRESH, NOW)
		self.assertEqual(d["wasl"]["state"], "linked")
		self.assertEqual(d["sim"]["imei_matches"], "yes")
		self.assertEqual(d["issues"], [])

	def test_sim_in_another_device_is_flagged(self):
		d = vs.build_summary(veh(), None, None, self.sim(imei="999"), FRESH, NOW)
		self.assertEqual(d["sim"]["imei_matches"], "no")
		self.assertTrue(any("different device" in i for i in d["issues"]))

	def test_sim_without_an_imei_is_unknown_not_a_mismatch(self):
		d = vs.build_summary(veh(), None, None, self.sim(imei="", status="Idle"), FRESH, NOW)
		self.assertEqual(d["sim"]["imei_matches"], "unknown")
		self.assertFalse(any("different device" in i for i in d["issues"]))

	def test_suspended_sim_is_an_issue(self):
		d = vs.build_summary(veh(), None, None, self.sim(status="Suspend"), FRESH, NOW)
		self.assertTrue(any("Suspend" in i for i in d["issues"]))

	def test_wasl_not_listed_vs_never_synced(self):
		listed = vs.build_summary(veh(), audit(), None, None, FRESH, NOW)
		self.assertEqual(listed["wasl"]["state"], "not_listed")
		self.assertTrue(any("not on the WASL list" in i for i in listed["issues"]))      # report says on Pilot WSL
		never = vs.build_summary(veh(), audit(), None, None, None, NOW)
		self.assertEqual(never["wasl"]["state"], "unknown")                                # no claim either way

	def test_linked_to_wasl_but_not_found_on_pilot(self):
		d = vs.build_summary(veh(), audit(on_pilot_1=0), {"state": "linked", "referencekey": "K"}, None, FRESH, NOW)
		self.assertTrue(any("did not find it on Pilot" in i for i in d["issues"]))
