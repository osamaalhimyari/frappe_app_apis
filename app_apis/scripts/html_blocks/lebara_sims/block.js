// Lebara SIMs -- Custom HTML Block. `root_element` is this block's shadow root.
// Every call goes to the "Lebara API" Server Script (api_method "lebara"), so
// what each button does is editable there; this file is only the screen.
//
//   list      the "Lebara SIM" list, which the "Lebara SIM Sync" Server Script
//             refreshes from Lebara every hour (Sync now = sync_now, in the
//             background). Searching and paging never call Lebara.
//   detail    sim_full       stored record + live status, session, location
//   status    resume/suspend/activate, then AjaxBssStatus is polled until
//             Lebara reports the new state (it lags a few seconds)
//   SMS       send_sms, then sent_check every 30 s until the SMS shows up in
//             Lebara's list with a final status (and the device's reply)
//   history   sim_history    only when "Load messages" is clicked

(function () {
	const root = root_element.querySelector(".lb");
	const $ = (sel) => root.querySelector(sel);
	const esc = (v) => frappe.utils.escape_html(v === undefined || v === null || v === "" ? "—" : String(v));
	const PAGE = 50;
	const SN_COLOR = { Active: 'green', Delivered: 'blue', Inactive: 'gray', Consumed: 'orange', Expired: 'red' };
	const STATUS_COLOR = { Active: "green", Suspend: "orange", Idle: "gray", Bar: "red", Deactivated: "darkgrey" };

	const state = { skip: 0, total: 0, rows: [], sim: null, history: [], history_loaded: false, polls: [], imeis: [], kpi: null, kpi_defs: {}, sn: new Set(), sn_status: {} };
	const COLS = 8;
	const DT = "Lebara SIM";
	const SENT_POLL_MS = 30000;
	const SENT_POLL_FOR_MS = 15 * 60000;
	const FINAL = { 2: "Success", 3: "Failed", 5: "Exceed Limit", 7: "Rejected By Lebara" };

	function api(action, args, freeze) {
		return new Promise((resolve, reject) => {
			frappe.call({
				method: "lebara",
				args: Object.assign({ action }, args || {}),
				freeze: !!freeze,
				freeze_message: typeof freeze === "string" ? freeze : undefined,
				callback: (r) => resolve(((r && r.message) || {}).result),
				error: (e) => {
					// The session may have just expired; show it in the header.
					if (action !== "status") refresh_session().catch(() => {});
					reject(e);
				},
			});
		});
	}

	const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
	const when = (v) => (v ? String(v).replace("T", " ").replace(/\.\d+$/, "") : "");
	const pill = (text, color) => `<span class="indicator-pill ${color || "gray"}">${esc(text)}</span>`;

	// ------------------------------------------------------------ session
	function refresh_session() {
		return api("status").then((s) => {
			const el = $(".lb-session");
			const ok = s && s.status === "Logged In";
			el.className = "lb-session indicator-pill " + (ok ? "green" : s && s.enabled ? "orange" : "gray");
			el.textContent = !s.enabled ? __("Lebara disabled") : s.status || __("Not logged in");
			el.title = s.last_error || (s.last_refresh ? __("Last refresh") + ": " + s.last_refresh : "");
			$(".lb-login").style.display = ok || !s.enabled ? "none" : "";
			$(".lb-synced").textContent = s.sims_synced_at
				? __("SIM list synced {0} · {1} SIMs · refreshes hourly, 6 am to 11 pm", [s.sims_synced_at.slice(0, 16), (s.sims_count || 0).toLocaleString()])
				: __("SIM list not synced yet — click Sync now");
			$(".lb-synced").title = s.sims_sync_note || "";
			return ok;
		});
	}

	function login() {
		api("request_otp", {}, __("Signing in to Lebara…")).then((res) => {
			if (res && res.logged_in) return after_login();
			frappe.prompt(
				[{ fieldname: "code", fieldtype: "Data", label: __("Code from SMS"), reqd: 1,
				   description: esc(res && res.message) + " " + __("Valid for about 60 seconds.") }],
				(v) => api("submit_otp", { code: v.code }, __("Checking the code…")).then(after_login),
				__("Lebara OTP"),
				__("Submit")
			);
		});
	}

	function after_login() {
		frappe.show_alert({ message: __("Logged in to Lebara."), indicator: "green" });
		refresh_session().then(load_page);
	}

	function sync_now() {
		api("sync_now", {}).then(() => {
			frappe.show_alert({ message: __("Syncing the SIM list in the background (about a minute)…"), indicator: "blue" });
			const started = Date.now();
			const tick = () => api("status").then((s) => {
				const at = s.sims_synced_at ? new Date(s.sims_synced_at.replace(" ", "T")).getTime() : 0;
				if (at >= started - 60000 && at > 0 && Date.now() - started > 5000) {
					refresh_session();
					load_page();
					load_usage();
					frappe.show_alert({ message: s.sims_sync_note || __("SIM list synced."), indicator: "green" });
				} else if (Date.now() - started < 10 * 60000) {
					setTimeout(tick, 10000);
				}
			});
			setTimeout(tick, 15000);
		});
	}

	// ------------------------------------------------------------ list
	// Search: digits look in MSISDN / ICCID / IMSI / both IMEIs; anything else
	// looks in the plate, vehicle and customer. "Find IMEIs" replaces it with an
	// exact list (the ERP device IMEI or the one Lebara's network reports).
	function list_filters() {
		const filters = [];
		const status = { 1: "Idle", 2: "Active", 3: "Bar", 4: "Suspend", 9: "Deactivated" }[$(".lb-status").value];
		if (status) filters.push([DT, "status", "=", status]);
		// The widget that was clicked (see state.kpi_defs in load_usage).
		const kd = state.kpi && state.kpi_defs[state.kpi];
		if (kd) {
			(kd.filters || []).forEach((f) => filters.push([DT].concat(f)));
			if (kd.names) filters.push([DT, "name", "in", kd.names.length ? kd.names : ["-"]]);
		}
		if (state.imeis.length) {
			return { filters, or_filters: [[DT, "erp_imei", "in", state.imeis], [DT, "imei", "in", state.imeis]] };
		}
		const raw = $(".lb-q").value.trim();
		const digits = raw.replace(/\D/g, "");
		let or_filters = [];
		if (raw && digits === raw.replace(/\s/g, "")) {
			or_filters = ["msisdn", "iccid", "imsi", "imei", "erp_imei"].map((f) => [DT, f, "like", "%" + digits + "%"]);
		} else if (raw) {
			or_filters = ["erp_plate", "erp_vehicle", "erp_customer", "group_name"].map((f) => [DT, f, "like", "%" + raw + "%"]);
		}
		return { filters, or_filters };
	}

	const LIST_FIELDS = ["subscriber_id", "msisdn", "serial_no", "status", "imei", "erp_imei", "erp_vehicle", "erp_plate",
		"erp_vehicle.device_statues as device_status", "erp_customer", "group_name", "last_connection", "this_month_mb"];

	function load_page() {
		const tbody = $(".lb-rows");
		tbody.innerHTML = `<tr><td colspan="${COLS}" class="lb-empty">${__("Loading…")}</td></tr>`;
		const f = list_filters();
		return Promise.all([
			frappe.xcall("frappe.client.get_list", {
				doctype: DT,
				fields: LIST_FIELDS,
				filters: f.filters,
				or_filters: f.or_filters,
				order_by: "msisdn asc",
				limit_start: state.skip,
				limit_page_length: PAGE,
			}),
			frappe.xcall("frappe.desk.reportview.get_count", { doctype: DT, filters: f.filters, or_filters: f.or_filters }),
		]).then(([rows, total]) => {
			state.total = total || 0;
			state.rows = rows || [];
			draw_list();
			draw_imeibar();
			draw_kpibar();
			check_serials();
		}, () => {
			tbody.innerHTML = `<tr><td colspan="${COLS}" class="lb-empty">${__("Could not load the SIM list.")}</td></tr>`;
		});
	}

	// A link to the Serial No form: a plain click opens it here, Ctrl/Cmd-click in a new tab.
	function sn_link(sn, text) {
		if (!sn) return esc(text);
		return `<a class="lb-sn" href="/app/serial-no/${encodeURIComponent(sn)}" data-sn="${esc(sn)}" title="${esc(__("Open Serial No {0}", [sn]))}">${esc(text)}</a>`;
	}

	// Which IMEIs on this page are Serial Nos in the ERP -- only those get a link.
	// (The SIM's own Serial No is a Link field, so that one is always there.)
	// Serial Status column: the status of the SIM's own Serial No in the ERP.
	// Device Status column: the Device Statues field on the SIM's Customer Vehicle.
	const DEVICE_COLOR = { Installed: "green", Processing: "orange", "Canceled Installation": "gray", Deleted: "red" };
	function device_status_cell(v) {
		return v ? pill(v, DEVICE_COLOR[v] || "gray") : '<span class="text-muted">—</span>';
	}

	function serial_status_cell(sn) {
		if (!sn) return '<span class="text-muted">—</span>';
		if (!(sn in state.sn_status)) return '<span class="text-muted">…</span>';
		const v = state.sn_status[sn];
		return v ? pill(v, SN_COLOR[v]) : '<span class="text-muted">—</span>';
	}

	function load_serial_status() {
		const rows = state.rows;
		const sns = rows.map((s) => s.serial_no).filter((v) => v && !(v in state.sn_status));
		if (!sns.length) return;
		frappe.xcall('frappe.client.get_list', { doctype: 'Serial No', fields: ['name', 'status'],
			filters: [['name', 'in', sns]], limit_page_length: 0 })
			.then((found) => {
				sns.forEach((n) => { state.sn_status[n] = ''; });
				(found || []).forEach((f) => { state.sn_status[f.name] = f.status || ''; });
				if (state.rows === rows) draw_list();
			})
			.catch(() => {
				sns.forEach((n) => { state.sn_status[n] = ''; });
				if (state.rows === rows) draw_list();
			});
	}

	function check_serials() {
		const rows = state.rows;
		const imeis = rows.map((s) => String(s.erp_imei || s.imei || "")).filter((v) => v && !state.sn.has(v));
		if (!imeis.length) return;
		frappe.xcall("frappe.client.get_list", { doctype: "Serial No", fields: ["name"],
			filters: [["name", "in", imeis]], limit_page_length: 0 })
			.then((found) => {
				(found || []).forEach((f) => state.sn.add(String(f.name)));
				if (state.rows === rows && found && found.length) draw_list();
			})
			.catch(() => {});
	}

	function draw_list() {
		const tbody = $(".lb-rows");
		if (!state.rows.length) {
			tbody.innerHTML = `<tr><td colspan="${COLS}" class="lb-empty">${__("No SIMs match.")}</td></tr>`;
		} else {
			tbody.innerHTML = state.rows.map((s) => `
				<tr data-id="${esc(s.subscriber_id)}" class="${state.sim && state.sim.Id === s.subscriber_id ? "lb-sel" : ""}">
					<td class="lb-mono">${sn_link(s.serial_no, s.msisdn)}</td>
					<td>${pill(s.status, STATUS_COLOR[s.status])}</td>
					<td>${device_status_cell(s.device_status)}</td>
					<td class="lb-mono" title="${__("Lebara")}: ${esc(s.imei)}">${state.sn.has(String(s.erp_imei || s.imei || "")) ? sn_link(s.erp_imei || s.imei, s.erp_imei || s.imei) : esc(s.erp_imei || s.imei)}</td>
					<td>${esc(s.erp_plate)}</td>
					<td>${esc(s.erp_customer)}</td>
					<td>${esc(when(s.last_connection).slice(0, 10))}</td>
					<td class="lb-num">${esc(s.this_month_mb == null ? "" : Number(s.this_month_mb).toFixed(2))}</td>
				</tr>`).join("");
		}
		const last = Math.min(state.skip + state.rows.length, state.total);
		$(".lb-count").textContent = state.total
			? `${state.skip + 1}–${last} ${__("of")} ${state.total.toLocaleString()}`
			: "";
		$(".lb-prev").disabled = state.skip <= 0;
		$(".lb-next").disabled = last >= state.total;
	}

	// ------------------------------------------------------------ IMEI list + export
	function find_imeis() {
		frappe.prompt(
			[{ fieldname: "imeis", fieldtype: "Small Text", label: __("Device IMEIs"), reqd: 1,
			   default: state.imeis.join("\n"),
			   description: __("One per line, or separated by commas / spaces. Matches the ERP device IMEI and the IMEI Lebara reports.") }],
			(v) => {
				const list = String(v.imeis || "").split(/[^0-9]+/).filter((x) => x.length >= 6);
				state.imeis = Array.from(new Set(list)).slice(0, 5000);
				$(".lb-q").value = "";
				state.skip = 0;
				load_page();
			},
			__("Find SIMs by IMEI"),
			__("Find")
		);
	}

	// Which of the pasted IMEIs found no SIM (only the current page is known,
	// so the full check is done with the export, which reads every match).
	function draw_imeibar() {
		const bar = $(".lb-imeibar");
		if (!state.imeis.length) {
			bar.style.display = "none";
			return;
		}
		bar.style.display = "";
		bar.innerHTML = `<span>${__("Showing SIMs for {0} pasted IMEIs — {1} found", [state.imeis.length, state.total])}
			${state.total < state.imeis.length ? `<span class="lb-missing">· ${__("{0} without a SIM (listed in the export)", [state.imeis.length - state.total])}</span>` : ""}</span>
			<button type="button" class="btn btn-default btn-xs lb-clearimeis">${__("Clear list")}</button>`;
	}

	function csv_cell(v) {
		const t = v === null || v === undefined ? "" : String(v);
		return /[",\n\r]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
	}

	// ------------------------------------------------------------ widget filter
	function draw_kpibar() {
		const bar = $(".lb-kpibar");
		const d = state.kpi && state.kpi_defs[state.kpi];
		const any = !!(d || state.imeis.length || $(".lb-q").value.trim() || $(".lb-status").value);
		$(".lb-clear").style.display = any ? "" : "none";
		if (!d) {
			bar.style.display = "none";
			return;
		}
		bar.style.display = "";
		bar.innerHTML = `<span>${esc(__("Showing"))} <b>${esc(d.label)}</b> — ${esc(d.hint)} · ${esc(__("{0} SIMs", [Number(state.total || 0).toLocaleString("en-US")]))}</span>`;
	}

	function mark_kpis() {
		root.querySelectorAll(".lb-kpi[data-kpi]").forEach((el) => el.classList.toggle("is-on", el.dataset.kpi === state.kpi));
	}

	// Clicking a widget lists exactly what it counts, so it resets the other filters
	// first. Status / search can then narrow it down further.
	function pick_kpi(key) {
		state.kpi = state.kpi === key ? null : key;
		if (state.kpi) {
			state.imeis = [];
			$(".lb-q").value = "";
			$(".lb-status").value = "";
		}
		state.skip = 0;
		mark_kpis();
		load_page();
	}

	function clear_filters() {
		$(".lb-q").value = "";
		$(".lb-status").value = "";
		state.imeis = [];
		state.kpi = null;
		state.skip = 0;
		mark_kpis();
		load_page();
	}

	// Account manager names, for the export only (the list itself doesn't show them).
	function add_am_names(rows) {
		const ids = [...new Set(rows.map((r) => r.account_manager).filter(Boolean))];
		if (!ids.length) return rows;
		return frappe.xcall("frappe.client.get_list", {
			doctype: "User",
			fields: ["name", "full_name"],
			filters: [["name", "in", ids]],
			limit_page_length: 1000,
		}).then((users) => {
			const names = {};
			users.forEach((u) => { names[u.name] = u.full_name || u.name; });
			rows.forEach((r) => { r.account_manager_name = names[r.account_manager] || r.account_manager || ""; });
			return rows;
		}).catch(() => rows);
	}

	function export_csv() {
		const f = list_filters();
		frappe.show_alert({ message: __("Preparing the export…"), indicator: "blue" });
		frappe.xcall("frappe.client.get_list", {
			doctype: DT,
			fields: ["msisdn", "iccid", "imsi", "status", "erp_vehicle.device_statues as device_status", "imei", "erp_imei", "erp_vehicle", "erp_plate", "erp_customer",
				"erp_customer.account_manager as account_manager", "match_by", "group_name", "sub_customer", "activation_date", "last_connection", "this_month_mb",
				"last_month_mb", "subscriber_id"],
			filters: f.filters,
			or_filters: f.or_filters,
			order_by: "msisdn asc",
			limit_page_length: 100000,
		}).then(add_am_names).then((rows) => {
			const head = ["Searched IMEI", "MSISDN", "ICCID", "IMSI", "Status", "Device Status", "IMEI (Lebara)", "IMEI (ERP)", "Customer Vehicle",
				"Plate", "Customer", "Account Manager", "Matched By", "Group", "Sub-customer", "Activation Date", "Last Connection",
				"This Month MB", "Last Month MB", "Subscriber Id"];
			const lines = [head];
			const found = new Set();
			rows.forEach((r) => {
				const searched = state.imeis.find((i) => i === r.erp_imei || i === r.imei) || "";
				if (searched) found.add(searched);
				lines.push([searched, r.msisdn, r.iccid, r.imsi, r.status, r.device_status || "", r.imei, r.erp_imei, r.erp_vehicle, r.erp_plate,
					r.erp_customer, r.account_manager_name || r.account_manager || "", r.match_by, r.group_name, r.sub_customer, r.activation_date, r.last_connection,
					r.this_month_mb, r.last_month_mb, r.subscriber_id]);
			});
			state.imeis.filter((i) => !found.has(i)).forEach((i) => lines.push([i, "NOT FOUND"]));
			// BOM first, so Excel reads it as UTF-8 and Arabic plates stay readable.
			const csv = "\ufeff" + lines.map((l) => l.map(csv_cell).join(",")).join("\r\n");
			const a = document.createElement("a");
			a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
			a.download = "lebara_sims_" + frappe.datetime.now_datetime().replace(/[^0-9]/g, "").slice(0, 12) + ".csv";
			document.body.appendChild(a);
			a.click();
			a.remove();
			frappe.show_alert({ message: __("Exported {0} rows.", [lines.length - 1]), indicator: "green" });
		});
	}

	// ------------------------------------------------------------ detail
	function stop_polls() {
		state.polls.forEach((p) => (p.stop = true));
		state.polls = [];
	}

	function open_sim(msisdn) {
		stop_polls();
		const box = $(".lb-detail");
		box.innerHTML = `<div class="lb-empty">${__("Loading {0}…", [esc(msisdn)])}</div>`;
		api("sim_full", { msisdn }).then((sim) => {
			if (!sim) {
				box.innerHTML = `<div class="lb-empty">${__("SIM not found.")}</div>`;
				return;
			}
			state.sim = sim;
			draw_list();
			draw_detail();
		}, () => (box.innerHTML = `<div class="lb-empty">${__("Could not load the SIM.")}</div>`));
	}

	function draw_detail() {
		const s = state.sim;
		const live = s.live_status || {};
		const prof = (s.live_profile && s.live_profile.simInfo) || {};
		const sess = (s.live_profile && s.live_profile.session) || {};
		const loc = s.location;
		const field = (label, value) => `<div><span>${__(label)}</span><b>${esc(value)}</b></div>`;

		$(".lb-detail").innerHTML = `
			<div class="lb-dhead">
				<div class="lb-dtitle lb-mono">${esc(s.Msisdn)}</div>
				<div class="lb-badges">
					<span title="${__("Stored status")}">${pill(s.StatusText, STATUS_COLOR[s.StatusText])}</span>
					<span title="${__("Live network status")}">${pill(__("Live") + ": " + (live.statusText || "—"), STATUS_COLOR[live.statusText])}</span>
					<button type="button" class="btn btn-default btn-xs lb-reload">${__("Refresh")}</button>
				</div>
			</div>
			<div class="lb-meta">
				${field("ICCID", s.ICCID)}${field("IMSI", s.IMSI)}${field("IMEI", s.IMEI)}
				${field("Subscriber Id", s.Id)}${field("Group", s.GroupName)}${field("Sub-customer", s.SubCustomerName)}
				${field("Activated", when(s.ActivationDate))}${field("Last connection", when(s.LastConnectionDate))}
				${field("This month (MB)", s.ThisMonthUsage)}${field("Last month (MB)", s.LastMonthUsage)}
				${field("APN", sess.apn && sess.apn !== "—" ? sess.apn : s.Apn)}
				${field("In data session", sess.inDataSessionText)}
				${field("IP", sess.ip)}${field("Session start", sess.start)}${field("Session time", sess.accuTime)}
				${field("Traffic in / out", (sess.input || "—") + " / " + (sess.output || "—"))}
				${field("Technology", prof.technology)}${field("Cell", prof.cellId)}
				<div><span>${__("Location")}</span><b>${loc && loc.map
					? `<a href="${esc(loc.map)}" target="_blank" rel="noopener">${esc(loc.lat)}, ${esc(loc.lng)}</a>`
					: "—"}</b></div>
			</div>

			<div class="lb-section">
				<h5>${__("Status")}</h5>
				<div class="lb-actions">
					<button type="button" class="btn btn-success btn-sm lb-act" data-act="resume" ${live.canResume ? "" : "disabled"}>${__("Make Active (Resume)")}</button>
					<button type="button" class="btn btn-warning btn-sm lb-act" data-act="suspend" ${live.canSuspend ? "" : "disabled"}>${__("Suspend")}</button>
					${live.canActivate ? `<button type="button" class="btn btn-primary btn-sm lb-act" data-act="activate">${__("Activate")}</button>` : ""}
				</div>
				<div class="lb-note lb-actnote"></div>
			</div>

			<div class="lb-section lb-sms">
				<h5>${__("Send SMS")}</h5>
				<textarea class="form-control lb-text" maxlength="160" placeholder="${__("e.g. im2m 0821 getio")}" ${live.canSendSms === false ? "disabled" : ""}></textarea>
				<div class="lb-smsbar">
					<label><input type="checkbox" class="lb-wait" checked> ${__("Wait for the device's reply")}</label>
					<span class="lb-note lb-len">0 / 160</span>
					<button type="button" class="btn btn-primary btn-sm lb-send" ${live.canSendSms === false ? "disabled" : ""}>${__("Send")}</button>
				</div>
				<div class="lb-note lb-smsnote">${live.canSendSms === false ? __("Lebara does not allow SMS to this SIM in its current state.") : ""}</div>
				<div class="lb-replybox"></div>
			</div>

			<div class="lb-section">
				<div class="lb-histbar">
					<h5 style="margin:0">${__("Message history")} <span class="lb-note lb-histcount"></span></h5>
					<div class="lb-actions">
						<select class="form-control lb-histfilter">
							<option value="">${__("All")}</option>
							<option value="1">${__("Sent to SIM")}</option>
							<option value="2">${__("Received from SIM")}</option>
						</select>
						<button type="button" class="btn btn-default btn-xs lb-histreload">${__("Load messages")}</button>
					</div>
				</div>
				<div class="lb-hist"><div class="lb-empty">${__("Click Load messages to fetch this SIM's messages from Lebara.")}</div></div>
			</div>`;
	}

	// ------------------------------------------------------------ status change
	const TARGET = { resume: "Active", suspend: "Suspend", activate: "Active" };

	function change_status(act) {
		const s = state.sim;
		const label = { resume: __("make this SIM Active"), suspend: __("suspend this SIM"), activate: __("activate this SIM") }[act];
		frappe.confirm(__("Are you sure you want to {0}?", [label]) + `<br><b>${esc(s.Msisdn)}</b>`, () => {
			const note = $(".lb-actnote");
			root.querySelectorAll(".lb-act").forEach((b) => (b.disabled = true));
			note.className = "lb-note lb-actnote";
			note.textContent = __("Sending to Lebara…");
			api(act, { id: s.Id }, __("Sending to Lebara…")).then((res) => {
				if (!res || !res.ok) {
					note.className = "lb-note lb-actnote lb-bad";
					note.textContent = (res && res.message) || __("Lebara did not confirm the change.");
					return draw_detail_after(1000);
				}
				note.className = "lb-note lb-actnote lb-ok";
				note.textContent = res.message + " — " + __("waiting for the network to show {0}…", [TARGET[act]]);
				wait_status(s.Id, TARGET[act]).then((st) => {
					note.textContent = st === TARGET[act]
						? __("Done: the network shows {0}.", [st])
						: __("Lebara accepted it; the network still shows {0}. Refresh in a minute.", [st || "—"]);
					refresh_current();
				});
			}, () => draw_detail_after(0));
		});
	}

	function draw_detail_after(ms) {
		setTimeout(refresh_current, ms);
	}

	function refresh_current() {
		if (!state.sim) return;
		const keep = state.sim.Msisdn;
		api("sim_full", { msisdn: keep }).then((sim) => {
			if (!sim || !state.sim || state.sim.Msisdn !== keep) return;
			const note = $(".lb-actnote");
			const text = note ? note.textContent : "";
			const cls = note ? note.className : "";
			state.sim = sim;
			const row = state.rows.find((r) => r.subscriber_id === sim.Id);
			if (row) row.status = sim.StatusText;
			draw_list();
			draw_detail();
			draw_history();
			if (text) Object.assign($(".lb-actnote"), { textContent: text, className: cls });
		});
	}

	async function wait_status(id, want) {
		const poll = { stop: false };
		state.polls.push(poll);
		let last = "";
		for (let i = 0; i < 12 && !poll.stop; i++) {
			await sleep(5000);
			if (poll.stop) break;
			try {
				const st = await api("sim_ajax", { id, name: "AjaxBssStatus" });
				last = (st && st.statusText) || last;
				if (last === want) break;
			} catch (e) {
				break;
			}
		}
		return last;
	}

	// ------------------------------------------------------------ SMS
	// SendSMS answers {} only, so "sent" is proven by the SMS showing up in
	// Lebara's own SMS list: sent_check every 30 s until it is there with a
	// final status (Success / Failed / ...), and then until the device replies.
	async function send_sms() {
		const s = state.sim;
		const text = $(".lb-text").value.trim();
		if (!text) return;
		const note = $(".lb-smsnote");
		const box = $(".lb-replybox");
		const wait = $(".lb-wait").checked;
		$(".lb-send").disabled = true;
		box.innerHTML = "";
		note.className = "lb-note lb-smsnote";
		note.textContent = __("Sending…");
		let res;
		try {
			res = await api("send_sms", { id: s.Id, msisdn: s.Msisdn, message: text });
		} catch (e) {
			$(".lb-send").disabled = false;
			note.textContent = "";
			return;
		}
		$(".lb-text").value = "";
		$(".lb-len").textContent = "0 / 160";
		$(".lb-send").disabled = false;

		const poll = { stop: false };
		state.polls.push(poll);
		const started = Date.now();
		let sent = null;
		let reply = null;
		const show = () => {
			const st = sent ? FINAL[sent.SmsStatus] || sent.StatusText || __("Pending") : null;
			box.innerHTML = `<div class="lb-reply">
				<div><b>${__("Sent")}:</b> ${sent
					? `${pill(st, sent.SmsStatus === 2 ? "green" : FINAL[sent.SmsStatus] ? "red" : "orange")}
					   <span class="lb-note">#${esc(sent.Id)} · ${esc(when(sent.SmsDate))}</span>`
					: `<span class="lb-note">${__("not in Lebara's list yet")}</span>`}</div>
				<div class="lb-msg lb-mono">${esc(text)}</div>
				${reply ? `<div style="margin-top:6px"><b>${__("Reply")}:</b> <span class="lb-note">${esc(when(reply.SmsDate))}</span>
					<div class="lb-msg lb-mono">${esc(reply.Message)}</div></div>` : ""}
			</div>`;
		};
		show();
		while (!poll.stop) {
			try {
				const r = await api("sent_check", { msisdn: s.Msisdn, after_id: res.before_id, message: text });
				sent = r.sent || sent;
				reply = r.reply || reply;
			} catch (e) {
				/* one failed check is not the end; try again in 30 s */
			}
			if (poll.stop) return;
			show();
			const final = sent && FINAL[sent.SmsStatus];
			const done = final && (!wait || reply || sent.SmsStatus !== 2);
			const elapsed = Date.now() - started;
			if (done) {
				note.className = "lb-note lb-smsnote " + (sent.SmsStatus === 2 ? "lb-ok" : "lb-bad");
				note.textContent = sent.SmsStatus === 2
					? (reply ? __("Delivered to Lebara and the device replied.") : __("Lebara confirms the SMS was sent."))
					: __("Lebara reports: {0}", [final]);
				if (state.history_loaded) load_history();
				return;
			}
			if (elapsed > SENT_POLL_FOR_MS) {
				note.className = "lb-note lb-smsnote";
				note.textContent = sent
					? __("Sent ({0}); no reply from the device within 15 minutes.", [FINAL[sent.SmsStatus] || __("pending")])
					: __("Not in Lebara's SMS list after 15 minutes — check the portal.");
				return;
			}
			note.className = "lb-note lb-smsnote";
			note.textContent = !sent
				? __("Waiting for the SMS to appear in Lebara's list… checking every 30 seconds")
				: !final
				? __("In Lebara's list, status {0} — checking every 30 seconds", [sent.StatusText || __("pending")])
				: __("Sent. Waiting for the device's reply… checking every 30 seconds");
			await sleep(SENT_POLL_MS);
		}
	}

	// ------------------------------------------------------------ history
	function load_history() {
		const s = state.sim;
		if (!s) return;
		const keep = s.Msisdn;
		const hist = $(".lb-hist");
		if (hist) hist.innerHTML = `<div class="lb-empty">${__("Loading messages…")}</div>`;
		api("sim_history", { msisdn: keep }).then((rows) => {
			if (!state.sim || state.sim.Msisdn !== keep) return;
			state.history = rows || [];
			state.history_loaded = true;
			const btn = $(".lb-histreload");
			if (btn) btn.textContent = __("Reload");
			draw_history();
		}, () => {
			if (hist) hist.innerHTML = `<div class="lb-empty">${__("Could not load the messages.")}</div>`;
		});
	}

	function draw_history() {
		const hist = $(".lb-hist");
		if (!hist || !state.history_loaded) return;
		const btn = $(".lb-histreload");
		if (btn) btn.textContent = __("Reload");
		const type = $(".lb-histfilter").value;
		const rows = state.history.filter((r) => !type || String(r.MessageType) === type);
		state.hist_rows = rows;
		$(".lb-histcount").textContent = state.history.length ? `(${rows.length})` : "";
		if (!rows.length) {
			hist.innerHTML = `<div class="lb-empty">${__("No messages.")}</div>`;
			return;
		}
		hist.innerHTML = `<table class="lb-table">
			<thead><tr><th>${__("Date")}</th><th></th><th>${__("Message")}</th><th>${__("Status")}</th></tr></thead>
			<tbody>${rows.map((r, i) => {
				const inbound = r.MessageType === 2;
				return `<tr class="lb-msgrow" data-msg="${i}" title="${esc(__("Click to open the message"))}">
					<td style="white-space:nowrap">${esc(when(r.SmsDate))}</td>
					<td class="lb-dir ${inbound ? "in" : "out"}">${inbound ? "← " + __("from SIM") : "→ " + __("to SIM")}</td>
					<td class="lb-msg lb-mono">${esc(r.Message)}</td>
					<td>${esc(r.StatusText)}${r.SentByUserName ? `<div class="lb-note">${esc(r.SentByUserName)}</div>` : ""}</td>
				</tr>`;
			}).join("")}</tbody></table>`;
	}

	// ------------------------------------------------------------ message dialog
	// A device reply ("DI1:0 DI2:0 ..." / "Data Link: 1 GPRS: 1 ...") as key/value
	// pairs. null when the text does not start with a "Key:".
	function msg_fields(text) {
		const t = String(text || "").trim();
		const re = /\b([A-Za-z][\w.\/-]*(?: [A-Za-z][\w.\/-]*)?):/g;
		const keys = [];
		let m;
		while ((m = re.exec(t))) keys.push({ key: m[1], start: m.index, end: re.lastIndex });
		if (keys.length < 2 || keys[0].start > 0) return null;
		return keys.map((k, i) => ({ key: k.key, value: t.slice(k.end, i + 1 < keys.length ? keys[i + 1].start : t.length).trim() }));
	}

	// A command we sent: "<login> <password> <command> [args]" (login and password
	// can be empty). setparam / getparam args are "id:value;id:value" / "id;id".
	function msg_command(text) {
		const parts = String(text || "").split(" ");
		if (parts.length < 3 || !/^[A-Za-z][\w]*$/.test(parts[2])) return null;
		const args = parts.slice(3).join(" ").trim();
		const params = /^(set|get)param$/i.test(parts[2]) && args
			? args.split(";").filter(Boolean).map((p) => { const x = p.split(":"); return [x[0].trim(), x.slice(1).join(":")]; })
			: null;
		return { command: parts[2], args, params };
	}

	function show_message(r) {
		if (!r) return;
		const inbound = r.MessageType === 2;
		const text = String(r.Message || "");
		const muted = "color:var(--text-muted)";
		const cap = (t) => `<div style="font-size:11px;${muted};text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px">${esc(t)}</div>`;
		const mono = "font-family:var(--font-stack-monospace, ui-monospace, monospace)";
		const tile = (k, v) => `<div style="border:1px solid var(--border-color);border-radius:8px;padding:6px 10px;min-width:0">
			<div style="font-size:11px;${muted};letter-spacing:.02em">${esc(k)}</div>
			<div style="${mono};font-size:14px;font-weight:600;word-break:break-word">${esc(v)}</div></div>`;
		const grid = (inner) => `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-bottom:16px">${inner}</div>`;

		let body = "";
		const fields = inbound ? msg_fields(text) : null;
		const cmd = inbound ? null : msg_command(text);
		if (fields) {
			body = cap(__("Reply")) + grid(fields.map((f) => tile(f.key, f.value)).join(""));
		} else if (cmd) {
			const valued = cmd.params && cmd.params.some((p) => p[1] !== "");
			body = grid(tile(__("Command"), cmd.command) +
				(cmd.params && !valued ? tile(__("Parameters"), cmd.params.map((p) => p[0]).join(", ")) : "") +
				(!cmd.params && cmd.args ? tile(__("Arguments"), cmd.args) : ""));
			if (valued) {
				body += cap(__("Parameters")) +
					`<table class="table table-bordered" style="font-size:12px;margin-bottom:16px"><thead><tr>
					<th style="width:30%">${esc(__("Parameter ID"))}</th><th>${esc(__("Value"))}</th></tr></thead><tbody>` +
					cmd.params.map((p) => `<tr><td style="${mono}">${esc(p[0])}</td><td style="${mono};word-break:break-all">${esc(p[1])}</td></tr>`).join("") +
					"</tbody></table>";
			}
		}

		const html =
			`<div style="display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;margin-bottom:16px;font-size:12px">
				<span class="indicator-pill ${inbound ? "green" : "blue"}">${inbound ? "← " + esc(__("from SIM")) : "→ " + esc(__("to SIM"))}</span>
				<span style="${muted}">${esc(when(r.SmsDate))}</span>
				<span style="${muted}">${esc(__("Status"))}: <b style="color:var(--text-color)">${esc(r.StatusText)}</b></span>
				${r.SentByUserName ? `<span style="${muted}">${esc(__("Sent by"))}: ${esc(r.SentByUserName)}</span>` : ""}
			</div>` +
			body + cap(__("Full message")) +
			`<pre style="white-space:pre-wrap;word-break:break-word;margin:0;padding:10px 12px;border-radius:8px;border:1px solid var(--border-color);background:var(--control-bg, #f4f5f6);color:var(--text-color);${mono};font-size:13px;line-height:1.55;max-height:40vh;overflow:auto">${esc(text)}</pre>`;

		const d = new frappe.ui.Dialog({
			title: (inbound ? __("Message from SIM") : __("Message to SIM")) + (state.sim && state.sim.Msisdn ? " · " + state.sim.Msisdn : ""),
			fields: [{ fieldtype: "HTML", fieldname: "body" }],
			primary_action_label: __("Copy message"),
			primary_action: () => frappe.utils.copy_to_clipboard(text),
		});
		d.fields_dict.body.$wrapper.html(html);
		d.show();
	}

	// ------------------------------------------------------------ events
	function search() {
		state.imeis = [];
		state.skip = 0;
		load_page();
	}

	$(".lb-search").addEventListener("click", search);
	$(".lb-clear").addEventListener("click", clear_filters);
	$(".lb-kpis").addEventListener("click", (e) => {
		const k = e.target.closest(".lb-kpi[data-kpi]");
		if (k) pick_kpi(k.dataset.kpi);
	});
	$(".lb-kpis").addEventListener("keydown", (e) => {
		const k = e.target.closest(".lb-kpi[data-kpi]");
		if (k && (e.key === "Enter" || e.key === " ")) {
			e.preventDefault();
			pick_kpi(k.dataset.kpi);
		}
	});
	$(".lb-q").addEventListener("keydown", (e) => e.key === "Enter" && search());
	$(".lb-status").addEventListener("change", search);
	$(".lb-prev").addEventListener("click", () => {
		state.skip = Math.max(0, state.skip - PAGE);
		load_page();
	});
	$(".lb-next").addEventListener("click", () => {
		state.skip += PAGE;
		load_page();
	});
	$(".lb-login").addEventListener("click", login);
	$(".lb-syncnow").addEventListener("click", sync_now);
	$(".lb-imeis").addEventListener("click", find_imeis);
	$(".lb-export").addEventListener("click", export_csv);
	$(".lb-imeibar").addEventListener("click", (e) => {
		if (!e.target.closest(".lb-clearimeis")) return;
		state.imeis = [];
		state.skip = 0;
		load_page();
	});
	$(".lb-rows").addEventListener("click", (e) => {
		// SIM number / IMEI -> its Serial No (instead of opening the SIM panel).
		const a = e.target.closest("a.lb-sn");
		if (a) {
			if (e.button === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
				e.preventDefault();
				frappe.set_route("Form", "Serial No", a.dataset.sn);
			}
			return;
		}
		const tr = e.target.closest("tr[data-id]");
		if (!tr) return;
		const sim = state.rows.find((r) => String(r.subscriber_id) === tr.dataset.id);
		if (sim) {
			state.history = [];
			state.history_loaded = false;
			open_sim(sim.msisdn);
		}
	});
	$(".lb-detail").addEventListener("click", (e) => {
		const act = e.target.closest(".lb-act");
		if (act && !act.disabled) return change_status(act.dataset.act);
		if (e.target.closest(".lb-send")) return send_sms();
		if (e.target.closest(".lb-reload")) return refresh_current();
		if (e.target.closest(".lb-histreload")) return load_history();
		const mr = e.target.closest("tr.lb-msgrow");
		if (mr && !String(window.getSelection ? window.getSelection() : "")) return show_message((state.hist_rows || [])[Number(mr.dataset.msg)]);
	});
	$(".lb-detail").addEventListener("input", (e) => {
		if (e.target.classList.contains("lb-text")) $(".lb-len").textContent = e.target.value.length + " / 160";
	});
	$(".lb-detail").addEventListener("change", (e) => {
		if (e.target.classList.contains("lb-histfilter")) draw_history();
	});

	// ------------------------------------------------------------ usage this month
	// Estimate of this month's Lebara bill from the stored SIM list:
	//   Active SIM     plan fee (SIM Type -> Lebara Price) + MB over the bundle x overage_per_mb
	//   Suspended SIM  suspended_fee
	//   Idle / Bar / Deactivated   nothing
	// Plan = SIM Type on the SIM's Serial No (none -> default_plan).
	// Waste = Active/Suspended SIMs that are NOT (device "Installed" in the ERP
	// AND on Pilot 1, Pilot 2 or IM), read from the Fleet Audit snapshot. A SIM
	// no device in the audit carries is waste too.
	const PRICING = {
		included_mb: { "Normal": 10, "Data 10G": 10240, "Data 30G": 30720, "Roming": null }, // null = flat fee
		default_plan: "Normal",
		overage_per_mb: 0.15, // SAR -- Lebara KSA public pay-as-you-go data rate
		suspended_fee: 2,     // SAR per suspended SIM per month
		vat: 0.15,
	};

	function money(v) {
		const d = Math.abs(v) >= 1000 ? 0 : 2;
		return "SAR " + Number(v || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
	}
	function size(mb) {
		mb = Number(mb || 0);
		return mb >= 1024 ? (mb / 1024).toLocaleString("en-US", { maximumFractionDigits: 1 }) + " GB"
			: mb.toLocaleString("en-US", { maximumFractionDigits: 1 }) + " MB";
	}
	const count = (n) => Number(n || 0).toLocaleString("en-US");

	async function usage_numbers() {
		const list = (doctype, args) => frappe.xcall("frappe.client.get_list",
			Object.assign({ doctype: doctype, limit_page_length: 0 }, args));
		const now = new Date();
		const days = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
		const elapsed = Math.max(1, (now - new Date(now.getFullYear(), now.getMonth(), 1)) / 86400000);
		const pace = days / elapsed;

		const [types, by_status, special_sn, sims, audit] = await Promise.all([
			list("SIM Type", { fields: ["name", "lebara_price"] }),
			list(DT, { fields: ["status", "count(name) as n", "sum(this_month_mb) as mb"], group_by: "status" }),
			list("Serial No", { fields: ["name", "custom_sim_type"],
				filters: [["custom_sim_type", "is", "set"], ["custom_sim_type", "!=", PRICING.default_plan]] }),
			list(DT, { fields: ["name", "serial_no", "msisdn", "status", "this_month_mb"],
				filters: [["status", "in", ["Active", "Suspend"]]] }),
			// The Fleet Audit snapshot: one row per device, with the SIM the audit tied
			// to it, its ERP status, and whether it was found on Pilot 1, Pilot 2 and IM.
			list("app_apis_fleet_audit", { fields: ["sim_msisdn", "erp_status", "on_pilot_1", "on_pilot_2", "on_im"],
				filters: [["sim_msisdn", "is", "set"]] }),
		]);
		const price = {};
		types.forEach((t) => { price[t.name] = flt(t.lebara_price); });
		const plan_of = {};
		special_sn.forEach((s) => { plan_of[s.name] = s.custom_sim_type; });
		// Digits only, without the 966 / 0 prefix, so both spellings of a number meet.
		const msisdn_key = (v) => String(v || "").replace(/\D/g, "").replace(/^(00)?966/, "").replace(/^0/, "");
		// A SIM is in use ONLY when its device is "Installed" in the ERP AND is on at
		// least one of the three systems (Pilot 1, Pilot 2, IM). Everything else is
		// waste: any other ERP status (Deleted, Canceled Installation, Processing,
		// Not in ERP, ...), on none of the three systems, or no device in the audit
		// carries the SIM at all. Same rule as the Fleet Audit block.
		const in_use = new Set();
		audit.forEach((d) => {
			const k = msisdn_key(d.sim_msisdn);
			if (k && d.erp_status === "Installed" && (Number(d.on_pilot_1) || Number(d.on_pilot_2) || Number(d.on_im))) in_use.add(k);
		});

		let total_mb = 0, active_mb = 0;
		by_status.forEach((r) => { total_mb += flt(r.mb); if (r.status === "Active") active_mb = flt(r.mb); });

		const u = {
			days, day: Math.ceil(elapsed), total_mb, active_mb, active: 0, suspended: 0, plans: {}, fees: 0, unpriced: 0,
			over_n: 0, over_mb: 0, proj_n: 0, proj_mb: 0, waste_active: 0, waste_suspended: 0, waste: 0,
			over_set: [], proj_set: [], waste_set: [],
		};
		sims.forEach((r) => {
			const plan = plan_of[r.serial_no] || PRICING.default_plan;
			const used = in_use.has(msisdn_key(r.msisdn));
			if (r.status === "Suspend") {
				u.suspended++;
				if (!used) { u.waste_suspended++; u.waste += PRICING.suspended_fee; u.waste_set.push(r.name); }
				return;
			}
			u.active++;
			u.plans[plan] = (u.plans[plan] || 0) + 1;
			let cost = 0;
			if (price[plan] === undefined) u.unpriced++;
			else cost = price[plan];
			const incl = PRICING.included_mb[plan];
			const mb = flt(r.this_month_mb);
			if (incl !== null && incl !== undefined) {
				if (mb > incl) { u.over_n++; u.over_mb += mb - incl; cost += (mb - incl) * PRICING.overage_per_mb; u.over_set.push(r.name); }
				if (mb * pace > incl) { u.proj_n++; u.proj_mb += mb * pace - incl; u.proj_set.push(r.name); }
			}
			u.fees += price[plan] || 0;
			if (!used) { u.waste_active++; u.waste += cost; u.waste_set.push(r.name); }
		});
		u.suspended_fees = u.suspended * PRICING.suspended_fee;
		u.over_cost = u.over_mb * PRICING.overage_per_mb;
		u.total = u.fees + u.suspended_fees + u.over_cost;
		u.projected = u.fees + u.suspended_fees + u.proj_mb * PRICING.overage_per_mb;
		return u;
	}

	function kpi(label, value, sub, cls) {
		// A widget with a list filter behind it (state.kpi_defs) is clickable.
		const key = Object.keys(state.kpi_defs).find((k) => state.kpi_defs[k].label === label);
		const d = key ? state.kpi_defs[key] : null;
		return '<div class="lb-kpi ' + (cls || "") + (d ? " is-click" + (state.kpi === key ? " is-on" : "") : "") + '"' +
			(d ? ' data-kpi="' + key + '" role="button" tabindex="0" title="' + esc(__("Click to list {0}. Click again to clear.", [d.hint])) + '"' : "") +
			'><div class="lb-kpi-label">' + esc(label) +
			'</div><div class="lb-kpi-value">' + esc(value) + '</div><div class="lb-kpi-sub">' + sub + "</div></div>";
	}

	function load_usage() {
		const box = $(".lb-kpis");
		if (!box) return Promise.resolve();
		return usage_numbers().then((u) => {
			const vat = 1 + PRICING.vat;
			// What each widget lists when clicked. kpi() finds these by label.
			const billing = [["status", "in", ["Active", "Suspend"]]];
			state.kpi_defs = {
				data: { label: __("Data used this month"), hint: __("SIMs that used data this month"), filters: [["this_month_mb", ">", 0]] },
				fees: { label: __("Plan fees"), hint: __("active and suspended SIMs (the ones Lebara bills)"), filters: billing },
				over: { label: __("Over bundle"), hint: __("SIMs already over their plan's bundle this month"), names: u.over_set },
				waste: { label: __("Waste · not in use"), hint: __("active / suspended SIMs whose device is not Installed in the ERP, is on none of Pilot 1, Pilot 2 and IM, or that are in no device"), names: u.waste_set },
				total: { label: __("Bill so far"), hint: __("active and suspended SIMs (the ones Lebara bills)"), filters: billing },
				month: { label: __("Month end"), hint: __("SIMs on pace to go over their bundle by month end"), names: u.proj_set },
			};
			if (state.kpi && !state.kpi_defs[state.kpi]) state.kpi = null;
			box.innerHTML =
				kpi(__("Data used this month"), size(u.total_mb),
					esc(__("Day {0} of {1}", [u.day, u.days]))) +
				kpi(__("Plan fees"), money(u.fees + u.suspended_fees),
					count(u.active) + " " + esc(__("active")) + " · " + count(u.suspended) + " " + esc(__("suspended"))) +
				kpi(__("Over bundle"), money(u.over_cost),
					count(u.over_n) + " " + esc(__("SIMs")) + " · " + esc(size(u.over_mb)), u.over_n ? "is-warn" : "") +
				kpi(__("Waste · not in use"), money(u.waste),
					count(u.waste_active) + " " + esc(__("active")) + " · " + count(u.waste_suspended) + " " + esc(__("suspended")),
					u.waste ? "is-waste" : "") +
				kpi(__("Bill so far"), money(u.total),
					esc(money(u.total * vat)) + " " + esc(__("incl. VAT")), "is-total") +
				kpi(__("Month end"), money(u.projected),
					esc(money(u.projected * vat)) + " " + esc(__("incl. VAT")));
		}).catch((e) => {
			box.innerHTML = '<div class="lb-kpi-note">' + esc(__("Could not work out this month's usage: {0}", [String(e && e.message || e)])) + "</div>";
		});
	}

	// ------------------------------------------------------------ bulk SMS (Excel)
	// The black "Bulk SMS" button reads an Excel / CSV file IN THE BROWSER and
	// sends every message with the same `send_sms` action the SIM panel uses
	// (Lebara has no bulk send), one after another. Nothing is written to
	// the ERP: the rows live in the browser only while the file is previewed and
	// sent; the log is downloaded as an Excel file when the run ends and then
	// everything is cleared from memory (closing the preview clears it too).
	//   File (first sheet):
	//     SIM column       the SIM's MSISDN, or the IMEI of the device it is in
	//                      (the IMEI Lebara reports, or the ERP device IMEI)
	//     message columns  Message 1, Message 2, ... -- one SMS per filled cell,
	//                      so a row sends one message or several.
	//   Header row: the SIM column is the one titled IMEI / MSISDN / Number /
	//   رقم ..., the message columns those titled Message / SMS / رسالة ... (if
	//   none is, every column after the SIM column). No header row: A = SIM,
	//   B, C, ... = messages.
	//   Sending goes column by column (every Message 1, then every Message 2, ...)
	//   and two SMS to the same SIM are at least BULK_SAME_SIM_MS apart.
	const BULK_MAX = 5000;            // SMS per file
	const BULK_GAP_MS = 400;          // pause between two sends
	const BULK_SAME_SIM_MS = 5000;    // pause between two sends to the same SIM
	const BULK_STOP_AFTER = 5;        // this many failures in a row -> stop (session expired, ...)
	const BULK_IMEI_RE = /imei|الجهاز/i;
	const BULK_NUM_RE = /msisdn|mobile|phone|number|^num|^no\.?$|^sim|رقم|جوال|هاتف|الشريحة/i;
	const BULK_MSG_RE = /message|^msg|sms|text|body|command|رسال|نص|أمر|امر/i;
	// Shared by every render of this block, so a running send survives the
	// workspace being redrawn (the dialog lives in <body>, not in this block).
	const bulk = window.__lb_bulk || (window.__lb_bulk = { rows: [], running: false, checking: false, stop: false, dialog: null, file: "", started: "", finished: "" });

	function bulk_xlsx() {
		if (window.XLSX) return Promise.resolve(window.XLSX);
		if (!window.__lb_xlsx_p) {
			const urls = ["https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js",
				"https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"];
			const load = (url) => new Promise((resolve, reject) => {
				const sc = document.createElement("script");
				sc.src = url;
				sc.onload = () => (window.XLSX ? resolve(window.XLSX) : reject(new Error("XLSX missing")));
				sc.onerror = () => { sc.remove(); reject(new Error("could not load " + url)); };
				document.head.appendChild(sc);
			});
			window.__lb_xlsx_p = load(urls[0]).catch(() => load(urls[1]));
			window.__lb_xlsx_p.catch(() => { window.__lb_xlsx_p = null; });
		}
		return window.__lb_xlsx_p;
	}

	// A call that never pops a message box (one bad row must not stop a run of
	// hundreds with dialogs); the error text goes into the log instead.
	function bulk_call(action, args) {
		return new Promise((resolve, reject) => {
			frappe.call({
				method: "lebara",
				args: Object.assign({ action }, args || {}),
				silent: true,
				callback: (r) => resolve(((r && r.message) || {}).result),
				error: (r) => reject(new Error(bulk_error(r))),
			});
		});
	}

	function bulk_error(r) {
		try {
			if (r && r._server_messages) {
				return JSON.parse(r._server_messages)
					.map((m) => { try { return JSON.parse(m).message; } catch (e) { return m; } })
					.map((m) => String(m || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim())
					.filter(Boolean).join(" | ") || "Error";
			}
			if (r && r.exception) return String(r.exception).split("\n").pop().slice(0, 300);
			if (r && r.responseJSON) return bulk_error(r.responseJSON);
			if (r && r.status !== undefined) return "HTTP " + r.status + " " + (r.statusText || "");
		} catch (e) { /* fall through */ }
		return String((r && r.message) || r || "Error").slice(0, 300);
	}

	const bulk_cell = (v) => {
		if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(0);
		return String(v === undefined || v === null ? "" : v).trim();
	};
	const bulk_text = (v) => String(v === undefined || v === null ? "" : v).replace(/\r\n?/g, "\n").trim();
	const bulk_digits = (v) => bulk_cell(v).replace(/\D/g, "");
	// Same spelling as the stored list: digits only, no 00966 / 966 / 0 prefix.
	const bulk_key = (v) => {
		let d = bulk_digits(v);
		if (d.length > 11) d = d.replace(/^(00)?966/, "");
		return d.replace(/^0+/, "");
	};
	const bulk_is_msisdn = (k) => k.length >= 9 && k.length <= 13;
	const bulk_is_imei = (d) => d.length >= 14 && d.length <= 17;
	const bulk_now = () => frappe.datetime.now_datetime();
	// Forget the file and the log (after the download, or when the preview is closed).
	function bulk_clear() {
		if (bulk.running) return;
		Object.assign(bulk, { rows: [], lines: 0, lay: null, file: "", started: "", finished: "", stop: false });
	}
	const bulk_letter = (i) => (i >= 26 ? String.fromCharCode(64 + Math.floor(i / 26)) : "") + String.fromCharCode(65 + (i % 26));

	function bulk_pick() {
		if (bulk.running) {
			if (bulk.dialog) bulk.dialog.show();
			return;
		}
		const input = $(".lb-bulkfile");
		input.value = "";
		input.click();
	}

	// Which column is the SIM and which are messages.
	function bulk_layout(aoa) {
		const width = aoa.reduce((w, r) => Math.max(w, (r || []).length), 0);
		const head = Array.from({ length: width }, (_, i) => bulk_text((aoa[0] || [])[i]));
		let id = head.findIndex((h) => h && !BULK_MSG_RE.test(h) && (BULK_IMEI_RE.test(h) || BULK_NUM_RE.test(h)));
		let start = 1;
		let type = "auto";
		if (id >= 0) {
			const imei = BULK_IMEI_RE.test(head[id]);
			const num = BULK_NUM_RE.test(head[id]);
			type = imei && !num ? "imei" : num && !imei ? "msisdn" : "auto";
		} else {
			id = 0;
			// first row is data (a number in A) -> no header row
			if (bulk_digits(head[0]).length >= 9) start = 0;
		}
		let msgs = start ? head.map((h, i) => i).filter((i) => i !== id && BULK_MSG_RE.test(head[i])) : [];
		if (!msgs.length) msgs = head.map((h, i) => i).filter((i) => i > id);
		const label = (i, n) => (start && head[i] ? head[i] : __("Message {0}", [n + 1])) + " (" + bulk_letter(i) + ")";
		return { id, type, start, msgs: msgs.map((i, n) => ({ i, label: label(i, n), no: n + 1 })), id_label: (start && head[id] ? head[id] : __("SIM")) + " (" + bulk_letter(id) + ")" };
	}

	// SIMs for the file's MSISDNs and IMEIs, from the stored list.
	async function bulk_lookup(lines) {
		const fields = ["msisdn", "subscriber_id", "status", "imei", "erp_imei"];
		const get = async (field, values) => {
			const out = [];
			const list = Array.from(new Set(values));
			for (let i = 0; i < list.length; i += 400) {
				const part = list.slice(i, i + 400);
				out.push(...(await frappe.xcall("frappe.client.get_list", {
					doctype: DT, fields, filters: [[field, "in", part]], limit_page_length: part.length * 3 + 20,
				})));
			}
			return out;
		};
		const by_msisdn = {};
		const by_imei = {};
		(await get("msisdn", lines.filter((l) => l.type !== "imei" && bulk_is_msisdn(l.key)).map((l) => l.key)))
			.forEach((s) => (by_msisdn[bulk_key(s.msisdn)] = s));
		const imeis = lines.filter((l) => l.type !== "msisdn" && bulk_is_imei(l.digits)).map((l) => l.digits);
		if (imeis.length) {
			const add = (s, how, field) => (by_imei[s[field]] = by_imei[s[field]] || []).push({ s, how });
			(await get("imei", imeis)).forEach((s) => add(s, "IMEI (Lebara)", "imei"));
			(await get("erp_imei", imeis)).forEach((s) => add(s, "IMEI (ERP)", "erp_imei"));
		}
		return { by_msisdn, by_imei };
	}

	// One SIM for a line: by MSISDN, else by IMEI (Lebara's IMEI first, then an
	// Active SIM), else (an MSISDN-looking value) looked up on Lebara while sending.
	function bulk_resolve(l, found) {
		const sim = (s) => ({ msisdn: s.msisdn, subscriber_id: s.subscriber_id || "", sim_status: s.status || "", imei: s.imei || s.erp_imei || "" });
		if (l.type !== "imei" && found.by_msisdn[l.key]) return Object.assign(sim(found.by_msisdn[l.key]), { found_by: "MSISDN" });
		if (l.type !== "msisdn" && found.by_imei[l.digits]) {
			const seen = {};
			const cands = found.by_imei[l.digits].filter((c) => !seen[c.s.msisdn] && (seen[c.s.msisdn] = 1)).sort((a, b) =>
				(a.how === "IMEI (Lebara)" ? 0 : 1) - (b.how === "IMEI (Lebara)" ? 0 : 1)
				|| (a.s.status === "Active" ? 0 : 1) - (b.s.status === "Active" ? 0 : 1));
			const c = cands[0];
			return Object.assign(sim(c.s), { found_by: c.how, imei: l.digits,
				note: cands.length > 1 ? __("{0} SIMs have this IMEI ({1}); used {2}", [cands.length, cands.map((x) => x.s.msisdn).join(", "), c.s.msisdn]) : "" });
		}
		if (l.type !== "imei" && bulk_is_msisdn(l.key)) return { msisdn: l.key, subscriber_id: "", sim_status: "", imei: "", found_by: "MSISDN", live: true };
		if (l.type !== "msisdn" && bulk_is_imei(l.digits)) return { error: __("No SIM found for this IMEI") };
		return { error: __("Not a valid MSISDN or IMEI") };
	}

	async function bulk_file(file) {
		if (!file) return;
		let XLSX;
		try {
			XLSX = await bulk_xlsx();
		} catch (e) {
			frappe.msgprint(__("Could not load the Excel reader. Check the internet connection and try again."));
			return;
		}
		let aoa;
		try {
			const csv = /\.(csv|txt)$/i.test(file.name);
			const wb = csv ? XLSX.read(await file.text(), { type: "string", raw: true })
				: XLSX.read(await file.arrayBuffer(), { type: "array" });
			const ws = wb.Sheets[wb.SheetNames[0]];
			aoa = XLSX.utils.sheet_to_json(ws, { header: 1, raw: true, defval: "", blankrows: false });
		} catch (e) {
			frappe.msgprint(__("Could not read {0}: {1}", [esc(file.name), esc(String((e && e.message) || e))]));
			return;
		}

		const lay = bulk_layout(aoa);
		if (!lay.msgs.length) {
			frappe.msgprint(__("{0} has no message column. Put the MSISDN or IMEI in column A and the messages in columns B, C, ...", [esc(file.name)]));
			return;
		}
		const lines = [];
		for (let i = lay.start; i < aoa.length; i++) {
			const r = aoa[i] || [];
			const input = bulk_cell(r[lay.id]);
			const msgs = lay.msgs.map((m) => ({ m, text: bulk_text(r[m.i]) }));
			if (!input && !msgs.some((x) => x.text)) continue;
			lines.push({ line: i + 1, input, digits: bulk_digits(input), key: bulk_key(input), type: lay.type, msgs });
		}
		if (!lines.length) {
			frappe.msgprint(__("No rows found in {0}.", [esc(file.name)]));
			return;
		}

		let found;
		try {
			found = await bulk_lookup(lines);
		} catch (e) {
			frappe.msgprint(__("Could not read the SIM list: {0}", [esc(bulk_error(e))]));
			return;
		}

		// One item per filled message cell (a row with no message at all is
		// logged once, as skipped).
		const rows = [];
		const seen = {};
		lines.forEach((l) => {
			const sim = bulk_resolve(l, found);
			const filled = l.msgs.filter((x) => x.text);
			(filled.length ? filled : [{ m: { label: "", no: 0 }, text: "" }]).forEach((x) => {
				const row = { line: l.line, input: l.input, found_by: sim.found_by || "", msisdn: sim.msisdn || "", imei: sim.imei || "",
					subscriber_id: sim.subscriber_id || "", sim_status: sim.sim_status || "", live: !!sim.live,
					col: x.m.label, col_no: x.m.no, message: x.text, result: "", error: "", note: sim.note || "", sent_at: "",
					before_id: 0, lebara: "", lebara_code: null, sms_id: "", sms_time: "", reply: "", reply_time: "" };
				const dup = row.msisdn + "\u0000" + row.message;
				if (sim.error) [row.result, row.error] = ["Skipped", sim.error];
				else if (!row.message) [row.result, row.error] = ["Skipped", __("No message")];
				else if (row.message.length > 160) [row.result, row.error] = ["Skipped", __("Message is {0} characters (max 160)", [row.message.length])];
				else if (seen[dup]) row.note = [row.note, __("Same SIM and message as row {0}; sent again", [seen[dup]])].filter(Boolean).join(" | ");
				else seen[dup] = l.line;
				rows.push(row);
			});
		});
		if (rows.length > BULK_MAX) {
			frappe.msgprint(__("{0} has {1} SMS; up to {2} can be sent from one file. Split it and upload the parts one by one.", [esc(file.name), rows.length, BULK_MAX]));
			return;
		}

		Object.assign(bulk, { rows, lines: lines.length, file: file.name, stop: false, started: "", finished: "", lay });
		bulk_preview();
	}

	function bulk_new_dialog() {
		if (bulk.dialog) {
			bulk.dialog.onhide = null;
			bulk.dialog.hide();
			bulk.dialog.$wrapper.remove();
		}
		bulk.dialog = new frappe.ui.Dialog({
			title: __("Bulk SMS") + " · " + bulk.file,
			size: "extra-large",
			fields: [{ fieldname: "body", fieldtype: "HTML" }],
		});
		bulk.dialog.onhide = () => bulk_clear();
		return bulk.dialog;
	}

	function bulk_counts() {
		const c = { total: bulk.rows.length, lines: bulk.lines || 0, ready: 0, skipped: 0, sent: 0, failed: 0, notsent: 0, left: 0, unknown: 0,
			by_imei: 0, delivered: 0, undelivered: 0, pending: 0, replies: 0 };
		bulk.rows.forEach((r) => {
			if (r.result === "Skipped") c.skipped++;
			else if (r.result === "Sent") c.sent++;
			else if (r.result === "Failed") c.failed++;
			else if (r.result === "Not sent") c.notsent++;
			else { c.ready++; c.left++; if (r.live) c.unknown++; if (/IMEI/.test(r.found_by)) c.by_imei++; }
			if (r.result === "Sent") {
				if (r.lebara === "Success") c.delivered++;
				else if (r.lebara && r.lebara !== "Pending") c.undelivered++;
				else c.pending++;
			}
			if (r.reply) c.replies++;
		});
		return c;
	}

	const BULK_RESULT_COLOR = { Sent: "green", Failed: "red", Skipped: "orange", "Not sent": "gray" };

	function bulk_table(rows, limit) {
		const shown = rows.slice(0, limit);
		const more = rows.length - shown.length;
		const mono = "font-family:var(--font-family-monospace, monospace)";
		return `<div style="max-height:45vh;overflow:auto;border:1px solid var(--border-color);border-radius:8px">
			<table class="table table-sm" style="margin:0;font-size:12px">
				<thead><tr style="position:sticky;top:0;background:var(--card-bg, #fff)">
					<th>${__("Row")}</th><th>${__("In file")}</th><th>${__("SIM")}</th><th>${__("Message")}</th>
					<th>${__("Result")}</th></tr></thead>
				<tbody>${shown.map((r) => `<tr>
					<td>${r.line}</td>
					<td style="${mono}">${esc(r.input)}${r.found_by && r.found_by !== "MSISDN" ? `<div class="text-muted" style="font-size:11px">${esc(r.found_by)}</div>` : ""}</td>
					<td><span style="${mono}">${r.msisdn ? esc(r.msisdn) : ""}</span>
						<div class="text-muted" style="font-size:11px">${r.subscriber_id ? esc(r.sim_status) : r.live && !r.result ? __("not in list, checked on Lebara") : ""}</div></td>
					<td style="max-width:320px;white-space:pre-wrap;word-break:break-word" dir="auto">${r.col ? `<div class="text-muted" style="font-size:11px">${esc(r.col)}</div>` : ""}${r.message ? esc(r.message) : ""}</td>
					<td>${r.result ? `<span class="indicator-pill ${BULK_RESULT_COLOR[r.result] || "gray"}">${esc(__(r.result))}</span>` : `<span class="text-muted">${__("waiting")}</span>`}
						${r.error ? `<div class="text-muted" style="font-size:11px">${esc(r.error)}</div>` : ""}
						${r.note ? `<div class="text-muted" style="font-size:11px">${esc(r.note)}</div>` : ""}</td>
				</tr>`).join("")}</tbody>
			</table>
			${more > 0 ? `<div class="text-muted" style="padding:6px 8px">${__("… and {0} more (all of them are in the log).", [more])}</div>` : ""}
		</div>`;
	}

	function bulk_summary(c) {
		const chip = (label, n, color) => `<span class="indicator-pill ${color}" style="margin:0 6px 6px 0">${label}: ${n}</span>`;
		return `<div style="display:flex;flex-wrap:wrap;margin-bottom:8px">
			${chip(__("Rows"), c.lines, "gray")}
			${chip(__("SMS"), c.total, "gray")}
			${c.left ? chip(__("To send"), c.left, "blue") : ""}
			${c.sent ? chip(__("Sent"), c.sent, "green") : ""}
			${c.failed ? chip(__("Failed"), c.failed, "red") : ""}
			${c.skipped ? chip(__("Skipped"), c.skipped, "orange") : ""}
			${c.notsent ? chip(__("Not sent"), c.notsent, "gray") : ""}
			${c.delivered ? chip(__("Delivered"), c.delivered, "green") : ""}
			${c.undelivered ? chip(__("Not delivered"), c.undelivered, "red") : ""}
			${c.replies ? chip(__("Replies"), c.replies, "purple") : ""}
		</div>`;
	}

	function bulk_preview() {
		const d = bulk_new_dialog();
		const c = bulk_counts();
		const lay = bulk.lay;
		const b = (t) => "<b><bdi>" + esc(t) + "</bdi></b>";
		d.fields_dict.body.$wrapper.html(`
			<p class="text-muted" style="margin-bottom:8px">
				${__("SIM (MSISDN or IMEI) from {0}; messages from {1}.", [b(lay.id_label), lay.msgs.map((m) => b(m.label)).join(", ")])}
				${lay.start ? "" : __("(No header row.)")}
				${lay.msgs.length > 1 ? "<br>" + __("Each filled message cell is one SMS. They go column by column (all of the first message column, then the next), at least {0} seconds apart for the same SIM.", [BULK_SAME_SIM_MS / 1000]) : ""}
				${c.unknown ? "<br>" + __("{0} SMS are for numbers not in the stored SIM list; those are looked up on Lebara while sending.", [c.unknown]) : ""}</p>
			${bulk_summary(c)}
			${bulk_table(bulk.rows, 300)}
			<p class="text-muted" style="margin:8px 0 0">${__("The SMS go out one by one (about 1–2 seconds each), so keep this browser tab open until it finishes. Nothing is saved in the system: the log is downloaded as an Excel file at the end and then cleared from the browser.")}</p>`);
		if (c.ready) {
			d.set_primary_action(__("Send {0} SMS", [c.ready]), () => bulk_send());
		} else {
			d.set_primary_action(__("Download log"), () => bulk_download().then(() => d.hide()));
		}
		d.set_secondary_action_label(__("Cancel"));
		d.set_secondary_action(() => d.hide());
		d.show();
	}

	function bulk_progress(note) {
		const d = bulk.dialog;
		if (!d) return;
		const c = bulk_counts();
		const todo = bulk.rows.filter((r) => r.result !== "Skipped").length || 1;
		const done = c.sent + c.failed + c.notsent;
		const pct = Math.round((100 * done) / todo);
		const recent = bulk.rows.filter((r) => r.result && r.result !== "Skipped").sort((a, b) => (b.sent_seq || 0) - (a.sent_seq || 0));
		d.fields_dict.body.$wrapper.html(`
			<div class="progress" style="height:8px;margin-bottom:10px"><div class="progress-bar" style="width:${pct}%"></div></div>
			${bulk_summary(c)}
			<p class="text-muted" style="margin-bottom:8px">${note || ""}</p>
			${bulk_table(bulk.running ? recent : bulk.rows, bulk.running ? 50 : 300)}`);
	}

	async function bulk_send() {
		if (bulk.running) return;
		const d = bulk.dialog;
		let s;
		try {
			s = await bulk_call("status");
		} catch (e) {
			s = null;
		}
		if (!s || s.status !== "Logged In") {
			frappe.msgprint(__("Lebara is not logged in ({0}). Log in first (Log in (OTP) at the top of the block), then upload the file again.", [esc((s && s.status) || __("unknown"))]));
			return;
		}

		bulk.running = true;
		bulk.stop = false;
		bulk.started = bulk_now();
		const leave = (e) => { e.preventDefault(); e.returnValue = ""; };
		window.addEventListener("beforeunload", leave);
		d.set_primary_action(__("Stop"), () => {
			bulk.stop = true;
			d.disable_primary_action();
		});
		d.set_secondary_action_label(__("Hide"));
		d.set_secondary_action(() => d.hide());

		let fails = 0;
		let seq = 0;
		const live = {};   // MSISDN not in the stored list -> what Lebara found (null = none)
		const last = {};   // MSISDN -> when the last SMS went to it
		// Column by column: every first message, then every second one, ...
		const queue = bulk.rows.filter((r) => !r.result).sort((a, b) => a.col_no - b.col_no || a.line - b.line);
		for (let i = 0; i < queue.length; i++) {
			const r = queue[i];
			if (bulk.stop) break;
			bulk_progress(__("Sending {0} of {1}: {2} to {3}…", [i + 1, queue.length, esc(r.col), esc(r.msisdn)]));
			try {
				if (!r.subscriber_id) {
					if (!(r.msisdn in live)) live[r.msisdn] = (await bulk_call("find_sim", { msisdn: r.msisdn })) || null;
					const sim = live[r.msisdn];
					if (!sim || !sim.Id) {
						[r.result, r.error] = ["Skipped", __("Not a Lebara SIM")];
						continue;
					}
					r.subscriber_id = sim.Id;
					r.sim_status = sim.StatusText || "";
					r.imei = r.imei || sim.IMEI || "";
				}
				const wait = (last[r.msisdn] || 0) + BULK_SAME_SIM_MS - Date.now();
				if (wait > 0) {
					bulk_progress(__("Waiting {0} s before the next SMS to {1}…", [Math.ceil(wait / 1000), esc(r.msisdn)]));
					await sleep(wait);
					if (bulk.stop) break;
				}
				const res = await bulk_call("send_sms", { id: r.subscriber_id, msisdn: r.msisdn, message: r.message });
				last[r.msisdn] = Date.now();
				r.result = "Sent";
				r.before_id = (res && res.before_id) || 0;
				r.sent_at = bulk_now();
				r.sent_seq = ++seq;
				fails = 0;
			} catch (e) {
				r.result = "Failed";
				r.error = String((e && e.message) || e);
				r.sent_at = bulk_now();
				r.sent_seq = ++seq;
				fails++;
				if (fails >= BULK_STOP_AFTER) {
					bulk.stop_reason = __("Stopped after {0} failures in a row: {1}", [fails, r.error]);
					break;
				}
			}
			await sleep(BULK_GAP_MS);
		}
		const why = bulk.stop_reason || (bulk.stop ? __("Stopped by user") : "");
		bulk.rows.forEach((r) => { if (!r.result) [r.result, r.error] = ["Not sent", why || __("Not sent")]; });
		bulk.stop_reason = "";
		bulk.running = false;
		bulk.finished = bulk_now();
		window.removeEventListener("beforeunload", leave);

		// Download the log once, then forget everything.
		const counts = bulk_counts();
		await bulk_download();
		bulk_clear();
		bulk_done(why, counts);
	}

	function bulk_done(note, c) {
		const d = bulk.dialog;
		if (!d) return;
		d.fields_dict.body.$wrapper.html(`
			<div class="progress" style="height:8px;margin-bottom:10px"><div class="progress-bar" style="width:100%"></div></div>
			${bulk_summary(c)}
			<p class="text-muted" style="margin:0">${(note ? esc(note) + " · " : "") + __("Finished: {0} sent, {1} failed, {2} skipped. The log was downloaded as an Excel file and cleared from the browser.", [c.sent, c.failed, c.skipped + c.notsent])}</p>`);
		d.enable_primary_action();
		d.set_primary_action(__("Close"), () => d.hide());
		d.get_secondary_btn().addClass("hide");
		d.show();
	}

	async function bulk_download() {
		const c = bulk_counts();
		const head = ["#", "File Row", "SIM (file)", "Found By", "MSISDN", "IMEI", "Message Column", "Message", "Characters",
			"Subscriber Id", "SIM Status", "Result", "Error / Note", "Sent At"];
		const data = bulk.rows.map((r, i) => [i + 1, r.line, r.input, r.found_by || "", r.msisdn || "", r.imei || "", r.col || "",
			r.message, r.message.length, r.subscriber_id || "", r.sim_status || "", r.result || "Not sent",
			[r.error, r.note].filter(Boolean).join(" | "), r.sent_at || ""]);
		const lay = bulk.lay || { msgs: [] };
		const info = [
			["File", bulk.file], ["SIM column", lay.id_label || ""], ["Message columns", lay.msgs.map((m) => m.label).join(", ")],
			["Started", bulk.started || ""], ["Finished", bulk.finished || ""], ["Downloaded", bulk_now()], ["By", frappe.session.user],
			["Rows in file", c.lines], ["SMS in file", c.total], ["Sent", c.sent], ["Failed", c.failed], ["Skipped", c.skipped],
			["Not sent", c.notsent + c.left],
		];
		const name = "lebara_bulk_sms_log_" + bulk_now().replace(/[^0-9]/g, "").slice(0, 14);
		try {
			const XLSX = await bulk_xlsx();
			const ws = XLSX.utils.aoa_to_sheet([head].concat(data));
			ws["!cols"] = [5, 8, 17, 13, 14, 17, 16, 45, 10, 12, 11, 9, 40, 19].map((w) => ({ wch: w }));
			ws["!autofilter"] = { ref: "A1:N" + (data.length + 1) };
			const sum = XLSX.utils.aoa_to_sheet(info);
			sum["!cols"] = [{ wch: 20 }, { wch: 50 }];
			const wb = XLSX.utils.book_new();
			XLSX.utils.book_append_sheet(wb, ws, "SMS Log");
			XLSX.utils.book_append_sheet(wb, sum, "Summary");
			XLSX.writeFile(wb, name + ".xlsx");
		} catch (e) {
			// No Excel writer (offline?) -> the same log as CSV, which Excel opens too.
			const csv = "\ufeff" + [head].concat(data).map((l) => l.map(csv_cell).join(",")).join("\r\n");
			const a = document.createElement("a");
			const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
			a.href = url;
			a.download = name + ".csv";
			document.body.appendChild(a);
			a.click();
			a.remove();
			setTimeout(() => URL.revokeObjectURL(url), 10000);
		}
		frappe.show_alert({ message: __("SMS log downloaded ({0} rows).", [data.length]), indicator: "green" });
	}

	$(".lb-bulk").addEventListener("click", bulk_pick);
	$(".lb-bulkfile").addEventListener("change", (e) => bulk_file(e.target.files && e.target.files[0]));

	// The list is the stored copy, so it shows even while Lebara is logged out.
	refresh_session().catch(() => {});
	load_page();
	load_usage();
})();
