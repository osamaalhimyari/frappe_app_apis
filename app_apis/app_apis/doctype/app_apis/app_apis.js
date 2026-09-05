// Copyright (c) 2026, osama and contributors
// For license information, please see license.txt

// Buttons for the merged settings form. Both connections are configured on one
// screen, so both connections get their tools here, grouped by provider rather
// than piled into one toolbar -- "Test Connection" is ambiguous once there are
// two providers to test.

frappe.ui.form.on("app_apis", {
	refresh(frm) {
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
						const u = d.user || {};
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
									${__("Node")}: ${esc(d.node)} \u00b7
									${__("Round trip")}: ${esc(d.elapsed_ms || 0)} ms
								</div>
								${
									d.ok
										? `<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">
												${__("Role")}: ${esc(u.role)} \u00b7
												${__("Account ID")}: ${esc(u.account_id)} \u00b7
												${__("Partner ID")}: ${esc(u.partner_id)}
												${u.ip_filter ? ` \u00b7 <b>${__("IP filter is on at Pilot")}</b>` : ""}
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
						const u = d.user || {};
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
									${__("Node")}: ${esc(d.node)} ·
									${__("Round trip")}: ${esc(d.elapsed_ms || 0)} ms
								</div>
								${
									d.ok
										? `<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">
												${__("Role")}: ${esc(u.role)} ·
												${__("Account ID")}: ${esc(u.account_id)} ·
												${__("Partner ID")}: ${esc(u.partner_id)}
												${u.ip_filter ? ` · <b>${__("IP filter is on at Pilot")}</b>` : ""}
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
	},
});
