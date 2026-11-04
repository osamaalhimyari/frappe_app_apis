// Copyright (c) 2026, osama and contributors
// For license information, please see license.txt

// Buttons for the merged settings form. Both connections are configured on one
// screen, so both connections get their tools here, grouped by provider rather
// than piled into one toolbar -- "Test Connection" is ambiguous once there are
// two providers to test.

// WhatsApp templates. The catalog is App Apis WhatsApp Template, filled from
// Chatwoot by the Sync Templates button; see app_apis/whatsapp_templates.py.
const WA_TEMPLATE = "App Apis WhatsApp Template";

// Only templates this app can actually send: approved at Meta, and nothing in
// them (a media header, a variable URL button) that a row has no way to fill.
const WA_SENDABLE = () => ({ filters: { status: "APPROVED", supported: 1 } });

function wa_params(stored) {
	return String(stored || "")
		.split(",")
		.map((s) => s.trim())
		.filter(Boolean);
}

function wa_prefill(params) {
	return params.map((p) => `{{${p}}} = `).join("\n");
}

function wa_fetch(name) {
	return frappe.db
		.get_value(WA_TEMPLATE, name, ["title", "params", "header", "body", "footer", "category"])
		.then((r) => (r && r.message) || {});
}

function wa_preview(t) {
	const esc = (v) => frappe.utils.escape_html(String(v || ""));
	const mark = (v) =>
		esc(v)
			.replace(/\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g, '<b style="color:var(--blue-600)">{{$1}}</b>')
			.replace(/\n/g, "<br>");
	return `<div dir="auto" style="font-size:13px;line-height:1.6">
		${t.header ? `<div style="font-weight:600;margin-bottom:6px">${mark(t.header)}</div>` : ""}
		<div>${mark(t.body)}</div>
		${t.footer ? `<div style="margin-top:6px;color:var(--text-muted);font-size:12px">${esc(t.footer)}</div>` : ""}
	</div>`;
}

function wa_send_test() {
	const d = new frappe.ui.Dialog({
		title: __("Send a Test WhatsApp Template"),
		fields: [
			{
				fieldname: "template",
				fieldtype: "Link",
				options: WA_TEMPLATE,
				label: __("Template"),
				reqd: 1,
				get_query: WA_SENDABLE,
				onchange() {
					const name = d.get_value("template");
					if (!name) {
						d.fields_dict.preview.$wrapper.empty();
						return;
					}
					wa_fetch(name).then((t) => {
						d.set_value("variables", wa_prefill(wa_params(t.params)));
						d.fields_dict.preview.$wrapper.html(wa_preview(t));
					});
				},
			},
			{
				fieldname: "phone",
				fieldtype: "Data",
				label: __("Send To"),
				reqd: 1,
				description: __("Your own WhatsApp number, e.g. 0500000000."),
			},
			{
				fieldname: "variables",
				fieldtype: "Small Text",
				label: __("Template Variables"),
				description: __(
					"Sample values, one per line, e.g. {{1}} = ABC-1234. There is no ticket here, so {placeholders} are sent exactly as typed."
				),
			},
			{ fieldname: "preview", fieldtype: "HTML" },
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			frappe.call({
				method: "app_apis.whatsapp_templates.send_test",
				args: { template: values.template, phone: values.phone, variables: values.variables || "" },
				freeze: true,
				freeze_message: __("Sending, then waiting for WhatsApp to confirm…"),
				callback(r) {
					const res = (r && r.message) || {};
					if (!res.ok) {
						frappe.msgprint({
							title: __("Not Sent"),
							indicator: "red",
							message: frappe.utils.escape_html(res.msg || __("Unknown error.")),
						});
						return;
					}
					d.hide();
					const confirmed = res.delivery === "accepted";
					frappe.msgprint({
						title: confirmed ? __("WhatsApp Accepted It") : __("Posted to Chatwoot"),
						indicator: confirmed ? "green" : "orange",
						message: confirmed
							? __("WhatsApp accepted the template; it should be on the phone now.")
							: __("Chatwoot has it, but WhatsApp had not confirmed within a few seconds. Check the conversation in Chatwoot."),
					});
				},
			});
		},
	});
	d.show();
}

