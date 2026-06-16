# Universal DDI Site Toolkit

Tag-driven site provisioning, decommissioning, querying, and template
authoring for **Infoblox Universal DDI**.

| Tool | Purpose |
|------|---------|
| `provision_site.py` | Bring up a new network site end-to-end |
| `decommission_site.py` | Tear down a previously provisioned site |
| `query_site.py` | Read-only inspection of a provisioned site |
| `batch_provision.py` | Provision or decommission multiple sites sequentially |
| `web_server.py` | Browser-based UI for templates and execution |
| `site_template_builder.html` | Standalone offline YAML template builder |

All operational scripts share the same YAML template format and INI
configuration file.

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
| 10 | **Reset** the block — `Status=decommissioned`, `Site=unassigned` |

All destructive steps support `--dry-run`.

---

## Prerequisites

```
Python 3.10+
pip install -r requirements.txt
```

`requirements.txt` contains: `requests`, `PyYAML`, `flask`

The standalone template builder (`site_template_builder.html`) is a
single self-contained HTML file — open it directly in any browser, no
server needed.

---

## Configuration

Copy `provision_site.ini.example` to `uddi.ini` and fill in:

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

All scripts default to `uddi.ini` in the current working directory.
Override with `-c /path/to/file.ini`.

### Parameter precedence

```
CLI flags  >  YAML template  >  INI [DEFAULTS]  >  hardcoded fallbacks
```

---

## YAML template schema

```yaml
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
available  →  allocated  →  decommissioned
            (provision)    (decommission --final-status decommissioned)

decommissioned  →  available
                   (decommission --final-status available)
```

---

## provision_site.py

### Usage

```
provision_site.py [-h] [-t FILE]
                  [-s NAME] [-r REGION] [-e ENV] [-l LOCATION]
                  [--subnet-size N] [--dns-parent ZONE]
                  [--dns-view VIEW] [--ip-space SPACE]
                  [--create-zone | --no-create-zone]
                  [--create-reverse-zone]
                  [--no-rollback]
                  [--dry-run] [-c FILE] [-d | -v] [-V]
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
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |
| `-V`, `--version` | Show version and exit |

### Examples

```bash
# Dry-run
python3 provision_site.py -t templates/site-london.yaml --dry-run -v

# Live run
python3 provision_site.py -t templates/site-london.yaml -v

# With DHCP ranges and reverse DNS zones
python3 provision_site.py -t templates/site-london.yaml --create-reverse-zone -v

# CLI-only (no template)
python3 provision_site.py -s london -r EMEA -e production -v

# Different config file
python3 provision_site.py -t templates/site-london.yaml -c /etc/uddi/uddi.ini -v
```

---

## decommission_site.py

### Usage

```
decommission_site.py [-h] [-t FILE] [-s NAME]
                     [--final-status {decommissioned,available}]
                     [--keep-zone]
                     [--dns-parent ZONE] [--dns-view VIEW]
                     [--ip-space SPACE]
                     [--dry-run] [--force]
                     [-c FILE] [-d | -v] [-V]
```

| Argument | Description |
|----------|-------------|
| `-t`, `--template` | Path to YAML site template |
| `-s`, `--site` | Short site name to decommission |
| `--final-status` | Block status after teardown: `decommissioned` (default) or `available` |
| `--keep-zone` | Leave the forward DNS zone intact |
| `--dns-parent` | Parent DNS zone |
| `--dns-view` | DNS view name |
| `--ip-space` | IP space name |
| `--dry-run` | Preview all steps without making any changes |
| `--force` | Skip the interactive confirmation prompt |
| `-c`, `--config` | INI config file (default: `uddi.ini`) |
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |
| `-V`, `--version` | Show version and exit |

### Examples

```bash
# Dry-run
python3 decommission_site.py -t templates/site-london.yaml --dry-run -v

# Live run (prompts for confirmation)
python3 decommission_site.py -t templates/site-london.yaml -v

# Non-interactive (CI/pipeline)
python3 decommission_site.py -t templates/site-london.yaml --force -v

