# =============================================================================
# lebara_serial_link  --  Server Script (API), separate from app_apis
# =============================================================================
#
# How a Lebara SIM is tied to its ERP Serial No. Edit the two lines below; nothing else has to change and
# the app is never touched. The link is made by every "Lebara SIM Sync" run (hourly) and shows in the
# Serial No column of the Lebara SIM list.
#
#   MATCH_FIELDS   which Lebara value is looked for as a Serial No NAME, tried in this order, the first
#                  hit wins. Any of: "iccid", "msisdn", "imsi".
#                  Today the SIM's Serial No is named by its ICCID (24,873 of 30,222 SIMs match that way;
#                  msisdn and imsi match none, they are here in case that ever changes).
#   ITEM_CODES     only Serial Nos of these ERP Items count, e.g. ["Lebara SIM", "sim_stc"].
#                  [] = any Item.
#
# A SIM with no Serial No is left blank (and a blank stays blank until one exists); an existing link is
# removed again when its Serial No is deleted or stops matching.
#
# If this script is missing, disabled or broken the sync uses the built-in rule (iccid, any Item) and writes
# an Error Log entry when the script itself fails.
#
# Install as: Server Script, Script Type "API", API Method lebara_serial_link.
# =============================================================================

MATCH_FIELDS = ["iccid"]
ITEM_CODES = []

# An API Server Script hands its answer back through frappe.flags; the app reads this one key.
frappe.flags["app_apis_lebara_serial"] = {
    "match_fields": MATCH_FIELDS,
    "item_codes": ITEM_CODES,
}