// ---------------------------------------------------------------- Pilot Connections: customer passwords
// Each connection row shows ITS customer passwords as a small table inside the row (the row's edit
// form, "Customer Passwords"). They are stored in the hidden Pilot Customer Passwords table, one
// encrypted line each, tagged with the row's conn_id; saving the settings saves them.
const PW_TABLE = "pilot_customer_passwords";
const PW_DT = "App Apis Pilot Password";

function pw_lines(frm, conn_id) {
	return (frm.doc[PW_TABLE] || [])
		.filter((r) => r.connection === conn_id)
		.sort((a, b) => (a.idx || 0) - (b.idx || 0));
}

function pw_renumber(frm) {
	(frm.doc[PW_TABLE] || []).sort((a, b) => (a.idx || 0) - (b.idx || 0)).forEach((r, i) => (r.idx = i + 1));
}

function pw_editor(frm, cdn) {
	const row = locals["App Apis Pilot Connection"][cdn];
	const grid_row = frm.fields_dict.pilot_connections.grid.grid_rows_by_docname[cdn];
	const field = grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict.passwords_html;
	if (!row || !field) return;
	if (!row.conn_id) frappe.model.set_value(row.doctype, row.name, "conn_id", frappe.utils.get_random(10));
	const esc = (v) => frappe.utils.escape_html(String(v || ""));
	const lines = pw_lines(frm, row.conn_id);
	const $w = field.$wrapper.empty();
	const $t = $(`<div class="app-apis-pw">
		<div class="text-muted small" style="margin-bottom:4px">${__("Customer Passwords")}</div>
		<table class="table table-bordered table-sm" style="margin-bottom:6px;font-size:12px">
			<thead><tr><th style="width:40px">${__("No.")}</th><th>${__("Password")}</th><th style="width:96px"></th></tr></thead>
			<tbody></tbody>
		</table>
		<button class="btn btn-xs btn-default pw-add">${__("Add Password")}</button>
	</div>`).appendTo($w);
	const $body = $t.find("tbody");
	if (!lines.length) $body.append(`<tr><td colspan="3" class="text-muted">${__("No passwords yet.")}</td></tr>`);
	lines.forEach((line, i) => {
		const saved = line.password && /^\*+$/.test(line.password);
		const $tr = $(`<tr>
			<td>${i + 1}</td>
			<td><input type="password" class="form-control input-xs" autocomplete="new-password"
				placeholder="${saved ? esc(__("saved — type to replace")) : ""}" value="${saved ? "" : esc(line.password)}"></td>
			<td style="white-space:nowrap">
				<button class="btn btn-xs btn-default pw-up" title="${esc(__("Move up"))}" ${i === 0 ? "disabled" : ""}>↑</button>
				<button class="btn btn-xs btn-default pw-down" title="${esc(__("Move down"))}" ${i === lines.length - 1 ? "disabled" : ""}>↓</button>
				<button class="btn btn-xs btn-danger pw-del" title="${esc(__("Remove"))}">✕</button>
			</td></tr>`).appendTo($body);
		$tr.find("input").on("change", function () {
			const v = String(this.value || "");
			if (v) frappe.model.set_value(PW_DT, line.name, "password", v);
		});
		const move = (step) => {
			const other = lines[i + step];
			if (!other) return;
			const a = line.idx;
			line.idx = other.idx;
			other.idx = a;
			pw_renumber(frm);
			frm.dirty();
			pw_editor(frm, cdn);
		};
		$tr.find(".pw-up").on("click", (e) => { e.preventDefault(); move(-1); });
		$tr.find(".pw-down").on("click", (e) => { e.preventDefault(); move(1); });
		$tr.find(".pw-del").on("click", (e) => {
			e.preventDefault();
			frappe.model.clear_doc(PW_DT, line.name);
			frm.doc[PW_TABLE] = (frm.doc[PW_TABLE] || []).filter((r) => r.name !== line.name);
			pw_renumber(frm);
			frm.dirty();
			pw_editor(frm, cdn);
		});
	});
	$t.find(".pw-add").on("click", (e) => {
		e.preventDefault();
		const child = frappe.model.add_child(frm.doc, PW_DT, PW_TABLE);
		child.connection = row.conn_id;
		pw_renumber(frm);
		frm.dirty();
		pw_editor(frm, cdn);
		field.$wrapper.find("input").last().focus();
	});
}

