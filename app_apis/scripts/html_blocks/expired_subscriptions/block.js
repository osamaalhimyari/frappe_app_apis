// Expired Subscriptions -- a Custom HTML Block.
//
// INDIVIDUALS ONLY: a vehicle is listed when its Customer Type is Individual and Paying is
// not Company (the same rule the subscription reminders use). Companies are left out.
// Plate, IMEI and Customer open the record when pressed. The last column has the row tools:
// Check SIM, Suspend SIM, Delete WASL only and Delete from Pilot WSL / Pilot 2 / IM, in a ⋯ menu.
//
// Every vehicle whose subscription ran out longer ago than app_apis >
// Subscription Reminders > "Days After Expiry" (the box at the top starts
// there and can be changed), with its IMEI, plate, customer, how many days it
// has been expired, and what Pilot and IM say about it.
//
// Everything goes through the Server Script API `expired_devices_api`; this
// block adds nothing to the app.

const root = root_element;
const $ = (sel) => root.querySelector(sel);
const $$ = (sel) => Array.prototype.slice.call(root.querySelectorAll(sel));

const API = "expired_devices_api";
const RENDER_LIMIT = 300;
const YES = "✓";
const NO = "–";

const COLUMNS = [
	{ key: "imei", label: "IMEI" },
	{ key: "plate", label: "Plate" },
	{ key: "customer_name", label: "Customer" },
	{ key: "customer_type", label: "Type" },
	{ key: "model", label: "Model" },
	{ key: "expiry", label: "Expiry" },
	{ key: "days", label: "Days" },
	{ key: "pilot_wsl", label: "Pilot WSL" },
	{ key: "pilot_2", label: "Pilot 2" },
	{ key: "im", label: "IM" },
	{ key: "sim_status", label: "SIM" },
	{ key: "erp_status", label: "ERP" },
	{ key: "keep", label: "Keep" },
	{ key: "tools", label: "Check / delete" },
];

let state = { rows: [], days: 0, total: 0, live: 0, can_act: 0, pilot_note: "", with_pilot: 0 };
let colFilter = {};
let busyText = "";

// ---------------------------------------------------------------- reading

async function load(withPilot) {
	const days = parseInt($(".exp-days-input").value, 10);
	busy(withPilot ? "Reading Pilot's estate, this takes about half a minute ..." : "Reading ...");
	try {
		const out = await frappe.xcall(API, {
			action: "list",
			days: days > 0 ? days : "",
			with_pilot: withPilot ? 1 : 0,
		});
		state = out || state;
		state.with_pilot = withPilot ? 1 : 0;
		if (!$(".exp-days-input").value) $(".exp-days-input").value = state.days;
	} catch (e) {
		const why = String((e && e.message) || e);
		// frappe keeps a cached table of Server Script routes; a script saved a
		// moment ago can be missing from it until the cache turns over. The
		// list does not need the script, so it is read the plain way instead.
		if (why.indexOf("Failed to get method") !== -1 || why.indexOf("404") !== -1) {
			await load_direct(days);
		} else {
			frappe.msgprint({ title: "Could not read the list", message: why, indicator: "red" });
		}
	}
	busy("");
	render();
}

