# =============================================================================
# fleet_audit_waste  --  Server Script (API), separate from app_apis
# =============================================================================
#
# What the Fleet Audit counts as a WASTED SIM. Edit the four lines below; nothing else has to change and
# the app is never touched.
#
# A SIM that is still billing (Active or Suspend) is WASTED when its vehicle matches ANY of:
#
#   WASTE_ERP_STATUSES   the vehicle's Device Statues is one of these names      ["Deleted"]
#   WASTE_EXPIRED_DAYS   the subscription ran out more than this many days ago   0 = expired at all,
#                        (None = ignore the subscription date)                   None = off
#   WASTE_VERDICTS       the audit verdict for the device is one of these         [] = off
#                        (OK, Missing on Pilot, Missing on IM, Missing on both, Deleted but still live,
#                        Unexpected platform, Not in ERP, No device serial, Not checked, Unmatchable ID)
#
#   SUSPENDED_PRICE      what ONE suspended SIM is billed, whatever its plan or carrier.
#
# Plan prices per carrier are not here: they are the SIM Type records (Estimated Price and the carrier
# columns).
#
# If this script is missing, disabled or broken, the Fleet Audit falls back to the built-in rule (Deleted,
# or expired at all, 2.0 for a suspended SIM) instead of showing an error page, and a broken script writes an
# Error Log. After editing, hover the "wasted" line on a SIM tile: it reads the rule back in words.
#
# Install as: Server Script, Script Type "API", API Method fleet_audit_waste.
# =============================================================================

SUSPENDED_PRICE = 2.0
WASTE_ERP_STATUSES = ["Deleted"]
WASTE_EXPIRED_DAYS = 0
WASTE_VERDICTS = []

# An API Server Script hands its answer back through frappe.flags; the app reads this one key.
frappe.flags["app_apis_fleet_waste"] = {
    "suspended_price": SUSPENDED_PRICE,
    "erp_statuses": WASTE_ERP_STATUSES,
    "expired_days": WASTE_EXPIRED_DAYS,
    "verdicts": WASTE_VERDICTS,
}
