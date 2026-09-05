# app_apis — install & test

Pulls live GPS/telematics data from the **Pilot GPS service** into ERPNext.

## Architecture — two components, nothing between them

```
Client Script (browser)
      |  frappe.call("app_apis.connector.get_snapshot", { ticket })
      v
app_apis.connector.get_snapshot()      <- the whole job lives here
      |    reads xticket + Customer, resolves IMEI + accounts,
      |    opens the connection, parses the payload
      |  HTTP GET (requests, Basic Auth)
      v
https://ksa.pilot-gps.com/api/api.php
```

**There is no Server Script.** The RestrictedPython sandbox has no `import`, no
`type`, no `getattr`, and the HTTP helper bound into its namespace is not
dependable across builds — `exec_safe_globals()` binds
`frappe.integrations.utils.make_get_request` while `render_safe_globals()` binds
the address-checking `make_safe_get_request`, and either can be missing. On top
of that, Pilot answers with `content-type: text/json`, which `make_get_request`
refuses to JSON-decode: it hands back a raw **string**, and the next
`body.get("code")` surfaces as the opaque `'NoneType' object is not callable`.
`requests.Response.json()` ignores content-type, so this app never has that
failure mode.

Set `server_script_enabled: 0` in `common_site_config.json` to keep it that way.

## File structure

```
apps/app_apis/
├── pyproject.toml                 flit build config; declares `requests`
├── INSTALL.md                     this file
└── app_apis/
    ├── hooks.py                   app metadata + the Client Script fixture
    ├── connector.py               ►► the entire integration ◄◄
    ├── modules.txt                "App Apis"
    ├── patches.txt
    ├── fixtures/
    │   └── client_script.json     the "Check Pilot" button, shipped with the app
    └── app_apis/doctype/pilot_api_settings/
        ├── pilot_api_settings.json    Single doctype: base_url, node,
        ├── pilot_api_settings.py      pilot_password, fallback_username,
        └── pilot_api_settings.js      request_timeout, stale_after_minutes
```

`connector.py` in one glance:

| symbol | whitelisted | role |
|---|---|---|
| `get_snapshot(ticket, imei=None, node=None)` | **yes — the only one** | the whole job |
| `_fetch_status(imei, email, node, settings)` | no | one HTTP call, one account |
| `_resolve_imei` / `_collect_emails` | no | pull inputs out of the ERP |
| `_parse_sensors` / `_parse_location` | no | shape the Pilot payload |
| `status()` | no | `bench console` introspection |

No business doctypes are defined here — `xticket`, `Customer Vehicle` and the
rest belong to the site. The app owns only its own config doctype and the
Client Script.

## Install

```bash
cd ~/frappe-bench

# 1. fetch the app into the bench
bench get-app app_apis /path/to/app_apis      # or a git URL

# 2. install it on the site (creates Pilot API Settings, loads the fixture)
bench --site <site> install-app app_apis

# 3. production posture: no sandboxed scripts, no dev mode
bench set-config -g developer_mode 0 --parse
bench set-config -g server_script_enabled 0 --parse
bench --site <site> set-config pilot_offline 0 --parse   # 1 = canned fixtures

bench --site <site> migrate
bench restart          # or: supervisorctl restart web worker scheduler
```

`bench restart` is **not optional** after editing `connector.py`: gunicorn
workers hold the imported module in memory, so a fresh `bench console` will
show new code while the HTTP endpoint still runs the old.

Then fill in **Pilot API Settings** (`/app/pilot-api-settings`):

| field | value |
|---|---|
| Base URL | `https://ksa.pilot-gps.com/api/api.php` |
| Node | `5` |
| Pilot Password | the shared Pilot password (stored encrypted) |
| Fallback Username | used only when neither ticket nor customer has an email |
| Request Timeout | `20` seconds |
| Stale After | `15` minutes |

### Per-account passwords

One password is currently tried against every candidate email. If Pilot uses a
different password per account, add a map to `site_config.json` — no schema
change needed:

```json
"pilot_passwords": { "fleet.tow@example.com": "…", "fleet.sfda@example.com": "…" }
```

`_password_for()` checks that map first and falls back to the shared password.
If per-account passwords become the norm rather than the exception, promote them
to a child table on Pilot API Settings (`pilot_email` + `pilot_password`) so they
are encrypted at rest and editable from the desk.

## Testing each layer independently

**Layer 1 — raw curl (does the credential work at all?)**

```bash
curl -i "https://ksa.pilot-gps.com/api/api.php?cmd=status&node=5&imei=<IMEI>" \
  -u "REAL_PILOT_EMAIL:REAL_PILOT_PASSWORD"
```

`200` + JSON → good. `401` → the app will hit the same wall; fix the account
with the Pilot provider first (valid email/password, correct `node`, IMEI
registered under that account). No code change can work around a 401.

**Layer 2 — the app, with no browser**

```bash
bench --site <site> console
```
```python
from app_apis import connector
connector.status()                              # config as the app sees it
connector.get_snapshot("ISSS-202608-00001")     # the full result
```

**Layer 3 — the HTTP endpoint the browser actually calls**

```bash
curl -s -X POST -H "Authorization: token <api_key>:<api_secret>" \
     -H "Content-Type: application/json" \
     -d '{"ticket":"ISSS-202608-00001"}' \
     "http://localhost:8080/api/method/app_apis.connector.get_snapshot"
```

**Layer 4 — the button.** Open any `xticket`, click **Check Pilot**.

## Offline mode

`pilot_offline: 1` (the default) serves canned payloads from `connector.py` and
opens no socket, so the whole chain can be exercised with no network and no
credentials. `connector.status()` reports which mode is active, and the result's
`diagnostics.offline` flag propagates all the way to the dialog footer.

## Notes on real payloads

- Pilot returns numbers as strings (`"lat": "21.3627566"`) and HTML-encodes some
  text (`Bakery &amp; Sweets`). The app coerces the former; the Client Script
  decodes the latter before escaping it.
- Sensor readings mix numbers with words (`"High"`, `"On"`). `frappe.utils.flt`
  turns those into a confident, wrong `0.0`, so `_as_number()` returns `None`
  instead and `display` carries the original text.
- Probe pairing prefers the `(T1)`/`(H1)` suffix. Accounts that report flat
  `Temperature`/`Humidity` names get keyword pairing instead, and the response
  says which rule fired via `probe_naming` (`label` / `keyword` / `none`).
