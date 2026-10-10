# app_apis core — build on top of it

Everything the buttons and scripts do to Pilot (WSL), Pilot 2, IM and WASL is available as plain Python
functions in `app_apis/core/`. You can write your own app or Server Script on top of them and never edit
app_apis.

```python
from app_apis.core import platforms, events, messages, im, pilot_panel
```

`platform` is always one of `"pilot_wsl"`, `"pilot2"`, `"im"`.

## 1. The verbs — `app_apis.core.platforms`

Plain Python, **no role check** (you are code, you decide who may call you). Nothing raises for a
platform-side failure; each function returns a dict.

| Function | What it does | Key result fields |
|---|---|---|
| `find(platform, imei, vehicle="")` | Is the device there? (IM: give `vehicle` to also get its IM id) | `state` yes/no/unknown, `found`, `id`, `name`, `detail` |
| `build(platform, vehicle)` | Resolve everything a create would send, send nothing | `payload`, `gaps` |
| `create(platform, vehicle, dry_run=False)` | Put a Customer Vehicle on the platform; reads it back before saying uploaded | `verdict`, `ok`, `result`, `agent_id` / `vehicle_id`, `sensors` |
| `delete(platform, vehicle, pin="", update_status=True)` | Take it off the platform; sets Device Statues when nothing is left | `verdict`, `ok`, `result`, `status_changed`, `status_note` |
| `block(platform, imei, blocked=True)` | Pilot's reversible off switch (IM has none) | `ok`, `result` |
| `live(platform, imei, vehicle="")` | Live snapshot through the existing connectors | the connector's own dict |
| `wasl_status(imei)` | Live WASL state of a Pilot (WSL) device | `state` not_on_pilot / not_linked / saved_not_registered / registered_inactive / linked / unknown |
| `wasl_check(vehicle)` | WASL's own inquiry, nothing changed | `state`, `detail` |
| `wasl_link(vehicle, form, register=True)` | Save the WASL form and register | the link result |
| `wasl_delete(vehicle)` | Delete from WASL **only** (Pilot vehicle stays) | `deleted`, `msg` |
| `summary(vehicle)` | What is already stored about where it is (no platform called) | `where`, `wasl`, `sim`, `issues` |

Verdicts: `uploaded`, `duplicate`, `failure`, `error`, `dry_run` (create) and `deleted`, `not_deleted`,
`failure`, `error` (delete). `unknown`/`error` always means "the platform did not answer" — never read it as
"not there". `create` only says `uploaded` after reading the device back.

### Device Statues after a platform delete
After a successful delete the vehicle's Device Statues becomes **Deleted** (with today's Deletion Date) when no
other *system* is left. Systems, from the Platforms section: **Pilot** (Pilot WSL, Towing, SFDA, Tracking Only
are one system), **IM Tracking**, **SARP**, **FMSI Medicine**, **FMSI Balady**. Pilot counts as left only if the
vehicle is still on the *other* Pilot estate (looked up live). The tick box of the platform just deleted from is
cleared. WASL-only deletes and SIM suspends never touch the status. Pass `update_status=False` to skip it.

## 2. Events — react without changing app_apis

In **your** app's `hooks.py`:

```python
app_apis_events = {
    "before_delete": ["my_app.handlers.may_delete"],      # raise to veto
    "after_delete":  ["my_app.handlers.vehicle_deleted"],
    "after_create":  ["my_app.handlers.vehicle_created"],
}
```

```python
def vehicle_deleted(event, platform, vehicle, imei, result, source, user, **data):
    ...
```

| Event | When | Extra keys |
|---|---|---|
| `before_create` / `before_delete` | just before; a handler may **raise** to veto | `payload` (create) |
| `after_create` / `after_delete` | after, with the outcome | `result` |
| `after_block` | a Pilot device was blocked/unblocked | `blocked`, `result` |
| `after_status` | Device Statues was changed by a delete | `status`, `note` |
| `after_wasl` | a WASL action finished | `action` link/delete/check, `result` |

Every event carries `event, platform, vehicle, imei, source, user`. `source` is `"core"` (done through
`app_apis.core.platforms`), `"api"` (through `app_apis.core.api`) or `"script"` (the vehicle_upload_api Server
Script reporting a button action). `after_*` handler errors are logged and never break the action.

## 3. Custom messages — `app_apis.core.messages`

In **app_apis settings → Custom Messages** add rows: **Code** (e.g. `BS`), **Message**, **Template** (a synced
WhatsApp template; its variables go in *Template Variables*, one per line like `{{1}} = {customer}`).

```python
messages.render("BS", {"customer": "Ali", "plate": "1234 - ABC"})        # text only
messages.send("BS", "+9665XXXXXXXX", {"customer": "Ali"})               # sends
messages.send("BS", phone, ctx, dry_run=True)                           # says what would go
```

Codes are tidied (upper-case) and must be unique. A row with a template goes out *as* that template (the
WhatsApp 24-hour window is closed for almost everyone); a row without one goes as text while the window is open.
Placeholders are `{names}`; an unknown one is left visible so a typo shows.

## 4. The lower layers

* `app_apis.core.im` — IM's web console: `login`, `companies`, `branches`, `first_branch`, `company_login`,
  `has_imei`, `name_taken`, `find_by_name`, `vehicle_fields`, `sensors_for`, `create`, `delete`, `pin_needed`,
  `check_pin`, `account_for_email`, `sync_customers`.