// The same list, built from ordinary reads only -- no Server Script involved.
// The buttons stay off in this mode, because blocking does need the script.
async function load_direct(days) {
	busy("Reading the list directly ...");
	if (!days || days < 1) {
		try {
			const setting = await frappe.xcall("frappe.client.get_value", {
				doctype: "app_apis", fieldname: "subscription_reminder_days_after", filters: {},
			});
			days = parseInt((setting && (setting.subscription_reminder_days_after || setting.value)) || 90, 10);
		} catch (e) {
			days = 90;
		}
	}
	const today = frappe.datetime.now_date();
	const cutoff = frappe.datetime.add_days(today, -days);

	const vehicles = await frappe.xcall("frappe.client.get_list", {
		doctype: "Customer Vehicle",
		filters: [["device_statues", "=", "Installed"], ["subscription_expiry_date", "<", cutoff],
			["device_serial", "!=", ""]],
		fields: ["name", "customer", "license_plate", "e_license_plate", "plate_num", "device_serial",
			"subscription_expiry_date", "device_type", "paying"],
		order_by: "subscription_expiry_date asc",
		limit_page_length: 3000,
	});

	const imeis = vehicles.map((v) => v.device_serial).filter(Boolean);
	const audit = {};
	for (let i = 0; i < imeis.length; i += 300) {
		busy("Reading the Fleet Audit data: " + Math.min(i + 300, imeis.length) + " of " + imeis.length + " ...");
		const page = await frappe.xcall("frappe.client.get_list", {
			doctype: "app_apis_fleet_audit",
			filters: [["imei", "in", imeis.slice(i, i + 300)]],
			fields: ["imei", "plate", "device_model", "erp_status", "sim_status", "sim_msisdn",
				"on_pilot_1", "on_pilot_2", "on_im", "pilot_active", "im_status"],
			limit_page_length: 0,
		});
		(page || []).forEach((a) => { audit[a.imei] = a; });
	}

	const names = Array.from(new Set(vehicles.map((v) => v.customer).filter(Boolean)));
	const people = {};
	for (let i = 0; i < names.length; i += 300) {
		const page = await frappe.xcall("frappe.client.get_list", {
			doctype: "Customer",
			filters: [["name", "in", names.slice(i, i + 300)]],
			fields: ["name", "customer_name", "customer_type", "paying"],
			limit_page_length: 0,
		});
		(page || []).forEach((c) => { people[c.name] = c; });
	}

	// individuals only: Customer Type Individual and Paying (vehicle's, else customer's) not Company
	const individual = (v) => {
		const c = people[v.customer] || {};
		if (String(c.customer_type || "").toLowerCase() !== "individual") return false;
		return String(v.paying || c.paying || "").toLowerCase() !== "company";
	};
	const kept = vehicles.filter(individual);

	state = {
		ok: true, days: days, days_after_setting: days, grace: 0, customer_types: "Individual only",
		total: kept.length, returned: kept.length, limit: 3000, live: 0, can_act: 0,
		with_pilot: 0,
		pilot_note: "Read without the Server Script, so the buttons and the paid/excluded marks are off. " +
			"Open Server Script expired_devices_api and save it once, then press Refresh.",
		rows: kept.map((v) => {
			const a = audit[v.device_serial] || {};
			const c = people[v.customer] || {};
			return {
				vehicle: v.name,
				imei: v.device_serial,
				plate: v.license_plate || v.e_license_plate || v.plate_num || a.plate || "",
				customer: v.customer,
				customer_name: c.customer_name || v.customer,
				customer_type: c.customer_type || "",
				model: a.device_model || v.device_type || "",
				expiry: String(v.subscription_expiry_date || "").slice(0, 10),
				days: Math.round(frappe.datetime.get_day_diff(today, v.subscription_expiry_date)),
				sim: a.sim_msisdn || "",
				sim_status: a.sim_status || "",
				erp_status: a.erp_status || "",
				on_pilot_1: a.on_pilot_1 ? 1 : 0,
				on_pilot_2: a.on_pilot_2 ? 1 : 0,
				on_im: a.on_im ? 1 : 0,
				im_status: a.im_status || "",
				pilot_active: a.pilot_active ? 1 : 0,
				agentid: null, node: null, paid: 0, excluded: 0,
			};
		}),
	};
	if (!$(".exp-days-input").value) $(".exp-days-input").value = days;
}

// ---------------------------------------------------------------- filters

function cell_value(row, key) {
	if (key === "pilot_wsl") return row.on_pilot_1 ? "Yes" : "No";
	if (key === "pilot_2") return row.on_pilot_2 ? "Yes" : "No";
	if (key === "im") return row.on_im ? "Yes" : "No";
	if (key === "keep") return keep_reason(row);
	if (key === "tools") return "";
	return String(row[key] === undefined || row[key] === null ? "" : row[key]);
}

function keep_reason(row) {
	if (row.paid) return "Renewed or paid";
	if (row.excluded) return "Excluded";
	return "";
}

// A column whose values repeat -- Yes/No, a status, a customer type -- filters
// better from a list than from a typed word, so the header builds one out of
// the values actually in the table. Anything freer than that stays a box.
const MAX_OPTIONS = 40;
let options = {};

