// "Check IM" toolbar button for xticket.
//
// The IM (Intelligent Machines, gps.im2m.ws) counterpart of pilot_script.js.
// Same shape, same two-tier layout, same map -- a technician who has used one
// should not have to learn the other. It is a SEPARATE Client Script record
// ("xticket-check-im"), so nothing here can break the Pilot button.
//
// Front end only: call the app method, render what comes back. It makes no
// request to IM -- it does not know IM's URL and holds no credentials -- and it
// contains no business logic. There is no Server Script in the chain:
//
//     this script  ->  app_apis.im_connector.get_snapshot  ->  IM
//
// Layout is deliberately two-tier. A technician standing at the vehicle needs
// to answer "is this thing installed and working?" in a few seconds, so the
// first screen is a pass/fail checklist plus the map. Everything else -- the
// port dump, diagnostics and raw payload -- sits behind "Show all".
//
// THE ONE REAL DIFFERENCE FROM THE PILOT SCRIPT: IM rate limits live data to
// roughly one call per one-to-two minutes per account, and answers a request
// that comes too soon with "The call exceeded the limit of one/two minute one
// call." Pilot's 5-second ignition poll would trip that on the second read. So
// the poll interval is not a constant here -- it comes from the server, which
// reads it from app_apis settings, and the test window is correspondingly longer.
//
// The map uses the Leaflet build frappe already bundles in libs.bundle.js, and
// the tile servers frappe itself configures in frappe.utils.map_defaults, so it
// adds no new dependency and no new external host beyond what the desk's own
// Geolocation control already contacts.
//
// Installed as a Client Script record (dt=xticket, view=Form) so it is editable
// from the desk at /app/client-script.

frappe.ui.form.on("xticket", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Check IM"), () => im_check(frm)).addClass("btn-primary");
	},
});

// Titles the app raises when the lookup simply found nothing. Kept as an
// explicit list rather than a keyword match: "IM: Sign-in Failed" also contains
// no vehicle, and calling a rejected password "not found" sends a technician
// hunting for a device when the account is what is broken.
const IM_NOT_FOUND_TITLES = [
	"IM: No Data",
	"No IMEI on This Ticket",
	"IM: Wrong Device Returned",
	"No Vehicle Number for This Device",
];

// The app's frappe.throw arrives as `_server_messages`: a JSON string holding an
// array of JSON strings, each {title, message, indicator}.
function im_server_messages(r) {
	try {
		return JSON.parse(r._server_messages || "[]").map((m) => {
			try {
				return JSON.parse(m);
			} catch (e) {
				return { message: String(m) };
			}
		});
	} catch (e) {
		return [];
	}
}

// Restate an empty result as a plain "not found" notice.
//
// This is registered as an `error_handlers` entry rather than an `error` or
// `always` callback, and that is the whole trick: frappe only draws its own
// dialog when NO handler is registered for the exception type --
//
//     if (handlers.length === 0) { frappe.hide_msgprint(); frappe.msgprint(messages); }
//         -- frappe/public/js/frappe/request.js
//
// so registering one replaces frappe's dialog outright instead of racing it.
// The consequence is that this function now owns EVERY ValidationError from the
// call: any branch that does not print something swallows the message, which is
// why the last branch re-renders exactly what frappe would have shown.
function im_error_handler(r) {
	const msgs = im_server_messages(r);
	const title = (msgs[0] && msgs[0].title) || "";
	const body = msgs.map((m) => m.message || "").join("\n\n");

	const detail = body
		? `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border-color);
				font-size:11px;color:var(--text-muted);white-space:pre-wrap">${frappe.utils.escape_html(
					body
				)}</div>`
		: "";

	// Credentials are tested FIRST, on the body rather than the title. A run in
	// which every account was rejected still comes back titled "No Data", so
	// matching the title alone would report a rejected login as a missing
	// device -- sending a technician to hunt for hardware when the account is
	// what is broken.
	if (/\b401\b|unauthor|incorrect username|credential|sign-?in failed/i.test(body + title)) {
		frappe.msgprint({
			title: __("IM Login Rejected"),
			indicator: "red",
			message:
				`<div style="font-size:13px"><b>${__(
					"IM rejected the account, so no vehicle could be read."
				)}</b></div>
				<div style="margin-top:6px;font-size:12px;color:var(--text-muted)">${__(
					"This is a credential problem at IM's end, not a missing device."
				)}</div>` + detail,
		});
		return;
	}

	if (IM_NOT_FOUND_TITLES.some((t) => title.indexOf(t) !== -1)) {
		frappe.msgprint({
			title: __("Not Found"),
			indicator: "orange",
			message:
				`<div style="font-size:13px"><b>${__(
					"No vehicle was found for this ticket on IM."
				)}</b></div>
				<div style="margin-top:6px;font-size:12px;color:var(--text-muted)">${__(
					"IM is queried by IMEI only, so nothing else from the ticket was substituted."
				)}</div>` + detail,
		});
		return;
	}

	// Not ours to interpret -- render exactly what frappe would have, so
	// registering this handler can never silently swallow a message.
	if (msgs.length) {
		msgs.forEach((m) => frappe.msgprint(m));
	} else {
		frappe.msgprint({
			title: __("IM"),
			indicator: "red",
			message: __("The request failed and IM returned no message."),
		});
	}
}

function im_check(frm) {
	frappe.call({
		method: "app_apis.im_connector.get_snapshot",
		args: { ticket: frm.doc.name },
		freeze: true,
		freeze_message: __("Contacting IM…"),
		error_handlers: { ValidationError: im_error_handler },
		callback(r) {
			if (!r || !r.message) {
				frappe.msgprint({
					title: __("IM"),
					message: __("IM returned an empty response."),
					indicator: "orange",
				});
				return;
			}
			im_show(frm, r.message);
		},
	});
	// Errors raised by the app (nothing to look up, no vehicle on this project,
	// rejected credentials) surface through frappe's standard error dialog with
	// their message intact, so they are deliberately not swallowed here.
}

