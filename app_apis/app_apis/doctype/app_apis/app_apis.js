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

		frm.add_custom_button(
			__("Test IM Connection"),
			() => {
				frappe.call({
					method: "app_apis.im_connector.test_connection",
					freeze: true,
					freeze_message: __("Signing in to IM…"),
					callback(r) {
						const d = (r && r.message) || {};
						frappe.msgprint({
							title: __("IM Connection"),
							indicator: d.ok ? "green" : "red",
							message: `<div style="font-size:13px">
								<div><b>${d.ok ? __("Signed in") : __("Failed")}</b></div>
								<div style="margin-top:6px;color:var(--text-muted)">
									${frappe.utils.escape_html(String(d.message || ""))}
								</div>
								<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">
									${__("Account")}: ${frappe.utils.escape_html(String(d.account || "—"))} ·
									${__("Mode")}: ${__("Live")} ·
									${__("Round trip")}: ${frappe.utils.escape_html(
										String(d.elapsed_ms || 0)
									)} ms
								</div>
							</div>`,
						});
					},
				});
			},
			__("IM")
		);

		frm.add_custom_button(
			__("Clear Cached Token"),
			() => {
				frappe.call({
					method: "app_apis.im_connector.clear_cache_from_desk",
					callback() {
						frappe.show_alert({
							message: __("Cached IM token and snapshots dropped."),
							indicator: "green",
						});
					},
				});
			},
			__("IM")
		);

		frm.add_custom_button(
			__("Test Admin Connection"),
			() => {
				frappe.call({
					method: "app_apis.pilot_admin.test_connection",
					freeze: true,
					freeze_message: __("Signing in to Pilot\u2026"),
					callback(r) {
						const d = (r && r.message) || {};
						const esc = (v) => frappe.utils.escape_html(String(v === undefined || v === null || v === "" ? "\u2014" : v));
						frappe.msgprint({
							title: __("Pilot Admin Connection"),
							indicator: d.ok ? "green" : "red",
							message: `<div style="font-size:13px">
								<div><b>${d.ok ? __("Signed in") : __("Failed")}</b></div>
								<div style="margin-top:6px;color:var(--text-muted)">
									${esc(d.message)}
								</div>
								<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">
									${__("Login")}: ${esc(d.account)} \u00b7
									${__("Server")}: ${esc(d.base_url)} \u00b7
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
			__("Pilot Admin")
		);

		frm.add_custom_button(
			__("Clear Cached Token"),
			() => {
				frappe.call({
					method: "app_apis.pilot_admin.clear_cache_from_desk",
					callback() {
						frappe.show_alert({
							message: __("Cached Pilot admin token dropped."),
							indicator: "green",
						});
					},
				});
			},
			__("Pilot Admin")
		);

		frm.add_custom_button(
			__("Test Admin Connection 2"),
			() => {
				frappe.call({
					method: "app_apis.pilot_admin.test_connection",
					args: { account: 2 },
					freeze: true,
					freeze_message: __("Signing in to Pilot…"),
					callback(r) {
						const d = (r && r.message) || {};
						const esc = (v) => frappe.utils.escape_html(String(v === undefined || v === null || v === "" ? "—" : v));
						frappe.msgprint({
							title: __("Pilot Admin Connection 2"),
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
			__("Pilot Admin 2")
		);

		frm.add_custom_button(
			__("Clear Cached Token 2"),
			() => {
				frappe.call({
					method: "app_apis.pilot_admin.clear_cache_from_desk",
					args: { account: 2 },
					callback() {
						frappe.show_alert({
							message: __("Cached Pilot admin token (2) dropped."),
							indicator: "green",
						});
					},
				});
			},
			__("Pilot Admin 2")
		);

		// Pilot has no sign-in-only endpoint: `cmd=status` needs an IMEI, and the
		// account comes off a ticket rather than from this form. So there is
		// nothing honest to put behind a "Test Pilot Connection" button here --
		// the real test is Check Pilot on a ticket, and saying so beats a button
		// that would have to invent an IMEI to click.
		frm.dashboard.add_comment(
			__(
				"The Pilot Connection above is tested from a ticket, not from here: its account is read off the xticket and its status call needs an IMEI. Use Check Pilot on any xticket. The Pilot Admin Connection has one site-wide login, so it does have a Test button."
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