function build_options(rows) {
	options = {};
	COLUMNS.forEach((c) => {
		if (c.key === "tools") return;
		const seen = {};
		let distinct = 0;
		for (const row of rows) {
			const v = cell_value(row, c.key);
			if (v === "") continue;
			if (seen[v] === undefined) {
				seen[v] = 0;
				distinct += 1;
				if (distinct > MAX_OPTIONS) return;        // too many: keep the box
			}
			seen[v] += 1;
		}
		if (distinct > 1) options[c.key] = seen;
	});
}

function filtered(rows) {
	const q = ($(".exp-search").value || "").trim().toLowerCase();
	const show = $(".exp-show").value;

	return rows.filter((r) => {
		const pilot = r.on_pilot_1 || r.on_pilot_2;
		if (show === "pilot" && !pilot) return false;
		if (show === "im" && !r.on_im) return false;
		if (show === "both" && !(pilot && r.on_im)) return false;
		if (show === "none" && (pilot || r.on_im)) return false;
		if (show === "ready" && !(pilot && r.agentid && !r.paid && !r.excluded)) return false;
		if (show === "kept" && !(r.paid || r.excluded)) return false;

		for (const key of Object.keys(colFilter)) {
			const want = (colFilter[key] || "").trim();
			if (!want) continue;
			const have = cell_value(r, key);
			// a value picked from a list must match it exactly; a typed word
			// only has to appear
			if (options[key]) {
				if (have !== want) return false;
			} else if (have.toLowerCase().indexOf(want.toLowerCase()) === -1) {
				return false;
			}
		}

		if (q) {
			const hay = [r.imei, r.plate, r.customer_name, r.customer, r.model].join(" ").toLowerCase();
			if (hay.indexOf(q) === -1) return false;
		}
		return true;
	});
}

// ---------------------------------------------------------------- row tools
// The same actions as the Delete and WASL / SIM buttons on the Customer Vehicle form, one set per
// row: Check SIM (read-only), Suspend SIM, Delete WASL only, Delete from Pilot (WSL) / Pilot 2 / IM.
// They talk to the Server Script `vehicle_upload_api`. Every action that changes something looks
// the vehicle up first, shows what it found, and needs a word typed before it goes ahead
// (DELETE, or SUSPEND for the SIM). IM may also ask for its Security PIN.

const TOOLS_API = "vehicle_upload_api";
const WORD_DELETE = "DELETE";

function tool_call(args) {
	return new Promise((resolve) => {
		frappe.call({
			method: TOOLS_API,
			args: args,
			callback: (r) => resolve((r && r.message) || { error: "Empty response." }),
			error: (e) => resolve({ error: (e && e.message) || "The call did not go through." }),
		});
	});
}

function platform_tool(key, label, flag) {
	return {
		key: key, label: label, flag: flag, word: WORD_DELETE,
		title: "Delete from " + label, done: "Deleted", busy: "Deleting from " + label + " ...",
		warn: "This cannot be undone. The vehicle and its stored data and sensors are removed from " + label +
			". The ERP vehicle is not touched.",
	};
}

const TOOLS = {
	del_p1: platform_tool("pilot_wsl", "Pilot (WSL)", "on_pilot_1"),
	del_p2: platform_tool("pilot2", "Pilot 2", "on_pilot_2"),
	del_im: platform_tool("im", "IM (Trakzee)", "on_im"),
	wasl: {
		key: "wasl", label: "Delete from WASL only", word: WORD_DELETE,
		title: "Delete from WASL only", done: "Deleted from WASL", busy: "Deleting from WASL ...",
		warn: "Deletes the vehicle from the WASL database only, through Pilot's own WASL window. " +
			"The Pilot vehicle stays. WASL may refuse if the vehicle is locked.",
	},
	suspend: {
		key: "sim", label: "Suspend SIM", word: "SUSPEND",
		title: "Suspend SIM", done: "SIM suspended", busy: "Suspending the SIM ...",
		warn: "Suspends this vehicle's SIM on Lebara (Lebara SIMs only). It can be resumed from Lebara. " +
			"The vehicle itself is not touched.",
	},
};