* `app_apis.core.pilot_panel` — Pilot's admin panel per estate (`account` 1 = WSL, 2 = Pilot 2): `token`,
  `call`, `find`, `accounts`, `account_by_email`, `vehicle_types`, `resolve_model`, `ensure_sensors`, `create`,
  `delete`, `api_post`, `set_block`, `wasl_row`.
* Existing and unchanged: `app_apis.connector` (Pilot live), `app_apis.pilot_admin` (Administrator API),
  `app_apis.im_connector` (IM's official webservice), `app_apis.wasl` (WASL link/delete + hourly list copy).

IM has **no hook / webhook / push** that this app can subscribe to (checked: no such screen in the console; the
only "callback URL" fields belong to its SMS-gateway settings). IM is request/response and polling only; the
official webservice is limited to about one call a minute.

## 5. Front door for scripts and the browser — `app_apis.core.api`

Whitelisted, role-checked wrappers (System Manager / Technical; reads also Support Team):
`find, build, create, delete, block, live, wasl_status, wasl_check, wasl_delete, summary, message_codes,
message_render, message_send, notify`.

```python
frappe.call("app_apis.core.api.create", platform="pilot2", vehicle=name)                   # dry run!
frappe.call("app_apis.core.api.create", platform="pilot2", vehicle=name, dry_run=0)         # really
frappe.call("app_apis.core.api.delete", platform="im", vehicle=name, confirm="DELETE")
```

Safe defaults: `create` is a dry run unless `dry_run=0`; `delete` does nothing without `confirm="DELETE"`.
`notify(event, platform, vehicle, imei, detail)` lets a script that did something *itself* tell the event
handlers (only the `after_*` events, `source="script"`).

## 6. Lebara — `app_apis.core.lebara`

Every request this app knows how to make to Lebara B2B, as plain functions (the portal has no API: these are
the requests its own pages make, inside the logged-in cookie jar). What Lebara holds, verified against the
live portal:

| Data | Function | Notes |
|---|---|---|
| the SIMs | `iter_subscribers`, `subscribers_page`, `find_sim`, `sim_full`, `sim_detail` | 30,000 SIMs in ~20 s; extra columns: IMEI date, usage per day/week, APN, registration |
| **the bill** | `fetch_invoice(year, month)`, `sync_invoice` | the invoice Excel has one line per SIM: MSISDN, plan, status, **amount** |
| **history** | `iter_transactions`, `sync_transactions` | every activate / suspend / resume / deactivate, who asked, how it ended (23,528 rows) |
| groups, SMS | `groups`, `sms_history`, `sms_query` | |
| changes | `sim_action("SuspendSIM"|"ResumeSIM"|"ActivateSIM", id)`, `send_sms` | the only two functions that change Lebara |
| session | `request_otp`, `submit_otp`, `ping`, `logout`, `status` | |

Not available to this account (AccessDenied): Offer, Customer, SubCustomer. The plan is therefore the
**invoice's Tariff Plan**, not a list column.

Mirrors the dashboard reads (all filled by the "Lebara SIM Sync" and "Lebara History Sync" scripts, or on
demand with `lebara.sync("sims" | "invoice" | "transactions")`):

* `Lebara SIM` — the list, each SIM matched to its ERP vehicle, stamped `synced_at` on every sync that still
  lists it (a SIM Lebara drops keeps its old stamp).
* `Lebara Invoice` / `Lebara Invoice Line` — one header per month and one line per SIM per month.
* `Lebara Transaction` — Lebara's own action history, read incrementally (a still-pending one is re-read).

`lebara.loss_report("2026-09")` answers the first dashboard question from those mirrors: what was billed,
how much of it has no ERP vehicle, how much sits behind a vehicle the Fleet Audit's waste rule calls dead
(the `fleet_audit_waste` script), how much is billed on a non-Active SIM.

Front door: `app_apis.core.api.lebara_status / lebara_find / lebara_sync / lebara_invoice / lebara_loss /
lebara_sim_action` (live Lebara calls need a System Manager; `lebara_loss` and `lebara_status` read the
mirrors and are open to Support Team).

## 7. Things to know

* **IM needs a System Manager login.** Its calls go through `app_apis.core.http`, which is limited to that
  role. Pilot calls work for any code that can read the app_apis settings.
* **Pilot create goes through the admin panel** (the Administrator API's `vehadd` refuses every folder on the
  WSL estate). Sensors are the six standard ones from the template in `pilot_panel.SENSOR_TEMPLATE`.
* **IM vehicles always go to the company's first branch** in IM's dropdown; the company is found from the
  vehicle's `im_platform` email (cached on the Customer), never by name.
* **WASL** is reached through the Pilot (WSL) admin account (account 1). A vehicle that exists only on Pilot 2
  cannot be seen there.
* **IM sensors** come from a template vehicle (`im.TEMPLATE_VEHICLE`) or the built-in minimum
  (`im.DEFAULT_SENSORS`). A device model with neither cannot be created until one is added.
* **Settings, not code:** IM admin id, default reseller, resellers to scan, SIM provider and the device-model
  table are in app_apis settings → IM. Pilot model overrides are `pilot_panel.MODEL_OVERRIDES`.
* Tests: `bench --site <site> run-tests --app app_apis` (or `python -m unittest app_apis.tests.test_core_platforms`).