// ---------------------------------------------------------------- helpers

// IM HTML-encodes some strings ("Queen&#39;s Taste Bakery &amp; Sweets").
// Decode first, then escape for output -- decoding alone would be an XSS hole,
// escaping alone would show the reader a literal "&amp;".
function im_decode(s) {
	const el = document.createElement("textarea");
	el.innerHTML = String(s);
	return el.value;
}

function im_esc(v) {
	return frappe.utils.escape_html(im_decode(v));
}

function im_dash(v) {
	return v === null || v === undefined || v === "" ? "&mdash;" : im_esc(v);
}

function im_ago(sec) {
	if (sec === null || sec === undefined) return __("unknown");
	sec = Math.max(0, Math.floor(sec));
	if (sec < 60) return __("{0}s ago", [sec]);
	if (sec < 3600) return __("{0}m ago", [Math.floor(sec / 60)]);
	if (sec < 86400) return __("{0}h ago", [Math.floor(sec / 3600)]);
	return __("{0}d ago", [Math.floor(sec / 86400)]);
}

function im_epoch_text(epoch) {
	if (!epoch) return "";
	// frappe renders in the user's configured timezone, which is what the
	// reader expects to see -- not the raw UTC of the epoch.
	return frappe.datetime.str_to_user(
		frappe.datetime.convert_to_user_tz(
			moment.unix(Number(epoch)).utc().format("YYYY-MM-DD HH:mm:ss")
		)
	);
}

// 344 -> "NNW". IM sends `Angle` in degrees, but pass a non-numeric value
// straight through rather than rendering a confident, wrong "N".
function im_compass(dir) {
	const n = Number(dir);
	if (dir === null || dir === undefined || dir === "" || Number.isNaN(n)) {
		return dir ? String(dir) : null;
	}
	const pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
		"S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
	return pts[Math.round((n % 360) / 22.5) % 16];
}

// true / false / null -> "ON" / "OFF" / "—". IM ports are tri-state: a port
// that is not wired reads "--", and that is NOT the same as OFF.
function im_onoff(v) {
	if (v === true) return __("ON");
	if (v === false) return __("OFF");
	return "&mdash;";
}

function im_group_readings(list) {
	const groups = {};
	(list || []).forEach((o) => {
		const g = o.group || __("Other");
		(groups[g] = groups[g] || []).push(o);
	});
	return groups;
}

// ---------------------------------------------------------------- render

