"""Bulk save for mirror tables that Server Scripts fill.

A Server Script syncing a provider's list (Lebara's ~30,000 SIMs every hour)
cannot afford one Document.insert/save per row, and the sandbox has no bulk
insert. This writes only what changed: new keys are bulk-inserted, changed rows
get one UPDATE each, unchanged rows are not touched.

Only doctypes of this app's module may be written, so the endpoint cannot be
used to overwrite arbitrary site data.
"""

import json

import frappe
from frappe.utils import cstr, flt, get_datetime, now_datetime

MODULE = "App Apis"


def _parse(value, default=None):
	if value in (None, ""):
		return default
	if isinstance(value, str):
		return json.loads(value)
	return value


def _same(old, new, fieldtype):
	if fieldtype in ("Int", "Check"):
		return int(old or 0) == int(new or 0)
	if fieldtype in ("Float", "Currency", "Percent"):
		return abs(flt(old) - flt(new)) < 1e-9
	if fieldtype in ("Datetime", "Date"):
		if not old and not new:
			return True
		if not old or not new:
			return False
		return get_datetime(old) == get_datetime(new)
	return cstr(old) == cstr(new)


@frappe.whitelist(methods=["POST"])
def upsert(doctype, key, rows):
	"""Insert or update `rows` (list of dicts with fieldnames) by `key` field.

	The doctype must be named by that key field (autoname "field:<key>").
	Returns {"inserted", "updated", "unchanged", "total"}.
	"""
	frappe.only_for("System Manager")
	meta = frappe.get_meta(doctype)
	if meta.module != MODULE or meta.issingle or meta.istable:
		frappe.throw(f"upsert is only allowed on {MODULE} list doctypes, not {doctype}")
	if (meta.autoname or "") != f"field:{key}":
		frappe.throw(f"{doctype} must be named by field:{key} for upsert")

	rows = _parse(rows, []) or []
	types = {df.fieldname: df.fieldtype for df in meta.fields}
	fields = [f for f in types if types[f] not in ("Section Break", "Column Break", "Tab Break", "HTML", "Button")]

	existing = {
		cstr(r[key]): r
		for r in frappe.get_all(doctype, fields=["name", *fields], limit_page_length=0)
	}

	now = now_datetime()
	user = frappe.session.user
	inserts, updated, unchanged, seen = [], 0, 0, set()
	for row in rows:
		k = cstr(row.get(key))
		if not k or k in seen:
			continue
		seen.add(k)
		values = {f: row.get(f) for f in fields if f in row}
		old = existing.get(k)
		if old is None:
			inserts.append(values)
			continue
		changed = {f: v for f, v in values.items() if not _same(old.get(f), v, types[f])}
		if changed:
			frappe.db.set_value(doctype, old["name"], changed, update_modified=True)
			updated += 1
		else:
			unchanged += 1

	if inserts:
		cols = ["name", "creation", "modified", "owner", "modified_by", "docstatus", *fields]
		frappe.db.bulk_insert(
			doctype,
			cols,
			[[cstr(r.get(key)), now, now, user, user, 0, *[r.get(f) for f in fields]] for r in inserts],
			ignore_duplicates=True,
		)

	frappe.db.commit()
	return {"inserted": len(inserts), "updated": updated, "unchanged": unchanged, "total": len(seen)}
