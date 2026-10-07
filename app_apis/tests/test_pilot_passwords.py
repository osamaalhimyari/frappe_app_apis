# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Tests for how app_apis.connector tries a Pilot account's passwords.

The settings table, the per-account memory and the HTTP request are replaced,
so these run with no site and no network:

    /home/frappe/frappe-bench/env/bin/python -m unittest \\
        app_apis.tests.test_pilot_passwords -v
"""

import unittest
from unittest import mock

import frappe

from app_apis import connector

OK = {"code": 0, "msg": "OK", "data": [{"imei": "1"}], "_pilot": {}}
REJECTED = {"code": -401, "msg": "401 Unauthorized -- Pilot rejected these credentials.", "data": [], "_pilot": {}}


def rows(*passwords):
	return [{"row": "r%d" % i, "no": i, "password": p} for i, p in enumerate(passwords, start=1)]


class Harness(unittest.TestCase):
	"""with_passwords() against a fake Pilot that accepts only `accepts`."""

	def walk(self, table, accepts, remembered="", conf=None, proves=None, fail_with=None):
		self.tried = []
		self.remembered_calls = []

		def attempt(pw):
			self.tried.append(pw)
			if fail_with:
				return fail_with
			return OK if pw in accepts else dict(REJECTED)

		conf = conf or {}
		with mock.patch.object(connector, "_table_passwords", return_value=table), \
		     mock.patch.object(connector, "_remembered_row", return_value=remembered), \
		     mock.patch.object(connector, "_remember",
		                       side_effect=lambda s, e, c, ok: self.remembered_calls.append((c and c["no"], ok))), \
		     mock.patch.object(frappe, "conf", frappe._dict(conf)):
			return connector.with_passwords("a@x.com", {"conf_key": "pilot_passwords"}, attempt, proves=proves)


class TestTryInOrder(Harness):
	def test_first_row_works(self):
		result, info = self.walk(rows("p1", "p2", "p3"), accepts={"p1"})
		self.assertEqual(self.tried, ["p1"])
		self.assertEqual(result["code"], 0)
		self.assertEqual(info, {"tried": 1, "used": 1, "all_rejected": False})

	def test_falls_through_to_the_second_then_the_third(self):
		_, info = self.walk(rows("p1", "p2", "p3"), accepts={"p2"})
		self.assertEqual(self.tried, ["p1", "p2"])
		self.assertEqual((info["tried"], info["used"]), (2, 2))
		_, info = self.walk(rows("p1", "p2", "p3"), accepts={"p3"})
		self.assertEqual(self.tried, ["p1", "p2", "p3"])
		self.assertEqual((info["tried"], info["used"]), (3, 3))

	def test_the_winner_is_remembered(self):
		self.walk(rows("p1", "p2", "p3"), accepts={"p3"})
		self.assertEqual(self.remembered_calls, [(3, True)])

	def test_the_remembered_row_goes_first_and_the_rest_keep_their_order(self):
		self.walk(rows("p1", "p2", "p3"), accepts={"p3"}, remembered="r3")
		self.assertEqual(self.tried, ["p3"])
		self.walk(rows("p1", "p2", "p3"), accepts={"p1"}, remembered="r3")
		self.assertEqual(self.tried, ["p3", "p1"])

	def test_a_remembered_row_that_is_no_longer_in_the_table_changes_nothing(self):
		self.walk(rows("p1", "p2"), accepts={"p2"}, remembered="deleted-row")
		self.assertEqual(self.tried, ["p1", "p2"])

	def test_when_the_remembered_password_stops_working_the_list_is_retried_and_the_new_one_remembered(self):
		_, info = self.walk(rows("p1", "p2", "p3"), accepts={"p2"}, remembered="r3")
		self.assertEqual(self.tried, ["p3", "p1", "p2"])
		self.assertEqual(self.remembered_calls, [(2, True)])
		self.assertEqual(info["used"], 2)


class TestEveryPasswordRejected(Harness):
	def test_all_rejected_returns_the_last_rejection_and_records_it(self):
		result, info = self.walk(rows("p1", "p2", "p3"), accepts=set())
		self.assertEqual(self.tried, ["p1", "p2", "p3"])
		self.assertEqual(result["code"], -401)
		self.assertTrue(info["all_rejected"])
		self.assertIn("all 3 passwords", result["msg"])
		self.assertIn("401 Unauthorized", result["msg"])
		self.assertEqual(self.remembered_calls, [(None, False)])

	def test_one_password_keeps_pilots_own_message(self):
		result, _ = self.walk(rows("p1"), accepts=set())
		self.assertNotIn("all 1", result["msg"])

	def test_the_message_never_contains_a_password(self):
		result, _ = self.walk(rows("hunter2", "swordfish"), accepts=set())
		self.assertNotIn("hunter2", str(result))
		self.assertNotIn("swordfish", str(result))


class TestWhatIsNotAPasswordVerdict(Harness):
	def test_a_timeout_stops_the_walk_and_remembers_nothing(self):
		timeout = {"code": -408, "msg": "timed out", "data": [], "_pilot": {}}
		result, info = self.walk(rows("p1", "p2", "p3"), accepts=set(), fail_with=timeout)
		self.assertEqual(self.tried, ["p1"])
		self.assertEqual(result["code"], -408)
		self.assertEqual(self.remembered_calls, [])
		self.assertFalse(info["all_rejected"])

	def test_forbidden_still_proves_the_password(self):
		forbidden = {"code": -403, "msg": "403", "data": [], "_pilot": {}}
		_, info = self.walk(rows("p1", "p2"), accepts=set(), fail_with=forbidden)
		self.assertEqual(self.tried, ["p1"])
		self.assertEqual(self.remembered_calls, [(1, True)])
		self.assertEqual(info["used"], 1)

	def test_a_pilot_error_on_a_signed_in_request_proves_the_password(self):
		# "Invalid imei" arrives as HTTP 200 with a positive code: the login worked.
		invalid = {"code": 1, "msg": "Invalid imei", "data": [], "_pilot": {}}
		_, info = self.walk(rows("p1", "p2"), accepts=set(), fail_with=invalid)
		self.assertEqual(self.tried, ["p1"])
		self.assertEqual(self.remembered_calls, [(1, True)])

	def test_a_result_that_does_not_prove_the_password_is_not_remembered(self):
		self.walk(rows("p1", "p2"), accepts={"p1"}, proves=lambda r: False)
		self.assertEqual(self.remembered_calls, [])


class TestWhereThePasswordsComeFrom(Harness):
	def test_no_password_at_all_returns_none(self):
		result, info = self.walk([], accepts=set())
		self.assertIsNone(result)
		self.assertEqual(info["tried"], 0)

	def test_site_config_override_goes_first_and_is_never_remembered(self):
		conf = {"pilot_passwords": {"a@x.com": "override"}}
		_, info = self.walk(rows("p1", "p2"), accepts={"override"}, conf=conf)
		self.assertEqual(self.tried, ["override"])
		self.assertEqual(info["used"], 0)

	def test_remember_ignores_a_site_config_override(self):
		db = mock.Mock()
		with mock.patch.object(frappe, "db", db):
			connector._remember({"account_no": 1}, "a@x.com", {"row": "", "no": 0, "password": "x"}, ok=True)
		db.get_value.assert_called()  # looked the account up ...
		db.set_value.assert_not_called()  # ... and wrote nothing
		db.commit.assert_not_called()

	def test_remember_writes_nothing_for_a_healthy_account(self):
		db = mock.Mock()
		db.get_value.return_value = frappe._dict(password_row="r2", last_failed=None, failures=0)
		with mock.patch.object(frappe, "db", db):
			connector._remember({"account_no": 1}, "a@x.com", {"row": "r2", "no": 2, "password": "x"}, ok=True)
		db.set_value.assert_not_called()
		db.commit.assert_not_called()

	def test_remember_updates_a_changed_winner_and_clears_the_failure(self):
		db = mock.Mock()
		db.get_value.return_value = frappe._dict(password_row="r1", last_failed="then", failures=2)
		with mock.patch.object(frappe, "db", db), \
		     mock.patch.object(frappe.utils, "now_datetime", return_value="now"):
			connector._remember({"account_no": 1}, "a@x.com", {"row": "r3", "no": 3, "password": "x"}, ok=True)
		values = db.set_value.call_args.args[2]
		self.assertEqual((values["password_row"], values["password_no"], values["failures"], values["last_failed"]),
		                 ("r3", 3, 0, None))
		db.commit.assert_called_once()

	def test_remember_counts_failures_in_a_row_and_forgets_the_row(self):
		db = mock.Mock()
		db.get_value.return_value = frappe._dict(password_row="r1", last_failed="then", failures=2)
		with mock.patch.object(frappe, "db", db), \
		     mock.patch.object(frappe.utils, "now_datetime", return_value="now"):
			connector._remember({"account_no": 1}, "a@x.com", None, ok=False)
		values = db.set_value.call_args.args[2]
		self.assertEqual((values["password_row"], values["password_no"], values["failures"]), ("", 0, 3))

	def test_remember_never_raises(self):
		db = mock.Mock()
		db.get_value.side_effect = RuntimeError("db down")
		with mock.patch.object(frappe, "db", db), mock.patch.object(frappe, "logger", mock.Mock()):
			connector._remember({"account_no": 1}, "a@x.com", {"row": "r1", "no": 1, "password": "x"}, ok=True)

	def test_site_config_override_falls_back_to_the_table(self):
		conf = {"pilot_passwords": {"a@x.com": "override"}}
		self.walk(rows("p1", "p2"), accepts={"p2"}, conf=conf)
		self.assertEqual(self.tried, ["override", "p1", "p2"])
		self.assertEqual(self.remembered_calls, [(2, True)])

	def test_the_same_password_in_both_places_is_tried_once(self):
		conf = {"pilot_passwords": {"a@x.com": "p1"}}
		self.walk(rows("p1", "p2"), accepts=set(), conf=conf)
		self.assertEqual(self.tried, ["p1", "p2"])


class TestTablePasswords(unittest.TestCase):
	def row(self, name, password):
		r = mock.Mock()
		r.name = name
		r.get_password.return_value = password
		return r

	def test_blank_rows_and_duplicates_are_skipped_and_positions_count_every_row(self):
		doc = mock.Mock()
		doc.get.return_value = [self.row("a", "p1"), self.row("b", ""), self.row("c", "p1"), self.row("d", "p4")]
		settings = {"doc": doc, "pw_table": "pilot_password_list"}
		got = connector._table_passwords(settings)
		self.assertEqual([(r["row"], r["no"], r["password"]) for r in got], [("a", 1, "p1"), ("d", 4, "p4")])
		doc.get.assert_called_once_with("pilot_password_list")

	def test_decrypted_once_per_settings_dict(self):
		doc = mock.Mock()
		doc.get.return_value = [self.row("a", "p1")]
		settings = {"doc": doc, "pw_table": "t"}
		connector._table_passwords(settings)
		connector._table_passwords(settings)
		doc.get.assert_called_once()


class TestFetchStatus(unittest.TestCase):
	"""_fetch_status walks the list; an explicit password does not."""

	def test_reports_which_password_worked_and_never_the_password(self):
		attempts = []

		def once(imei, email, node, settings, password=None):
			attempts.append(password)
			return dict(OK, _pilot={}) if password == "p2" else dict(REJECTED, _pilot={})

		with mock.patch.object(connector, "_fetch_once", side_effect=once), \
		     mock.patch.object(connector, "is_offline", return_value=False), \
		     mock.patch.object(connector, "_table_passwords", return_value=rows("p1", "p2")), \
		     mock.patch.object(connector, "_remembered_row", return_value=""), \
		     mock.patch.object(connector, "_remember"), \
		     mock.patch.object(frappe, "conf", frappe._dict()):
			body = connector._fetch_status("123", "a@x.com", "5", {"conf_key": "pilot_passwords"})
		self.assertEqual(attempts, ["p1", "p2"])
		self.assertEqual((body["_pilot"]["password_no"], body["_pilot"]["passwords_tried"]), (2, 2))
		self.assertNotIn("p2", str(body))

	def test_an_explicit_password_is_used_once_as_is(self):
		with mock.patch.object(connector, "_fetch_once", return_value=dict(REJECTED)) as once, \
		     mock.patch.object(connector, "is_offline", return_value=False), \
		     mock.patch.object(connector, "with_passwords") as walk:
			connector._fetch_status("123", "a@x.com", "5", {}, password="mine")
		once.assert_called_once()
		walk.assert_not_called()

	def test_no_password_configured_gives_the_set_passwords_message(self):
		with mock.patch.object(connector, "is_offline", return_value=False), \
		     mock.patch.object(connector, "_table_passwords", return_value=[]), \
		     mock.patch.object(frappe, "conf", frappe._dict()):
			body = connector._fetch_status(
				"123", "a@x.com", "5",
				{"base_url": "https://x", "label": "Pilot (WSL)", "conf_key": "pilot_passwords", "timeout": 5},
			)
		self.assertEqual(body["code"], -500)
		self.assertIn("Pilot Passwords", body["msg"])


if __name__ == "__main__":
	unittest.main()
