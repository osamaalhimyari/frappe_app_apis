// Bottom-right toast when an automatic customer message goes out.
//
// The send happens in a background worker seconds after the ticket is saved,
// so there is no ajax response left to report into -- the server pushes
// `app_apis_message_sent` over the realtime socket and this listens for it.
//
// SOURCE OF TRUTH for the Client Script `xticket-message-toast`. Mirrored
// byte-for-byte into fixtures/client_script.json; edit here, then copy across.

frappe.provide("app_apis");

// Registering inside `refresh` means every form load would add another handler
// and the operator would get two toasts, then three. The flag pins it to one
// per page load -- and because the listener lives on the global socket rather
// than the form, it keeps working after they navigate away from the ticket.
if (!app_apis._message_toast_bound) {
	app_apis._message_toast_bound = true;

	// What the operator calls each message. Kept here rather than sent from the
	// server so the toast follows the desk language, not the customer's.
	//
	// The engineer's messages are named as such rather than shown with a
	// recipient field beside them: a toast is read in a second, and "Feedback
	// link sent" next to "Engineer job assignment sent" needs no explaining.
	const LABELS = {
		accepted: __("Acceptance message"),
		working: __("Work started message"),
		valuation: __("Feedback link"),
		tech_accepted: __("Engineer job assignment"),
		tech_working: __("Engineer work-started notice"),
		tech_done: __("Engineer job-closed notice"),
	};

	const LOOK = {
		Sent: { indicator: "green", icon: "✅", seconds: 5 },
		Failed: { indicator: "red", icon: "⚠️", seconds: 10 },
		Skipped: { indicator: "orange", icon: "⏭️", seconds: 7 },
	};

	frappe.realtime.on("app_apis_message_sent", function (d) {
		if (!d || !d.ticket) return;

		const look = LOOK[d.status] || LOOK.Skipped;
		const label = LABELS[d.template] || d.template || __("Message");

		let line;
		if (d.status === "Sent") {
			line = __("{0} sent to {1}", [label, frappe.utils.escape_html(d.phone || "-")]);
		} else if (d.status === "Failed") {
			line = __("{0} could NOT be sent", [label]);
		} else {
			line = __("{0} skipped", [label]);
		}

		// The ticket is a link: a toast about a message is only useful if you
		// can get to the ticket it was about.
		const ticket = frappe.utils.escape_html(d.ticket);
		const href = `/app/xticket/${encodeURIComponent(d.ticket)}`;
		let body = `${look.icon} ${line}<br><a href="${href}"><b>${ticket}</b></a>`;

		if (d.reason) {
			body += `<br><span style="opacity:.75">${frappe.utils.escape_html(String(d.reason).slice(0, 160))}</span>`;
		}

		frappe.show_alert({ message: body, indicator: look.indicator }, look.seconds);
	});
}
