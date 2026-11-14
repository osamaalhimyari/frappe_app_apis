"""Access groups, the service-location placeholders, IM's 12-hour times and the health alert diff."""

import unittest
from unittest import mock

import frappe

from app_apis import access, chatwoot_connector, health
from app_apis.im_connector import _epoch


class TestAccess(unittest.TestCase):
	def allowed(self, rules, roles, group, user="u@x.y", staff=True):
		with mock.patch.object(access, "is_staff", return_value=staff), \
				mock.patch.object(access, "rules", return_value=rules), \
				mock.patch.object(access.frappe, "get_roles", return_value=roles):
			return access.allowed(group, user)

	def test_a_group_without_rows_is_open_to_all_staff(self):
		self.assertTrue(self.allowed({}, ["Stock User"], access.DELETE))

	def test_portal_and_guest_are_always_refused(self):
		self.assertFalse(self.allowed({}, ["Customer"], access.VIEW, staff=False))

	def test_a_group_with_rows_needs_one_of_its_roles(self):
		rules = {access.DELETE: ["Technical"]}
		self.assertFalse(self.allowed(rules, ["Stock User"], access.DELETE))
		self.assertTrue(self.allowed(rules, ["Stock User", "Technical"], access.DELETE))
		self.assertTrue(self.allowed(rules, ["Stock User"], access.VIEW))   # other groups stay open

	def test_system_manager_and_administrator_always_pass(self):
		rules = {access.ADMIN: ["Nobody Has This"]}
		self.assertTrue(self.allowed(rules, ["System Manager"], access.ADMIN))
		self.assertTrue(self.allowed(rules, [], access.ADMIN, user="Administrator"))

	def test_an_unknown_group_is_a_bug_not_a_pass(self):
		with self.assertRaises(ValueError):
			self.allowed({}, [], "Delete everything")


class TestServiceLocationPlaceholders(unittest.TestCase):
	def test_map_link_from_the_pin(self):
		doc = frappe._dict(service_location_type="At Customer Site", latitude=24.693238, longitude=46.796539)
		self.assertEqual(chatwoot_connector._map_link(doc),
		                 "https://www.google.com/maps/search/?api=1&query=24.693238,46.796539")

	def test_no_link_without_a_pin_or_for_a_remote_job(self):
		self.assertEqual(chatwoot_connector._map_link(frappe._dict(latitude=0, longitude=0)), "")
		self.assertEqual(chatwoot_connector._map_link(
			frappe._dict(service_location_type="Remote (no visit)", latitude=24.1, longitude=46.1)), "")

	def test_where_names_the_kind_and_the_place(self):
		doc = frappe._dict(service_location_type="At Customer Site", service_address="Al Rawabi, Riyadh")
		self.assertIn("Al Rawabi, Riyadh", chatwoot_connector._where(doc))
		self.assertIn("branch", chatwoot_connector._where(frappe._dict(service_location_type="At Our Branch")))
		self.assertEqual(chatwoot_connector._where(frappe._dict()), "")


class TestImTimes(unittest.TestCase):
	def test_twelve_hour_track_times(self):
		tz = {"data_timezone": "Asia/Riyadh"}
		self.assertEqual(_epoch("05-11-2026 02:15:33 PM", tz), _epoch("05-11-2026 14:15:33", tz))
		self.assertEqual(_epoch("05-11-2026 12:05:00 AM", tz), _epoch("05-11-2026 00:05:00", tz))
		self.assertIsNone(_epoch("--", tz))


class TestHealthProblems(unittest.TestCase):
	def test_only_broken_checks_are_problems(self):
		checks = [
			health._check("a", "Jobs", "A", health.FAIL, "last run failed"),
			health._check("b", "Jobs", "B", health.WARN, "2 failed runs"),
			health._check("c", "Jobs", "C", health.OK),
		]
		self.assertEqual(health._problems(checks), {"a": "A: last run failed"})
