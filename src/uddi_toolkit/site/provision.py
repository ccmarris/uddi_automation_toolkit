#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Tag-driven site provisioning script for Infoblox Universal DDI.

    Automates the full lifecycle of bringing up a new network site:

      1. Discover an available address block using metadata tags
         (Region, Environment, Status=available)
      2. Carve subnets from the block (standard 3-subnet plan or fully
         customised via a YAML template)
      3. Apply per-subnet tags (Site, Purpose, DHCP, etc.)
      4. Mark the parent block as allocated and update its Site tag
      5. Create a forward DNS authoritative zone for the site
      6. Provision one or more host records (IPAM + DNS A/PTR)

    All destructive steps support --dry-run so you can preview the
    full plan before committing any changes.

    Site definitions can be supplied three ways (highest to lowest
    precedence):

      1. CLI flags  (-s, -r, -e, --subnet-size, ...)
      2. YAML template  (--template site.yaml)
      3. INI configuration defaults  (uddi.ini)

 Usage:
    provision_site.py [-h] [-t TEMPLATE]
                      [-s SITE] [-r REGION] [-e ENVIRONMENT]
                      [-l LOCATION] [--subnet-size SUBNET_SIZE]
                      [--dns-parent DNS_PARENT] [--dns-view DNS_VIEW]
                      [--ip-space IP_SPACE] [--dry-run]
                      [-c CONFIG] [-d] [-v]

 Examples:
    # Dry-run using a YAML template
    provision_site.py -t templates/site-london.yaml --dry-run -v

    # Execute using a YAML template
    provision_site.py -t templates/site-london.yaml -v

    # CLI-only (no template) with verbose output
    provision_site.py -s london -r EMEA -e production -l "London, UK" -v

    # Template + CLI override (CLI wins)
    provision_site.py -t templates/site-london.yaml --dns-view internal

 Requirements:
    Python 3.8+ with requests and PyYAML modules

    pip install requests pyyaml

 Configuration:
    Create an INI file (default: uddi.ini):

      [UDDI]
      api_key  = <your BloxOne/Universal DDI API key>
      url      = https://csp.infoblox.com

      [DEFAULTS]
      ip_space    = my-ip-space
      dns_parent  = internal.example.com
      dns_view    = default
      owner       = network-team
      subnet_size = 24

    YAML template schema (all keys optional — missing keys fall back
    to INI defaults or CLI flags):

      site:
        name:        london
        region:      EMEA
        environment: production
        location:    "London, UK"

      network:
        ip_space:    my-ip-space     # overrides INI default
        subnet_size: 24              # default size for subnets

        subnets:
          - name:    london-mgmt
            purpose: mgmt
            dhcp:    false
            cidr:    24              # per-subnet override of subnet_size
          - name:    london-lan
            purpose: user-lan
            dhcp:    true

      dns:
        parent:      internal.example.com
        view:        default
        create_zone: true    # create zone if absent (default: false)

      hosts:
        - hostname: gw01
          subnet:   london-mgmt      # name from subnets list above
          comment:  "Site gateway"
        - hostname: dns01
          subnet:   london-server
          comment:  "Site DNS server"

      tags:
        Owner:      network-team
        CostCentre: CC-1234

 Author: Chris Marrison

 Date Last Updated: 20260529

 Copyright (c) 2026 Chris Marrison / Infoblox

 Redistribution and use in source and binary forms,
 with or without modification, are permitted provided
 that the following conditions are met:

 1. Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

 2. Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in
    the documentation and/or other materials provided with the
    distribution.

 THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
 FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
 INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
 BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
 CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
 ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 POSSIBILITY OF SUCH DAMAGE.