function tool_css() {
	if (document.getElementById("exp-tool-css")) return;
	const css = document.createElement("style");
	css.id = "exp-tool-css";
	css.textContent = [
		".cvu-card{border:1px solid var(--border-color);border-radius:10px;margin-top:10px;overflow:hidden;font-size:13px}",
		".cvu-card-head{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border-color)}",
		".cvu-card-head .cvu-title{font-weight:600;flex:1}",
		".cvu-dot{width:10px;height:10px;border-radius:50%;flex:none}",
		".cvu-pill{font-size:11px;font-weight:600;padding:2px 10px;border-radius:999px;white-space:nowrap}",
		".cvu-body{padding:10px 14px}",
		".cvu-row{display:flex;gap:12px;padding:5px 0;border-top:1px dashed var(--border-color)}",
		".cvu-row:first-of-type{border-top:0}",
		".cvu-key{color:var(--text-muted);min-width:130px;flex:none}",
		".cvu-val{flex:1;word-break:break-word}",
		".cvu-said{font-family:var(--font-family-monospace,monospace);font-size:11px;color:var(--text-muted)}",
		".cvu-strip{margin-top:8px;padding:8px 12px;border-radius:8px;font-size:12px;border:1px solid}",
		".cvu-green{background:var(--green-100,#e4f5e9);color:var(--green-800,#14532d)}",
		".cvu-red{background:var(--red-100,#fde8e8);color:var(--red-800,#7f1d1d)}",
		".cvu-orange{background:var(--orange-100,#fdf0e1);color:var(--orange-800,#7c2d12)}",
		".cvu-grey{background:var(--gray-100,#f1f1f3);color:var(--gray-700,#4b5563)}",
		".cvu-dot.cvu-green{background:var(--green-500,#22c55e)}",
		".cvu-dot.cvu-red{background:var(--red-500,#ef4444)}",
		".cvu-dot.cvu-orange{background:var(--orange-500,#f59e0b)}",
		".cvu-dot.cvu-grey{background:var(--gray-400,#9ca3af)}",
		".cvu-strip.cvu-orange{border-color:var(--orange-200,#f8d3a8)}",
	].join("\n");
	document.head.appendChild(css);
}

function trow(key, val, raw) {
	return '<div class="cvu-row"><div class="cvu-key">' + esc(key) + '</div><div class="cvu-val">' +
		(raw ? val : esc(val)) + "</div></div>";
}

function tstrip(tone, text) {
	return '<div class="cvu-strip cvu-' + tone + '">' + esc(text) + "</div>";
}

const STATE_LABEL = {
	found: "Found on the platform", missing: "Not on the platform",
	mismatch: "A vehicle with this name has a different IMEI", unknown: "Could not check",
	skipped: "No Device Serial", suspended: "Already suspended",
};

async function check_sim(row) {
	tool_css();
	busy("Asking Lebara about the SIM ...");
	const res = await tool_call({ action: "sim_check", vehicle: row.vehicle });
	busy("");
	const head = (row.plate || row.vehicle) + (row.imei ? " · IMEI " + row.imei : "");
	const sim = res.sim;
	if (!sim) {
		frappe.msgprint({
			title: "Check SIM", indicator: "orange",
			message: '<div style="color:var(--text-muted);margin-bottom:6px">' + esc(head) + "</div>" +
				tstrip("orange", res.error || res.why || "No SIM found."),
		});
		return;
	}
	const tone = sim.status === "Active" ? "green" : (sim.status === "Suspend" || sim.status === "Bar" ? "orange" : "grey");
	const match = sim.imei_matches_vehicle;
	const rows =
		trow("Status", '<span class="cvu-pill cvu-' + tone + '">' + esc(sim.status || "?") + "</span>", true) +
		trow("Number", sim.msisdn) + trow("ICCID", sim.iccid) + trow("IMSI", sim.imsi) +
		trow("Group", sim.group) + trow("Customer", sim.customer) +
		trow("Activated", sim.activated) + trow("Last connection", sim.last_connection) +
		trow("Usage this month (MB)", sim.this_month_usage) + trow("Usage last month (MB)", sim.last_month_usage) +
		trow("In a data session", sim.in_data_session) +
		trow("Device it was last in (IMEI)", sim.sim_imei +
			(match === "yes" ? " ✓ this vehicle's device" : (match === "no" ? " ✕ not this vehicle's device" : ""))) +
		(sim.live_status ? trow("Live status", '<span class="cvu-said">' + esc(sim.live_status) + "</span>", true) : "");
	frappe.msgprint({
		title: "Check SIM", indicator: tone === "green" ? "green" : "orange", wide: true,
		message: '<div style="color:var(--text-muted);margin-bottom:6px">' + esc(head) + "</div>" +
			'<div class="cvu-card"><div class="cvu-card-head"><span class="cvu-dot cvu-' + tone + '"></span>' +
			'<span class="cvu-title">Lebara SIM</span></div><div class="cvu-body">' + rows + "</div></div>" +
			(match === "no" ? tstrip("orange", "Lebara last saw this SIM in a different device than this vehicle's.") : ""),
	});
}

