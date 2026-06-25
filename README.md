# Universal DDI Site Toolkit

Tag-driven site, address-block, and DNS provisioning, decommissioning,
querying, and template authoring for **Infoblox Universal DDI**.

The toolkit is packaged as `uddi_toolkit` and exposes a single `uddi`
command with subcommands:

| Command | Purpose |
|---------|---------|
| `uddi provision {site\|address-block\|dns}` | Create resources from a template |
| `uddi decommission {site\|address-block\|dns}` | Tear them down |
| `uddi query {site\|address-block\|dns}` | Read-only inspection |
| `uddi validate -t FILE` | Structural template validation (no API calls) |
| `uddi drift -t FILE` | Compare a site template against live state |
| `uddi batch --action ...` | Provision/decommission many templates |
| `uddi retag-block ...` | Reset an address block's lifecycle Status |
| `uddi web` | Browser-based UI for templates and execution |

All commands share the same YAML template format and INI configuration file.

---

## Installation

```bash
# from the project root (use a virtualenv)
pip install -e .
```

This installs the `uddi` console command. You can also run it without
installing via `python -m uddi_toolkit ...` (with `src/` on `PYTHONPATH`).

> **Migration note (v2.0):** the former top-level scripts moved into the
> `uddi_toolkit` package and are now subcommands. Examples elsewhere in this
> document written as `python3 <script>.py ...` map to the `uddi` CLI:
>
> | Old | New |
> |-----|-----|
> | `python3 provision_site.py -t F` | `uddi provision site -t F` |
> | `python3 decommission_site.py -t F` | `uddi decommission site -t F` |
> | `python3 query_site.py -t F` | `uddi query site -t F` |
> | `python3 provision_block.py -t F` | `uddi provision address-block -t F` |
> | `python3 provision_dns.py -t F` | `uddi provision dns -t F` |
> | `python3 batch_provision.py ...` | `uddi batch ...` |
> | `python3 retag_block.py ...` | `uddi retag-block ...` |
> | `python3 web_server.py` | `uddi web` |
> | `python3 drift_detect.py -t F` | `uddi drift -t F` |

---

## How it works

Site definitions are driven by a YAML template (or CLI flags for quick
one-liners). Address blocks are discovered by metadata tags — no
hardcoded CIDRs required.

### Provisioning sequence

| Step | Action |
|------|--------|
| 1 | Resolve the configured IP space |
| 2 | **Discover** an available block matching `Region`, `Environment`, `Status=available` tags |
| 3 | Resolve the DNS view |
| 4 | **Carve** subnets per the plan (YAML template or built-in mgmt/lan/server default) |
| 5 | **Create** DHCP ranges for subnets with `dhcp: true` |
| 6 | **Update** the block — `Status=allocated`, `Site=<name>` |
| 7 | **Create** a forward DNS zone: `site-<name>.<dns_parent>` |
| 8 | **Create** reverse (in-addr.arpa) zones if `create_reverse_zone: true` |
| 9 | **Provision** all hosts defined in the template (IPAM + DNS A/PTR) |

On any failure, all resources created so far are automatically rolled
back unless `--no-rollback` is set.

### Decommissioning sequence

| Step | Action |
|------|--------|
| 1 | Resolve the configured IP space |
| 2 | **Discover** the site's block (`Site=<name>`, `Status=allocated`) |
| 3 | Resolve the DNS view |
| 4 | **Enumerate** all subnets inside the block |
| 5 | **Delete** all IPAM host records (removes DNS A/PTR automatically) |
| 6 | **Delete** DHCP ranges within each subnet |
| 7 | **Delete** the forward DNS zone (unless `--keep-zone`) |
| 8 | **Delete** reverse DNS zones if present |
| 9 | **Delete** all site subnets |
| 10 | **Reset** the block — `Status=available` (default; `decommissioned` with `--final-status`), `Site=unassigned` |

All destructive steps support `--dry-run`.

---

## Prerequisites

```
Python 3.10+
pip install -e .        # installs the `uddi` command + deps (requests, PyYAML, flask)
```

The standalone template builder (`src/uddi_toolkit/web/site_template_builder.html`)
is a single self-contained HTML file — open it directly in any browser, no
server needed.

