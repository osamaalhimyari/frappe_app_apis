# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Unit tests for the pure decision functions in `app_apis.fleet_audit`.

These three carry every money figure and every search result on the Fleet Audit
page, and all three are pure: given their arguments they touch no database, no
site and no network. That is what makes them worth testing here -- they can be
run in a second, and they break silently when they break at all.

    bench --site erpnext.test run-tests --app app_apis
    # or, with no site at all:
    /home/frappe/frappe-bench/env/bin/python -m unittest \\
        app_apis.tests.test_fleet_audit -v
"""

import unittest

from app_apis.fleet_audit import _carrier_key, _fold, _sim_price

# Arabic letters, spelled out so the intent survives a diff and an editor that
# "helpfully" normalises hamzas.
ALEF = "ا"           # ا
ALEF_HAMZA = "أ"     # أ
ALEF_HAMZA_BELOW = "إ"  # إ
ALEF_MADDA = "آ"     # آ
DAL = "د"            # د
SAD = "ص"            # ص
TEH_MARBUTA = "ة"    # ة
HEH = "ه"            # ه
SHEEN = "ش"          # ش
REH = "ر"            # ر
KAF = "ك"            # ك
WAW_HAMZA = "ؤ"      # ؤ
WAW = "و"            # و


class TestFold(unittest.TestCase):
	"""Plates are stored with their letters spaced apart and spelled with
	whichever alef the person entering them reached for. Folding is what makes
	the Vehicle and Customer filters find anything at all -- before it, only
	digits ever matched."""

	def test_spaces_and_dash_are_dropped(self):
		# "9880 - أ د ص" is how a plate is stored; nobody types it that way.
		stored = "9880 - %s %s %s" % (ALEF_HAMZA, DAL, SAD)
		typed = "9880%s%s%s" % (ALEF_HAMZA, DAL, SAD)
		self.assertEqual(_fold(stored), _fold(typed))

	def test_every_alef_folds_together(self):
		base = _fold(ALEF + DAL + SAD)
		for variant in (ALEF_HAMZA, ALEF_HAMZA_BELOW, ALEF_MADDA, "ٱ"):
			self.assertEqual(_fold(variant + DAL + SAD), base, variant)

	def test_teh_marbuta_folds_to_heh(self):
		# شركة vs شركه -- both spellings are in the customer data.
		self.assertEqual(
			_fold(SHEEN + REH + KAF + TEH_MARBUTA),
			_fold(SHEEN + REH + KAF + HEH),
		)

	def test_waw_hamza_folds_to_waw(self):
		self.assertEqual(_fold(WAW_HAMZA), _fold(WAW))

	def test_latin_and_digits_are_untouched(self):
		self.assertEqual(_fold("Dammam"), "Dammam")
		self.assertEqual(_fold("350317171869179"), "350317171869179")

	def test_non_breaking_space_is_dropped(self):
		self.assertEqual(_fold("a b"), "ab")

	def test_empty_and_none_are_safe(self):
		self.assertEqual(_fold(""), "")
		self.assertEqual(_fold(None), "")

	def test_folding_is_idempotent(self):
		once = _fold("9880 - %s %s %s" % (ALEF_HAMZA, DAL, SAD))
		self.assertEqual(_fold(once), once)


class TestCarrierKey(unittest.TestCase):
	"""The three carrier Items are spelled three different ways, so the match
	is on substrings. A carrier it does not recognise must return None, which
	is what sends pricing to the Estimated Price fallback."""

	def test_lebara(self):
		self.assertEqual(_carrier_key("Lebara SIM"), "lebara")
		self.assertEqual(_carrier_key("LEBARA"), "lebara")

	def test_stc_by_name_and_by_item_code(self):
		self.assertEqual(_carrier_key("STC SIM"), "stc")
		self.assertEqual(_carrier_key("sim_stc"), "stc")

	def test_mobily_in_both_scripts(self):
		self.assertEqual(_carrier_key("Mobily SIM"), "mobily")
		arabic = "شرائح موبايلي"
		self.assertEqual(_carrier_key(arabic), "mobily")

	def test_unknown_and_empty_are_none(self):
		self.assertIsNone(_carrier_key("Zain SIM"))
		self.assertIsNone(_carrier_key(""))
		self.assertIsNone(_carrier_key(None))


class TestSimPrice(unittest.TestCase):
	"""What a SIM costs is decided by status, then plan, then carrier -- and by
	the difference between "free" and "unknown", which is the distinction that
	keeps the page from under-reporting the bill."""

	PRICES = {
		"Normal": {"default": 5.0, "lebara": 6.0, "stc": 9.0, "mobily": 0.0},
		"Data 30G": {"default": 65.0},
	}

	def price(self, plan, carrier, status):
		return _sim_price(plan, carrier, status, self.PRICES)

	def test_carrier_price_wins_when_set(self):
		self.assertEqual(self.price("Normal", "Lebara SIM", "Active"), 6.0)
		self.assertEqual(self.price("Normal", "STC SIM", "Active"), 9.0)

	def test_zero_carrier_price_means_unset_not_free(self):
		# Mobily is 0 on this plan, so it must fall back, not bill nothing.
		arabic = "شرائح موبايلي"
		self.assertEqual(self.price("Normal", arabic, "Active"), 5.0)

	def test_unknown_or_missing_carrier_falls_back(self):
		self.assertEqual(self.price("Normal", "Zain SIM", "Active"), 5.0)
		self.assertEqual(self.price("Normal", "", "Active"), 5.0)
		self.assertEqual(self.price("Data 30G", "Lebara SIM", "Active"), 65.0)

	def test_suspend_is_flat_and_ignores_plan_and_carrier(self):
		self.assertEqual(self.price("Normal", "STC SIM", "Suspend"), 2.0)
		self.assertEqual(self.price("", "", "Suspend"), 2.0)
		self.assertEqual(self.price("Data 30G", "Lebara SIM", "Suspend"), 2.0)

	def test_unbilled_statuses_cost_nothing(self):
		for status in ("Deactivated", "Temp Deactivated", "Idle", "", None):
			self.assertEqual(self.price("Normal", "Lebara SIM", status), 0.0, status)

	def test_active_without_a_plan_is_unknown_not_zero(self):
		# The distinction the whole money panel rests on: None must never be
		# added up as if it were 0.0.
		self.assertIsNone(self.price("", "Lebara SIM", "Active"))
		self.assertIsNone(self.price(None, "Lebara SIM", "Active"))
		self.assertIsNone(self.price("Roming", "Lebara SIM", "Active"))

	def test_plan_name_is_stripped_before_lookup(self):
		self.assertEqual(self.price("  Normal  ", "Lebara SIM", "Active"), 6.0)


if __name__ == "__main__":
	unittest.main()