// Look up -> show what was found -> type the word -> do it -> read it back.
async function run_tool(row, tool) {
	tool_css();
	busy("Looking " + (row.plate || row.vehicle) + " up ...");
	const res = await tool_call({ action: "delete_plan", target: tool.key, vehicle: row.vehicle });
	busy("");
	const found = (res.results && res.results[0]) || { state: "unknown", detail: res.error || "No answer." };
	if (res.error && !(res.results && res.results.length)) found.detail = res.error;
	const can = found.state === "found";
	const tone = can ? "green" : (found.state === "missing" ? "grey" : "red");

	const d = new frappe.ui.Dialog({
		title: tool.title + " — " + (row.plate || row.vehicle),
		fields: [
			{ fieldtype: "HTML", fieldname: "body" },
			{ fieldtype: "Data", fieldname: "confirm", label: "Type " + tool.word + " to confirm", hidden: can ? 0 : 1 },
			{ fieldtype: "Password", fieldname: "pin", label: "IM Security PIN", hidden: (can && found.pin_needed) ? 0 : 1 },
		],
		primary_action_label: tool.title,
		primary_action: async () => {
			if (String(d.get_value("confirm") || "").trim() !== tool.word) {
				frappe.msgprint({ title: "Not confirmed", indicator: "orange", message: "Type " + tool.word + " in the box to continue." });
				return;
			}
			const pin = String(d.get_value("pin") || "").trim();
			if (found.pin_needed && !pin) {
				frappe.msgprint({ title: "Security PIN", indicator: "orange", message: "IM asks for its Security PIN before it deletes." });
				return;
			}
			d.get_primary_btn().prop("disabled", true);
			busy(tool.busy);
			const r2 = await tool_call({ action: "delete", target: tool.key, vehicle: row.vehicle, confirm: tool.word, pin: pin });
			busy("");
			d.hide();
			const out = (r2.results && r2.results[0]) || { verdict: "failure", result: r2.error || "No answer." };
			if (r2.error && !(r2.results && r2.results.length)) out.result = r2.error;
			const ok = out.verdict === "deleted" || out.verdict === "done";
			if (ok) {
				if (tool.flag) row[tool.flag] = 0;                  // gone from that platform
				if (tool.key === "sim") row.sim_status = "Suspend";
			}
			frappe.msgprint({
				title: ok ? tool.done : "Not done", indicator: ok ? "green" : "red",
				message: '<div class="cvu-card"><div class="cvu-card-head"><span class="cvu-dot cvu-' + (ok ? "green" : "red") +
					'"></span><span class="cvu-title">' + esc(tool.label) + "</span></div><div class=\"cvu-body\">" +
					esc(out.result || "") + "</div></div>",
			});
			render();
		},
	});
	if (!can) d.get_primary_btn().hide(); else d.get_primary_btn().addClass("btn-danger");
	d.fields_dict.body.$wrapper.html(
		'<div style="font-size:13px"><div class="cvu-card"><div class="cvu-card-head">' +
		'<span class="cvu-dot cvu-' + tone + '"></span><span class="cvu-title">' + esc(tool.label) + "</span>" +
		'<span class="cvu-pill cvu-' + tone + '">' + esc(STATE_LABEL[found.state] || found.state) + "</span></div>" +
		'<div class="cvu-body">' + trow("Vehicle", (row.plate || row.vehicle) + (row.imei ? " · IMEI " + row.imei : "")) +
		trow("On the platform", found.detail || "") + "</div></div>" +
		(can ? tstrip("orange", tool.warn) : "") + "</div>");
	d.show();
}

