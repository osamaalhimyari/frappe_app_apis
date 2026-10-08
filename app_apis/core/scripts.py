"""Seed the app's Server Scripts, Client Scripts and HTML blocks into the desk.

Two kinds of script ship with the app:

* **Seeded** (the default). The script is the editable half of the app, so it is
  created from app_apis/scripts/ only when no record with its name exists. After
  that the desk copy is the master -- `bench migrate` never overwrites an edit.
  (Fixtures would: frappe re-imports them with force=True on every migrate, which
  is why the Client Scripts are no longer fixtures.)

* **Managed** (`"managed": true` in the manifest). The app is the master: every
  `bench migrate` creates the script if it is missing and REPLACES the desk copy when
  it differs from the shipped one, so an app update reaches the server. Two things
  are protected:
    - the operator's own settings inside the script. `carry` lists constants whose
      value is kept from the desk copy (e.g. `LIVE_TARGETS`), and `carry_prefix`
      keeps every constant starting with it (the `SHOW_*` button switches);
    - the old copy: it is written to private/files/app_apis_script_backups/ before
      it is replaced, so nothing typed into the desk is lost.
  An operator who unticks Enabled / ticks Disabled keeps that choice.

To get the shipped version of a seeded script back, delete the record (or rename
yours) and run `bench --site <site> migrate`, or call `app_apis.core.scripts.seed`
with `names=[...]`. To stop a script, tick Disabled rather than deleting it --
deleted ones come back on the next migrate.
"""

import json
import os
import re

import frappe

SERVER_MANIFEST = "scripts/server_scripts.json"
CLIENT_SCRIPTS = "scripts/client_script.json"
BACKUP_DIR = "app_apis_script_backups"
MANAGED_KEYS = ("managed", "carry", "carry_prefix")


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
	keep = ("name", "dt", "view", "enabled", "script", *MANAGED_KEYS)
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
					"roles": [{"role": r} for r in meta.get("roles", [])],
					"managed": bool(meta.get("managed"))})
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


# ---------------------------------------------------------------- managed scripts
def _assign(name: str) -> "re.Pattern":
	"""`[var|let|const] NAME = value  [# or // comment]`, one assignment per line."""
	return re.compile(r"^(\s*(?:(?:var|let|const)\s+)?" + re.escape(name) + r"\s*=\s*)(.*?)(\s*(?:#|//).*)?$")


def carry_settings(old: str, new: str, names=(), prefix: str = "") -> str:
	"""`new` (the shipped script) with the VALUE of each setting taken from `old` (the
	desk copy). A setting is a one-line assignment named in `names` or starting with
	`prefix`; a setting that is not in the shipped script, or not in the old one, is left as
	shipped. Comments and everything else come from `new`."""
	wanted = list(names)
	if prefix:
		wanted += [
			m.group(1)
			for m in re.finditer(r"^\s*(?:(?:var|let|const)\s+)?(" + re.escape(prefix) + r"\w*)\s*=", new, re.M)
		]
	kept = {}
	for name in dict.fromkeys(wanted):
		for line in (old or "").splitlines():
			m = _assign(name).match(line)
			if m and m.group(2).strip():
				kept[name] = m.group(2)
				break
	if not kept:
		return new
	out = []
	for line in new.splitlines(keepends=True):
		body = line.rstrip("\r\n")
		eol = line[len(body) :]
		for name, value in kept.items():
			m = _assign(name).match(body)
			if m:
				body = m.group(1) + value + (m.group(3) or "")
				break
		out.append(body + eol)
	return "".join(out)


def _backup(doctype: str, name: str, text: str) -> str:
	folder = frappe.get_site_path("private", "files", BACKUP_DIR)
	os.makedirs(folder, exist_ok=True)
	stamp = frappe.utils.now().replace(":", "").replace("-", "").replace(" ", "-")
	path = os.path.join(folder, f"{doctype.replace(' ', '_')}-{name}-{stamp}.txt")
	with open(path, "w") as f:
		f.write(text or "")
	return path


