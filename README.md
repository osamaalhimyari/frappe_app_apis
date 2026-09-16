### App Apis

Everything this ERPNext site needs to talk to the outside world for vehicle
tracking and customer support -- Pilot GPS, IM (gps.im2m.ws), Chatwoot -- lives
in this one app, configured from one settings screen (**App APIs**, a Single
doctype at `/app/app-apis`). It defines no business doctypes of its own for
the ticketing/vehicle side: `xticket` and `Customer Vehicle` belong to the
site, and this app only owns its connectors, its Fleet Audit snapshot, and a
handful of small support tables.

Every settings field is prefixed by which connection it belongs to
(`pilot_`, `im_`, `chatwoot_`...) so two connectors that both happen to have a
"base URL" and a "request timeout" can never silently share a value. Every
section on the settings form is collapsible.

---

### Pilot GPS -- three ways in, on purpose

Pilot is reached three different ways, and they are not interchangeable:

| Module | Auth | Scope | Used for |
|---|---|---|---|
| `connector.py` ("Pilot Connection") | Basic, per ticket | Whatever Pilot account the **customer** owns (read off the ticket's `email_pilot` or the Customer's own email fields) | "Check Pilot" on an xticket; live per-vehicle reads anywhere else in the app (Fleet Audit's Pilot button) |
| `pilot_admin.py`, REST v3 ("Pilot Admin Connection") | Bearer token | The **signed-in admin account's own fleet only** -- proven not to see other accounts' vehicles | Partner-level admin tasks: list accounts, list a user's vehicles, plate→IMEI lookup |
| `pilot_admin.py`, Administrator API (`/backend/api.php`) | Basic | **The whole estate** the partner administers, in one call | Fleet Audit's bulk Pilot read |

Two Administrator API accounts can be configured (**Pilot Admin Connection --
Pilot (WSL)** and **Pilot Admin Connection 2**). They are two separate
estates, not a primary and a backup -- a device only needs to be visible to
one of them to count as "on Pilot", and the Fleet Audit table shows each one
its own column so a finding is traceable to the right estate.

The legacy per-ticket connection and the first Admin connection are the same
estate (Pilot (WSL)); a second Admin account, if configured, is for an
unrelated one.

Why not just use the fast Administrator sweep for everything? Because a
partner-admin login can enumerate the whole estate but cannot read a live,
detailed snapshot (speed, ignition, sensors, GPS fix) for an arbitrary device
in it -- only the device's own customer account can. So the bulk sweep tells
you *whether* a device is on Pilot; the per-customer live read tells you
*what it's doing right now*, and can fail on its own (a stale customer
password) even when the sweep says the device is there. Both dialogs in Fleet
Audit fall back to "last known, from the last Fetch All" rather than showing
nothing when that happens.

### IM (gps.im2m.ws)

One site-wide account (`im_connector.py`, "IM Connection"). Live reads are
IMEI-only and rate-limited to roughly one call a minute, so results are
cached and a repeated read within the window is served from cache rather than
re-asked. `fetch_fleet()` enumerates the whole tracked fleet in one call with
an empty filter body (undocumented, verified empirically) -- used by Fleet
Audit; `get_snapshot`/`get_vehicle_live` are the live per-device reads behind
"Check IM" on a ticket and the Fleet Audit IM button, respectively.

### Chatwoot

Two independent send routes (`chatwoot_connector.py`), "Chatwoot Connection"
and "Chatwoot Connection 2 (Alternative)" -- the active one is chosen by
`chatwoot_active_connector`. Connector 2 can also run in `webhook` mode
(POST the message to your own URL) instead of talking to the Chatwoot API
directly. `sync_inboxes` pulls every inbox on the account into the **Chatwoot
Inboxes** table, pre-enabling the WhatsApp ones; the "Channel" column there
is just Chatwoot's own label for the inbox type (kept editable -- nothing
downstream reads it, it exists for the operator's own reference).

Message templates (Accepted / Work Started / Feedback Request, and the
Technician equivalents) live in their own sections, Arabic and English
together, with placeholders like `{ticket} {customer} {vehicle} {link}`.
`render_message` lets a Client Script preview the exact text before sending.

### Automatic messages, subscription reminders, do-not-contact

- **Automatic Messages**: messages a customer as their ticket's status
  changes, per a `Status Rules` table (`auto_messages.py`).
- **Subscription Reminders**: a scheduled scan of `Customer Vehicle` that
  warns a customer before (and after) their tracking subscription expires,
  one message per customer listing every plate (`subscription_reminders.py`).
  Dry Run stays on until the log looks right.
- **Excluded Customers** / **App Apis Do Not Contact**: every automatic
  message -- ticket stage or subscription reminder -- checks this list first
  (`do_not_contact.py`). An agent can add a row from either place.

### Fleet Audit -- the three-way reconciliation

`fleet_audit.py` + the Fleet Audit block on the desk (a self-contained Custom
HTML Block named "Fleet Audit" -- no separate Page, no file under
`public/js`; its `html`/`style`/`script` fields carry the whole UI).

**What it answers**: for every device the ERP, Pilot and IM each know about,
is it actually where the ERP thinks it is? One snapshot table,
`app_apis_fleet_audit`, rebuilt each time by:

1. Reading `Customer Vehicle` (one SQL query).
2. Reading IM's whole tracked fleet (one call).
3. Reading every enabled Pilot Administrator account's whole estate (one call
   each), merged into a single by-IMEI map -- a device is "on Pilot" if
   *either* configured account has it, and the table also shows which
   account(s) specifically (`On Pilot (WSL)` / `On Pilot 2`).
4. Joining all three on `Customer Vehicle.device_serial` (the IMEI) and
   classifying each row: `OK`, `Missing on Pilot`, `Missing on IM`, `Missing
   on both`, `Deleted but still live`, `Unexpected platform`, `Not in ERP`,
   `No device serial`, `Unmatchable ID` (Pilot's own device id is sometimes a
   10-digit registration number or a SIM-length id, never a real IMEI), or
   `Not checked` (a platform that could not be read this run produces no
   finding at all, rather than a false "Missing").

**SIM status (Lebara)**: "Upload SIM Export" takes a Lebara `.xlsx` and merges
SIM Status + last-connection date onto each row -- matched by IMEI first,
by Pilot's own MSISDN as a fallback. The SIM data lives in its own table
(`App Apis Sim Import`) so it survives the next "Fetch All" refresh, which
would otherwise wipe it along with the rest of the rebuilt snapshot.

**Live per-device checks**: pressing a Pilot or IM round button in the table
fetches that ONE device's current status right now -- the same call and the
same two-tier (checklist + map, "Show all" for the full dump) dialog as
"Check Pilot"/"Check IM" on an xticket, including the live ignition test
("Check Now": ask for the opposite value, then poll until it flips or the
window runs out -- proof the ACC wire actually works, not just that a value
exists). Pilot's live check always signs in as the **customer's** own
account (`connector.get_vehicle_live`), never the admin connection -- the
admin login cannot see an arbitrary customer's device in detail. If that
fails (most often a stale customer password), the dialog falls back to
whatever the last Fetch All already recorded for that row instead of
showing nothing.

**Filtering**: every column in the table filters -- text boxes for
IMEI/Plate/Customer, dropdowns for everything with a fixed set of values, and
From/To date pickers for the three "last update" columns (Pilot, IM, SIM).
"Export CSV" downloads the current filtered set.

**Never writes to ERPNext, Pilot or IM.** Every read in this module is a
read; the only table it writes is its own snapshot. An audit that could "fix"
what it found is an audit nobody could trust to run unattended.

---

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench install-app app_apis
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/app_apis
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