// One "⋯" button per row. Pressing it opens a small menu with the row tools. The menu is a single
// floating element (fixed position), so the table's own scrolling never clips it.
function tool_buttons(row, at) {
	return '<td class="act"><button class="dots" data-dots="' + at + '" title="Check SIM, suspend, delete" ' +
		'aria-haspopup="menu">⋯</button></td>';
}

let open_menu_el = null;

function close_menu() {
	if (open_menu_el) {
		open_menu_el.remove();
		open_menu_el = null;
	}
}

function menu_items(row) {
	return [
		{ tool: "check", label: "Check SIM", on: true, tip: "Read-only: what Lebara says about this SIM" },
		{ tool: "suspend", label: "Suspend SIM", on: true, cls: "warn", tip: "Suspend the SIM on Lebara" },
		{ tool: "wasl", label: "Delete from WASL only", on: !!row.on_pilot_1, cls: "warn",
			tip: "Delete from WASL only; the Pilot vehicle is kept" },
		{ sep: true },
		{ tool: "del_p1", label: "Delete from Pilot (WSL)", on: !!row.on_pilot_1, cls: "danger",
			tip: "Delete the vehicle from Pilot (WSL)" },
		{ tool: "del_p2", label: "Delete from Pilot 2", on: !!row.on_pilot_2, cls: "danger",
			tip: "Delete the vehicle from Pilot 2" },
		{ tool: "del_im", label: "Delete from IM (Trakzee)", on: !!row.on_im, cls: "danger",
			tip: "Delete the vehicle from IM (Trakzee)" },
	];
}

function open_menu(button, row) {
	close_menu();
	const menu = document.createElement("div");
	menu.className = "exp-menu";
	menu.setAttribute("role", "menu");
	menu.innerHTML = menu_items(row).map((it, i) => it.sep
		? '<div class="exp-menu-sep"></div>'
		: '<button role="menuitem" data-item="' + i + '"' + (it.on ? "" : " disabled") +
			(it.cls ? ' class="' + it.cls + '"' : "") + ' title="' + esc(it.tip) + '">' + esc(it.label) + "</button>").join("");
	$(".exp").appendChild(menu);
	open_menu_el = menu;

	// below the button, right edges aligned; flip above or shift left when it would leave the screen
	const r = button.getBoundingClientRect();
	const w = menu.offsetWidth;
	const h = menu.offsetHeight;
	let left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
	let top = r.bottom + 4;
	if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
	menu.style.left = left + "px";
	menu.style.top = top + "px";

	const items = menu_items(row);
	menu.querySelectorAll("button[data-item]").forEach((el) => {
		el.onclick = (ev) => {
			ev.stopPropagation();
			const it = items[parseInt(el.getAttribute("data-item"), 10)];
			close_menu();
			if (!it || !it.on) return;
			if (it.tool === "check") check_sim(row); else run_tool(row, TOOLS[it.tool]);
		};
	});
}

