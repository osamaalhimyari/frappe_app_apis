# app_apis — the IM (gps.im2m.ws) integration

Pulls live GPS/telematics data from the **Intelligent Machines** platform into
ERPNext.

IM is the **only** integration in this app, every read is a **live** call, and
every lookup is **by IMEI**. There is no second provider, no offline stand-in and
no plate/company fallback. The former Pilot connector (`connector.py`,
`Pilot API Settings`, `xticket-check-pilot`) has been removed.

## Architecture — two components, nothing between them

```
Client Script "xticket-check-im" (browser)
      |  frappe.call("app_apis.im_connector.get_snapshot", { ticket })
      v
app_apis.im_connector.get_snapshot()      <- the whole job lives here
      |    reads the xticket's IMEI, signs in once, calls IM by imei_nos,
      |    parses the payload
      |  HTTP POST (requests, JSON, `auth-code` header)
      v
https://gps.im2m.ws/webservice
```

**There is no Server Script**: the RestrictedPython sandbox has no `import`, and
its HTTP helper cannot POST a JSON body with a custom header at all — which is
exactly what IM requires.

## Files

```
apps/app_apis/app_apis/
├── im_connector.py                        ►► the entire integration ◄◄
├── public/im_script.js                    the front end, as a plain file
├── fixtures/client_script.json            the same script, as an app fixture
└── app_apis/doctype/im_settings/
    ├── im_settings.json                   Single doctype (see below)
    ├── im_settings.py                     validation + cache invalidation
    ├── im_settings.js                     "Test Connection" / "Clear Cached Token"
    └── test_im_settings.py
```

`im_connector.py` in one glance:

| symbol | whitelisted | role |
|---|---|---|
| `get_snapshot(ticket, imei=None)` | **yes** | the whole job |
| `get_track_logs(ticket, start_date=None, end_date=None, imei=None, hours=None)` | **yes** | history for the map overlay |
| `test_connection()` | yes | the button on IM Settings |
| `clear_cache_from_desk()` | yes | drop token + snapshots |
| `_get_token` / `_fetch_live` / `_cached_or_fetch` | no | HTTP + rate-limit guard |
| `_resolve_imei` | no | pull the IMEI out of the ERP |
| `_pick_vehicle` | no | confirm IM answered about *that* IMEI |
| `_shape` / `_parse_ports` / `_epoch` | no | normalise the IM payload |
| `status()` | no | `bench console` introspection |

## The three IM endpoints this wraps

All on `https://gps.im2m.ws/webservice`, all `POST`, command in `?token=`:

| command | auth | body | used by |
|---|---|---|---|
| `generateAccessToken` | none | `{username, password}` | `_get_token` |
| `getTokenBaseLiveData` (+ `&ProjectId=`) | `auth-code: <token>` | `{imei_nos}` — never `vehicle_nos` or `company_names` | `get_snapshot` |
| `getVehicleTrackLogs` | `auth-code: <token>` | `{vehicle_no, start_date, end_date}` | `get_track_logs` |

`getVehicleTrackLogs` is the one command IM insists on keying by vehicle number.
That number is taken from the **live row for the IMEI**, not from the ticket's
plate fields — it is IM's own spelling for exactly that device.

## Install

Already installed on this bench. On a new one:

```bash
cd ~/frappe-bench
bench --site <site> migrate      # creates IM Settings
bench restart                    # gunicorn holds the old module in memory
```

`bench restart` is **not optional** after editing `im_connector.py`.

Then fill in **IM Settings** (`/app/im-settings`) and press **Test Connection**:

| field | value |
|---|---|
| Base URL | `https://gps.im2m.ws/webservice` |
| Project ID | `37` |
| IM Username | the one platform account |
| IM Password | its password (stored encrypted) |
| Request Timeout | `20` seconds |
| Stale After | `15` minutes |
| Data Timezone | blank = the site's System Timezone (`Asia/Riyadh` here) |
| Minimum Call Interval | `60` seconds |
| Token Cache TTL | `45` minutes |
| Ignition Test Poll | `90` seconds |
| Ignition Test Window | `10` minutes |

There is no offline switch to flip: the app is live-only, so a working
credential is a hard requirement rather than a deployment step.

The password may instead live in `site_config.json` as `im_password`, which
takes precedence over the encrypted field and keeps the secret out of the
database.

### The Client Script ships as a fixture

`fixtures/client_script.json` holds `xticket-check-im`, and `hooks.py` lists it:

```python
fixtures = [
    {"dt": "Client Script", "filters": [["name", "in", ["xticket-check-im"]]]},
]
```

so a fresh `bench install-app app_apis` creates the button. The filename must
stay `client_script.json` — Frappe looks the fixture file up by scrubbed doctype
name, so a differently-named file is silently never imported.