# Reset block back to available (ready to re-provision)
python3 decommission_site.py -t templates/site-london.yaml --final-status available --force -v
```

---

## query_site.py

Read-only inspection of a provisioned site. Makes no changes to the
infrastructure — safe to run at any time.

### Usage

```
query_site.py [-h] [-t FILE]
              [-s SITE] [--dns-parent ZONE] [--dns-view VIEW]
              [--ip-space SPACE] [--json]
              [-c FILE] [-d | -v] [-V]
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
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |
| `-V`, `--version` | Show version and exit |

### Examples

```bash
# Human-readable report
python3 query_site.py -t templates/site-london.yaml -v

# Machine-readable JSON
python3 query_site.py -t templates/site-london.yaml --json | python3 -m json.tool
```

---

## batch_provision.py

Runs provision or decommission sequentially across multiple templates.
Each site is executed as a subprocess so a single failure does not
prevent the remaining sites from being processed.

### Usage

```
batch_provision.py --action {provision,decommission}
                   [--templates-dir DIR | --templates FILE [FILE ...]]
                   [--dry-run] [--force] [--no-rollback]
                   [--stop-on-error]
                   [-c FILE] [-d | -v] [-V]
```

| Argument | Description |
|----------|-------------|
| `--action` | `provision` or `decommission` (required) |
| `--templates-dir` | Directory of `.yaml`/`.yml` templates to process |
| `--templates` | One or more explicit template paths |
| `--dry-run` | Forward `--dry-run` to each child script |
| `--force` | Forward `--force` to decommission_site.py |
| `--no-rollback` | Forward `--no-rollback` to provision_site.py |
| `--stop-on-error` | Abort after the first failed template |
| `-c`, `--config` | INI config file (default: `uddi.ini`) |

### Examples

```bash
# Dry-run all templates in a directory
python3 batch_provision.py --action provision --templates-dir templates --dry-run -v

# Provision specific templates
python3 batch_provision.py --action provision \
    --templates templates/site-london.yaml templates/site-paris.yaml -v

# Decommission all, non-interactively, stop on first failure
python3 batch_provision.py --action decommission \
    --templates-dir templates --force --stop-on-error -v
```

---

## web_server.py

Flask-based web UI. Provides a browser interface for managing templates
and executing provision / decommission / query operations with real-time
streaming output.

### Usage

```
web_server.py [-c CONFIG] [--templates-dir DIR]
              [-p PORT] [--host HOST] [--debug-flask]
              [-d | -v] [-V]
```

| Argument | Description |
|----------|-------------|
| `-c`, `--config` | INI config file (default: `uddi.ini`) |
| `--templates-dir` | Template directory (default: `templates/` alongside the script) |
| `-p`, `--port` | Port to listen on (default: `5000`) |
| `--host` | Host to bind (default: `127.0.0.1`) |
| `--debug-flask` | Enable Flask debug/auto-reload mode |
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |

### Starting the server

```bash
python3 web_server.py -c uddi.ini -v
# Open http://127.0.0.1:5000/
```

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
open site_template_builder.html          # macOS
xdg-open site_template_builder.html     # Linux
start site_template_builder.html        # Windows
```

---

## Shared modules

| Module | Provides |
|--------|----------|
| `uddi_client.py` | `UDDIClient` — HTTP get/post/patch/delete wrapper with auth |
| `uddi_utils.py` | `load_yaml_template`, `read_config`, `setup_logging`, `reverse_zone_fqdn` |

These are imported by all operational scripts and are not intended to be
run directly.

---

## File layout

```
uddi_automation_toolkit/
├── provision_site.py           # Provisioning script
├── decommission_site.py        # Decommissioning script
├── query_site.py               # Read-only site inspector
├── batch_provision.py          # Batch runner
├── web_server.py               # Flask web UI
├── uddi_client.py              # Shared API client
├── uddi_utils.py               # Shared utility functions
├── site_template_builder.html  # Standalone offline template builder
├── uddi.ini                    # Your config (not committed — see .gitignore)
├── uddi.ini.example            # Config template
├── requirements.txt
├── README.md
├── static/
│   ├── index.html              # Web UI frontend
│   └── app.js                  # Web UI JavaScript
└── templates/
    ├── site-london.yaml        # Full-featured example
    └── site-minimal.yaml       # Minimal example (uses INI defaults)
```

---

## Changelog

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