---

## Configuration

Copy `uddi.ini.example` to `uddi.ini` and fill in:

```ini
[UDDI]
api_key = <your-api-key>
url     = https://csp.infoblox.com

[DEFAULTS]
ip_space    = my-ip-space
dns_parent  = internal.example.com
dns_view    = default
owner       = network-team
subnet_size = 24
dhcp_start_offset = 10
dhcp_end_offset   = 250
```

> **Security:** `uddi.ini` is listed in `.gitignore` and will never be
> committed. Keep your API key out of source control.

All commands default to `uddi.ini` in the current working directory.
Override with `-c /path/to/file.ini`.

### Parameter precedence

```
CLI flags  >  YAML template  >  INI [DEFAULTS]  >  hardcoded fallbacks
```

---

## Template types

The toolkit supports three kinds of template, distinguished by a top-level
`type:` field (inferred from structure when omitted, for backward
compatibility):

| `type:`         | Manages                                  | Commands                                                            |
|-----------------|------------------------------------------|--------------------------------------------------------------------|
| `site`          | Address block + subnets + DNS + hosts    | `uddi {provision,decommission,query} site`                         |
| `address-block` | IPAM address blocks (with nested children) | `uddi {provision,decommission,query} address-block`             |
| `dns`           | Auth zones + standalone DNS records      | `uddi {provision,decommission,query} dns`                          |

`address-block` templates create the pool of blocks that `site` provisioning
later discovers by `Region` / `Environment` / `Status=available` tags.
`dns` templates manage zones and records directly (A, AAAA, CNAME, MX, TXT,
PTR). In the web UI the template type is shown as a badge and the available
actions adjust to the type. Drift detection currently applies to `site`
templates only.

---

## Site template schema (`type: site`)

```yaml
type: site                     # optional — inferred from the `site:` section
site:
  name:        london          # required
  region:      EMEA            # required
  environment: production      # required
  location:    "London, UK"    # optional

network:
  ip_space:    my-ip-space     # optional — overrides INI default
  subnet_size: 24              # default prefix length for subnets

  subnets:                     # optional — defaults to mgmt/lan/server /24
    - name:    london-mgmt
      purpose: mgmt            # mgmt | user-lan | server | dmz | storage | voice | iot | general
      dhcp:    false
      cidr:    24
    - name:    london-lan
      purpose: user-lan
      dhcp:    true
      dhcp_start: 10           # host offset from subnet base (default: INI dhcp_start_offset)
      dhcp_end:   250          # host offset from subnet base (default: INI dhcp_end_offset)
      cidr:    24
    - name:    london-server
      purpose: server
      dhcp:    false
      cidr:    24

dns:
  parent:              internal.example.com  # optional — overrides INI
  view:                default               # optional — overrides INI
  create_zone:         false                 # create zone if absent (default: false)
  create_reverse_zone: false                 # create in-addr.arpa zones (default: false)

hosts:                         # optional — defaults to single gw01
  - hostname: gw01
    subnet:   london-mgmt      # must match a name in subnets above
    comment:  "Site gateway"
  - hostname: dns01
    subnet:   london-server

tags:                          # extra tags applied to block + all subnets
  Owner:      network-team
  CostCentre: CC-1234
```

---

## Address-block template schema (`type: address-block`)

Creates IPAM address blocks (and optional nested children) to seed the
discovery pool. Every block is tagged `Template=<name>` so it can be found
again at decommission/query time. Child addresses must fall within their
parent.

```yaml
type: address-block
name: emea-prod-pool           # logical name; stamped as tag Template=<name>
ip_space: my-ip-space          # optional — overrides INI default

address_blocks:
  - address: 10.20.0.0
    cidr: 16
    region: EMEA               # -> tag Region   (site discovery filters on this)
    environment: production    # -> tag Environment
    status: available          # -> tag Status   (default: available)
    location: "EMEA"           # optional -> tag Location
    comment: "EMEA supernet"
    tags: {Owner: network-team}
    children:                  # optional, recursive
      - address: 10.20.0.0
        cidr: 18

tags:                          # template-wide tags merged onto every block
  CostCentre: CC-EMEA-001
```