Regenerate it after editing the script in the desk with
`bench --site <site> export-fixtures --app app_apis`.

## One account, not many

IM signs in with a single username and password for the whole site, held in IM
Settings. There is no candidate loop and no per-customer email lookup.

## IM's rate limit is a design constraint, not an edge case

IM answers a live call that arrives too soon with:

```json
{"root": {"error": "The call exceeded the limit of one/two minute one call."}}
```

so the connector is built around it:

1. A snapshot younger than `min_call_interval_seconds` is served from cache and
   IM is not called at all.
2. If IM does report the limit and a previous payload is cached, that payload is
   returned with `diagnostics.throttled = true` — a two-minute-old position is
   worth more to a technician than an error dialog. The dialog shows a `CACHED`
   pill and names the age, so it is never mistaken for a live reading.
3. With a cold cache, a rate-limit answer is raised, never swallowed.

This is also why the ignition test polls every `ignition_poll_seconds` (90s by
default) over a 10-minute window. The browser does not choose the rate — the
server sends it as `poll_interval_seconds`, read from IM Settings.

## Testing each layer independently

**Layer 1 — raw curl (does the credential work at all?)**

```bash
TOKEN=$(curl -s 'https://gps.im2m.ws/webservice?token=generateAccessToken' \
  -H 'Content-Type: application/json' \
  -d '{"username":"REAL_USER","password":"REAL_PASS"}' | jq -r .data.token)

curl -s 'https://gps.im2m.ws/webservice?token=getTokenBaseLiveData&ProjectId=37' \
  -H 'Content-Type: application/json' -H "auth-code: $TOKEN" \
  -d '{"imei_nos":"<IMEI>"}'
```

A token plus `root.VehicleData` → good. `Incorrect username password..` → the app
will hit the same wall; no code change works around it.

**Layer 2 — the app, with no browser**

```bash
bench --site <site> console
```
```python
from app_apis import im_connector as im
im.status()                                  # config as the app sees it
im.test_connection()                         # sign in only
im.get_snapshot("ISSS-202608-00001")         # the full result (by IMEI)
im.get_track_logs("ISSS-202608-00001", hours=6)
```

**Layer 3 — the HTTP endpoint the browser actually calls**

```bash
curl -s -X POST -H "Authorization: token <api_key>:<api_secret>" \
     -H "Content-Type: application/json" \
     -d '{"ticket":"ISSS-202608-00001"}' \
     "http://localhost:8080/api/method/app_apis.im_connector.get_snapshot"
```

**Layer 4 — the button.** Open any `xticket`, click **Check IM**.

## No offline mode

Every read is a live call to IM. There are no canned payloads and no
`im_offline` switch: if IM is unreachable or the credential is rejected, the
dialog says so rather than showing stand-in data that reads as real.

## Notes on real payloads

- IM returns numbers as strings (`"Speed": "63"`) and HTML-encodes some text
  (`Bakery &amp; Sweets`). The app coerces the former; the Client Script decodes
  the latter before escaping it.
- Ports are **tri-state**. An unwired input reads `"--"`, which is *not* OFF.
  `_as_bool` returns `None` for it, and the UI shows `—`, never a confident OFF.
  Unwired ports are listed separately so "Door 3 is not wired" stays visible as
  a finding rather than disappearing.
- `Temperature` is a bare number off the temp port; the app appends `°C`.
- `Odometer` is reported in **metres** (`57260700` = 57 260 km). Both the raw
  figure and the km conversion are published.
- Timestamps (`28-09-2020 22:43:29`, `DD-MM-YYYY`) carry no timezone. They are
  read in `data_timezone`, defaulting to the site's System Timezone. Getting
  this wrong shifts "last seen" by whole hours — hence the setting.
- `Latitude`/`Longitude` of `0`/`0` means "never had a fix"; the app nulls it
  rather than dropping a pin in the Gulf of Guinea.
- **Lookup is by IMEI, always.** `_resolve_imei` reads `xticket.device_serial`
  (then the other device fields, then `Customer Vehicle.device_serial`) and
  sends only `imei_nos`. There is no plate fallback: IM matches `vehicle_nos` as
  a literal string and this site spells the same plate several ways, so a plate
  lookup can succeed against the *wrong* vehicle with nothing on screen to
  betray it. A ticket with no IMEI is an error, not a reason to guess.
- An explicit `imei=` override is **authoritative** — the app will not
  substitute another identifier from the ticket.
- `_pick_vehicle` requires the returned row's `Imeino` to equal the IMEI asked
  about. If IM answers with a different device, the app refuses rather than
  reporting it as this ticket's vehicle.
- The `getVehicleTrackLogs` response schema is not documented anywhere IM
  publishes, so `_extract_points` searches the response for the first list whose
  rows carry latitude-shaped keys, and `raw` always carries the original.
```