// IM Connections: each row shows ITS device models as a small table inside the row; they are stored in
// the hidden IM Device Models table, tagged with the row's conn_id.
const IM_MODELS_TABLE = "im_device_models";
const IM_MODEL_DT = "App Apis IM Model";
const IM_MODEL_COLS = [["device_type", __("ERP Device Type")], ["im_model_id", __("IM Model ID")], ["note", __("Note")]];

function im_models_editor(frm, cdn) {
	const row = locals["App Apis IM Connection"][cdn];
	const grid_row = frm.fields_dict.im_connections.grid.grid_rows_by_docname[cdn];
	const field = grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict.models_html;
	if (!row || !field) return;
	if (!row.conn_id) frappe.model.set_value(row.doctype, row.name, "conn_id", frappe.utils.get_random(10));
	const esc = (v) => frappe.utils.escape_html(String(v || ""));
	const lines = (frm.doc[IM_MODELS_TABLE] || []).filter((r) => r.connection === row.conn_id)
		.sort((a, b) => (a.idx || 0) - (b.idx || 0));
	const $w = field.$wrapper.empty();
	const $t = $(`<div class="app-apis-im-models">
		<div class="text-muted small" style="margin-bottom:4px">${__("Device Models")}</div>
		<table class="table table-bordered table-sm" style="margin-bottom:6px;font-size:12px">
			<thead><tr><th style="width:36px">${__("No.")}</th>${IM_MODEL_COLS.map((c) => `<th>${c[1]}</th>`).join("")}<th style="width:40px"></th></tr></thead>
			<tbody></tbody>
		</table>
		<button class="btn btn-xs btn-default im-add">${__("Add Model")}</button>
	</div>`).appendTo($w);
	const $body = $t.find("tbody");
	if (!lines.length) $body.append(`<tr><td colspan="${IM_MODEL_COLS.length + 2}" class="text-muted">${__("No models yet.")}</td></tr>`);
	lines.forEach((line, i) => {
		const $tr = $(`<tr><td>${i + 1}</td>${IM_MODEL_COLS.map((c) =>
			`<td><input class="form-control input-xs" data-f="${c[0]}" value="${esc(line[c[0]])}"></td>`).join("")}
			<td><button class="btn btn-xs btn-danger im-del" title="${esc(__("Remove"))}">✕</button></td></tr>`).appendTo($body);
		$tr.find("input").on("change", function () {
			frappe.model.set_value(IM_MODEL_DT, line.name, $(this).attr("data-f"), String(this.value || "").trim());
		});
		$tr.find(".im-del").on("click", (e) => {
			e.preventDefault();
			frappe.model.clear_doc(IM_MODEL_DT, line.name);
			frm.doc[IM_MODELS_TABLE] = (frm.doc[IM_MODELS_TABLE] || []).filter((r) => r.name !== line.name);
			(frm.doc[IM_MODELS_TABLE] || []).forEach((r, k) => (r.idx = k + 1));
			frm.dirty();
			im_models_editor(frm, cdn);
		});
	});
	$t.find(".im-add").on("click", (e) => {
		e.preventDefault();
		const child = frappe.model.add_child(frm.doc, IM_MODEL_DT, IM_MODELS_TABLE);
		child.connection = row.conn_id;
		frm.dirty();
		im_models_editor(frm, cdn);
		field.$wrapper.find("tbody tr").last().find("input").first().focus();
	});
}

frappe.ui.form.on("App Apis IM Connection", {
	form_render(frm, cdt, cdn) {
		im_models_editor(frm, cdn);
	},
	im_connections_add(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "conn_id", frappe.utils.get_random(10));
	},
});

frappe.ui.form.on("App Apis Pilot Connection", {
	form_render(frm, cdt, cdn) {
		pw_editor(frm, cdn);
	},
	pilot_connections_add(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "conn_id", frappe.utils.get_random(10));
	},
});

