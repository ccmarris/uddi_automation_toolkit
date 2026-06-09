# Universal DDI Site Toolkit

Tag-driven site provisioning, decommissioning, and template authoring
for **Infoblox Universal DDI**.

| Tool | Purpose |
|------|---------|
| `provision_site.py` | Bring up a new network site end-to-end |
| `decommission_site.py` | Tear down a previously provisioned site |
| `site_template_builder.html` | Browser form that generates YAML templates |

All three tools share the same YAML template format and INI configuration
file — author a template once in the builder, then use it for both
provisioning and decommissioning.

---

## How it works

Site definitions are driven by a YAML template (or CLI flags for quick
one-liners). Address blocks are discovered by metadata tags — no
hardcoded CIDRs required.

### Provisioning sequence

| Step | Action |
|------|--------|
| 1 | Resolves the configured IP space |
| 2 | **Discovers** an available address block matching `Region`, `Environment`, `Status=available` tags |
| 3 | Resolves the DNS view |
| 4 | **Carves** subnets per the plan (YAML template or built-in mgmt/lan/server default) |
| 5 | **Updates** the block — sets `Status=allocated`, `Site=<name>` |
| 6 | **Creates** a forward DNS zone: `site-<name>.<dns_parent>` |
| 7 | **Provisions** all hosts defined in the template (IPAM + DNS A/PTR) |

### Decommissioning sequence

| Step | Action |
|------|--------|
| 1 | Resolves the configured IP space |
| 2 | **Discovers** the site's address block (`Site=<name>`, `Status=allocated`) |
| 3 | Resolves the DNS view |
| 4 | **Enumerates** all subnets inside the block |
| 5 | **Deletes** all IPAM host records in site subnets (removes DNS A/PTR automatically) |
| 6 | **Deletes** the forward DNS authoritative zone (unless `--keep-zone`) |
| 7 | **Deletes** all site subnets |
| 8 | **Resets** the block — `Status=decommissioned`, `Site=unassigned`, clears `Location`/`Provisioned` |

All destructive steps in both scripts support `--dry-run`.

---

## Prerequisites

```
Python 3.8+
pip install requests pyyaml
```

The template builder (`site_template_builder.html`) is a single
self-contained HTML file — open it directly in any modern browser.
No server or additional dependencies required.

---

## Configuration

Copy `provision_site.ini.example` to `provision_site.ini` and fill in:

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
```

> **Security:** `provision_site.ini` is listed in `.gitignore` and will
> never be committed. Keep your API key out of source control.

Both `provision_site.py` and `decommission_site.py` read the same INI
file (default: `provision_site.ini` in the current directory).

---

## Parameter precedence

Values are resolved in this order (highest wins):

```
CLI flags  >  YAML template  >  INI [DEFAULTS]  >  hardcoded fallbacks
```

This means you can:
- Use a template for the subnet/host plan and override the DNS view via CLI
- Use CLI-only flags with no template at all (backwards-compatible with v1.0.0)

---

## site_template_builder.html

A browser-based form for authoring YAML site templates without editing
YAML by hand. Open the file directly — no web server needed.

### Features

- **Live YAML preview** with syntax highlighting, updated as you type
- **Dynamic lists** — add and remove subnets, hosts, and extra tags
- **Subnet-aware host form** — the subnet dropdown in the Hosts section
  auto-populates from the names defined in the Subnets section above
- **Download** the finished template as `site-<name>.yaml`
- **Copy to clipboard** button in the preview panel
- **CLI command hints** — the exact `provision_site.py` and
  `decommission_site.py` commands (with the correct template path) are
  shown at the bottom of the form and update in real time

### Usage

```bash
open site_template_builder.html          # macOS
xdg-open site_template_builder.html     # Linux
start site_template_builder.html        # Windows
```

Fill in the form, download or copy the YAML, save it to the `templates/`
directory, then run:

```bash
python3 provision_site.py    -t templates/site-<name>.yaml --dry-run -v
python3 decommission_site.py -t templates/site-<name>.yaml --dry-run -v
```

---

## YAML template schema

The same template file is accepted by both `provision_site.py` and
`decommission_site.py`.  All keys are optional except `site.name`,
`site.region`, and `site.environment`.

```yaml
site:
  name:        london          # required (or via -s)
  region:      EMEA            # required (or via -r)
  environment: production      # required (or via -e)
  location:    "London, UK"    # optional