Run: `uddi provision address-block -t templates/blocks/regional_address_blocks.yaml --dry-run -v`

---

## DNS template schema (`type: dns`)

Creates authoritative zones and standalone records via the `/dns/record`
API. Supported record types: **A, AAAA, CNAME, MX, TXT, PTR**. Use scalar
`rdata` shorthand for single-value types and a mapping for MX. `create: false`
adds records into a pre-existing zone without creating (or deleting) the zone.

```yaml
type: dns
view: default                  # optional — overrides INI dns_view

zones:
  - fqdn: corp.example.com
    kind: forward              # forward | reverse (default forward)
    primary_type: cloud        # default cloud
    create: true               # create zone if absent (default true)
    comment: "Corporate zone"
    records:
      - name: www              # name in zone; '@' or '' = apex
        type: A
        rdata: 10.20.1.10
        ttl: 3600              # optional
      - name: ftp
        type: CNAME
        rdata: www.corp.example.com
      - name: mail
        type: MX
        rdata: {preference: 10, exchange: mail.corp.example.com}

tags:                          # applied to created zones
  Owner: dns-team
```

Run: `uddi provision dns -t templates/dns/corp.yaml --dry-run -v`

---

## Tag schema

Address blocks must be pre-tagged before the provisioning script can
discover them.

| Tag | Required | Values | Description |
|-----|----------|--------|-------------|
| `Owner` | Yes | any string | Responsible team or individual |
| `Environment` | Yes | `production`, `lab`, … | Deployment tier |
| `Region` | Yes | `AMER`, `EMEA`, `APAC`, … | Geographic region |
| `Site` | Yes | site name / `unassigned` | Populated at provision time |
| `Status` | Yes | `available`, `allocated`, `decommissioned` | Lifecycle state |
| `Location` | No | any string | Human-readable location |
| `Provisioned` | No | ISO date | Set automatically at provision time |

Status lifecycle:

```
available  →  allocated  →  available
            (provision)    (decommission — default returns to the pool)

allocated  →  decommissioned
            (decommission --final-status decommissioned — retire the block)

decommissioned  →  available
                   (uddi retag-block --status available — re-enter the pool)
```

By default `uddi decommission site` returns the block to `available` so the
site can be re-provisioned. Pass `--final-status decommissioned` to retire a
block instead; a retired block is picked up by neither provision nor
decommission, so use `uddi retag-block` to return it to the pool.

---

## uddi provision

`uddi provision {site|address-block|dns} -t FILE` — creates resources from a
template. The flags below are for `site`; `address-block` and `dns` take
`-t/--template`, `--dry-run`, `--no-rollback`, the common credential/logging
flags, plus `--ip-space`/`--name` (block) or `--view` (dns).

### Usage

```
uddi provision site [-h] [-t FILE]
                    [-s NAME] [-r REGION] [-e ENV] [-l LOCATION]
                    [--subnet-size N] [--dns-parent ZONE]
                    [--dns-view VIEW] [--ip-space SPACE]
                    [--create-zone | --no-create-zone]
                    [--create-reverse-zone]
                    [--no-rollback]
                    [--dry-run] [-c FILE] [--api-key KEY] [--no-verify-ssl]
                    [-d | -v]
```

| Argument | Description |
|----------|-------------|
| `-t`, `--template` | Path to YAML site template |
| `-s`, `--site` | Short site name |
| `-r`, `--region` | Region tag to match on available blocks |
| `-e`, `--environment` | Environment tag to match |
| `-l`, `--location` | Human-readable location applied to the block |
| `--subnet-size` | Default subnet prefix length |
| `--dns-parent` | Parent DNS zone |
| `--dns-view` | DNS view name |
| `--ip-space` | IP space name |
| `--create-zone` | Create the site DNS zone if absent |
| `--no-create-zone` | Abort if the site DNS zone does not exist (safe default) |
| `--create-reverse-zone` | Create in-addr.arpa reverse zones per subnet |
| `--no-rollback` | Leave partial resources in place on failure |
| `--dry-run` | Preview all steps without making any changes |
| `-c`, `--config` | INI config file (default: `uddi.ini`) |
| `--api-key` | API key (overrides INI / env vars) |
| `--no-verify-ssl` | Disable SSL certificate verification |
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |

