# Copyright (c) 2026, osama and contributors
# For license information, please see license.txt

"""Seed the IM Upload section of the app_apis settings.

The vehicle_upload_api Server Script used to carry the IM admin / reseller ids and
the Device Type -> IM model table inside its own source, so changing one meant
editing code on every server. They now live in the IM section of the app_apis
settings. This fills them from data/im_models.json. (Which IM company a vehicle goes
to is not stored here: it is found from the vehicle's IM Platform email and cached on
the Customer -- see add_im_customer_cache_fields.)

Safe to run again, and safe on a site where somebody has edited the table: a row
is added only when its ERP Device Type is not already there, and a
setting is written only when it is still blank. Nothing an operator typed is changed.
"""

import json
import os

import frappe

DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DEFAULTS = {
	"im_admin_id": "1213",
	"im_default_reseller_id": "3237",
	"im_reseller_ids": "3237,3267",
	"im_sim_provider": "2",
}


def _load(name):
	path = os.path.join(DATA, name)
	if not os.path.exists(path):
		return []
	with open(path, encoding="utf-8") as f:
		return json.load(f)


def execute():
	doc = frappe.get_doc("app_apis")
	for field, value in DEFAULTS.items():
		if not str(doc.get(field) or "").strip():
			doc.set(field, value)

	have = {str(r.device_type or "").strip().lower() for r in doc.get("im_models") or []}
	for row in _load("im_models.json"):
		key = str(row.get("device_type") or "").strip().lower()
		if not key or key in have:
			continue
		have.add(key)
		doc.append("im_models", {"device_type": row["device_type"], "im_model_id": str(row["im_model_id"]),
		                         "note": row.get("note") or ""})

	doc.flags.ignore_permissions = True
	doc.save()
