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
	const STATUS_COLOR = { Active: "green", Suspend: "orange", Idle: "gray", Bar: "red", Deactivated: "darkgrey" };

	const state = { skip: 0, total: 0, rows: [], sim: null, history: [], history_loaded: false, polls: [], imeis: [] };
	const COLS = 7;
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
				? __("SIM list synced {0} · {1} SIMs · refreshes every hour", [s.sims_synced_at.slice(0, 16), (s.sims_count || 0).toLocaleString()])
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

	const LIST_FIELDS = ["subscriber_id", "msisdn", "status", "imei", "erp_imei", "erp_vehicle", "erp_plate",
		"erp_customer", "group_name", "last_connection", "this_month_mb"];

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
		}, () => {
			tbody.innerHTML = `<tr><td colspan="${COLS}" class="lb-empty">${__("Could not load the SIM list.")}</td></tr>`;
		});
	}

	function draw_list() {
		const tbody = $(".lb-rows");
		if (!state.rows.length) {
			tbody.innerHTML = `<tr><td colspan="${COLS}" class="lb-empty">${__("No SIMs match.")}</td></tr>`;
		} else {
			tbody.innerHTML = state.rows.map((s) => `
				<tr data-id="${esc(s.subscriber_id)}" class="${state.sim && state.sim.Id === s.subscriber_id ? "lb-sel" : ""}">
					<td class="lb-mono">${esc(s.msisdn)}</td>
					<td>${pill(s.status, STATUS_COLOR[s.status])}</td>
					<td class="lb-mono" title="${__("Lebara")}: ${esc(s.imei)}">${esc(s.erp_imei || s.imei)}</td>
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

	function export_csv() {
		const f = list_filters();
		frappe.show_alert({ message: __("Preparing the export…"), indicator: "blue" });
		frappe.xcall("frappe.client.get_list", {
			doctype: DT,
			fields: ["msisdn", "iccid", "imsi", "status", "imei", "erp_imei", "erp_vehicle", "erp_plate", "erp_customer",
				"match_by", "group_name", "sub_customer", "activation_date", "last_connection", "this_month_mb",
				"last_month_mb", "subscriber_id"],
			filters: f.filters,
			or_filters: f.or_filters,
			order_by: "msisdn asc",
			limit_page_length: 100000,
		}).then((rows) => {
			const head = ["Searched IMEI", "MSISDN", "ICCID", "IMSI", "Status", "IMEI (Lebara)", "IMEI (ERP)", "Customer Vehicle",
				"Plate", "Customer", "Matched By", "Group", "Sub-customer", "Activation Date", "Last Connection",
				"This Month MB", "Last Month MB", "Subscriber Id"];
			const lines = [head];
			const found = new Set();
			rows.forEach((r) => {
				const searched = state.imeis.find((i) => i === r.erp_imei || i === r.imei) || "";
				if (searched) found.add(searched);
				lines.push([searched, r.msisdn, r.iccid, r.imsi, r.status, r.imei, r.erp_imei, r.erp_vehicle, r.erp_plate,
					r.erp_customer, r.match_by, r.group_name, r.sub_customer, r.activation_date, r.last_connection,
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
		$(".lb-histcount").textContent = state.history.length ? `(${rows.length})` : "";
		if (!rows.length) {
			hist.innerHTML = `<div class="lb-empty">${__("No messages.")}</div>`;
			return;
		}
		hist.innerHTML = `<table class="lb-table">
			<thead><tr><th>${__("Date")}</th><th></th><th>${__("Message")}</th><th>${__("Status")}</th></tr></thead>
			<tbody>${rows.map((r) => {
				const inbound = r.MessageType === 2;
				return `<tr>
					<td style="white-space:nowrap">${esc(when(r.SmsDate))}</td>
					<td class="lb-dir ${inbound ? "in" : "out"}">${inbound ? "← " + __("from SIM") : "→ " + __("to SIM")}</td>
					<td class="lb-msg lb-mono">${esc(r.Message)}</td>
					<td>${esc(r.StatusText)}${r.SentByUserName ? `<div class="lb-note">${esc(r.SentByUserName)}</div>` : ""}</td>
				</tr>`;
			}).join("")}</tbody></table>`;
	}

	// ------------------------------------------------------------ events
	function search() {
		state.imeis = [];
		state.skip = 0;
		load_page();
	}

	$(".lb-search").addEventListener("click", search);
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
	});
	$(".lb-detail").addEventListener("input", (e) => {
		if (e.target.classList.contains("lb-text")) $(".lb-len").textContent = e.target.value.length + " / 160";
	});
	$(".lb-detail").addEventListener("change", (e) => {
		if (e.target.classList.contains("lb-histfilter")) draw_history();
	});

	// The list is the stored copy, so it shows even while Lebara is logged out.
	refresh_session().catch(() => {});
	load_page();
})();