(`uddi -V` shows the toolkit version.)

### Examples

```bash
# Dry-run
uddi provision site -t templates/site-london.yaml --dry-run -v

# Live run
uddi provision site -t templates/site-london.yaml -v

# With DHCP ranges and reverse DNS zones
uddi provision site -t templates/site-london.yaml --create-reverse-zone -v

# CLI-only (no template)
uddi provision site -s london -r EMEA -e production -v

# Different config file
uddi provision site -t templates/site-london.yaml -c /etc/uddi/uddi.ini -v

# Seed the address-block pool, then a DNS zone+records
uddi provision address-block -t templates/blocks/regional_address_blocks.yaml -v
uddi provision dns -t templates/dns/corp.yaml -v
```

---

## uddi decommission

`uddi decommission {site|address-block|dns} -t FILE`. Flags below are for
`site`; `address-block`/`dns` take `-t`, `--dry-run`, `--force`, and the
common flags (plus `--ip-space`/`--name` or `--view`).

### Usage

```
uddi decommission site [-h] [-t FILE] [-s NAME]
                       [--final-status {decommissioned,available}]
                       [--keep-zone]
                       [--dns-parent ZONE] [--dns-view VIEW]
                       [--ip-space SPACE]
                       [--dry-run] [--force]
                       [-c FILE] [--api-key KEY] [--no-verify-ssl] [-d | -v]
```

| Argument | Description |
|----------|-------------|
| `-t`, `--template` | Path to YAML site template |
| `-s`, `--site` | Short site name to decommission |
| `--final-status` | Block status after teardown: `available` (default, returns to pool) or `decommissioned` (retire) |
| `--keep-zone` | Leave the forward DNS zone intact |
| `--dns-parent` | Parent DNS zone |
| `--dns-view` | DNS view name |
| `--ip-space` | IP space name |
| `--dry-run` | Preview all steps without making any changes |
| `--force` | Skip the interactive confirmation prompt |
| `-c`, `--config` | INI config file (default: `uddi.ini`) |
| `--api-key` | API key (overrides INI / env vars) |
| `--no-verify-ssl` | Disable SSL certificate verification |
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |

### Examples

```bash
# Dry-run
uddi decommission site -t templates/site-london.yaml --dry-run -v

# Live run (prompts for confirmation)
uddi decommission site -t templates/site-london.yaml -v

# Non-interactive (CI/pipeline)
uddi decommission site -t templates/site-london.yaml --force -v

# Retire the block instead of returning it to the pool
uddi decommission site -t templates/site-london.yaml --final-status decommissioned --force -v

# Recover a retired/stuck block back into the available pool
uddi retag-block --address 10.20.0.0 --cidr 16 -v
uddi retag-block --site london -v
```

---

## uddi query

Read-only inspection. Makes no changes to the infrastructure — safe to run
at any time. Works for `site`, `address-block`, and `dns` templates; flags
below are for `site`.

### Usage

```
uddi query site [-h] [-t FILE]
                [-s SITE] [--dns-parent ZONE] [--dns-view VIEW]
                [--ip-space SPACE] [--json]
                [-c FILE] [--api-key KEY] [--no-verify-ssl] [-d | -v]
```

| Argument | Description |
|----------|-------------|
| `-t`, `--template` | Path to YAML site template |
| `-s`, `--site` | Short site name |
| `--dns-parent` | Parent DNS zone |
| `--dns-view` | DNS view name |
| `--ip-space` | IP space name |
| `--json` | Emit machine-readable JSON instead of formatted text |
| `-c`, `--config` | INI config file (default: `uddi.ini`) |
| `--api-key` | API key (overrides INI / env vars) |
| `--no-verify-ssl` | Disable SSL certificate verification |
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |

### Examples