frappe.ui.form.on("app_apis", {
	setup(frm) {
		frm.set_query("whatsapp_template", "auto_message_rules", WA_SENDABLE);
		frm.set_query("whatsapp_template", "stale_ticket_rules", WA_SENDABLE);
		frm.set_query("whatsapp_template", "subscription_messages", WA_SENDABLE);
	},

	sync_whatsapp_templates() {
		frappe.call({
			method: "app_apis.whatsapp_templates.sync",
			freeze: true,
			freeze_message: __("Fetching WhatsApp templates from Chatwoot…"),
			callback(r) {
				const d = (r && r.message) || {};
				const esc = (v) => frappe.utils.escape_html(String(v === undefined || v === null ? "" : v));
				if (!d.ok) {
					frappe.msgprint({
						title: __("WhatsApp Templates"),
						indicator: "red",
						message: esc(d.msg || __("Sync failed.")),
					});
					return;
				}
				const templates = d.templates || [];
				const rows = templates
					.map(
						(t) => `<tr>
							<td dir="auto">${esc(t.name)} <span class="text-muted">(${esc(t.language)})</span></td>
							<td>${esc(t.inbox)}</td>
							<td>${esc(t.category)}</td>
							<td>${wa_params(t.params).map((p) => esc(`{{${p}}}`)).join(" ") || "—"}</td>
							<td>${
								t.supported
									? `<span class="indicator-pill green">${__("Ready")}</span>`
									: `<span class="text-muted">${esc(t.reason)}</span>`
							}</td>
						</tr>`
					)
					.join("");
				frappe.msgprint({
					title: __("WhatsApp Templates"),
					indicator: "green",
					message: `<div style="font-size:12.5px">
						<p>${__("{0} template(s) across {1} WhatsApp inbox(es). Pick one in the WhatsApp Template column of Status Rules.", [
							templates.length,
							(d.inboxes || []).length,
						])}</p>
						${
							d.refreshed
								? `<p class="text-muted">${__(
										"Chatwoot was also asked to re-read Meta; a template approved in the last minute or two may need one more press."
								  )}</p>`
								: ""
						}
						${(d.removed || []).length ? `<p class="text-muted">${__("No longer in Chatwoot")}: ${esc(d.removed.join(", "))}</p>` : ""}
						<div style="overflow-x:auto"><table class="table table-bordered" style="margin:0">
							<thead><tr><th>${__("Template")}</th><th>${__("Inbox")}</th><th>${__("Category")}</th><th>${__(
								"Variables"
							)}</th><th></th></tr></thead>
							<tbody>${rows || `<tr><td colspan="5" class="text-muted">${__("No WhatsApp templates found.")}</td></tr>`}</tbody>
						</table></div>
					</div>`,
				});
			},
		});
	},

	refresh(frm) {
		frm.add_custom_button(__("Send Test Template"), () => wa_send_test(), __("WhatsApp"));

		lebara_buttons(frm);

		// frappe hides "Customize" on Singles; this one is customizable
		// (app_apis.core.customize).
		frm.page.add_menu_item(__("Customize"), () =>
			frappe.set_route("Form", "Customize Form", { doc_type: frm.doctype })
		);

		// One Test button per row of IM Connections (saved rows only: a test reads the saved login).
		(frm.doc.im_connections || []).forEach((row) => {
			const name = row.label || __("IM {0}", [row.idx]);
			frm.add_custom_button(
				__("Test {0}", [name]),
				() => {
					frappe.call({
						method: "app_apis.im_connector.test_connection",
						args: { account: row.idx },
						freeze: true,
						freeze_message: __("Signing in to IM\u2026"),
						callback(r) {
							const d = (r && r.message) || {};
							const esc = (v) => frappe.utils.escape_html(String(v === undefined || v === null || v === "" ? "\u2014" : v));
							frappe.msgprint({
								title: name,
								indicator: d.ok ? "green" : "red",
								message: `<div style="font-size:13px">
									<div><b>${d.ok ? __("Signed in") : __("Failed")}</b></div>
									<div style="margin-top:6px;color:var(--text-muted)">${esc(d.message)}</div>
									<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">
										${__("Account")}: ${esc(d.account)} \u00b7 ${__("Project")}: ${esc(d.project_id)} \u00b7
										${__("Round trip")}: ${esc(d.elapsed_ms || 0)} ms
									</div>
								</div>`,
							});
						},
					});
				},
				__("IM Connections")
			);
		});

		frm.add_custom_button(
			__("Clear Cached Tokens"),
			() => {
				frappe.call({
					method: "app_apis.im_connector.clear_cache_from_desk",
					callback() {
						frappe.show_alert({ message: __("Cached IM tokens and snapshots dropped."), indicator: "green" });
					},
				});
			},
			__("IM Connections")
		);


		// One Test button per row of Pilot Connections (saved rows only: a test reads the saved login).
		(frm.doc.pilot_connections || []).forEach((row) => {
			const name = row.label || __("Pilot {0}", [row.idx]);
			frm.add_custom_button(
				__("Test {0}", [name]),
				() => {
					frappe.call({
						method: "app_apis.pilot_admin.test_connection",
						args: { account: row.idx },
						freeze: true,
						freeze_message: __("Signing in to Pilot…"),
						callback(r) {
							const d = (r && r.message) || {};
							const esc = (v) => frappe.utils.escape_html(String(v === undefined || v === null || v === "" ? "—" : v));
							frappe.msgprint({
								title: name,
								indicator: d.ok ? "green" : "red",
								message: `<div style="font-size:13px">
									<div><b>${d.ok ? __("Signed in") : __("Failed")}</b></div>
									<div style="margin-top:6px;color:var(--text-muted)">
										${esc(d.message)}
									</div>
									<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">
										${__("Login")}: ${esc(d.account)} ·
										${__("Server")}: ${esc(d.base_url)} ·
										${__("Round trip")}: ${esc(d.elapsed_ms || 0)} ms
									</div>
									${
										d.ok
											? `<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">
													${__("Accounts visible")}: ${esc(d.account_count)}
												</div>`
											: ""
									}
								</div>`,
							});
						},
					});
				},
				__("Pilot Connections")
			);
		});

		frm.add_custom_button(
			__("Clear Cached Tokens"),
			() => {
				frappe.call({
					method: "app_apis.pilot_admin.clear_cache_from_desk",
					callback() {
						frappe.show_alert({
							message: __("Cached Pilot admin tokens dropped."),
							indicator: "green",
						});
					},
				});
			},
			__("Pilot Connections")
		);

		// Pilot has no sign-in-only endpoint: `cmd=status` needs an IMEI, and the
		// account comes off a ticket rather than from this form. So there is
		// nothing honest to put behind a "Test Pilot Connection" button here --
		// the real test is Check Pilot on a ticket, and saying so beats a button
		// that would have to invent an IMEI to click.
		frm.dashboard.add_comment(
			__(
				"The Pilot Connection above is tested from a ticket, not from here: its account is read off the xticket and its status call needs an IMEI. Use Check Pilot on any xticket. Each row of Pilot Connections has one site-wide login, so it does have a Test button."
			),
			"blue",
			true
		);

		frm.add_custom_button(
			__("Check Stale Tickets Now"),
			() => {
				frappe.call({
					method: "app_apis.stale_reminders.check_now",
					freeze: true,
					freeze_message: __("Scanning tickets…"),
					callback(r) {
						const d = (r && r.message) || {};
						const esc = (v) => frappe.utils.escape_html(String(v === undefined || v === null || v === "" ? "—" : v));
						frappe.msgprint({
							title: __("Stale Ticket Reminders"),
							indicator: d.ok && d.ran ? "green" : d.ok ? "orange" : "red",
							message: d.ran
								? `<div style="font-size:13px">
										<div><b>${__("{0} stuck ticket(s); queued {1} message(s), one per technician per status.", [esc(d.checked), esc(d.queued)])}</b></div>
										<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">
											${__("Rules checked")}: ${esc(d.rules)}
										</div>
									</div>`
								: `<div style="font-size:13px">${esc(d.reason)}</div>`,
						});
					},
				});
			},
			__("Stale Tickets")
		);
	},
});

