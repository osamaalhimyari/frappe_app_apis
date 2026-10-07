// Customize Form: offer the app_apis settings Single in "Enter Form Type".
// frappe's own onload filters every Single out of that list; this runs after
// it and swaps in a query that adds the Singles the app allows
// (app_apis.core.customize.CUSTOMIZABLE_SINGLES).
frappe.ui.form.on("Customize Form", {
	onload(frm) {
		frm.set_query("doc_type", () => ({ query: "app_apis.core.customize.doctype_query" }));
	},
});