function im_show(frm, d) {
	const esc = im_esc;
	const dash = im_dash;

	const st = d.state || {};
	const loc = d.location || {};
	const dev = d.device || {};
	const info = d.ticket_info || {};
	const diag = d.diagnostics || {};
	const lu = d.last_update || {};
	const readings = d.readings || [];
	const unwired = d.unwired || [];

	// Prefer the server's figures: it compares IM's timestamp against the
	// server clock, in the timezone app_apis settings says IM stamps its data in.
	// Recomputing from Date.now() would inherit the browser's clock instead.
	let age = lu.age_seconds;
	let is_stale = lu.is_stale;
	if (age === null || age === undefined) {
		age = lu.epoch ? Math.max(0, Math.floor(Date.now() / 1000) - lu.epoch) : null;
	}
	if (is_stale === null || is_stale === undefined) {
		is_stale = age === null || age > (d.stale_after_minutes || 15) * 60;
	}

	const ign_reported = st.ignition !== null && st.ignition !== undefined;
	const has_fix =
		loc.lat !== null && loc.lat !== undefined && loc.lon !== null && loc.lon !== undefined;
	const compass = im_compass(st.direction);
	const map_id = "im-map-" + frappe.utils.get_random(8);

	// ---- the installation checklist ----------------------------------------
	// Status never rides on colour alone: every row carries an icon AND a word
	// AND the colour, so it survives colourblindness, greyscale print and
	// forced-colours mode.
	const ICONS = { ok: "✓", warn: "!", bad: "✕", na: "–" };
	const TONES = {
		ok: "var(--green-600, #16794c)",
		warn: "var(--orange-600, #b25000)",
		bad: "var(--red-600, #c0341d)",
		na: "var(--text-muted)",
	};
	const WORDS = { ok: __("OK"), warn: __("Check"), bad: __("Fail"), na: __("n/a") };

	// `opts.id` makes a row addressable so it can be rewritten live (the
	// ignition test does this); `opts.action` renders a control on the right edge.
	const check_row = (tone, label, value, hint, opts) => {
		opts = opts || {};
		return `
		<tr style="border-top:1px solid var(--border-color)"${opts.id ? ` id="${opts.id}"` : ""}>
			<td class="im-check-status" style="width:78px;padding:9px 0;vertical-align:top">
				<span style="display:inline-flex;align-items:center;gap:5px;font-weight:600;
							 font-size:11px;color:${TONES[tone]}">
					<span style="font-size:13px;line-height:1">${ICONS[tone]}</span>${WORDS[tone]}
				</span>
			</td>
			<td style="width:130px;padding:9px 0;vertical-align:top;color:var(--text-muted)">${label}</td>
			<td class="im-check-value" style="padding:9px 0;vertical-align:top">
				<b>${value}</b>
				${hint ? `<div style="font-size:11px;color:var(--text-muted);margin-top:2px">${hint}</div>` : ""}
			</td>
			<td class="im-check-action" style="padding:9px 0 9px 10px;vertical-align:top;
					text-align:right;white-space:nowrap">${opts.action || ""}</td>
		</tr>`;
	};

	// Status chip markup, reused when the ignition row rewrites itself.
	const chip = (tone) =>
		`<span style="display:inline-flex;align-items:center;gap:5px;font-weight:600;
					  font-size:11px;color:${TONES[tone]}">
			<span style="font-size:13px;line-height:1">${ICONS[tone]}</span>${WORDS[tone]}
		 </span>`;

	const checks = [];

	// 1. Is it talking to IM at all? Everything else is meaningless if not.
	// A throttled read is honest about being a cached figure: the reader must
	// not mistake a two-minute-old fix for a live one.
	checks.push(
		check_row(
			is_stale ? "warn" : "ok",
			__("Reporting"),
			is_stale ? __("Last seen {0}", [im_ago(age)]) : __("Live, {0}", [im_ago(age)]),
			(is_stale
				? __("No update for longer than {0} min — check power and GSM antenna.", [
						d.stale_after_minutes,
				  ])
				: lu.epoch
				? __("Last fix {0}", [im_epoch_text(lu.epoch)])
				: "") +
				(diag.throttled
					? " · " +
					  __("IM rate limit hit — showing the cached reading from {0}.", [
							im_ago(diag.cache_age),
					  ])
					: diag.cached
					? " · " + __("served from cache ({0})", [im_ago(diag.cache_age)])
					: "")
		)
	);

	// 2. Platform status. IM's own view of whether the unit is live on the
	// platform, which is a different question from "did it send a fix".
	checks.push(
		check_row(
			st.status_text === null || st.status_text === undefined
				? "na"
				: String(st.status_text).toUpperCase() === "ACTIVE"
				? "ok"
				: "warn",
			__("Platform"),
			dash(st.status_text),
			__("IM subscription / device state for this vehicle.")
		)
	);

	// 3. GPS fix. IM reports no satellite count, so the evidence is the GPS
	// port flag plus whether a real coordinate came back. 0/0 is not a fix and
	// the server already nulls it.
	checks.push(
		check_row(
			!has_fix ? "bad" : st.gps === false ? "warn" : "ok",
			__("GPS Fix"),
			!has_fix ? __("No position") : esc(loc.lat + ", " + loc.lon),
			!has_fix
				? __("Device has no GPS lock — reposition the antenna with a clear view of the sky.")
				: st.gps === false
				? __("GPS port reads OFF while a position is present — verify the antenna.")
				: loc.address
				? esc(loc.address)
				: ""
		)
	);

	// 4. Ignition wiring. Reporting a value only proves the field exists -- it
	// does NOT prove the ACC wire is connected, because a disconnected input
	// reports a permanent OFF quite happily. The only real proof is that the
	// value FOLLOWS the key, which needs someone at the vehicle -- so the test
	// waits for "Check Now" and then rewrites this row in place. The tone stays
	// "warn" until the flip is actually observed: an untested row must never
	// look like a pass.
	checks.push(
		check_row(
			"warn",
			__("Ignition"),
			ign_reported ? im_onoff(st.ignition) : __("Not reported"),
			ign_reported
				? __("Reported value only — not yet tested. Press Check Now at the vehicle.")
				: __("No ignition input — check the ACC wire."),
			{
				id: "im-check-ignition",
				action: ign_reported
					? `<button class="btn btn-xs btn-default im-ign-test">${__("Check Now")}</button>`
					: "",
			}
		)
	);

	// 5. Power feed. External voltage is the useful number; the Power port and
	// the battery percentage qualify it.
	const volt = st.external_volt;
	const batt = st.battery_percentage;
	checks.push(
		check_row(
			volt === null || volt === undefined
				? st.power === null || st.power === undefined
					? "na"
					: st.power
					? "ok"
					: "bad"
				: volt < 11
				? "bad"
				: volt < 12
				? "warn"
				: "ok",
			__("Power"),
			volt === null || volt === undefined ? im_onoff(st.power) : esc(volt) + " V",
			[
				st.power === null || st.power === undefined ? "" : __("Power port") + ": " + im_onoff(st.power),
				batt === null || batt === undefined ? "" : __("Battery") + ": " + esc(batt) + "%",
			]
				.filter((x) => x)
				.join(" · ") || __("This device does not report a power sensor.")
		)
	);

	// 6. Anything wired to the inputs (doors, temp probe, A/C, SOS). The values
	// themselves are in the grid below, so this row only has to answer "is
	// anything wired and reporting at all?" -- and name what is NOT.
	checks.push(
		check_row(
			readings.length ? "ok" : "warn",
			__("Ports"),
			__("{0} reporting", [readings.length]),
			unwired.length
				? __("Not wired: {0}", [unwired.map((o) => im_decode(o.name)).join(", ")])
				: __("Every documented port is reporting.")
		)
	);

	// ---- status tiles ------------------------------------------------------
	// A handful of headline numbers reads as stat tiles, not a chart. Ignition
	// and GPS live in the checklist above, so they are not repeated here.
	const tile = (label, value, unit) => `
		<div style="flex:1 1 90px;min-width:90px;padding:8px 10px;border:1px solid var(--border-color);
					border-radius:var(--border-radius-md);background:var(--fg-color)">
			<div style="font-size:10px;letter-spacing:.6px;text-transform:uppercase;color:var(--text-muted)">${label}</div>
			<div style="font-size:20px;font-weight:600;line-height:1.25;margin-top:2px">${value}<span
				style="font-size:11px;font-weight:400;color:var(--text-muted);margin-left:3px">${unit || ""}</span></div>
		</div>`;

	const kpis = [
		tile(__("Speed"), st.speed === null || st.speed === undefined ? "&mdash;" : esc(st.speed), "km/h"),
		tile(__("Heading"), compass ? esc(compass) : "&mdash;",
			Number.isFinite(Number(st.direction)) ? esc(st.direction) + "°" : ""),
		tile(__("Temperature"), st.temperature === null || st.temperature === undefined
			? "&mdash;" : esc(st.temperature), "°C"),
		tile(__("Humidity"), st.humidity === null || st.humidity === undefined
			? "&mdash;" : esc(st.humidity), "%RH"),
		tile(__("Battery"), batt === null || batt === undefined ? "&mdash;" : esc(batt), "%"),
		tile(__("Odometer"), dev.current_mileage === null || dev.current_mileage === undefined
			? "&mdash;" : esc(dev.current_mileage), "km"),
	].join("");

	// A calm rule instead of a heavy heading: the eye finds the section without
	// the label competing with the data underneath it.
	const section = (title, extra) =>
		`<div style="display:flex;align-items:center;gap:10px;margin:22px 0 10px">
			<span style="font-size:11px;letter-spacing:.6px;text-transform:uppercase;
						 color:var(--text-muted);font-weight:600;white-space:nowrap">${title}</span>
			${extra ? `<span style="font-size:11px;color:var(--text-muted);white-space:nowrap">${extra}</span>` : ""}
			<span style="flex:1;height:1px;background:var(--border-color)"></span>
		</div>`;

	// One airy label-over-value cell. Used in a wrapping flex row, this reads
	// far faster than a bordered table for a handful of identity fields.
	const field = (label, value) => `
		<div style="flex:1 1 145px;min-width:145px">
			<div style="font-size:10px;letter-spacing:.5px;text-transform:uppercase;
						color:var(--text-muted)">${label}</div>
			<div style="margin-top:2px;word-break:break-word">${value}</div>
		</div>`;

	// One port reading.
	const reading_tile = (name, primary, foot) => `
		<div style="flex:1 1 148px;min-width:148px;padding:9px 11px;border:1px solid var(--border-color);
					border-radius:var(--border-radius-md);background:var(--fg-color)">
			<div style="font-size:10px;letter-spacing:.5px;text-transform:uppercase;color:var(--text-muted);
						white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${name}</div>
			<div style="font-size:18px;font-weight:600;line-height:1.3;margin-top:2px">${primary}</div>
			${foot ? `<div style="font-size:10px;color:var(--text-muted);margin-top:1px">${foot}</div>` : ""}
		</div>`;

	const kv = (rows) =>
		`<table class="table table-borderless" style="margin:0;font-size:12px">${rows
			.filter((r) => r)
			.map(
				([k, v]) =>
					`<tr><td style="width:42%;padding:3px 0;color:var(--text-muted)">${k}</td>
					     <td style="padding:3px 0;word-break:break-word">${v}</td></tr>`
			)
			.join("")}</table>`;

	// ---- port readings (quick view) ----------------------------------------
	// Every port IM reported, grouped the way it is wired on the vehicle, as one
	// scannable grid. Ports that are not wired are listed separately below --
	// showing them here as blank tiles would only bury the live ones.
	const grouped = im_group_readings(readings);
	const reading_html = Object.keys(grouped)
		.map((g) => {
			const tiles = grouped[g]
				.map((o) =>
					reading_tile(
						esc(o.name),
						o.display ? esc(o.display) : "&mdash;",
						o.ts ? esc(im_ago((diag.server_epoch || Math.floor(Date.now() / 1000)) - o.ts)) : ""
					)
				)
				.join("");
			return `<div style="margin-bottom:10px">
				<div style="font-size:10px;letter-spacing:.5px;text-transform:uppercase;
							color:var(--text-muted);margin-bottom:5px">${esc(g)}</div>
				<div style="display:flex;gap:8px;flex-wrap:wrap">${tiles}</div>
			</div>`;
		})
		.join("");

	// ---- device identity (quick view) --------------------------------------
	const device_fields = [
		field(__("IMEI"), `<span style="font-family:monospace">${dash(dev.imei)}</span>`),
		field(__("Vehicle No."), dash(dev.name)),
		field(__("Vehicle Name"), dash(dev.vehicle_name)),
		field(__("Model"), dash(dev.model)),
		field(__("Type"), dash(dev.type)),
		field(__("Company"), dash(dev.company)),
		field(__("Branch"), dash(dev.folder)),
		field(__("Driver"), dash(dev.driver_name)),
		field(
			__("Odometer"),
			dev.current_mileage === null || dev.current_mileage === undefined
				? "&mdash;"
				: esc(dev.current_mileage) + " km"
		),
	].join("");

	// ---- ESSENTIAL VIEW ----------------------------------------------------
	const essential = `
		<div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:8px">
			<div>
				<div style="font-size:17px;font-weight:600">
					${dash(dev.name || dev.vehicle_name || info.license_plate)}
					<span class="indicator-pill ${is_stale ? "orange" : "green"}" style="margin-left:6px">
						${is_stale ? __("STALE") : __("LIVE")} · ${esc(im_ago(age))}
					</span>
					${
						diag.throttled
							? `<span class="indicator-pill orange" style="margin-left:4px">${__("CACHED")}</span>`
							: ""
					}
				</div>
				<div style="font-size:12px;color:var(--text-muted);margin-top:3px">
					${__("IMEI")}
					<span style="font-family:monospace;font-size:13px;color:var(--text-color)">${dash(dev.imei)}</span>
					<button class="btn btn-xs btn-default im-copy-imei" style="padding:0 5px;margin-left:4px">${__("Copy")}</button>
					&nbsp;·&nbsp; ${dash(info.customer)}
					${info.license_plate ? "&nbsp;·&nbsp; " + dash(info.license_plate) : ""}
				</div>
			</div>
			<div class="text-muted" style="font-size:11px;text-align:right">
				${__("via")} ${dash(d.account)}<br>${__("project")} ${dash(d.project_id)}
				· ${__("matched by")} ${dash(d.matched_by)}
			</div>
		</div>

		${section(__("Installation Checks"))}
		<table style="width:100%;font-size:12px;border-collapse:collapse">${checks.join("")}</table>

		${section(__("Status"))}
		<div style="display:flex;gap:8px;flex-wrap:wrap">${kpis}</div>

		${section(__("Port Readings"), unwired.length ? __("{0} port(s) not wired", [unwired.length]) : "")}
		${reading_html || `<div class="text-muted">${__("No ports are reporting")}</div>`}

		${section(__("Device"))}
		<div style="display:flex;gap:14px 18px;flex-wrap:wrap;font-size:12px">${device_fields}</div>

		${section(__("Location"), loc.poi ? __("POI") + ": " + esc(loc.poi) : "")}
		${
			has_fix
				? `<div id="${map_id}" style="height:320px;border:1px solid var(--border-color);
							border-radius:var(--border-radius-md);overflow:hidden;background:var(--control-bg)"></div>
					<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
						<a class="btn btn-xs btn-default" target="_blank" rel="noopener noreferrer"
						   href="https://www.google.com/maps?q=${encodeURIComponent(loc.lat + "," + loc.lon)}">
							${__("Google Maps")}</a>
						<a class="btn btn-xs btn-default" target="_blank" rel="noopener noreferrer"
						   href="https://www.openstreetmap.org/?mlat=${encodeURIComponent(loc.lat)}&mlon=${encodeURIComponent(loc.lon)}#map=16/${encodeURIComponent(loc.lat)}/${encodeURIComponent(loc.lon)}">
							${__("OpenStreetMap")}</a>
						<button class="btn btn-xs btn-default im-copy-coords">${__("Copy Coordinates")}</button>
						<button class="btn btn-xs btn-default im-track">${__("Show Track (6h)")}</button>
						<span class="im-track-note text-muted" style="font-size:11px"></span>
					</div>
					${
						loc.address
							? `<div class="text-muted" style="font-size:11px;margin-top:6px">${esc(loc.address)}</div>`
							: ""
					}`
				: `<div class="text-muted">${__("No GPS fix reported")}</div>`
		}`;

	// ---- FULL DETAIL (behind "Show all") -----------------------------------
	const now_epoch = diag.server_epoch || Math.floor(Date.now() / 1000);

	const port_rows = readings
		.concat(unwired)
		.map(
			(o) => `<tr>
				<td>${dash(o.name)}</td>
				<td><b>${o.display ? esc(o.display) : "&mdash;"}</b></td>
				<td class="text-muted">${o.value === null || o.value === undefined ? "&mdash;" : esc(o.value)}</td>
				<td class="text-muted">${o.raw_value === null || o.raw_value === undefined ? "&mdash;" : esc(o.raw_value)}</td>
				<td class="text-muted">${dash(o.group)}</td>
				<td class="text-muted">${o.reported ? __("yes") : __("not wired")}</td>
			</tr>`
		)
		.join("");

	const details = `
		<div class="row">
			<div class="col-sm-6">
				${section(__("Device"))}
				${kv([
					[__("IMEI"), dash(dev.imei)],
					[__("Vehicle No."), dash(dev.name)],
					[__("Vehicle Name"), dash(dev.vehicle_name)],
					[__("Company"), dash(dev.company)],
					[__("Branch"), dash(dev.folder)],
					[__("Type"), dash(dev.type)],
					[__("Device Model"), dash(dev.model)],
					[__("Driver"), dash(dev.driver_name)],
					[__("Odometer"), dev.current_mileage === null || dev.current_mileage === undefined
						? "&mdash;" : esc(dev.current_mileage) + " km"],
					[__("Odometer (raw)"), dash(dev.odometer_raw)],
				])}
			</div>
			<div class="col-sm-6">
				${section(__("State"))}
				${kv([
					[__("Platform Status"), dash(st.status_text)],
					[__("Ignition"), st.ignition ? `<b>${__("ON")}</b>` : im_onoff(st.ignition)],
					[__("Ignition (raw)"), dash(st.ignition_raw)],
					[__("Speed"), st.speed === null || st.speed === undefined
						? "&mdash;" : esc(st.speed) + " km/h"],
					[__("Heading"), compass ? esc(compass) + (Number.isFinite(Number(st.direction))
						? " (" + esc(st.direction) + "°)" : "") : "&mdash;"],
					[__("GPS Port"), im_onoff(st.gps)],
					[__("Power Port"), im_onoff(st.power)],
					[__("SOS"), im_onoff(st.sos)],
					[__("A/C"), im_onoff(st.ac)],
					[__("Immobiliser"), im_onoff(st.immobilised)],
					[__("External Voltage"), volt === null || volt === undefined
						? "&mdash;" : esc(volt) + " V"],
					[__("Battery"), batt === null || batt === undefined ? "&mdash;" : esc(batt) + " %"],
					[__("Coordinates"), has_fix ? esc(loc.lat + ", " + loc.lon) : "&mdash;"],
					[__("Satellites"), loc.sats === null || loc.sats === undefined
						? "&mdash;" : esc(loc.sats)],
					[__("Altitude"), loc.altitude === null || loc.altitude === undefined
						? "&mdash;" : esc(loc.altitude) + " m"],
					[__("Address"), dash(loc.address)],
					[__("GPS Time"), lu.gps_time_text
						? esc(lu.gps_time_text) + " (" + esc(im_epoch_text(lu.epoch)) + ")" : "&mdash;"],
					[__("Insert Time"), dash(lu.insert_time_text)],
					[__("Reading Age"), esc(im_ago(age))],
				])}

				${section(__("Ticket"))}
				${kv([
					[__("Ticket"), `<a href="/app/xticket/${encodeURIComponent(d.ticket)}">${dash(d.ticket)}</a>`],
					[__("Customer"), dash(info.customer)],
					[__("License Plate"), dash(info.license_plate)],
					[__("Issue Type"), dash(info.issue_type)],
					[__("Status"), dash(info.status)],
				])}
			</div>
		</div>

		${section(__("All Ports"))}
		${
			port_rows
				? `<table class="table table-bordered" style="margin:0;font-size:12px">
					<thead><tr><th>${__("Port")}</th><th>${__("Reading")}</th><th>${__("Value")}</th>
						<th>${__("Raw")}</th><th>${__("Group")}</th><th>${__("Wired")}</th>
					</tr></thead><tbody>${port_rows}</tbody></table>`
				: `<div class="text-muted">${__("IM reported no ports")}</div>`
		}

		${section(__("Diagnostics"))}
		<div style="font-size:11px;color:var(--text-muted)">
			${kv([
				[__("IM Account"), dash(d.account)],
				[__("Project ID"), dash(d.project_id)],
				[__("Endpoint"), dash(diag.base_url)],
				[__("Looked Up By"), dash(d.matched_by) + " = " + dash(d.matched_value)],
				[__("IMEI Source"), dash(d.imei_source)],
				[__("IM Message"), dash(diag.im_msg)],
				[__("HTTP Status"), dash(diag.http_status)],
				[__("Round Trip"), diag.elapsed_ms ? esc(diag.elapsed_ms) + " ms" : "&mdash;"],
				[__("Token"), dash(diag.token_source)],
				[__("Served From Cache"), diag.cached
					? __("yes") + " (" + esc(im_ago(diag.cache_age)) + ")" : __("no")],
				[__("Rate Limited"), diag.throttled
					? `<b>${__("yes")}</b> — ${dash(diag.throttle_msg)}` : __("no")],
				[__("Min Call Interval"), esc(d.min_call_interval_seconds) + " s"],
				[__("Rows Returned"), dash(diag.rows_returned)],
				[__("Mode"), __("Live")],
			])}
		</div>

		<details style="margin-top:16px">
			<summary style="cursor:pointer;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--text-muted)">
				${__("Raw IM payload")}
			</summary>
			<pre style="margin-top:8px;max-height:280px;overflow:auto;font-size:11px;
						background:var(--control-bg);padding:10px;border-radius:var(--border-radius-md)">${frappe.utils.escape_html(
							JSON.stringify(d.raw || {}, null, 2)
						)}</pre>
		</details>`;

	const html = `
	<div style="font-size:13px">
		${essential}

		<div style="margin:18px 0 0;padding-top:14px;border-top:1px solid var(--border-color);text-align:center">
			<button class="btn btn-sm btn-default im-toggle-all">${__("Show all")}</button>
		</div>

		<div class="im-details" style="display:none">${details}</div>
	</div>`;

	const dlg = new frappe.ui.Dialog({
		title: __("IM Status") + " · " + frm.doc.name,
		size: "extra-large",
		primary_action_label: __("Refresh"),
		primary_action() {
			dlg.hide();
			im_check(frm);
		},
		secondary_action_label: __("Copy JSON"),
		secondary_action() {
			frappe.utils.copy_to_clipboard(JSON.stringify(d, null, 2));
		},
	});

	dlg.$body.html(html);
	dlg.show();

	// Draw the map first: if wiring a button below ever throws, the map has
	// already been scheduled rather than silently skipped.
	if (has_fix) im_render_map(map_id, d);

	dlg.$body.find(".im-toggle-all").on("click", function () {
		const $d = dlg.$body.find(".im-details");
		const showing = $d.is(":visible");
		$d.toggle(!showing);
		$(this).text(showing ? __("Show all") : __("Hide details"));
	});

	dlg.$body.find(".im-copy-coords").on("click", () => {
		frappe.utils.copy_to_clipboard(loc.lat + "," + loc.lon);
	});

	dlg.$body.find(".im-copy-imei").on("click", () => {
		frappe.utils.copy_to_clipboard(String(dev.imei || ""));
	});

	dlg.$body.find(".im-track").on("click", function () {
		im_load_track(dlg, frm, $(this));
	});

	dlg.$body.find(".im-ign-test").on("click", () => {
		im_ignition_test(dlg, frm, d, { chip });
	});

	// The ignition test is started by the technician, never by opening this
	// window. It used to auto-start on the theory that whoever opened it was
	// already at the vehicle with the key -- but most people opening a ticket
	// are not: help-desk staff read these snapshots all day, and every one of
	// those reads was silently starting a minutes-long poll against a
	// rate-limited API, then asking someone at a desk to turn a key that is not
	// in front of them. Only the person holding the key can pass this test, so
	// only the person holding the key should start it: "Check Now" on the
	// Ignition row.
}