// close on any click elsewhere, on Escape, and when the page or table scrolls or resizes
document.addEventListener("click", close_menu);
root.addEventListener("click", (ev) => {
	if (!ev.target.closest || !ev.target.closest(".exp-menu, button.dots")) close_menu();
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") close_menu(); });
window.addEventListener("resize", close_menu);
window.addEventListener("scroll", close_menu, true);

function vehicle_url(row) { return "/app/customer-vehicle/" + encodeURIComponent(row.vehicle); }
function customer_url(row) { return "/app/customer/" + encodeURIComponent(row.customer); }
function link(href, text, cls) {
	return '<a class="exp-link' + (cls ? " " + cls : "") + '" href="' + esc(href) + '" target="_blank" rel="noopener">' +
		esc(text) + "</a>";
}

// ---------------------------------------------------------------- drawing

function esc(v) {
	return frappe.utils.escape_html(String(v === undefined || v === null ? "" : v));
}

function pill(text) {
	const t = String(text || "").toLowerCase();
	const tone = /^(active|installed|running)/.test(t) ? "ok"
		: /(deactiv|inactive|expired|deleted|stop)/.test(t) ? "bad"
			: /(suspend|idle|temp)/.test(t) ? "off" : "na";
	return '<span class="pill ' + tone + '">' + esc(text || "—") + "</span>";
}

function mark(on) {
	return on ? '<span class="mid yes">' + YES + "</span>" : '<span class="mid muted">' + NO + "</span>";
}

function busy(text) {
	busyText = text;
	const note = state.rows.length
		? "Expired more than " + state.days + " days (app_apis says " + state.days_after_setting +
		") · " + state.total + " vehicles, showing " + state.returned +
		" · customer types: " + (state.customer_types || "All") +
		(state.live ? "" : " · buttons are in report mode") +
		(state.pilot_note ? " · " + state.pilot_note : "")
		: "Press Refresh. The Pilot buttons need Refresh with Pilot ids, which also reads Pilot's estate.";
	$(".exp-sub").innerHTML = text ? '<span class="exp-busy">' + esc(text) + "</span>" : esc(note);
}

function tiles(rows) {
	const pilot = rows.filter((r) => r.on_pilot_1 || r.on_pilot_2).length;
	const im = rows.filter((r) => r.on_im).length;
	const keep = rows.filter((r) => r.paid || r.excluded).length;
	const ready = rows.filter((r) => (r.on_pilot_1 || r.on_pilot_2) && r.agentid && !r.paid && !r.excluded).length;
	const none = rows.filter((r) => !r.on_pilot_1 && !r.on_pilot_2 && !r.on_im).length;

	$(".exp-tiles").innerHTML = [
		[state.total, "Past the cutoff"],
		[rows.length, "Listed"],
		[pilot, "On Pilot"],
		[im, "On IM"],
		[none, "On neither"],
		[keep, "Paid or excluded"],
		[ready, "Ready to block"],
	].map((t) =>
		'<div class="exp-tile"><div class="n">' + esc(Number(t[0]).toLocaleString()) +
		'</div><div class="l">' + esc(t[1]) + "</div></div>").join("");
}

function scrollbar_on_top() {
	const bar = $(".exp-scrolltop");
	const wrap = $(".exp-wrap");
	const table = $(".exp-table");
	if (!bar || !wrap || !table) return;
	bar.firstElementChild.style.width = table.scrollWidth + "px";
	bar.style.display = table.scrollWidth > wrap.clientWidth + 2 ? "block" : "none";
	let mine = false;
	bar.onscroll = () => {
		if (mine) return;
		mine = true;
		wrap.scrollLeft = bar.scrollLeft;
		mine = false;
	};
	wrap.onscroll = () => {
		if (mine) return;
		mine = true;
		bar.scrollLeft = wrap.scrollLeft;
		mine = false;
	};
}

function header_filter(c) {
	if (c.key === "tools") return "<th></th>";

	const choices = options[c.key];
	if (!choices) {
		return '<th><input data-col="' + esc(c.key) + '" value="' + esc(colFilter[c.key] || "") +
			'" placeholder="filter"></th>';
	}

	const now = colFilter[c.key] || "";
	const keys = Object.keys(choices).sort((a, b) => {
		const na = parseFloat(a), nb = parseFloat(b);
		if (!isNaN(na) && !isNaN(nb)) return na - nb;
		return a < b ? -1 : a > b ? 1 : 0;
	});
	return '<th><select data-col="' + esc(c.key) + '"><option value="">all</option>' +
		keys.map((v) => '<option value="' + esc(v) + '"' + (now === v ? " selected" : "") + ">" +
			esc(v) + " (" + choices[v].toLocaleString() + ")</option>").join("") +
		"</select></th>";
}

function render() {
	close_menu();
	const rows = state.rows || [];
	busy(busyText);
	tiles(rows);
	build_options(rows);

	const shown = filtered(rows);
	$(".exp-count").textContent = shown.length.toLocaleString() + " of " + rows.length.toLocaleString();

	$(".exp-table thead").innerHTML =
		"<tr>" + COLUMNS.map((c) => "<th>" + esc(c.label) + "</th>").join("") + "</tr>" +
		'<tr class="exp-fr">' + COLUMNS.map(header_filter).join("") + "</tr>";

	$(".exp-table tbody").innerHTML = shown.slice(0, RENDER_LIMIT).map((r, i) => {
		const at = rows.indexOf(r);
		return '<tr class="' + ((r.paid || r.excluded) ? "kept" : "") + '">' + COLUMNS.map((c) => {
			if (c.key === "tools") return tool_buttons(r, at);
			if (c.key === "imei") return '<td class="num sticky">' + link(vehicle_url(r), r.imei) + "</td>";
			if (c.key === "plate") return "<td>" + link(vehicle_url(r), r.plate || r.vehicle) + "</td>";
			if (c.key === "days") {
				return '<td class="num right"><span class="days-badge' + (r.days > 365 ? " old" : "") + '">' +
					esc(r.days) + "</span></td>";
			}
			if (c.key === "pilot_wsl") return "<td>" + mark(r.on_pilot_1) + "</td>";
			if (c.key === "pilot_2") return "<td>" + mark(r.on_pilot_2) + "</td>";
			if (c.key === "im") return "<td>" + mark(r.on_im) + "</td>";
			if (c.key === "sim_status") return "<td>" + pill(r.sim_status) + "</td>";
			if (c.key === "erp_status") return "<td>" + pill(r.erp_status) + "</td>";
			if (c.key === "keep") return '<td class="muted">' + esc(keep_reason(r)) + "</td>";
			if (c.key === "customer_name") {
				return '<td class="muted">' + (r.customer ? link(customer_url(r), r.customer_name || r.customer) : esc(r.customer_name)) + "</td>";
			}
			return "<td>" + esc(r[c.key] || "") + "</td>";
		}).join("") + "</tr>";
	}).join("");

	$$(".exp-fr input").forEach((input) => {
		input.oninput = frappe.utils.debounce(() => {
			const at = input.getAttribute("data-col");
			colFilter[at] = input.value;
			render();
			const again = $('.exp-fr input[data-col="' + at + '"]');
			if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
		}, 250);
	});
	$$(".exp-fr select").forEach((sel) => {
		sel.onchange = () => {
			colFilter[sel.getAttribute("data-col")] = sel.value;
			render();
		};
	});

	$$(".exp-table tbody button[data-dots]").forEach((b) => {
		b.onclick = (ev) => {
			ev.stopPropagation();
			const row = rows[parseInt(b.getAttribute("data-dots"), 10)];
			if (!row) return;
			if (open_menu_el && open_menu_el.getAttribute("data-for-row") === String(b.getAttribute("data-dots"))) {
				close_menu();
				return;
			}
			open_menu(b, row);
			if (open_menu_el) open_menu_el.setAttribute("data-for-row", String(b.getAttribute("data-dots")));
		};
	});

	scrollbar_on_top();

	$(".exp-more").textContent = shown.length > RENDER_LIMIT
		? "Showing the first " + RENDER_LIMIT + " of " + shown.length.toLocaleString() +
		". Narrow the filters, or export to see them all."
		: "";
}

// ---------------------------------------------------------------- export

async function export_rows() {
	const rows = filtered(state.rows || []);
	if (!rows.length) {
		frappe.show_alert({ message: "Nothing to export.", indicator: "orange" });
		return;
	}
	const header = ["IMEI", "Plate", "Customer", "Type", "Model", "Expiry", "Days expired",
		"Pilot WSL", "Pilot 2", "IM", "Active on Pilot", "SIM", "ERP", "Keep", "agentid", "node", "Vehicle"];
	const body = rows.map((r) => [
		r.imei, r.plate, r.customer_name, r.customer_type, r.model, r.expiry, r.days,
		r.on_pilot_1 ? "Yes" : "No", r.on_pilot_2 ? "Yes" : "No", r.on_im ? "Yes" : "No",
		r.pilot_active ? "Yes" : "No", r.sim_status, r.erp_status, keep_reason(r),
		r.agentid || "", r.node || "", r.vehicle,
	]);
	const csv = [header].concat(body)
		.map((line) => line.map((c) => '"' + String(c === undefined || c === null ? "" : c).replace(/"/g, '""') + '"').join(","))
		.join("\n");
	const a = document.createElement("a");
	a.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
	a.download = "expired-subscriptions.csv";
	a.click();
}

// ---------------------------------------------------------------- wiring

$(".exp-load").onclick = () => load(false);
$(".exp-load-pilot").onclick = () => load(true);
$(".exp-export").onclick = () => export_rows();
$(".exp-search").oninput = frappe.utils.debounce(render, 250);
$(".exp-show").onchange = render;
$(".exp-days-input").onchange = () => load(!!state.with_pilot);

busy("");
load(false);