------------------------------------------------------------------------
'''
__version__ = '1.2.0'
__author__ = 'Chris Marrison'
__author_email__ = 'chris@infoblox.com'

import argparse
import dataclasses
import datetime
import ipaddress
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

from uddi_toolkit.client import UDDIClient, UDDIError
from uddi_toolkit.core import env_config, load_yaml_template, read_config, resolve_credentials, setup_logging, reverse_zone_fqdn, add_common_args

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SubnetDef:
    '''
    Definition of a single subnet to be carved from the address block.

    Attributes:
        name:    Resource name applied in IPAM (e.g. london-mgmt)
        purpose: Tag value for the Purpose key (e.g. mgmt, user-lan)
        dhcp:    Whether DHCP is enabled on this subnet ('true'/'false')
        cidr:    Prefix length override; falls back to SiteConfig.subnet_size
    '''
    name: str
    purpose: str
    dhcp: str = 'false'
    cidr: Optional[int] = None
    dhcp_start: Optional[int] = None   # host offset from subnet base for DHCP range start
    dhcp_end: Optional[int] = None     # host offset from subnet base for DHCP range end


@dataclass
class HostDef:
    '''
    Definition of a host to be provisioned in IPAM with DNS records.

    Attributes:
        hostname: Short hostname (FQDN will be hostname.dns_zone)
        subnet:   Name of the subnet (must match a SubnetDef.name) in
                  which to allocate the first available IP
        comment:  Free-text description stored on the IPAM host object
    '''
    hostname: str
    subnet: str
    comment: str = ''


@dataclass
class SiteConfig:
    '''
    Holds all parameters needed to provision a single site.

    The subnet_plan and hosts lists drive what gets created.  When no
    YAML template is supplied the built-in three-subnet / one-gateway
    defaults are used, preserving backwards compatibility with v1.0.0.
    '''
    site: str
    region: str
    environment: str
    location: str
    ip_space: str
    dns_parent: str
    dns_view: str
    owner: str
    subnet_size: int
    dry_run: bool
    create_zone: bool = False
    create_reverse_zone: bool = False
    no_rollback: bool = False
    if_not_exists: bool = False
    extra_tags: dict = field(default_factory=dict)
    _subnet_plan: list[SubnetDef] = field(default_factory=list)
    _hosts: list[HostDef] = field(default_factory=list)
    date: str = field(default_factory=lambda: datetime.date.today().isoformat())

    @property
    def dns_zone(self) -> str:
        '''Fully-qualified DNS zone name for the site.'''
        return f'site-{self.site}.{self.dns_parent}'

    @property
    def subnet_plan(self) -> list[SubnetDef]:
        '''
        Custom subnet plan from YAML template, or the built-in default
        (mgmt / user-lan / server) when no template was supplied.
        '''
        if self._subnet_plan:
            plan = self._subnet_plan
        else:
            plan = [
                SubnetDef(name=f'{self.site}-mgmt',   purpose='mgmt',     dhcp='false'),
                SubnetDef(name=f'{self.site}-lan',    purpose='user-lan', dhcp='true'),
                SubnetDef(name=f'{self.site}-server', purpose='server',   dhcp='false'),
            ]
        return plan

    @property
    def hosts(self) -> list[HostDef]:
        '''
        Custom host list from YAML template, or a single gateway host
        (gw01 in the first subnet) when no template was supplied.
        '''
        if self._hosts:
            host_list = self._hosts
        else:
            first_subnet = self.subnet_plan[0].name
            host_list = [
                HostDef(
                    hostname=f'gw01',
                    subnet=first_subnet,
                    comment=f'{self.site.capitalize()} site gateway',
                )
            ]
        return host_list


@dataclass
class ProvisionResult:
    '''
    Accumulates resource IDs created during provisioning so callers
    can inspect, log, or roll back.
    '''
    block_id: str = ''
    block_address: str = ''
    subnets: list[dict] = field(default_factory=list)
    dhcp_ranges: list[dict] = field(default_factory=list)
    dns_zone_id: str = ''
    dns_zone_fqdn: str = ''
    reverse_zones: list[dict] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    dry_run: bool = False
    skipped: bool = False
    skip_reason: str = ''


# ---------------------------------------------------------------------------
# YAML template loader
# ---------------------------------------------------------------------------

def template_to_site_config(
    template: dict,
    ini_defaults: dict,
    cli_args: argparse.Namespace,
) -> SiteConfig:
    '''
    Merge a YAML template, INI defaults, and CLI arguments into a
    SiteConfig.

    Precedence (highest → lowest):
        CLI flags  >  YAML template  >  INI defaults  >  hardcoded fallbacks

    Args:
        template:     Parsed YAML template dict (may be empty)
        ini_defaults: Dict of key/value pairs from [DEFAULTS] in the
                      INI configuration file
        cli_args:     Parsed argparse Namespace from parseargs()

    Returns:
        Fully populated SiteConfig instance

    Raises:
        SystemExit if mandatory values (site, region, environment,
        ip_space, dns_parent) cannot be resolved from any source
    '''
    site_sec  = template.get('site', {}) or {}
    net_sec   = template.get('network', {}) or {}
    dns_sec   = template.get('dns', {}) or {}
    tags_sec  = template.get('tags', {}) or {}
    hosts_sec = template.get('hosts', []) or []
    subnets_sec = net_sec.get('subnets', []) or []

    def resolve(cli_val, yaml_val, ini_key, fallback=''):
        '''Return first non-empty value: CLI > YAML > env var > INI > fallback.'''
        if cli_val is not None and cli_val != '':
            value = cli_val
        elif yaml_val is not None and yaml_val != '':
            value = yaml_val
        else:
            env_val = env_config(ini_key)
            if env_val:
                value = env_val
            else:
                value = ini_defaults.get(ini_key, fallback)
        return value

    # --- Mandatory fields ---
    site = resolve(
        getattr(cli_args, 'site', None),
        site_sec.get('name'),
        'site',
    )
    region = resolve(
        getattr(cli_args, 'region', None),
        site_sec.get('region'),
        'region',
    )
    environment = resolve(
        getattr(cli_args, 'environment', None),
        site_sec.get('environment'),
        'environment',
    )
    ip_space = resolve(
        getattr(cli_args, 'ip_space', None),
        net_sec.get('ip_space'),
        'ip_space',
    )
    dns_parent = resolve(
        getattr(cli_args, 'dns_parent', None),
        dns_sec.get('parent'),
        'dns_parent',
    )

    errors = []
    for label, value in [
        ('site / --site', site),
        ('region / site.region', region),
        ('environment / site.environment', environment),
        ('ip_space / network.ip_space', ip_space),
        ('dns_parent / dns.parent', dns_parent),
    ]:
        if not value:
            errors.append(label)
    if errors:
        logger.error(
            'Required values missing (supply via CLI, YAML template, or INI): %s',
            ', '.join(errors),
        )
        sys.exit(1)

    # --- Optional fields ---
    location = resolve(
        getattr(cli_args, 'location', None),
        site_sec.get('location'),
        'location',
        fallback=site.capitalize(),
    )
    dns_view = resolve(
        getattr(cli_args, 'dns_view', None),
        dns_sec.get('view'),
        'dns_view',
        fallback='default',
    )
    owner = resolve(
        None,
        tags_sec.get('Owner') or site_sec.get('owner'),
        'owner',
        fallback='network-team',
    )
    subnet_size_raw = resolve(
        getattr(cli_args, 'subnet_size', None),
        net_sec.get('subnet_size'),
        'subnet_size',
        fallback=24,
    )
    try:
        subnet_size = int(subnet_size_raw)
    except (TypeError, ValueError):
        logger.error(
            'subnet_size must be an integer, got %r (check CLI --subnet-size, '
            'YAML network.subnet_size, or INI [DEFAULTS] subnet_size)',
            subnet_size_raw,
        )
        sys.exit(1)

    # --- Subnet plan from YAML ---
    subnet_plan: list[SubnetDef] = []
    for s in subnets_sec:
        subnet_plan.append(SubnetDef(
            name=s.get('name', f'{site}-{s.get("purpose", "net")}'),
            purpose=s.get('purpose', 'general'),
            dhcp=str(s.get('dhcp', False)).lower(),
            cidr=s.get('cidr'),          # None → use subnet_size default
            dhcp_start=s.get('dhcp_start'),
            dhcp_end=s.get('dhcp_end'),
        ))

    # --- Host list from YAML ---
    host_list: list[HostDef] = []
    for h in hosts_sec:
        if 'hostname' not in h:
            logger.warning('Skipping host entry with no hostname: %s', h)
            continue
        # Default subnet is the first in the plan (mgmt)
        default_subnet = subnet_plan[0].name if subnet_plan else f'{site}-mgmt'
        host_list.append(HostDef(
            hostname=h['hostname'],
            subnet=h.get('subnet', default_subnet),
            comment=h.get('comment', ''),
        ))

    # Extra tags from the YAML [tags] section (Owner already extracted above)
    extra_tags = {k: str(v) for k, v in tags_sec.items()}

    # --- DNS zone creation option ---
    # Precedence: CLI --create-zone/--no-create-zone > YAML dns.create_zone > False
    cli_create_zone = getattr(cli_args, 'create_zone', None)
    if cli_create_zone is not None:
        create_zone = bool(cli_create_zone)
    else:
        create_zone = bool(dns_sec.get('create_zone', False))

    # --- Reverse DNS zone creation option ---
    # Precedence: CLI --create-reverse-zone > YAML dns.create_reverse_zone > False
    cli_reverse_zone = getattr(cli_args, 'create_reverse_zone', None)
    if cli_reverse_zone is not None:
        create_reverse_zone = bool(cli_reverse_zone)
    else:
        create_reverse_zone = bool(dns_sec.get('create_reverse_zone', False))

    return SiteConfig(
        site=site.lower(),
        region=region,
        environment=environment,
        location=location,
        ip_space=ip_space,
        dns_parent=dns_parent,
        dns_view=dns_view,
        owner=owner,
        subnet_size=subnet_size,
        dry_run=getattr(cli_args, 'dry_run', False),
        create_zone=create_zone,
        create_reverse_zone=create_reverse_zone,
        no_rollback=getattr(cli_args, 'no_rollback', False),
        if_not_exists=getattr(cli_args, 'if_not_exists', False),
        extra_tags=extra_tags,
        _subnet_plan=subnet_plan,
        _hosts=host_list,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _block_sort_key(block: dict) -> tuple:
    '''
    Sort key for address blocks: (numeric address, cidr).

    Falls back to a high sentinel for unparseable addresses so they sort
    last rather than raising.
    '''
    try:
        addr_int = int(ipaddress.ip_address(block.get('address', '')))
    except ValueError:
        addr_int = 1 << 128
    try:
        cidr = int(block.get('cidr', 0))
    except (TypeError, ValueError):
        cidr = 0
    return (addr_int, cidr)


# ---------------------------------------------------------------------------
# Site provisioner
# ---------------------------------------------------------------------------

class SiteProvisioner:
    '''
    Orchestrates the full site provisioning sequence against the
    Infoblox Universal DDI API.

    Steps
    -----
    1. resolve_ip_space()     - look up IP space ID from name
    2. find_available_block() - tag-based discovery of address block
    3. resolve_dns_view()     - look up DNS view ID from name
    4. create_subnets()       - carve subnets per plan (standard or YAML)
    5. update_block_status()  - mark block as allocated
    6. create_dns_zone()      - create forward authoritative zone
    7. provision_hosts()      - IPAM host + DNS A/PTR for each host in plan
    '''

    def __init__(self, client: UDDIClient, cfg: SiteConfig, ini_defaults: Optional[dict] = None) -> None:
        self.client = client
        self.cfg = cfg
        self._space_id: str = ''
        self._view_id: str = ''
        self._zone_id: str = ''
        self._zone_created: bool = False
        self._original_block: dict = {}
        _ini = ini_defaults or {}
        self._dhcp_start_default: int = int(_ini.get('dhcp_start_offset', 10))
        self._dhcp_end_default: int = int(_ini.get('dhcp_end_offset', 250))
        return

    # ------------------------------------------------------------------
    # Step 1: Resolve IP space
    # ------------------------------------------------------------------

    def resolve_ip_space(self) -> str:
        '''
        Resolve IP space name to its API resource ID.

        Returns:
            IP space resource ID string

        Raises:
            SystemExit if the space is not found
        '''
        logger.info('Resolving IP space: %s', self.cfg.ip_space)
        data = self.client.get(
            '/ipam/ip_space',
            params={'_filter': f'name=="{self.cfg.ip_space}"'},
        )
        results = data.get('results', [])
        if not results:
            logger.error('IP space not found: %s', self.cfg.ip_space)
            sys.exit(1)
        space_id = results[0]['id']
        logger.debug('IP space ID: %s', space_id)
        self._space_id = space_id
        return space_id

    # ------------------------------------------------------------------
    # Step 2: Find available address block by tags
    # ------------------------------------------------------------------

    def find_existing_site(self) -> dict:
        '''
        Check whether this site has already been provisioned by looking
        for a block in the IP space tagged Site==cfg.site and
        Status==allocated.

        Returns:
            Existing block resource dict if found, or {} if not found.
        '''
        filter_expr = (
            f'space=="{self._space_id}" and '
            f'tags.Site=="{self.cfg.site}" and '
            f'tags.Status=="allocated"'
        )
        data = self.client.get(
            '/ipam/address_block',
            params={'_filter': filter_expr},
        )
        results = data.get('results', [])
        found: dict = {}
        if results:
            block = results[0]
            logger.info(
                'Existing allocated block found for site %r: %s/%s  id=%s',
                self.cfg.site, block['address'], block['cidr'], block['id'],
            )
            found = block
        return found

    def find_available_block(self) -> dict:
        '''
        Search address blocks in the configured IP space for one whose
        tags match:
            Region      == cfg.region
            Environment == cfg.environment
            Status      == "available"

        Returns:
            Address block resource dict (id, address, cidr, tags, ...)

        Raises:
            SystemExit if no matching block is found
        '''
        logger.info(
            'Searching for available block: Region=%s Environment=%s Status=available',
            self.cfg.region, self.cfg.environment,
        )
        filter_expr = (
            f'space=="{self._space_id}" and '
            f'tags.Region=="{self.cfg.region}" and '
            f'tags.Environment=="{self.cfg.environment}" and '
            f'tags.Status=="available"'
        )
        data = self.client.get(
            '/ipam/address_block',
            params={'_filter': filter_expr},
        )
        results = data.get('results', [])
        if not results:
            logger.error(
                'No available address block found for Region=%s Environment=%s',
                self.cfg.region, self.cfg.environment,
            )
            sys.exit(1)

        # Deterministic selection: lowest address first, so repeated runs against
        # the same pool of candidate blocks always pick the same one.
        block = min(results, key=_block_sort_key)
        if len(results) > 1:
            logger.info(
                '%d candidate blocks matched — selecting lowest: %s/%s',
                len(results), block['address'], block['cidr'],
            )
        logger.info(
            'Found block: %s/%s  id=%s',
            block['address'], block['cidr'], block['id'],
        )
        self._original_block = block
        return block

    # ------------------------------------------------------------------
    # Step 3: Resolve DNS view
    # ------------------------------------------------------------------

    def resolve_dns_view(self) -> str:
        '''
        Resolve DNS view name to its API resource ID.

        Returns:
            DNS view resource ID string

        Raises:
            SystemExit if the view is not found
        '''
        logger.info('Resolving DNS view: %s', self.cfg.dns_view)
        data = self.client.get(
            '/dns/view',
            params={'_filter': f'name=="{self.cfg.dns_view}"'},
        )
        results = data.get('results', [])
        if not results:
            logger.error('DNS view not found: %s', self.cfg.dns_view)
            sys.exit(1)
        view_id = results[0]['id']
        logger.debug('DNS view ID: %s', view_id)
        self._view_id = view_id
        return view_id

    # ------------------------------------------------------------------
    # Step 4: Carve subnets
    # ------------------------------------------------------------------

    def create_subnets(self, block: dict, result: 'ProvisionResult') -> dict[str, dict]:
        '''
        Carve one subnet per entry in cfg.subnet_plan from the given
        address block, assigning addresses sequentially from the start
        of the block.

        Each subnet uses its own cidr if specified in the plan, otherwise
        falls back to cfg.subnet_size.

        Each subnet receives tags:
            Site, Region, Environment, Owner, Purpose, DHCP, Name
            plus any extra_tags defined in the YAML template.

        Args:
            block:  Address block resource dict from find_available_block()
            result: ProvisionResult updated incrementally so rollback has
                    accurate state even if we fail partway through.

        Returns:
            Dict mapping subnet name → subnet resource dict (or dry-run
            plan dict), allowing hosts to look up subnets by name.
        '''
        try:
            block_net = ipaddress.ip_network(
                f'{block["address"]}/{block["cidr"]}', strict=False,
            )
        except (KeyError, ValueError) as exc:
            logger.error('Invalid address block %s: %s', block.get('address'), exc)
            sys.exit(1)
        # Running cursor (as a 32-bit int) marking the next free address in the
        # block.  Each subnet is aligned up to its own prefix boundary, carved,
        # then the cursor advances past it — so mixed subnet sizes and blocks
        # that do not start on a /16 boundary are both handled correctly.
        cursor = int(block_net.network_address)
        created: dict[str, dict] = {}

        for sdef in self.cfg.subnet_plan:
            cidr = sdef.cidr if sdef.cidr is not None else self.cfg.subnet_size
            size = 1 << (block_net.max_prefixlen - cidr)
            # Align the cursor up to the subnet's prefix boundary
            if cursor % size:
                cursor += size - (cursor % size)
            try:
                subnet_net = ipaddress.ip_network((cursor, cidr))
            except ValueError as exc:
                logger.error('Cannot carve /%s subnet %s: %s', cidr, sdef.name, exc)
                sys.exit(1)
            if subnet_net.broadcast_address > block_net.broadcast_address:
                logger.error(
                    'Subnet plan does not fit in block %s: %s/%s would exceed %s',
                    block_net, subnet_net.network_address, cidr, block_net.broadcast_address,
                )
                sys.exit(1)
            subnet_addr = str(subnet_net.network_address)
            cursor = int(subnet_net.broadcast_address) + 1
            tags = {
                'Site':        self.cfg.site,
                'Region':      self.cfg.region,
                'Environment': self.cfg.environment,
                'Owner':       self.cfg.owner,
                'Purpose':     sdef.purpose,
                'DHCP':        sdef.dhcp,
                'Name':        sdef.name,
                **{k: v for k, v in self.cfg.extra_tags.items() if k != 'Owner'},
            }
            logger.info(
                '%sCreating subnet %s/%s  name=%s  purpose=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                subnet_addr, cidr, sdef.name, sdef.purpose,
            )
            if self.cfg.dry_run:
                subnet = {
                    'dry_run': True,
                    'address': subnet_addr,
                    'cidr': cidr,
                    'name': sdef.name,
                    'tags': tags,
                }
                result.subnets.append({
                    'address': f'{subnet_addr}/{cidr}',
                    'name':    sdef.name,
                    'id':      '(dry-run)',
                })
                if sdef.dhcp == 'true':
                    dhcp_range = self.create_dhcp_range(subnet, sdef)
                    subnet['_dhcp_range'] = dhcp_range
                    result.dhcp_ranges.append({
                        'id':    '(dry-run)',
                        'start': dhcp_range.get('start', ''),
                        'end':   dhcp_range.get('end', ''),
                        'name':  f'{sdef.name}-dhcp',
                    })
                if self.cfg.create_reverse_zone:
                    reverse_zone = self.create_reverse_zone(subnet_addr, cidr)
                    subnet['_reverse_zone'] = reverse_zone
                    result.reverse_zones.append({
                        'id':   '(dry-run)',
                        'fqdn': reverse_zone.get('fqdn', ''),
                    })
                created[sdef.name] = subnet
                continue

            body = {
                'address': subnet_addr,
                'cidr':    cidr,
                'name':    sdef.name,
                'space':   self._space_id,
                'comment': f'{self.cfg.site.capitalize()} site - {sdef.purpose} network',
                'tags':    tags,
            }
            api_result = self.client.post('/ipam/subnet', body)
            subnet = api_result.get('result', {})
            logger.info('  Created subnet id=%s', subnet.get('id'))
            # Track immediately so rollback covers this subnet if a later step fails
            result.subnets.append({
                'address': f'{subnet_addr}/{cidr}',
                'name':    sdef.name,
                'id':      subnet.get('id', ''),
            })
            if sdef.dhcp == 'true':
                dhcp_range = self.create_dhcp_range(subnet, sdef)
                subnet['_dhcp_range'] = dhcp_range
                result.dhcp_ranges.append({
                    'id':    dhcp_range.get('id', ''),
                    'start': dhcp_range.get('start', ''),
                    'end':   dhcp_range.get('end', ''),
                    'name':  f'{sdef.name}-dhcp',
                })
            if self.cfg.create_reverse_zone:
                reverse_zone = self.create_reverse_zone(subnet_addr, cidr)
                subnet['_reverse_zone'] = reverse_zone
                result.reverse_zones.append({
                    'id':   reverse_zone.get('id', ''),
                    'fqdn': reverse_zone.get('fqdn', ''),
                })
            created[sdef.name] = subnet

        return created

    # ------------------------------------------------------------------
    # DHCP range creation (called from create_subnets when dhcp=true)
    # ------------------------------------------------------------------

    def create_dhcp_range(self, subnet: dict, sdef: 'SubnetDef') -> dict:
        '''
        Create a DHCP range within the given subnet.

        The range start and end are computed as offsets from the subnet
        base address.  Offsets are taken from the SubnetDef fields when
        present, otherwise from the INI dhcp_start_offset / dhcp_end_offset
        defaults.

        Args:
            subnet: Subnet resource dict (must have 'address' and 'cidr')
            sdef:   SubnetDef with optional dhcp_start / dhcp_end offsets

        Returns:
            DHCP range resource dict (or dry-run plan dict)
        '''
        start_off = sdef.dhcp_start if sdef.dhcp_start is not None else self._dhcp_start_default
        end_off   = sdef.dhcp_end   if sdef.dhcp_end   is not None else self._dhcp_end_default

        subnet_addr = subnet.get('address', '')
        cidr = subnet.get('cidr', self.cfg.subnet_size)
        dhcp_range: dict = {}
        try:
            net = ipaddress.ip_network(f'{subnet_addr}/{cidr}', strict=False)
            start_ip = str(net.network_address + start_off)
            end_ip   = str(net.network_address + end_off)
        except ValueError as exc:
            logger.warning('Cannot compute DHCP range for subnet %s: %s', sdef.name, exc)
        else:
            logger.info(
                '%sCreating DHCP range %s-%s  subnet=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                start_ip, end_ip, sdef.name,
            )

            if self.cfg.dry_run:
                dhcp_range = {
                    'dry_run': True,
                    'start':   start_ip,
                    'end':     end_ip,
                    'name':    f'{sdef.name}-dhcp',
                }
            else:
                body = {
                    'start': start_ip,
                    'end':   end_ip,
                    'space': self._space_id,
                    'comment': f'DHCP range for {sdef.name}',
                    'tags': {
                        'Site':    self.cfg.site,
                        'Purpose': sdef.purpose,
                        'Name':    f'{sdef.name}-dhcp',
                        **self.cfg.extra_tags,
                    },
                }
                result = self.client.post('/ipam/range', body)
                dhcp_range = result.get('result', {})
                logger.info('  Created DHCP range id=%s', dhcp_range.get('id'))

        return dhcp_range

    # ------------------------------------------------------------------
    # Step 5: Update block status
    # ------------------------------------------------------------------

    def update_block_status(self, block: dict) -> dict:
        '''
        Update the address block tags to mark it as allocated and
        record the site name and provision date.

        Args:
            block: Original address block resource dict

        Returns:
            Updated address block resource dict (or dry-run plan dict)
        '''
        existing_tags = block.get('tags', {})
        updated_tags = {
            **existing_tags,
            **self.cfg.extra_tags,
            'Status':    'allocated',
            'Site':       self.cfg.site,
            'Location':   self.cfg.location,
            'Provisioned': self.cfg.date,
        }
        logger.info(
            '%sUpdating block %s/%s: Status=allocated, Site=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            block['address'], block['cidr'], self.cfg.site,
        )
        if self.cfg.dry_run:
            updated_block = {'dry_run': True, 'tags': updated_tags}
        else:
            result = self.client.patch(
                f'/{block["id"]}',
                body={'tags': updated_tags},
            )
            updated_block = result.get('result', {})
        return updated_block

    # ------------------------------------------------------------------
    # Step 6: Create DNS zone
    # ------------------------------------------------------------------

    def create_dns_zone(self) -> dict:
        '''
        Ensure the forward authoritative DNS zone exists for the site.

        Checks whether the zone already exists in the configured view
        before attempting to create it, so the script is safe to re-run
        and will not fail on duplicate-zone errors.

        Zone name: site-<site>.<dns_parent>

        Returns:
            DNS zone resource dict (existing or newly created), or a
            dry-run plan dict.  Always stores the zone ID in
            self._zone_id for use by provision_hosts().

        Raises:
            SystemExit if zone creation fails for any reason other than
            the zone already existing.
        '''
        fqdn = self.cfg.dns_zone
        logger.info(
            '%sEnsuring DNS zone exists: %s  view=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            fqdn, self.cfg.dns_view,
        )

        if self.cfg.dry_run:
            zone = {'dry_run': True, 'fqdn': fqdn, 'view': self.cfg.dns_view}
        else:
            # Check whether the zone already exists in this view
            existing = self.client.get(
                '/dns/auth_zone',
                params={
                    '_filter': (
                        f'fqdn=="{fqdn}." and '
                        f'view=="{self._view_id}"'
                    ),
                },
            )
            results = existing.get('results', [])
            if results:
                zone = results[0]
                logger.info(
                    '  Zone already exists: %s  id=%s — skipping creation',
                    fqdn, zone.get('id'),
                )
                self._zone_id = zone['id']
            else:
                # Zone does not exist
                if not self.cfg.create_zone:
                    logger.error(
                        'DNS zone "%s" does not exist in view "%s".  '
                        'Set dns.create_zone: true in the YAML template or pass '
                        '--create-zone on the CLI to create it automatically.',
                        fqdn, self.cfg.dns_view,
                    )
                    sys.exit(1)

                # create_zone is True — create the zone
                logger.info('  Zone not found — creating: %s  view=%s', fqdn, self.cfg.dns_view)
                body = {
                    'fqdn':         fqdn,
                    'view':         self._view_id,
                    'primary_type': 'cloud',
                }
                result = self.client.post('/dns/auth_zone', body)
                zone = result.get('result', {})
                self._zone_id = zone['id']
                self._zone_created = True
                logger.info('  Created zone id=%s', self._zone_id)

        return zone

    # ------------------------------------------------------------------
    # Step 6b: Create reverse DNS zone (optional)
    # ------------------------------------------------------------------

    def create_reverse_zone(self, subnet_addr: str, cidr: int) -> dict:
        '''
        Create (or locate) a reverse DNS authoritative zone for a subnet.

        Uses the same existence-check-then-create pattern as create_dns_zone().

        Args:
            subnet_addr: Subnet base address (e.g. '10.20.1.0')
            cidr:        Prefix length (e.g. 24)

        Returns:
            DNS zone resource dict (existing or newly created), or a
            dry-run plan dict.

        Note:
            Only a single classful (/8, /16, /24) reverse zone is created.
            For prefixes that span multiple reverse-zone boundaries (9–15
            or 17–23) a warning is logged because PTRs for addresses
            outside the created zone will have nowhere to live.  RFC 2317
            classless delegation is not implemented.
        '''
        if cidr not in (8, 16, 24) and not (cidr >= 24):
            logger.warning(
                'Subnet /%s spans multiple reverse zones; only %s will be '
                'created — PTRs outside it will not resolve. Use /24 subnets '
                'for full reverse coverage.',
                cidr, reverse_zone_fqdn(subnet_addr, cidr),
            )

        fqdn = reverse_zone_fqdn(subnet_addr, cidr)
        logger.info(
            '%sEnsuring reverse DNS zone: %s  view=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            fqdn, self.cfg.dns_view,
        )

        if self.cfg.dry_run:
            zone = {'dry_run': True, 'fqdn': fqdn, 'id': '(dry-run)'}
        else:
            existing = self.client.get(
                '/dns/auth_zone',
                params={
                    '_filter': (
                        f'fqdn=="{fqdn}." and '
                        f'view=="{self._view_id}"'
                    ),
                },
            )
            results = existing.get('results', [])
            if results:
                zone = results[0]
                logger.info('  Reverse zone already exists: %s  id=%s', fqdn, zone.get('id'))
            else:
                logger.info('  Creating reverse zone: %s', fqdn)
                body = {
                    'fqdn':         fqdn,
                    'view':         self._view_id,
                    'primary_type': 'cloud',
                }
                result = self.client.post('/dns/auth_zone', body)
                zone = result.get('result', {})
                logger.info('  Created reverse zone id=%s', zone.get('id'))

        return zone

    # ------------------------------------------------------------------
    # Step 7: Provision hosts
    # ------------------------------------------------------------------

    def provision_hosts(self, subnets: dict[str, dict]) -> list[dict]:
        '''
        Provision each host defined in cfg.hosts, allocating the first
        available IP in the named subnet and creating DNS A/PTR records.

        The host IP is derived as subnet_base + 1.  For multiple hosts
        in the same subnet the offset increments automatically.

        Args:
            subnets: Dict of subnet_name → subnet resource dict returned
                     by create_subnets()

        Returns:
            List of created IPAM host resource dicts (or dry-run plan
            dicts), one per host definition.
        '''
        # Track per-subnet offset so multiple hosts in the same subnet
        # get sequential IPs (.1, .2, ...)
        subnet_offsets: dict[str, int] = {}
        results = []

        for hdef in self.cfg.hosts:
            subnet = subnets.get(hdef.subnet)
            if subnet is None:
                logger.warning(
                    'Host %s references unknown subnet "%s" — skipping',
                    hdef.hostname, hdef.subnet,
                )
                continue

            base_addr = subnet.get('address', '')
            cidr = subnet.get('cidr', self.cfg.subnet_size)

            offset = subnet_offsets.get(hdef.subnet, 1)
            subnet_offsets[hdef.subnet] = offset + 1

            # Derive the host IP as subnet_base + offset using integer maths so
            # offsets that cross octet boundaries (larger subnets, many hosts)
            # are handled correctly instead of overflowing the final octet.
            try:
                net = ipaddress.ip_network(f'{base_addr}/{cidr}', strict=False)
                host_addr = net.network_address + offset
            except ValueError as exc:
                logger.warning(
                    'Cannot compute IP for host %s in subnet %s: %s — skipping',
                    hdef.hostname, hdef.subnet, exc,
                )
                continue
            if host_addr not in net:
                logger.warning(
                    'Host %s offset %d falls outside subnet %s — skipping',
                    hdef.hostname, offset, net,
                )
                continue
            host_ip = str(host_addr)

            fqdn = f'{hdef.hostname}.{self.cfg.dns_zone}'
            logger.info(
                '%sProvisioning host: %s -> %s  (subnet=%s)',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                fqdn, host_ip, hdef.subnet,
            )

            if self.cfg.dry_run:
                results.append({
                    'dry_run':  True,
                    'fqdn':     fqdn,
                    'ip':       host_ip,
                    'subnet':   hdef.subnet,
                    'hostname': hdef.hostname,
                })
                continue

            body = {
                'name':    fqdn,
                'comment': hdef.comment or f'{self.cfg.site.capitalize()} - {hdef.hostname}',
                'addresses': [{
                    'address': host_ip,
                    'space':   self._space_id,
                }],
                'auto_generate_records': True,
                # host_names drives A/PTR generation; zone is the auth-zone ID
                # and name is the label WITHIN that zone (bare hostname) — using
                # the FQDN here doubles the zone (host.zone.zone).
                # (The IpamHost schema has no dns_zone/enable_dhcp fields.)
                'host_names': [{
                    'name':         hdef.hostname,
                    'zone':         self._zone_id,
                    'primary_name': True,
                }],
            }
            result = self.client.post('/ipam/host', body)
            host = result.get('result', {})
            host['fqdn'] = fqdn
            host['ip'] = host_ip
            host['hostname'] = hdef.hostname
            logger.info('  Created host id=%s', host.get('id'))
            results.append(host)

        return results

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def _rollback(self, partial: ProvisionResult) -> None:
        '''
        Attempt to delete all resources created so far in a failed
        provisioning run, in reverse order.

        Each deletion is individually guarded — a failure to delete one
        resource is logged and skipped so the remaining rollback steps
        still run.

        Args:
            partial: ProvisionResult populated up to the point of failure
        '''
        logger.warning('Starting rollback of partial site provisioning ...')
        errors = 0

        # 1. Delete hosts
        for h in reversed(partial.hosts):
            host_id = h.get('id', '')
            if not host_id or host_id == '(dry-run)':
                continue
            try:
                logger.warning('  Rollback: deleting host %s  id=%s', h.get('fqdn', ''), host_id)
                self.client.delete(f'/{host_id}')
            except UDDIError:
                logger.error('  Rollback: failed to delete host id=%s', host_id)
                errors += 1

        # 2. Delete DNS zone (only if this run created it)
        if self._zone_created and partial.dns_zone_id and partial.dns_zone_id != '(dry-run)':
            try:
                logger.warning('  Rollback: deleting DNS zone %s  id=%s',
                               partial.dns_zone_fqdn, partial.dns_zone_id)
                self.client.delete(f'/{partial.dns_zone_id}')
            except UDDIError:
                logger.error('  Rollback: failed to delete DNS zone id=%s', partial.dns_zone_id)
                errors += 1

        # 3. Delete reverse DNS zones
        for rz in reversed(partial.reverse_zones):
            rz_id = rz.get('id', '')
            if not rz_id or rz_id == '(dry-run)':
                continue
            try:
                logger.warning('  Rollback: deleting reverse zone %s  id=%s', rz.get('fqdn', ''), rz_id)
                self.client.delete(f'/{rz_id}')
            except UDDIError:
                logger.error('  Rollback: failed to delete reverse zone id=%s', rz_id)
                errors += 1

        # 4. Delete DHCP ranges
        for r in reversed(partial.dhcp_ranges):
            range_id = r.get('id', '')
            if not range_id or range_id == '(dry-run)':
                continue
            try:
                logger.warning('  Rollback: deleting DHCP range %s-%s  id=%s',
                               r.get('start', ''), r.get('end', ''), range_id)
                self.client.delete(f'/{range_id}')
            except UDDIError:
                logger.error('  Rollback: failed to delete DHCP range id=%s', range_id)
                errors += 1

        # 5. Delete subnets
        for s in reversed(partial.subnets):
            subnet_id = s.get('id', '')
            if not subnet_id or subnet_id == '(dry-run)':
                continue
            try:
                logger.warning('  Rollback: deleting subnet %s  id=%s', s.get('address', ''), subnet_id)
                self.client.delete(f'/{subnet_id}')
            except UDDIError:
                logger.error('  Rollback: failed to delete subnet id=%s', subnet_id)
                errors += 1

        # 6. Reset block tags to available
        block = self._original_block
        if block.get('id'):
            try:
                existing_tags = block.get('tags', {})
                reset_tags = {
                    **existing_tags,
                    'Status':      'available',
                    'Site':        'unassigned',
                    'Location':    '',
                    'Provisioned': '',
                }
                logger.warning('  Rollback: resetting block %s/%s tags to available',
                               block.get('address', ''), block.get('cidr', ''))
                self.client.patch(f'/{block["id"]}', body={'tags': reset_tags})
            except UDDIError:
                logger.error('  Rollback: failed to reset block tags id=%s', block['id'])
                errors += 1

        if errors:
            logger.error('Rollback finished with %d error(s) — manual cleanup may be required', errors)
        else:
            logger.warning('Rollback complete.')
        return

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def provision(self) -> ProvisionResult:
        '''
        Run the full site provisioning sequence and return a result
        object containing all created resource IDs.

        Returns:
            ProvisionResult with details of everything created
        '''
        result = ProvisionResult(dry_run=self.cfg.dry_run)

        try:
            # Step 1: Resolve IP space
            self.resolve_ip_space()

            # Idempotency: check whether this site is already provisioned
            existing = self.find_existing_site()
            if existing:
                msg = (
                    f'Site {self.cfg.site!r} is already provisioned '
                    f'(block {existing["address"]}/{existing["cidr"]}  id={existing["id"]})'
                )
                if self.cfg.if_not_exists:
                    logger.info('%s — skipping (--if-not-exists)', msg)
                    result.skipped = True
                    result.skip_reason = 'already provisioned'
                    result.block_id = existing.get('id', '')
                    result.block_address = f'{existing["address"]}/{existing["cidr"]}'
                else:
                    logger.error('%s — use --if-not-exists to skip', msg)
                    sys.exit(1)
            else:
                # Step 2: Find available block by tags
                block = self.find_available_block()
                result.block_id = block.get('id', '')
                result.block_address = f'{block["address"]}/{block["cidr"]}'

                # Step 3: Resolve DNS view
                self.resolve_dns_view()

                # Step 4: Carve subnets (result updated incrementally inside)
                subnets = self.create_subnets(block, result)

                # Step 5: Update block status
                self.update_block_status(block)

                # Step 6: Create DNS zone
                zone = self.create_dns_zone()
                result.dns_zone_id = zone.get('id', '(dry-run)')
                result.dns_zone_fqdn = zone.get('fqdn', self.cfg.dns_zone)

                # Step 7: Provision hosts
                hosts = self.provision_hosts(subnets)
                result.hosts = [
                    {
                        'fqdn':     h.get('fqdn', ''),
                        'ip':       h.get('ip', ''),
                        'hostname': h.get('hostname', ''),
                        'id':       h.get('id', '(dry-run)'),
                    }
                    for h in hosts
                ]

        except (SystemExit, Exception) as exc:
            if not self.cfg.dry_run and not self.cfg.no_rollback:
                logger.error('Provisioning failed (%r) — initiating rollback', exc)
                self._rollback(result)
            raise SystemExit(1) from exc

        return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_result(result: ProvisionResult) -> None:
    '''
    Print a human-readable provisioning summary to stdout.

    Args:
        result: ProvisionResult from SiteProvisioner.provision()
    '''
    if result.skipped:
        print()
        print('=' * 60)
        print(f'Site already provisioned — skipped ({result.skip_reason})')
        print(f'  Address block : {result.block_address}  id={result.block_id}')
        print('=' * 60)
    else:
        mode = '[DRY-RUN] ' if result.dry_run else ''
        print()
        print('=' * 60)
        print(f'{mode}Site Provisioning Summary')
        print('=' * 60)
        print(f'  Address block : {result.block_address}')
        print()
        print('  Subnets:')
        for s in result.subnets:
            print(f'    {s["address"]:<22}  {s["name"]:<28}  id={s["id"]}')
        print()
        print(f'  DNS zone      : {result.dns_zone_fqdn}  id={result.dns_zone_id}')
        print()
        print('  Hosts:')
        for h in result.hosts:
            print(f'    {h["fqdn"]:<45}  -> {h["ip"]:<16}  id={h["id"]}')
        print('=' * 60)
        if result.dry_run:
            print('DRY-RUN complete. Rerun without --dry-run to execute.')
        else:
            print('Provisioning complete.')
        print()
    return


# ---------------------------------------------------------------------------
# Configuration and CLI
# ---------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    '''
    Add module-specific command-line arguments to the given parser.

    Args:
        parser: ArgumentParser to which the arguments are added
    '''
    # YAML template
    parser.add_argument(
        '-t', '--template',
        default=None,
        metavar='FILE',
        help='Path to a YAML site definition template',
    )

    # Site parameters (all optional when a template is provided)
    site_grp = parser.add_argument_group(
        'site parameters',
        'Override or supplement values from the YAML template',
    )
    site_grp.add_argument(
        '-s', '--site',
        default=None,
        metavar='NAME',
        help='Short site name used in subnet names and DNS zone (e.g. london)',
    )
    site_grp.add_argument(
        '-r', '--region',
        default=None,
        metavar='REGION',
        help='Geographic region tag to match on address block (e.g. EMEA)',
    )
    site_grp.add_argument(
        '-e', '--environment',
        default=None,
        metavar='ENV',
        help='Environment tag to match on address block (e.g. production)',
    )
    site_grp.add_argument(
        '-l', '--location',
        default=None,
        metavar='LOCATION',
        help='Human-readable location applied to the block (e.g. "London, UK")',
    )

    # Optional overrides
    opt_grp = parser.add_argument_group('optional overrides')
    opt_grp.add_argument(
        '--subnet-size',
        type=int,
        default=None,
        metavar='CIDR',
        help='Default subnet prefix length to carve (overrides template/config)',
    )
    opt_grp.add_argument(
        '--dns-parent',
        default=None,
        metavar='ZONE',
        help='Parent DNS zone (overrides template/config)',
    )
    opt_grp.add_argument(
        '--dns-view',
        default=None,
        metavar='VIEW',
        help='DNS view name (overrides template/config)',
    )
    opt_grp.add_argument(
        '--ip-space',
        default=None,
        metavar='SPACE',
        help='IP space name (overrides template/config)',
    )

    # Execution control
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Preview all steps without making any changes',
    )
    parser.add_argument(
        '--no-rollback',
        action='store_true',
        default=False,
        help='Do not roll back partially created resources on failure (for debugging)',
    )

    # DNS zone creation control
    # Default is None so template_to_site_config() knows no CLI flag was given
    # and can fall back to the YAML dns.create_zone value.
    zone_grp = parser.add_mutually_exclusive_group()
    zone_grp.add_argument(
        '--create-zone',
        dest='create_zone',
        action='store_const',
        const=True,
        default=None,
        help='Create the site DNS zone if it does not already exist',
    )
    zone_grp.add_argument(
        '--no-create-zone',
        dest='create_zone',
        action='store_const',
        const=False,
        help='Abort if the site DNS zone does not already exist (safe default)',
    )
    parser.add_argument(
        '--create-reverse-zone',
        dest='create_reverse_zone',
        action='store_const',
        const=True,
        default=None,
        help='Create reverse (in-addr.arpa) DNS zones for each provisioned subnet',
    )

    # Idempotency
    parser.add_argument(
        '--if-not-exists',
        dest='if_not_exists',
        action='store_true',
        default=False,
        help='Skip provisioning silently (exit 0) if the site is already provisioned; '
             'without this flag the script exits with an error when the site exists',
    )

    # Output format
    parser.add_argument(
        '--json',
        dest='json_output',
        action='store_true',
        default=False,
        help='Emit a single JSON object to stdout instead of human-readable output; '
             'log messages still go to stderr',
    )

    add_common_args(parser)

    return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    '''
    Main entry point.

    Reads configuration and optional YAML template, builds SiteConfig,
    runs the provisioner, and prints a summary.
    '''
    setup_logging(debug=args.debug, verbose=args.verbose)

    logger.debug('Arguments: %s', args)

    # Resolve credentials (CLI flag > env var > INI file)
    verify_ssl_override = None if args.verify_ssl else False
    api_key, base_url, verify_ssl = resolve_credentials(
        args.api_key, args.config, verify_ssl_override,
    )
    if not api_key:
        logger.error(
            'No API key found. Supply via --api-key, INFOBLOX_PORTAL_KEY env var, '
            'or [UDDI] api_key in %s', args.config,
        )
        sys.exit(1)

    # Load INI config for [DEFAULTS] section only
    cfg_file = read_config(args.config)
    ini_defaults = dict(cfg_file['DEFAULTS']) if cfg_file.has_section('DEFAULTS') else {}

    # Load YAML template (empty dict if none supplied)
    template: dict = {}
    if args.template:
        template = load_yaml_template(args.template)

    # Merge template + INI defaults + CLI args into SiteConfig
    site_cfg = template_to_site_config(template, ini_defaults, args)

    logger.info('Site config: %s', site_cfg)

    mode_label = '[DRY-RUN] ' if site_cfg.dry_run else ''
    if not args.json_output:
        print(f'\n{mode_label}Provisioning site: {site_cfg.site}')
        if args.template:
            print(f'  Template: {args.template}')

    # Initialise API client
    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)

    # Run provisioner
    provisioner = SiteProvisioner(client, site_cfg, ini_defaults=ini_defaults)
    result = provisioner.provision()

    # Output result
    if args.json_output:
        print(json.dumps(dataclasses.asdict(result)))
    else:
        print_result(result)
    return 0