// ---------------------------------------------------------------- ignition test
//
// Proves the ACC wire actually works, without anyone phoning the technician.
//
// Reads the current state, asks for the OPPOSITE, then re-reads IM until the
// flip is seen or the window closes. Flip observed -> pass. Nothing by the
// deadline -> FALSE.
//
// A disconnected ignition input reports a permanent, plausible-looking "OFF",
// which is why "the field has a value" is not evidence of anything. Only the
// transition is.
//
// THE POLL RATE IS NOT A CONSTANT HERE. IM allows roughly one live call per
// one-to-two minutes per account, so the interval comes from the snapshot
// (`poll_interval_seconds`, set in app_apis settings) and the window is minutes long
// rather than Pilot's five. Hard-coding 5s would simply collect rate-limit
// errors and prove nothing.

// IM sends IGN as "ON"/"OFF"/"--"; the server has already reduced that to a
// tri-state boolean, but a raw value can still arrive on the refresh path.
function im_ign_bool(v) {
	if (v === true) return true;
	if (v === false) return false;
	if (v === null || v === undefined || v === "") return null;
	const s = String(v).trim().toLowerCase();
	if (["on", "1", "true", "yes"].includes(s)) return true;
	if (["off", "0", "false", "no"].includes(s)) return false;
	return null;
}

