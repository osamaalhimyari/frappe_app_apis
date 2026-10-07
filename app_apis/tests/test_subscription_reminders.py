# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Tests for the decisions in `app_apis.subscription_reminders`.

Everything that touches the database or Chatwoot is replaced, so these run in a
second with no site:

    /home/frappe/frappe-bench/env/bin/python -m unittest \\
        app_apis.tests.test_subscription_reminders -v
"""

import datetime
import unittest
from unittest import mock

import frappe

from app_apis import subscription_reminders as sr
from app_apis.phone import saudi_mobile

TODAY = "2026-10-07"


def cfg(**over):
	base = dict(sr.DEFAULTS)
	base.update(over)
	base["doc"] = frappe._dict({"chatwoot_use_templates": 1})
	return base


def vehicle(name, customer, days, mobile="0501234567", plate=None, deletion=None):
	expiry = datetime.date(2026, 10, 7) + datetime.timedelta(days=days)
	return frappe._dict(
		name=name, customer=customer, license_plate=plate or name, e_license_plate="", plate_num="",
		driver_mobile=mobile, subscription_expiry_date=expiry, device_statues="Installed",
		paying=1, deletion_date=deletion,
	)


class TestSaudiMobile(unittest.TestCase):
	def test_accepts_every_way_of_writing_a_saudi_mobile(self):
		for raw in ("0501234567", "501234567", "+966501234567", "966501234567", "00966501234567",
		            "+966 50 123 4567", "050-123-4567",
		            "+9660501234567", "9660501234567 "):
			self.assertEqual(saudi_mobile(raw), "+966501234567", raw)

	def test_rejects_what_cannot_be_one(self):
		for raw in (None, "", "+966", "0112345678", "+971501234567", "+201234567890", "12345",
		            "+966598222588 / 0562261442"):
			self.assertIsNone(saudi_mobile(raw), raw)


class TestVariablesText(unittest.TestCase):
	def test_empty_variable_gets_the_plate(self):
		self.assertEqual(sr._variables_text("{{1}} = "), "{{1}} = {plate}")
		self.assertEqual(sr._variables_text("{{1}} =\n{{2}} = {customer}"), "{{1}} = {plate}\n{{2}} = {customer}")

	def test_nothing_at_all_means_the_plate_is_variable_one(self):
		self.assertEqual(sr._variables_text(""), "{{1}} = {plate}")
		self.assertEqual(sr._variables_text(None), "{{1}} = {plate}")


class TestDueNow(unittest.TestCase):
	def at(self, hour, day=7):
		return datetime.datetime(2026, 10, day, hour, 30)  # 2026-10-07 is a Wednesday

	def test_every_hour_from_start_until_end_exclusive(self):
		c = cfg(subscription_reminder_hour=9, subscription_reminder_end_hour=21)
		self.assertFalse(sr.due_now(c, self.at(8))[0])
		self.assertTrue(sr.due_now(c, self.at(9))[0])
		self.assertTrue(sr.due_now(c, self.at(20))[0])
		self.assertFalse(sr.due_now(c, self.at(21))[0])

	def test_a_bad_end_hour_still_allows_the_start_hour(self):
		c = cfg(subscription_reminder_hour=9, subscription_reminder_end_hour=5)
		self.assertTrue(sr.due_now(c, self.at(9))[0])
		self.assertFalse(sr.due_now(c, self.at(10))[0])

	def test_weekday(self):
		c = cfg(subscription_reminder_weekday="Thursday")
		self.assertFalse(sr.due_now(c, self.at(10, day=7))[0])
		self.assertTrue(sr.due_now(c, self.at(10, day=8))[0])


class Planning(unittest.TestCase):
	"""plan() with the database replaced by whatever each test hands it."""

	ROWS = {
		sr.EXPIRING: {"message": "m", "whatsapp_template": "t1", "template_variables": "{{1}} = {plate}",
		              "template_title": "renewal_1"},
		sr.EXPIRED: {"message": "m", "whatsapp_template": "t2", "template_variables": "{{1}} = {plate}",
		             "template_title": "renewal_3"},
	}

	def plan(self, vehicles, config=None, rows=None, budget=50, today_counts=None, recent=(),
	         renewals=None, customers=None, blocked_customers=(), blocked_phones=()):
		# Fresh containers per call: plan() adds to the per-customer counts.
		today_counts = today_counts or (0, {}, {})
		renewals = renewals or ({}, {})
		customers = customers if customers is not None else {
			v.customer: frappe._dict(name=v.customer, customer_name=v.customer.title(),
			                         mobile_no="", customer_type="Company")
			for v in vehicles
		}
		db = mock.Mock()
		db.sql.return_value = list(customers.values())
		with mock.patch.object(frappe, "db", db), \
		     mock.patch.object(sr, "vehicles_in_window", return_value=vehicles), \
		     mock.patch.object(sr, "today", return_value=TODAY), \
		     mock.patch.object(sr, "_today_counts", return_value=today_counts), \
		     mock.patch.object(sr, "_recently_reminded", return_value=set(recent)), \
		     mock.patch.object(sr, "_renewal_state", return_value=renewals), \
		     mock.patch.object(sr.do_not_contact, "blocked_phones", return_value=set(blocked_phones)), \
		     mock.patch.object(sr.do_not_contact, "check",
		                       side_effect=lambda customer=None, **kw: {"blocked": customer in blocked_customers}):
			return sr.plan(config or cfg(), budget=budget, rows=rows or self.ROWS)

	def plates(self, found):
		return [e["plates"][0] for e in found["due"]]

	def test_one_entry_per_vehicle_with_its_own_kind(self):
		found = self.plan([vehicle("A", "c1", 3), vehicle("B", "c1", -4)])
		self.assertEqual(self.plates(found), ["A", "B"])
		self.assertEqual([e["kind"] for e in found["due"]], [sr.EXPIRING, sr.EXPIRED])
		self.assertEqual([e["template_title"] for e in found["due"]], ["renewal_1", "renewal_3"])
		self.assertEqual((found["expiring_soon"], found["already_expired"]), (1, 1))

	def test_unticked_kind_is_never_planned_or_counted_as_passed_over(self):
		rows = {sr.EXPIRING: self.ROWS[sr.EXPIRING]}
		found = self.plan([vehicle("A", "c1", 3), vehicle("B", "c2", -4)], rows=rows)
		self.assertEqual(self.plates(found), ["A"])
		self.assertEqual(found["passed"], {})

	def test_budget_stops_filling(self):
		vs = [vehicle("V%d" % i, "c%d" % i, i) for i in range(10)]
		self.assertEqual(len(self.plan(vs, budget=4)["due"]), 4)

	def test_daily_limit_leaves_only_what_is_left_of_the_day(self):
		vs = [vehicle("V%d" % i, "c%d" % i, i) for i in range(10)]
		found = self.plan(vs, config=cfg(subscription_reminder_batch_size=50), budget=50,
		                  today_counts=(47, {}, {}))
		self.assertEqual(len(found["due"]), 3)
		found = self.plan(vs, config=cfg(subscription_reminder_batch_size=50), budget=50,
		                  today_counts=(50, {}, {}))
		self.assertEqual((found["due"], found["budget"]), ([], 0))

	def test_no_daily_limit_when_zero(self):
		vs = [vehicle("V%d" % i, "c%d" % i, i) for i in range(10)]
		found = self.plan(vs, config=cfg(subscription_reminder_batch_size=0), today_counts=(900, {}, {}))
		self.assertEqual(len(found["due"]), 10)

	def test_per_customer_daily_cap_moves_on_to_the_next_customer(self):
		vs = [vehicle("F%d" % i, "fleet", i) for i in range(5)] + [vehicle("S", "small", 9)]
		found = self.plan(vs, config=cfg(subscription_reminder_max_per_customer=2))
		self.assertEqual(self.plates(found), ["F0", "F1", "S"])
		self.assertEqual(sum(found["passed"].values()), 3)

	def test_cap_counts_what_was_already_sent_today(self):
		vs = [vehicle("F0", "fleet", 1), vehicle("S", "small", 2)]
		found = self.plan(vs, config=cfg(subscription_reminder_max_per_customer=2),
		                  today_counts=(2, {"fleet": 2}, {}))
		self.assertEqual(self.plates(found), ["S"])

	def test_already_reminded_vehicle_is_skipped_silently(self):
		found = self.plan([vehicle("A", "c1", 3), vehicle("B", "c1", 4)], recent=["c1||A"])
		self.assertEqual(self.plates(found), ["B"])
		self.assertEqual(found["passed"], {})

	def test_a_vehicle_that_failed_twice_today_waits_for_tomorrow(self):
		found = self.plan([vehicle("A", "c1", 3), vehicle("B", "c2", 4)], today_counts=(0, {}, {"c1||A": 2}))
		self.assertEqual(self.plates(found), ["B"])
		self.assertEqual(len(found["passed"]), 1)
		found = self.plan([vehicle("A", "c1", 3)], today_counts=(0, {}, {"c1||A": 1}))
		self.assertEqual(self.plates(found), ["A"])

	def test_paid_renewals_are_not_reminded(self):
		pending = {"A": [frappe._dict(new_end=datetime.date(2027, 10, 7))]}
		invoiced = {"B": [frappe._dict(posting_date=datetime.date(2026, 9, 1))]}
		found = self.plan([vehicle("A", "c1", 3), vehicle("B", "c2", 4), vehicle("C", "c3", 5)],
		                  renewals=(pending, invoiced))
		self.assertEqual(self.plates(found), ["C"])
		self.assertEqual(found["passed"], {"already renewed": 2})

	def test_old_invoice_from_the_previous_cycle_does_not_count(self):
		# expiry in 3 days, lead time 50: this cycle's window opened 47 days ago
		invoiced = {"A": [frappe._dict(posting_date=datetime.date(2025, 10, 1))]}
		found = self.plan([vehicle("A", "c1", 3)], renewals=({}, invoiced))
		self.assertEqual(self.plates(found), ["A"])

	def test_a_pending_renewal_that_does_not_move_the_date_does_not_count(self):
		pending = {"A": [frappe._dict(new_end=datetime.date(2026, 10, 1))]}
		found = self.plan([vehicle("A", "c1", 3)], renewals=(pending, {}))
		self.assertEqual(self.plates(found), ["A"])

	def test_deletion_date_excluded_customer_and_no_number(self):
		vs = [
			vehicle("DEL", "c1", 1, deletion=datetime.date(2026, 9, 1)),
			vehicle("EXC", "bad", 2),
			vehicle("NONUM", "c3", 3, mobile="+966"),
			vehicle("OK", "c4", 4),
		]
		found = self.plan(vs, blocked_customers={"bad"})
		self.assertEqual(self.plates(found), ["OK"])
		self.assertEqual(len(found["passed"]), 3)

	def test_blocked_phone_is_stepped_over_to_the_next_number(self):
		vs = [vehicle("A", "c1", 1, mobile="0501111111"), vehicle("B", "c1", 2, mobile="0502222222")]
		found = self.plan(vs, blocked_phones={"+966501111111"})
		self.assertEqual(found["due"][0]["phone"], "+966502222222")
		self.assertEqual(found["due"][0]["phone_source"], "driver_mobile (another vehicle)")

	def test_customer_number_leads_unless_vehicle_only(self):
		customers = {"c1": frappe._dict(name="c1", customer_name="C1", mobile_no="0509999999", customer_type="")}
		v = [vehicle("A", "c1", 1, mobile="0501111111")]
		self.assertEqual(self.plan(v, customers=customers)["due"][0]["phone"], "+966509999999")
		found = self.plan(v, customers=customers, config=cfg(subscription_reminder_phone_source="Vehicle only"))
		self.assertEqual(found["due"][0]["phone"], "+966501111111")

	def test_customer_types(self):
		customers = {
			"c1": frappe._dict(name="c1", customer_name="C1", mobile_no="", customer_type="Individual"),
			"c2": frappe._dict(name="c2", customer_name="C2", mobile_no="", customer_type="Company"),
		}
		vs = [vehicle("A", "c1", 1), vehicle("B", "c2", 2)]
		found = self.plan(vs, customers=customers, config=cfg(subscription_reminder_customer_types="company"))
		self.assertEqual(self.plates(found), ["B"])
		found = self.plan(vs, customers=customers, config=cfg(subscription_reminder_customer_types="all"))
		self.assertEqual(self.plates(found), ["A", "B"])

	def test_same_number_and_plate_is_planned_once(self):
		vs = [vehicle("A", "c1", 1, plate="X1"), vehicle("A2", "c2", 2, plate="X1")]
		found = self.plan(vs)
		self.assertEqual(len(found["due"]), 1)


class Rounds(unittest.TestCase):
	"""_round() with the send, the log and the database replaced."""

	ROWS = Planning.ROWS

	def entries(self, n):
		return [{
			"customer": "c%d" % i, "customer_name": "C%d" % i, "vehicle": "V%d" % i, "vehicles": [],
			"count": 1, "plates": ["V%d" % i], "expiry": TODAY, "days_to_expiry": i, "kind": sr.EXPIRING,
			"template_title": "renewal_1", "phone": "+96650000000%d" % i, "phone_source": "x",
		} for i in range(n)]

	def run_round(self, entries, results, config=None):
		logged = []
		found = {"due": entries, "passed": {}, "in_window": len(entries), "expiring_soon": len(entries),
		         "already_expired": 0, "sent_today": 0, "budget": 50}
		send = mock.Mock(side_effect=results)
		with mock.patch.object(sr, "plan", return_value=found), \
		     mock.patch.object(sr, "context", return_value={}), \
		     mock.patch.object(sr.cw, "_render", return_value="body"), \
		     mock.patch.object(sr.cw, "_send", send), \
		     mock.patch.object(sr, "_log", side_effect=lambda e, s, *a, **k: logged.append((e["plates"][0], s))), \
		     mock.patch.object(sr, "_store_note"), \
		     mock.patch.object(sr, "now_datetime", return_value=datetime.datetime(2026, 10, 7, 14, 30)), \
		     mock.patch.object(frappe, "db", mock.Mock()), \
		     mock.patch.object(frappe, "logger", mock.Mock()):
			summary = sr._round(config or cfg(subscription_reminder_dry_run=0), self.ROWS, [], None, True)
		return summary, logged, send

	def test_sends_every_planned_vehicle_as_a_forced_template(self):
		ok = {"ok": True, "message": "hi", "via": "template"}
		summary, logged, send = self.run_round(self.entries(3), [ok, ok, ok])
		self.assertEqual((summary["sent"], summary["failed"]), (3, 0))
		self.assertEqual([s for _, s in logged], ["Sent"] * 3)
		fallback = send.call_args.kwargs["template_fallback"]
		self.assertTrue(fallback["force"])
		self.assertEqual(fallback["template"], "t1")
		self.assertTrue(send.call_args.kwargs["confirm"])

	def test_stops_after_failures_in_a_row_and_a_success_resets_the_count(self):
		bad = {"ok": False, "code": -500, "msg": "throttled"}
		ok = {"ok": True, "message": "hi", "via": "template"}
		summary, logged, _ = self.run_round(self.entries(6), [bad, bad, ok, bad, bad, bad, ok])
		self.assertEqual([s for _, s in logged], ["Failed", "Failed", "Sent", "Failed", "Failed", "Failed"])
		self.assertIn("Stopped early after 3 failures", summary["stopped_early"])

	def test_closed_window_without_a_template_is_skipped_not_failed(self):
		result = {"ok": False, "code": -409, "msg": "window closed"}
		_, logged, _ = self.run_round(self.entries(1), [result])
		self.assertEqual(logged, [("V0", "Skipped")])

	def test_an_exception_in_send_is_a_failure_not_a_crash(self):
		_, logged, _ = self.run_round(self.entries(1), [RuntimeError("boom")])
		self.assertEqual(logged, [("V0", "Failed")])

	def test_dry_run_sends_nothing(self):
		summary, logged, send = self.run_round(self.entries(2), [], config=cfg(subscription_reminder_dry_run=1))
		send.assert_not_called()
		self.assertEqual([s for _, s in logged], ["Dry run", "Dry run"])
		self.assertEqual(summary["sent_today"], 2)


if __name__ == "__main__":
	unittest.main()
