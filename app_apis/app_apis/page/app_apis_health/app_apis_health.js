// App Apis Health: connections, hourly jobs, errors and failed messages, from app_apis.health.
frappe.pages["app-apis-health"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("App Apis Health"), single_column: true });
	const STATE = {
		ok: { color: "green", label: __("OK") },
		warn: { color: "orange", label: __("Check") },
		fail: { color: "red", label: __("Broken") },
		off: { color: "gray", label: __("Off") },
	};
	const esc = frappe.utils.escape_html;
	const $body = $(`<div class="app-apis-health" style="padding: var(--padding-md) 0"></div>`).appendTo(page.main);

	page.set_primary_action(__("Test all connections"), () => load(true), "es-line-reload");
	page.set_secondary_action(__("Refresh"), () => load(false));
	page.add_menu_item(__("Action Log"), () => frappe.set_route("List", "App Apis Action Log"));
	page.add_menu_item(__("Error Log"), () => frappe.set_route("List", "Error Log"));
	page.add_menu_item(__("Settings (Access, Alerts)"), () => frappe.set_route("Form", "app_apis"));

	function pill(state) {
		const s = STATE[state] || STATE.off;
		return `<span class="indicator-pill ${s.color}">${s.label}</span>`;
	}

	function render(data) {
		const c = data.counts || {};
		const areas = [];
		(data.checks || []).forEach((row) => {
			let a = areas.find((x) => x.name === row.area);
			if (!a) areas.push((a = { name: row.area, rows: [] }));
			a.rows.push(row);
		});
		const alerts = (data.alert_users || []).length
			? __("Alerts go to: {0}", [data.alert_users.map(esc).join(", ")])
			: __("Nobody gets alerts yet. Add users under app_apis settings > Health and Alerts.");
		let html = `
			<div class="flex flex-wrap" style="gap: var(--margin-sm); margin-bottom: var(--margin-md)">
				${["fail", "warn", "ok", "off"].map((s) => `<span class="indicator-pill ${STATE[s].color}">${c[s] || 0} ${STATE[s].label}</span>`).join("")}
			</div>
			<p class="text-muted small">${esc(alerts)} ${data.live_hourly ? __("Connections are also tested every hour.") : ""}
				${__("Checked {0}.", [frappe.datetime.prettyDate(data.at)])}</p>`;
		areas.forEach((a) => {
			html += `<div class="frappe-card" style="margin-bottom: var(--margin-md); padding: var(--padding-sm) var(--padding-md)">
				<h5 style="margin: var(--margin-sm) 0">${esc(__(a.name))}</h5>
				<table class="table table-sm" style="margin: 0">
				<tbody>${a.rows
					.map(
						(r) => `<tr data-key="${esc(r.key)}">
						<td style="width: 90px">${pill(r.state)}</td>
						<td style="width: 30%"><b>${esc(r.name)}</b></td>
						<td class="text-muted">${esc(r.detail || "")}</td>
						<td class="text-muted small text-nowrap" title="${esc(r.when || "")}">${esc(r.ago || "")}</td>
						<td style="width: 70px; text-align: right">${
							r.test && r.state !== "off"
								? `<button class="btn btn-xs btn-default" data-test="${esc(r.test)}">${__("Test")}</button>`
								: ""
						}</td>
					</tr>`
					)
					.join("")}</tbody></table></div>`;
		});
		$body.html(html);
		$body.find("button[data-test]").on("click", function () {
			const $b = $(this);
			const $row = $b.closest("tr");
			$b.prop("disabled", true).text(__("Testing…"));
			frappe
				.xcall("app_apis.health.test_now", { test: $b.attr("data-test") })
				.then((r) => {
					$row.find("td:eq(0)").html(pill(r.ok ? "ok" : "fail"));
					$row.find("td:eq(2)").text(r.message || (r.ok ? __("signed in") : __("failed")));
					$row.find("td:eq(3)").text(__("just now"));
				})
				.finally(() => $b.prop("disabled", false).text(__("Test")));
		});
	}

	function load(live) {
		$body.html(`<p class="text-muted">${live ? __("Signing in to every connection…") : __("Loading…")}</p>`);
		const call = live
			? frappe.xcall("app_apis.health.test_now", {})
			: frappe.xcall("app_apis.health.status");
		call.then(render).catch(() => $body.html(`<p class="text-muted">${__("Could not load the health page.")}</p>`));
	}

	load(false);
};