// Every table whose rows can name a WhatsApp template gets the same picker
// behaviour. Which placeholders each one can use is listed once, under the
// Chatwoot section's tables -- not repeated here, where a second copy had
// already drifted (it offered {customer} to Stale Ticket Rules, which only
// fill {engineer} {count} {state} {hours}).
const WA_TABLES = [
	"App Apis Auto Message",
	"App Apis Stale Ticket Rule",
	"App Apis Subscription Message",
];

WA_TABLES.forEach((doctype) => {
	frappe.ui.form.on(doctype, {
		whatsapp_template(frm, cdt, cdn) {
			const row = locals[cdt][cdn];
			if (!row.whatsapp_template) return;
			wa_fetch(row.whatsapp_template).then((t) => {
				const params = wa_params(t.params);
				// One "{{n}} = " line per variable, so what is left to fill is
				// visible -- never over something already typed.
				if (params.length && !String(row.template_variables || "").trim()) {
					frappe.model.set_value(cdt, cdn, "template_variables", wa_prefill(params));
				}
				frappe.msgprint({
					title: t.title || row.whatsapp_template,
					indicator: "blue",
					message:
						wa_preview(t) +
						`<hr><div style="font-size:12px">${
							params.length
								? __("Open the row and complete Template Variables, one line per variable, e.g. {0}. Placeholders are listed under the Chatwoot section.", [
										"<code>{{1}} = {plate}</code>",
								  ])
								: __("No variables: it is sent exactly as shown.")
						}</div>`,
				});
			});
		},
	});
});