function im_mmss(ms) {
	const s = Math.max(0, Math.ceil(ms / 1000));
	return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function im_ignition_test(dlg, frm, d, helpers) {
	const $row = dlg.$body.find("#im-check-ignition");
	const $status = $row.find(".im-check-status");
	const $value = $row.find(".im-check-value");
	const $action = $row.find(".im-check-action");

	const start_state = im_ign_bool((d.state || {}).ignition);
	if (start_state === null) {
		frappe.msgprint({
			title: __("Ignition"),
			message: __("This device does not report an ignition value, so it cannot be tested."),
			indicator: "orange",
		});
		return;
	}

	// Both numbers are the server's, because the rate limit is the server's
	// problem to describe. The fallbacks only matter if an older payload is
	// somehow rendered by a newer script.
	const poll_ms = Math.max(30, Number(d.poll_interval_seconds) || 90) * 1000;
	const window_ms = Math.max(1, Number(d.ignition_window_minutes) || 10) * 60 * 1000;

	const want = !start_state;
	const want_word = want ? __("ON") : __("OFF");
	const from_word = start_state ? __("ON") : __("OFF");

	const started_at = Date.now();
	const deadline = started_at + window_ms;

	let ticks = 0;           // 1s UI ticks
	let polls = 0;           // completed IM reads
	let in_flight = false;
	let seen = start_state;  // most recent value IM reported
	let last_read_at = null; // desk-clock time of the last successful read
	let next_poll_at = Date.now() + poll_ms;
	let timer = null;

	const stop = () => {
		if (timer) clearInterval(timer);
		timer = null;
		dlg.$wrapper.off("hidden.bs.modal", stop);
	};

	// Polling must not outlive the dialog, or it keeps hitting IM unseen --
	// and on a rate-limited API that is not merely wasteful, it locks out the
	// next technician who opens the window.
	dlg.$wrapper.on("hidden.bs.modal", stop);

	const seen_word = () => (seen === null ? __("unknown") : seen ? __("ON") : __("OFF"));

	const render = () => {
		const left = deadline - Date.now();
		const pct = Math.min(100, ((Date.now() - started_at) / window_ms) * 100);
		const next_in = Math.max(0, Math.ceil((next_poll_at - Date.now()) / 1000));
		$value.html(`
			<div style="font-size:14px;font-weight:600;color:var(--text-color)">
				${__("Turn the ignition {0} now", [want_word])}
			</div>
			<div style="height:4px;border-radius:2px;background:var(--control-bg);margin:7px 0 5px;overflow:hidden">
				<div style="height:100%;width:${pct}%;background:var(--blue-500,#2490ef);transition:width .9s linear"></div>
			</div>
			<div style="font-size:11px;color:var(--text-muted)">
				${__("Watching IM")} · ${im_mmss(left)} ${__("left")} ·
				${__("{0} checks", [polls])} ·
				${__("next in {0}s", [next_in])} ·
				${__("currently reading")} <b>${seen_word()}</b>
			</div>
			<div style="font-size:10px;color:var(--text-muted);margin-top:2px">
				${__("IM allows about one live call per minute, so this re-reads every {0}s.", [
					Math.round(poll_ms / 1000),
				])}
			</div>`);
		$action.html(`<button class="btn btn-xs btn-default im-ign-stop">${__("Stop")}</button>`);
		$action.find(".im-ign-stop").on("click", () => {
			stop();
			finish(null);
		});
	};

	// Whatever the verdict, close by stating the value IM actually handed back.
	// Pass/fail answers "is the wire good?"; this answers "what is the ignition
	// doing right now?", which is what he checks against the key.
	const state_tail = () => `
		<div style="font-size:11px;margin-top:5px;padding-top:5px;
					border-top:1px dashed var(--border-color)">
			${__("State received from IM")}: <b>${seen_word()}</b>
			<span style="color:var(--text-muted)">
				· ${__("{0} checks", [polls])}${
					last_read_at ? " · " + __("last read {0}", [last_read_at]) : ""
				}
			</span>
		</div>`;

	// result: true = flipped, false = timed out, null = cancelled
	const finish = (result) => {
		stop();
		const secs = Math.round((Date.now() - started_at) / 1000);

		if (result === true) {
			$status.html(helpers.chip("ok"));
			$value.html(
				`<b style="color:var(--green-600,#16794c)">${__("TRUE")} — ${__(
					"ignition follows the key"
				)}</b>
				 <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
					${__("Changed {0} → {1} after {2}s ({3} checks). The ACC wire is working.",
						[from_word, want_word, secs, polls])}
				 </div>
				 ${state_tail()}`
			);
		} else if (result === false) {
			$status.html(helpers.chip("bad"));
			$value.html(
				`<b style="color:var(--red-600,#c0341d)">${__("FALSE")} — ${__(
					"ignition did not change"
				)}</b>
				 <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
					${__("Still {0} after {1} and {2} checks.", [from_word, im_mmss(window_ms), polls])}
					${__("Check the ACC wire, the fuse, and that the key was actually turned.")}
				 </div>
				 ${state_tail()}`
			);
		} else {
			$status.html(helpers.chip("warn"));
			$value.html(
				`<b>${__("Test stopped")}</b>
				 <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
					${__("Stopped after {0}s and {1} checks — result unknown.", [secs, polls])}
				 </div>
				 ${state_tail()}`
			);
		}

		$action.html(`<button class="btn btn-xs btn-default im-ign-test">${__("Check Again")}</button>`);
		$action.find(".im-ign-test").on("click", () => im_ignition_test(dlg, frm, d, helpers));
	};

	const poll = () => {
		// Never stack requests: if IM is slow, skip this slot rather than
		// queueing calls that all land at once and trip the rate limit.
		if (in_flight) return;
		in_flight = true;
		next_poll_at = Date.now() + poll_ms;

		frappe.call({
			method: "app_apis.im_connector.get_snapshot",
			args: { ticket: frm.doc.name },
			// No freeze: the dialog must stay readable while this runs.
			callback(r) {
				in_flight = false;
				polls++;
				const msg = r && r.message;
				if (!msg) return;

				const cur = im_ign_bool((msg.state || {}).ignition);
				if (cur !== null) {
					seen = cur;
					last_read_at = frappe.datetime.now_time();
				}

				if (cur === want) {
					finish(true);
					return;
				}
				render();
			},
			error() {
				// A single failed read should not abort a multi-minute test.
				in_flight = false;
				polls++;
			},
		});
	};

	render();
	poll(); // read immediately so the first result is not a full interval away

	timer = setInterval(() => {
		ticks++;
		if (Date.now() >= deadline) {
			finish(false);
			return;
		}
		render();
		if (Date.now() >= next_poll_at) poll();
	}, 1000);
}

// ---------------------------------------------------------------- map

// The map instance for the open dialog, so the track overlay can find it.
let im_map_instance = null;

function im_render_map(map_id, d) {
	const loc = d.location || {};
	const st = d.state || {};
	const dev = d.device || {};

	// Runs more than once on purpose (see the scheduling at the bottom): the
	// _leaflet_id guard makes every call after the first a no-op.
	const init = () => {
		const el = document.getElementById(map_id);
		if (!el || el._leaflet_id) return;

		try {
			im_map_instance = im_build_map(el, map_id, loc, st, dev);
		} catch (e) {
			// Never leave an unexplained grey rectangle: say what went wrong
			// and still give the reader a way to see the position.
			console.error("[im] map failed to render", e);
			el.innerHTML =
				`<div class="text-muted" style="padding:16px">
					${__("Map could not be drawn")}: ${frappe.utils.escape_html(String(e.message || e))}.
					<a target="_blank" rel="noopener noreferrer"
					   href="https://www.openstreetmap.org/?mlat=${encodeURIComponent(loc.lat)}&mlon=${encodeURIComponent(loc.lon)}">
					   ${__("Open map")}</a>
				 </div>`;
		}
	};

	// A Leaflet map sizes itself from its container, and the container has no
	// layout until the modal's fade-in finishes -- initialising too early
	// yields a 0x0 map and a grey box. Hook the modal's own "shown" event and
	// keep two timed retries as a fallback for when that event is missed.
	$(document).one("shown.bs.modal", init);
	setTimeout(init, 150);
	setTimeout(init, 700);
}

function im_build_map(el, map_id, loc, st, dev) {
	if (typeof L === "undefined" || !L.map) {
		throw new Error(__("Leaflet is not loaded"));
	}

	const defaults = (frappe.utils.map_defaults || {}).tiles || {};
	const map = L.map(map_id, { scrollWheelZoom: false }).setView([loc.lat, loc.lon], 15);

	const street = L.tileLayer(
		(defaults.default_tile || {}).url || "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
		(defaults.default_tile || {}).options || {}
	);
	const satellite = defaults.satellite_tile
		? L.tileLayer(defaults.satellite_tile.url, defaults.satellite_tile.options)
		: null;

	street.addTo(map);
	if (satellite) {
		L.control.layers({ [__("Street")]: street, [__("Satellite")]: satellite }, {}).addTo(map);
	}

	// A divIcon rather than the default PNG marker: frappe's bundled marker
	// images are named "leafletmarker-icon.png", which does not match the
	// "marker-icon.png" Leaflet derives from imagePath, so the stock icon
	// 404s. Drawing the marker in CSS also lets it carry the heading.
	const heading = Number(st.direction);
	const arrow = Number.isFinite(heading)
		? `<div style="position:absolute;left:50%;top:50%;width:0;height:0;
					transform:translate(-50%,-50%) rotate(${heading}deg)">
				<div style="position:absolute;left:-6px;top:-24px;width:0;height:0;
							border-left:6px solid transparent;border-right:6px solid transparent;
							border-bottom:10px solid #2490ef"></div>
		   </div>`
		: "";

	const icon = L.divIcon({
		className: "",
		iconSize: [22, 22],
		iconAnchor: [11, 11],
		html: `<div style="position:relative;width:22px;height:22px">
				${arrow}
				<div style="position:absolute;inset:0;border-radius:50%;background:#2490ef;
							border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.25)"></div>
			   </div>`,
	});

	const popup = [
		`<b>${im_esc(dev.name || dev.vehicle_name || dev.imei)}</b>`,
		`${__("Speed")}: ${im_esc(st.speed)} km/h`,
		`${__("Ignition")}: ${st.ignition === true ? __("ON") : st.ignition === false ? __("OFF") : "—"}`,
		`${loc.lat}, ${loc.lon}`,
	].join("<br>");

	L.marker([loc.lat, loc.lon], { icon }).addTo(map).bindPopup(popup);

	// Accuracy is not reported, so draw nothing implying it. A light circle
	// only marks the point at low zoom.
	L.circle([loc.lat, loc.lon], {
		radius: 40,
		color: "#2490ef",
		weight: 1,
		opacity: 0.5,
		fillOpacity: 0.08,
	}).addTo(map);

	// The container may still have been mid-animation when this ran.
	map.invalidateSize();
	setTimeout(() => map.invalidateSize(), 300);

	return map;
}

// ---------------------------------------------------------------- track overlay
//
// Draws the last six hours from `?token=getVehicleTrackLogs` over the live
// position. IM does not document that endpoint's response shape, so the server
// extracts points defensively and may legitimately return none -- which this
// reports as "no points", never as a silent empty map.

function im_load_track(dlg, frm, $btn) {
	const $note = dlg.$body.find(".im-track-note");
	$btn.prop("disabled", true).text(__("Loading…"));
	$note.text("");

	frappe.call({
		method: "app_apis.im_connector.get_track_logs",
		args: { ticket: frm.doc.name, hours: 6 },
		callback(r) {
			$btn.prop("disabled", false).text(__("Show Track (6h)"));
			const t = (r && r.message) || {};
			const pts = t.points || [];

			if (!pts.length) {
				$note.text(
					__("IM returned no track points for {0} between {1} and {2}.", [
						t.vehicle_no,
						t.start_date,
						t.end_date,
					])
				);
				return;
			}
			if (!im_map_instance) {
				$note.text(__("{0} points returned, but the map is not drawn.", [pts.length]));
				return;
			}

			const line = pts.map((p) => [p.lat, p.lon]);
			const poly = L.polyline(line, { color: "#7c3aed", weight: 3, opacity: 0.8 });
			poly.addTo(im_map_instance);
			// Start of the run, so the direction of travel is readable at a glance.
			L.circleMarker(line[0], {
				radius: 5,
				color: "#7c3aed",
				fillColor: "#fff",
				fillOpacity: 1,
				weight: 2,
			})
				.addTo(im_map_instance)
				.bindPopup(`<b>${__("Track start")}</b><br>${im_esc(pts[0].time_text || "")}`);

			im_map_instance.fitBounds(poly.getBounds(), { padding: [20, 20] });
			$note.text(
				__("{0} points · {1} → {2}", [pts.length, t.start_date, t.end_date])
			);
			$btn.prop("disabled", true);
		},
		error() {
			$btn.prop("disabled", false).text(__("Show Track (6h)"));
		},
	});
}