```bash
# Human-readable report
uddi query site -t templates/site-london.yaml -v

# Machine-readable JSON
uddi query site -t templates/site-london.yaml --json | python3 -m json.tool

# Inspect a block pool or a DNS template's zones/records
uddi query address-block -t templates/blocks/regional_address_blocks.yaml
uddi query dns -t templates/dns/corp.yaml --json
```

---

## uddi validate / uddi drift

```bash
# Structural validation only (no API calls) — works for all template types
uddi validate -t templates/dns/corp.yaml

# Compare a site template against live API state (site templates only)
uddi drift -t templates/site-london.yaml
```

`validate` exits non-zero if the template has schema errors. `drift` exits
`0` (no drift), `1` (drift detected), or `2` (site not provisioned).

---

## uddi batch

Runs provision or decommission sequentially across multiple templates of
**any type** (it picks the right command per template's `type:`). Each
template runs as a `python -m uddi_toolkit …` subprocess, so a single
failure does not prevent the remaining templates from being processed.

### Usage

```
uddi batch --action {provision,decommission}
           [--templates-dir DIR | --templates FILE [FILE ...]]
           [--dry-run] [--force] [--no-rollback]
           [--stop-on-error]
           [-c FILE] [--api-key KEY] [--no-verify-ssl] [-d | -v]
```

| Argument | Description |
|----------|-------------|
| `--action` | `provision` or `decommission` (required) |
| `--templates-dir` | Directory of `.yaml`/`.yml` templates to process |
| `--templates` | One or more explicit template paths |
| `--dry-run` | Forward `--dry-run` to each run |
| `--force` | Forward `--force` to decommission runs |
| `--no-rollback` | Forward `--no-rollback` to provision runs |
| `--stop-on-error` | Abort after the first failed template |
| `-c`, `--config` | INI config file (default: `uddi.ini`) |
| `--api-key` / `--no-verify-ssl` | Forwarded to each run |

### Examples

```bash
# Dry-run all templates in a directory (any mix of site/block/dns)
uddi batch --action provision --templates-dir templates --dry-run -v

# Provision specific templates
uddi batch --action provision \
    --templates templates/site-london.yaml templates/blocks/regional_address_blocks.yaml -v

# Decommission all, non-interactively, stop on first failure
uddi batch --action decommission \
    --templates-dir templates --force --stop-on-error -v
```

---

## uddi web

Flask-based web UI. Provides a browser interface for managing templates
and executing provision / decommission / query operations with real-time
streaming output. The UI shows each template's type as a badge and adjusts
the available actions accordingly.

### Usage

```
uddi web [-c CONFIG] [--templates-dir DIR]
         [-p PORT] [--host HOST] [--debug-flask]
         [--api-key KEY] [--no-verify-ssl] [-d | -v]
```

| Argument | Description |
|----------|-------------|
| `-c`, `--config` | INI config file (default: `uddi.ini`) |
| `--templates-dir` | Template directory (default: `./templates`) |
| `-p`, `--port` | Port to listen on (default: `5000`) |
| `--host` | Host to bind (default: `127.0.0.1`) |
| `--debug-flask` | Enable Flask debug/auto-reload mode |
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |

### Starting the server

```bash
uddi web -c uddi.ini -v
# Open http://127.0.0.1:5000/
```

Run it from the directory that holds your `uddi.ini` and `templates/`
(those paths resolve relative to the launch directory).

### API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve the web UI |
| `GET` | `/site_template_builder.html` | Serve the standalone offline builder |
| `GET` | `/api/templates` | List templates |
| `GET` | `/api/templates/<name>` | Get template YAML content |
| `POST` | `/api/templates/<name>` | Save template |
| `DELETE` | `/api/templates/<name>` | Delete template |
| `POST` | `/api/provision` | Stream provision execution (SSE) |
| `POST` | `/api/decommission` | Stream decommission execution (SSE) |
| `POST` | `/api/query` | Stream query execution (SSE) |
| `POST` | `/api/query-json` | Run query and return structured JSON |
| `POST` | `/api/validate` | Validate a template schema (no API calls) |
| `POST` | `/api/drift` | Compare a site template against live state |
| `GET` | `/api/config` | Return non-secret config values |
| `GET` | `/api/health` | Health check |

The web UI features a three-column layout:

- **Left** — template list with load, save, delete, and new
- **Centre** — tabbed Builder (form + live YAML preview) and Raw YAML editor
- **Right** — execution panel with action selector, toggles, streaming
  output terminal, and a structured query results view

---

## site_template_builder.html

Standalone browser-based form for authoring YAML templates. No web
server required — open directly in any browser.

Features: live YAML preview with syntax highlighting, dynamic
subnet/host/tag lists, download and copy-to-clipboard buttons, and
CLI command hints that update as you type.

```bash
# the file lives at src/uddi_toolkit/web/site_template_builder.html
open src/uddi_toolkit/web/site_template_builder.html       # macOS
xdg-open src/uddi_toolkit/web/site_template_builder.html   # Linux
start src/uddi_toolkit/web/site_template_builder.html      # Windows
```

It is also served by the running web UI at
`http://127.0.0.1:5000/site_template_builder.html`.

---

## Shared modules

| Module | Provides |
|--------|----------|
| `uddi_toolkit.client` | `UDDIClient` / `UDDIError` — HTTP get/post/patch/delete wrapper (+ `get_all` pagination) |
| `uddi_toolkit.core` | `load_yaml_template`, `read_config`, `resolve_credentials`, `setup_logging`, `template_type`, `validate_template`, `build_record_body`, `detect_drift`, `add_common_args`, … |

These are imported by every command module and are not run directly.

---

## File layout

```
uddi_automation_toolkit/
├── pyproject.toml              # packaging + the `uddi` console entry point
├── README.md
├── uddi.ini.example            # config template
├── uddi.ini                    # your config (not committed — see .gitignore)
├── templates/                  # your YAML templates (user data)
│   ├── site-london.yaml
│   ├── blocks/regional_address_blocks.yaml
│   └── dns/corp.yaml
└── src/uddi_toolkit/
    ├── cli.py                  # unified `uddi` CLI (subcommand registry)
    ├── __main__.py             # enables `python -m uddi_toolkit`
    ├── client.py               # shared API client
    ├── core.py                 # shared helpers (config, templates, records, drift)
    ├── batch.py  retag.py  drift_cli.py
    ├── site/   {provision,decommission,query}.py
    ├── block/  {provision,decommission,query}.py
    ├── dns/    {provision,decommission,query}.py
    └── web/
        ├── server.py           # Flask web UI
        ├── static/             # index.html + app.js
        └── site_template_builder.html
```

---

## Changelog

### Toolkit

| Version | Changes |
|---------|---------|
| 2.0.0 | Restructured into the installable `uddi_toolkit` package with a single `uddi` CLI (`uddi <verb> <type>`); added `address-block` and `dns` template types, `validate`/`drift`/`retag-block` commands, and web-UI type awareness |
| 1.x | Standalone per-tool scripts (`provision_site.py`, …) |

The per-tool history below predates the 2.0 package restructure.

### provision_site.py

| Version | Changes |
|---------|---------|
| 1.3.0 | DHCP range creation per subnet; reverse DNS zone creation; rollback on failure; shared `uddi_client` and `uddi_utils` modules |
| 1.2.0 | `dns.create_zone` option (YAML + CLI) |
| 1.1.0 | YAML template support; multiple hosts; per-subnet CIDR; extra tags |
| 1.0.0 | Initial release |

### decommission_site.py

| Version | Changes |
|---------|---------|
| 1.2.0 | DHCP range deletion; reverse DNS zone deletion; shared modules |
| 1.1.0 | YAML template support |
| 1.0.0 | Initial release |

### query_site.py

| Version | Changes |
|---------|---------|
| 1.0.0 | Initial release — read-only site inspector with human and JSON output |

### batch_provision.py

| Version | Changes |
|---------|---------|
| 1.0.0 | Initial release — sequential batch provision/decommission |

### web_server.py

| Version | Changes |
|---------|---------|
| 1.0.0 | Initial release — Flask UI with builder, template CRUD, SSE execution streaming, structured query results |

### site_template_builder.html

| Version | Changes |
|---------|---------|
| 1.0.0 | Initial release |

---

## Author

Chris Marrison — chris@infoblox.com