// ---------------------------------------------------------------------------
// Lebara: the login is two steps (password -> SMS code), both done by the
// "Lebara API" Server Script (api_method "lebara"). Everything here is only
// buttons around it.
// ---------------------------------------------------------------------------
function lebara_call(action, args, freeze_message) {
	return frappe
		.call({
			method: "lebara",
			args: Object.assign({ action }, args || {}),
			freeze: true,
			freeze_message: freeze_message || __("Talking to Lebara…"),
		})
		.then((r) => ((r && r.message) || {}).result);
}

function lebara_ask_otp(frm, hint) {
	frappe.prompt(
		[
			{
				fieldname: "code",
				fieldtype: "Data",
				label: __("Code from SMS"),
				reqd: 1,
				description: frappe.utils.escape_html(hint || "") + " " + __("The code is valid for about 60 seconds."),
			},
		],
		(v) =>
			lebara_call("submit_otp", { code: v.code }, __("Checking the code…")).then((res) => {
				frappe.show_alert({ message: (res && res.message) || __("Logged in to Lebara."), indicator: "green" });
				frm.reload_doc();
			}),
		__("Lebara OTP"),
		__("Submit")
	);
}

function lebara_buttons(frm) {
	const group = __("Lebara");
	frm.add_custom_button(
		__("Request OTP"),
		() => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the form first, so the Lebara username and password are stored."));
				return;
			}
			lebara_call("request_otp", {}, __("Signing in to Lebara…")).then((res) => {
				if (res && res.logged_in) {
					frappe.show_alert({ message: res.message, indicator: "green" });
					frm.reload_doc();
					return;
				}
				frm.reload_doc();
				lebara_ask_otp(frm, res && res.message);
			});
		},
		group
	);
	frm.add_custom_button(__("Submit OTP"), () => lebara_ask_otp(frm, ""), group);
	frm.add_custom_button(
		__("Test Session"),
		() =>
			lebara_call("ping").then(() => {
				frappe.show_alert({ message: __("Lebara session is alive."), indicator: "green" });
				frm.reload_doc();
			}),
		group
	);
	frm.add_custom_button(
		__("Find SIM"),
		() =>
			frappe.prompt(
				[{ fieldname: "msisdn", fieldtype: "Data", label: __("MSISDN"), reqd: 1 }],
				(v) =>
					lebara_call("sim_full", { msisdn: v.msisdn.trim() }, __("Looking up the SIM…")).then((sim) => {
						frappe.msgprint({
							title: __("Lebara SIM"),
							indicator: sim ? "green" : "orange",
							message: sim
								? `<pre style="font-size:11px;max-height:60vh;overflow:auto">${frappe.utils.escape_html(
										JSON.stringify(sim, null, 2)
								  )}</pre>`
								: __("No SIM with that MSISDN."),
						});
					}),
				__("Find Lebara SIM"),
				__("Find")
			),
		group
	);
	frm.add_custom_button(
		__("Logout"),
		() =>
			frappe.confirm(__("Log out of Lebara? You will need a new OTP to log in again."), () =>
				lebara_call("logout").then(() => frm.reload_doc())
			),
		group
	);
}