network:
  ip_space:    my-ip-space     # optional — overrides INI default
  subnet_size: 24              # default prefix length for subnets

  subnets:                     # optional — defaults to mgmt/lan/server /24
    - name:    london-mgmt
      purpose: mgmt
      dhcp:    false
      cidr:    24              # per-subnet override of subnet_size
    - name:    london-lan
      purpose: user-lan
      dhcp:    true
    - name:    london-server
      purpose: server
      dhcp:    false
    - name:    london-dmz
      purpose: dmz
      dhcp:    false

dns:
  parent:      internal.example.com # optional — overrides INI default
  view:        default              # optional — overrides INI default
  create_zone: false                # create zone if absent (default: false)

hosts:                         # optional — defaults to single gw01
  - hostname: gw01
    subnet:   london-mgmt      # must match a name in subnets above
    comment:  "Site gateway"
  - hostname: dns01
    subnet:   london-server
    comment:  "Site DNS server"

tags:                          # extra tags applied to block + all subnets
  Owner:      network-team
  CostCentre: CC-1234
```

---

## Tag schema

Address blocks must be tagged before the script can discover them.

| Tag | Required | Description | Example values |
|-----|----------|-------------|----------------|
| `Owner` | Yes | Responsible team or individual | `network-team` |
| `Environment` | Yes | Deployment tier | `production`, `lab` |
| `Region` | Yes | Geographic region | `AMER`, `EMEA`, `APAC` |
| `Site` | Yes | Short site name | `london`, `unassigned` |
| `Status` | Yes | Lifecycle state | `available`, `allocated` |
| `Location` | No | Human-readable location | `London, UK` |
| `Provisioned` | No | ISO date of allocation | `2026-05-29` |
| `BlockSize` | No | CIDR size of block | `/16` |

Status lifecycle:

```
available  →  allocated  →  decommissioned
               (provision)    (decommission)

decommissioned  →  available
                   (--final-status available, ready to re-provision)
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
                  [--dry-run] [-c FILE] [-d | -v] [-V]
```

| Argument | Description |
|----------|-------------|
| `-t`, `--template` | Path to YAML site definition template |
| `-s`, `--site` | Short site name (e.g. `london`) |
| `-r`, `--region` | Region tag to match on block (e.g. `EMEA`) |
| `-e`, `--environment` | Environment tag to match on block (e.g. `production`) |
| `-l`, `--location` | Human-readable location applied to the block |
| `--subnet-size` | Default subnet prefix length (overrides template/config) |
| `--dns-parent` | Parent DNS zone (overrides template/config) |
| `--dns-view` | DNS view name (overrides template/config) |
| `--ip-space` | IP space name (overrides template/config) |
| `--create-zone` | Create the site DNS zone if it does not already exist |
| `--no-create-zone` | Abort if the site DNS zone does not exist (safe default) |
| `--dry-run` | Preview all steps without making changes |
| `-c`, `--config` | INI config file path (default: `provision_site.ini`) |
| `-d`, `--debug` | Enable DEBUG logging (shows all API calls) |
| `-v`, `--verbose` | Enable INFO logging |
| `-V`, `--version` | Show version and exit |

### Examples

```bash
# Dry-run with full YAML template
python3 provision_site.py -t templates/site-london.yaml --dry-run -v

# Execute with YAML template
python3 provision_site.py -t templates/site-london.yaml -v

# Minimal template (site/region/environment only) — rest from INI
python3 provision_site.py -t templates/site-minimal.yaml -v

# Template + CLI override (CLI view wins over template)
python3 provision_site.py -t templates/site-london.yaml --dns-view internal

# CLI-only, no template (backwards compatible with v1.0.0)
python3 provision_site.py -s london -r EMEA -e production -l "London, UK" -v

# Custom subnet size
python3 provision_site.py -t templates/site-london.yaml --subnet-size 22

# Different config file
python3 provision_site.py -t templates/site-london.yaml -c /etc/infoblox/provision_site.ini
```

### Example output

```
Provisioning site: london
  Template: templates/site-london.yaml