def _update_managed(doc: dict) -> bool:
	"""Replace the desk copy of a managed script with the shipped one (keeping its
	settings). True when something changed."""
	current = frappe.get_doc(doc["doctype"], doc["name"])
	if doc["doctype"] == "Custom HTML Block":
		return _update_managed_block(current, doc)
	merged = carry_settings(
		current.script or "", doc["script"], doc.get("carry") or (), doc.get("carry_prefix") or ""
	)
	if (current.script or "").strip() == merged.strip():
		return False
	path = _backup(doc["doctype"], doc["name"], current.script)
	current.script = merged
	current.flags.ignore_permissions = True
	current.save()
	print(f"app_apis: updated {doc['name']} (old copy saved to {path})")
	return True


def _update_managed_block(current, doc: dict) -> bool:
	"""A managed Custom HTML Block: html, style and script come from the app. Who may see it
	(roles) stays as the desk has it. The old three are saved first."""
	fields = ("html", "style", "script")
	if all((current.get(f) or "").strip() == (doc.get(f) or "").strip() for f in fields):
		return False
	old = "\n\n".join(f"===== {f} =====\n{current.get(f) or ''}" for f in fields)
	path = _backup(doc["doctype"], doc["name"], old)
	for f in fields:
		current.set(f, doc.get(f) or "")
	current.flags.ignore_permissions = True
	current.save()
	print(f"app_apis: updated {doc['name']} (old copy saved to {path})")
	return True


@frappe.whitelist(methods=["POST"])
def seed(names=None) -> list[str]:
	"""Create every shipped script that does not exist yet, and bring the managed ones up
	to date. Returns the names created or updated. `names` limits it to those scripts."""
	frappe.only_for("System Manager")
	if isinstance(names, str):
		names = json.loads(names)
	changed = []
	for doc in _server_scripts() + _client_scripts() + _html_blocks():
		if names and doc["name"] not in names:
			continue
		managed = bool(doc.get("managed"))
		fields = {k: v for k, v in doc.items() if k not in MANAGED_KEYS}
		try:
			if frappe.db.exists(doc["doctype"], doc["name"]):
				if managed and _update_managed(doc):
					changed.append(doc["name"] + " (updated)")
				continue
			d = frappe.get_doc(fields)
			d.flags.ignore_permissions = True
			d.insert(set_name=doc["name"])
			changed.append(doc["name"])
		except Exception:
			# one bad script must not stop the rest from being seeded
			frappe.log_error(title=f"app_apis: seeding {doc['name']} failed")
	if changed:
		frappe.db.commit()
	return changed


def after_install():
	seed_quietly()


def after_migrate():
	seed_quietly()


def ensure_site_data():
	"""Create or fill what the shipped scripts need in the database, every install and every migrate.

	These used to be patches only. A patch does not run on a FRESH install (Frappe marks every
	patch as done when an app is installed), so a new site would have lacked the Customer cache
	fields and the IM settings. Both functions are idempotent: they add what is missing and never
	overwrite what an operator has set.
	"""
	from app_apis.patches import add_im_customer_cache_fields, seed_im_upload_settings

	for label, step in (("Customer IM cache fields", add_im_customer_cache_fields.execute),
	                    ("IM upload settings", seed_im_upload_settings.execute)):
		try:
			step()
		except Exception:
			frappe.log_error(title="app_apis: " + label + " failed")
			print("app_apis: WARNING -", label, "could not be set up; see the Error Log")

	# every shipped API/scheduler script is a Server Script, and Frappe runs none of them unless
	# the site allows it. Say so loudly rather than leave the app looking installed but dead.
	if not frappe.conf.get("server_script_enabled"):
		print("app_apis: WARNING - server_script_enabled is off for this site, so the Server Scripts "
		      "(vehicle_upload_api, expired_devices_api, ...) will not run. Turn it on with:\n"
		      "    bench --site <site> set-config server_script_enabled true")


def seed_quietly():
	"""Seed as Administrator; a bad script must not fail install or migrate."""
	user = frappe.session.user if getattr(frappe.local, "session", None) else None
	try:
		frappe.set_user("Administrator")
		changed = seed()
		if changed:
			print("app_apis: scripts created/updated:", ", ".join(changed))
		ensure_site_data()
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="app_apis: seeding scripts failed")
	finally:
		if user:
			frappe.set_user(user)
