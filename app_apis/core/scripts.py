"""Seed the app's Server Scripts, Client Scripts and HTML blocks into the desk.

The scripts are the editable half of the app, so they are *seeded*, not synced:
a script is created from app_apis/scripts/ only when no record with its name
exists. After that the desk copy is the master -- `bench migrate` never
overwrites an edit. (Fixtures would: frappe re-imports them with force=True on
every migrate, which is why the Client Scripts are no longer fixtures.)

To get the shipped version back, delete the record (or rename yours) and run
`bench --site <site> migrate`, or call `app_apis.core.scripts.seed` with
`names=[...]`. To stop a script, tick Disabled rather than deleting it --
deleted ones come back on the next migrate.
"""

import json
import os

import frappe

SERVER_MANIFEST = "scripts/server_scripts.json"
CLIENT_SCRIPTS = "scripts/client_script.json"


def _app_path(rel: str) -> str:
	return frappe.get_app_path("app_apis", *rel.split("/"))


def _server_scripts() -> list[dict]:
	with open(_app_path(SERVER_MANIFEST)) as f:
		manifest = json.load(f)
	out = []
	for meta in manifest:
		with open(_app_path("scripts/server/" + meta.pop("file"))) as f:
			meta["script"] = f.read()
		out.append({"doctype": "Server Script", **meta})
	return out


def _client_scripts() -> list[dict]:
	path = _app_path(CLIENT_SCRIPTS)
	if not os.path.exists(path):
		return []
	with open(path) as f:
		rows = json.load(f)
	keep = ("name", "dt", "view", "enabled", "script")
	return [{"doctype": "Client Script", **{k: r[k] for k in keep if k in r}} for r in rows]


def _html_blocks() -> list[dict]:
	"""Each folder under scripts/html_blocks/ is one Custom HTML Block:
	block.html / block.css / block.js, plus meta.json naming it and the
	workspace (if any) that shows it."""
	base = _app_path("scripts/html_blocks")
	out = []
	for folder in sorted(os.listdir(base)) if os.path.isdir(base) else []:
		path = os.path.join(base, folder)
		with open(os.path.join(path, "meta.json")) as f:
			meta = json.load(f)
		parts = {}
		for key, fname in (("html", "block.html"), ("style", "block.css"), ("script", "block.js")):
			with open(os.path.join(path, fname)) as f:
				parts[key] = f.read()
		out.append({"doctype": "Custom HTML Block", "name": meta["name"], "private": 0, **parts,
					"roles": [{"role": r} for r in meta.get("roles", [])]})
		ws = meta.get("workspace")
		if ws:
			out.append({
				"doctype": "Workspace",
				"name": ws["name"],
				"label": ws["name"],
				"title": ws["name"],
				"icon": ws.get("icon") or "message",
				"public": 1,
				"is_hidden": 0,
				"parent_page": ws.get("parent_page") or "",
				"sequence_id": ws.get("sequence_id") or 50,
				"content": json.dumps([
					{"id": "lbHead", "type": "header", "data": {"text": f'<span class="h4">{ws["name"]}</span>', "col": 12}},
					{"id": "lbBlock", "type": "custom_block", "data": {"custom_block_name": meta["name"], "col": 12}},
				]),
				"custom_blocks": [{"custom_block_name": meta["name"], "label": meta["name"]}],
				"roles": [{"role": r} for r in meta.get("roles", [])],
			})
	return out


@frappe.whitelist(methods=["POST"])
def seed(names=None) -> list[str]:
	"""Create every shipped script that does not exist yet. Returns the names
	created. `names` limits it to those scripts."""
	frappe.only_for("System Manager")
	if isinstance(names, str):
		names = json.loads(names)
	created = []
	for doc in _server_scripts() + _client_scripts() + _html_blocks():
		if names and doc["name"] not in names:
			continue
		if frappe.db.exists(doc["doctype"], doc["name"]):
			continue
		d = frappe.get_doc(doc)
		d.flags.ignore_permissions = True
		d.insert(set_name=doc["name"])
		created.append(doc["name"])
	if created:
		frappe.db.commit()
	return created


def after_install():
	seed_quietly()


def after_migrate():
	seed_quietly()


def seed_quietly():
	"""Seed as Administrator; a bad script must not fail install or migrate."""
	try:
		user = frappe.session.user if getattr(frappe.local, "session", None) else None
		frappe.set_user("Administrator")
		created = seed()
		if created:
			print("app_apis: created scripts:", ", ".join(created))
	except Exception:
		frappe.log_error(title="app_apis: seeding scripts failed")
	finally:
		if user:
			frappe.set_user(user)