============================================================
Site Provisioning Summary
============================================================
  Address block : 10.20.0.0/16

  Subnets:
    10.20.0.0/24         london-mgmt              id=ipam/subnet/...
    10.20.1.0/24         london-lan               id=ipam/subnet/...
    10.20.2.0/24         london-server            id=ipam/subnet/...
    10.20.3.0/24         london-dmz               id=ipam/subnet/...

  DNS zone      : site-london.internal.example.com  id=dns/auth_zone/...

  Hosts:
    gw01.site-london.internal.example.com         -> 10.20.0.1    id=ipam/host/...
    dns01.site-london.internal.example.com        -> 10.20.2.1    id=ipam/host/...
    mon01.site-london.internal.example.com        -> 10.20.0.2    id=ipam/host/...
============================================================
Provisioning complete.
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
| `-t`, `--template` | Path to YAML site template (same file used to provision) |
| `-s`, `--site` | Short site name to decommission (or via `site.name` in template) |
| `--final-status` | Block status after teardown: `decommissioned` (default) or `available` |
| `--keep-zone` | Leave the site DNS zone intact |
| `--dns-parent` | Parent DNS zone (overrides INI default) |
| `--dns-view` | DNS view name (overrides INI default) |
| `--ip-space` | IP space name (overrides INI default) |
| `--dry-run` | Preview all steps without making changes |
| `--force` | Skip the interactive confirmation prompt |
| `-c`, `--config` | INI config file path (default: `provision_site.ini`) |
| `-d`, `--debug` | Enable DEBUG logging |
| `-v`, `--verbose` | Enable INFO logging |
| `-V`, `--version` | Show version and exit |

### Examples

```bash
# Preview what would be removed (no changes made)
python3 decommission_site.py -t templates/site-london.yaml --dry-run -v

# Full decommission from template — prompts: "Type the site name to confirm"
python3 decommission_site.py -t templates/site-london.yaml -v

# Non-interactive (CI/pipeline use)
python3 decommission_site.py -t templates/site-london.yaml --force -v

# Reset block back to available (ready to re-provision)
python3 decommission_site.py -t templates/site-london.yaml --final-status available --force -v

# Keep the DNS zone, only tear down IPAM resources
python3 decommission_site.py -t templates/site-london.yaml --keep-zone -v

# CLI-only (no template)
python3 decommission_site.py -s london --dry-run -v

# Batch decommission all sites listed in a file
while IFS= read -r site; do
    python3 decommission_site.py -s "$site" --force -v
done < sites-to-retire.txt
```

---

## Extending the scripts

### Add more subnets

Add entries to the `network.subnets` list in your YAML template — or
use the template builder form — no code changes required.

### Add more hosts

Add entries to the `hosts` list in your YAML template. Multiple hosts
in the same subnet are assigned sequential IPs (`.1`, `.2`, ...).

### Add a reverse DNS zone

Override `SiteProvisioner.provision()` or add a `create_reverse_zone()`
method alongside `create_dns_zone()`.

### Pipeline / batch provisioning

Loop over a directory of YAML templates:

```bash
for tmpl in templates/sites/*.yaml; do
    python3 provision_site.py -t "$tmpl" -v
done
```

---

## File layout

```
claude_mcp_provision_site/
├── provision_site.py           # Provisioning script
├── decommission_site.py        # Decommissioning script
├── site_template_builder.html  # Browser-based YAML template builder
├── provision_site.ini.example  # Configuration template (shared by both scripts)
├── README.md                   # This file
└── templates/
    ├── site-london.yaml        # Full featured example
    └── site-minimal.yaml       # Minimal example (uses INI defaults)
```

---

## Changelog

### site_template_builder.html

| Version | Changes |
|---------|---------|
| 1.0.0 | Initial release — browser form with live YAML preview, download, and CLI hints |

### decommission_site.py

| Version | Changes |
|---------|---------|
| 1.1.0 | YAML template support — same template file works for provision and decommission |
| 1.0.0 | Initial release — full site teardown: hosts, DNS zone, subnets, block reset |

### provision_site.py

| Version | Changes |
|---------|---------|
| 1.2.0 | `dns.create_zone` option (YAML + CLI); safe default — abort if zone absent |
| 1.1.0 | YAML template support; multiple hosts; per-subnet CIDR; extra tags |
| 1.0.0 | Initial release — CLI-only, three-subnet plan, single gateway host |

---

## Author

Chris Marrison — chris@infoblox.com
