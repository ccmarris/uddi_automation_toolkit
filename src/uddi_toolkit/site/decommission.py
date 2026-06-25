#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Tag-driven site decommissioning script for Infoblox Universal DDI.

    Automates the full teardown of a provisioned site, using the Site
    and Status tags on address blocks to drive discovery — the exact
    reverse of provision_site.py.

    Decommission sequence:

      1. Find the site's subnets by their Site=<name> tag (the pool
         address block is shared and is never tagged for the site)
      2. Delete the forward DNS authoritative zone for the site
         (site-<name>.<dns_parent>)
      3. Delete DHCP ranges and reverse zones for the site subnets
      4. Delete all the site's subnets — this releases the host addresses
         (a DHCP-bound address cannot be deleted while the host holds it)
      5. Delete the now-addressless IPAM host records (removes the IPAM
         record + auto-generated DNS A/PTR), matched by the site DNS zone

    The shared pool address block is left untouched throughout, so other
    sites in the same block are unaffected and the block stays available.

    All destructive steps support --dry-run so you can preview the
    full plan before committing any changes.

    --force skips the interactive confirmation prompt.

 Usage:
    decommission_site.py [-h] [-t FILE] [-s SITE]
                         [--keep-zone] [--dns-parent ZONE]
                         [--dns-view VIEW] [--ip-space SPACE]
                         [--dry-run] [--force]
                         [-c CONFIG] [-d | -v] [-V]

 Examples:
    # Dry-run using the same YAML template used to provision
    decommission_site.py -t templates/site-london.yaml --dry-run -v

    # Full decommission from template (prompts for confirmation)
    decommission_site.py -t templates/site-london.yaml -v

    # Template + CLI override (CLI wins)
    decommission_site.py -t templates/site-london.yaml --dns-view other -v

    # CLI-only (no template)
    decommission_site.py -s london --dry-run -v

    # Skip confirmation (for pipelines / batch runs)
    decommission_site.py -t templates/site-london.yaml --force -v

    # Keep the DNS zone (hosts only removed from IPAM, not DNS)
    decommission_site.py -t templates/site-london.yaml --keep-zone -v

 Configuration:
    Shares the same INI file as provision_site.py (default:
    uddi.ini):

      [UDDI]
      api_key  = <your BloxOne/Universal DDI API key>
      url      = https://csp.infoblox.com

      [DEFAULTS]
      ip_space    = my-ip-space
      dns_parent  = internal.example.com
      dns_view    = default

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
__version__ = '1.1.0'
__author__ = 'Chris Marrison'
__author_email__ = 'chris@infoblox.com'

import argparse
import dataclasses
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
class DecommissionConfig:
    '''
    Holds all parameters needed to decommission a single site.

    Attributes:
        site:         Short site name (matches Site tag on address block)
        ip_space:     IP space to search for the allocated block
        dns_parent:   Parent DNS zone (used to derive site zone FQDN)
        dns_view:     DNS view containing the site zone
        keep_zone:    When True, leave the DNS zone intact
        dry_run:      When True, print plan but make no API changes
        force:        When True, skip interactive confirmation prompt
    '''
    site: str
    ip_space: str
    dns_parent: str
    dns_view: str
    keep_zone: bool = False
    dry_run: bool = False
    force: bool = False

    @property
    def dns_zone(self) -> str:
        '''Fully-qualified DNS zone name for the site.'''
        return f'site-{self.site}.{self.dns_parent}'


@dataclass
class DecommissionResult:
    '''
    Accumulates counts and IDs of resources removed during teardown.
    '''
    site: str = ''
    ip_space: str = ''
    hosts_deleted: list[dict] = field(default_factory=list)
    dhcp_ranges_deleted: list[dict] = field(default_factory=list)
    dns_zone_deleted: bool = False
    dns_zone_fqdn: str = ''
    reverse_zones_deleted: list[dict] = field(default_factory=list)
    subnets_deleted: list[dict] = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# YAML template loader
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Site decommissioner
# ---------------------------------------------------------------------------

class SiteDecommissioner:
    '''
    Orchestrates the full site teardown sequence against the
    Infoblox Universal DDI API.

    Steps
    -----
    1. resolve_ip_space()     - look up IP space ID from name
    2. resolve_dns_view()     - look up DNS view ID from name
    3. find_subnets()         - list the site's subnets (by Site tag)
    4. delete_dns_zone()      - delete forward authoritative zone
    5. delete_dhcp_ranges()   - delete DHCP ranges within each subnet
    6. delete_reverse_zones() - delete reverse zones for the subnets
    7. delete_subnets()       - remove all carved subnets (releases host IPs)
    8. delete_hosts()         - remove the now-addressless IPAM host records

    The pool address block is shared and is never tagged for the site, so it
    is neither discovered nor reset here.
    '''

    def __init__(self, client: UDDIClient, cfg: DecommissionConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._space_id: str = ''
        self._view_id: str = ''
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
        self._space_id = results[0]['id']
        logger.debug('IP space ID: %s', self._space_id)
        return self._space_id

    # ------------------------------------------------------------------
    # Step 2: Resolve DNS view
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
        self._view_id = results[0]['id']
        logger.debug('DNS view ID: %s', self._view_id)
        return self._view_id

    # ------------------------------------------------------------------
    # Step 4: Find subnets inside the block
    # ------------------------------------------------------------------

    def find_subnets(self) -> list[dict]:
        '''
        List all subnets belonging to this site.

        Subnets are matched by their Site tag within the IP space.  This is
        how a site is identified — the pool address block is shared and is
        never tagged for the site, so there is no per-site block to find.
        (Provisioning stamps Site=<name> on every subnet, which may nest
        under a child of the pool block.)

        Returns:
            List of subnet resource dicts (may be empty)
        '''
        logger.info('Listing subnets for site %s', self.cfg.site)
        subnets = self.client.get_all(
            '/ipam/subnet',
            params={
                '_filter': (
                    f'space=="{self._space_id}" and '
                    f'tags.Site=="{self.cfg.site}"'
                ),
            },
        )
        logger.info('  Found %d subnet(s)', len(subnets))
        for s in subnets:
            logger.debug('  Subnet: %s/%s  name=%s  id=%s',
                         s['address'], s['cidr'], s.get('name', ''), s['id'])
        return subnets

    # ------------------------------------------------------------------
    # Step 5: Delete IPAM hosts in site subnets
    # ------------------------------------------------------------------

    def delete_hosts(self) -> list[dict]:
        '''
        Find and delete all IPAM host records belonging to this site.

        Hosts are matched by their FQDN within the site DNS zone
        (site-<name>.<dns_parent>), not by subnet membership, so this can
        run AFTER the subnets are deleted.  That ordering matters: a
        DHCP-bound host address reports "in use" and refuses host deletion
        while the host still holds it, but deleting the subnet first
        releases the address, leaving an addressless host record that
        deletes cleanly.  Deleting an IPAM host removes its auto-generated
        DNS A/PTR records.

        Returns:
            List of dicts describing each deleted (or dry-run) host
        '''
        removed: list[dict] = []
        suffix = f'.{self.cfg.dns_zone}'

        logger.debug('Fetching all hosts to match site zone %s', self.cfg.dns_zone)
        all_hosts = self.client.get_all('/ipam/host')
        site_hosts = [h for h in all_hosts if str(h.get('name', '')).endswith(suffix)]

        for host in site_hosts:
            fqdn = host.get('name', host.get('id', 'unknown'))
            host_id = host['id']
            logger.info(
                '%sDeleting host: %s  id=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                fqdn, host_id,
            )
            if not self.cfg.dry_run:
                self.client.delete(f'/{host_id}')
            removed.append({'fqdn': fqdn, 'id': host_id})

        if not removed:
            logger.info('  No IPAM hosts found for site zone %s', self.cfg.dns_zone)

        return removed

    # ------------------------------------------------------------------
    # Step 6: Delete DNS zone
    # ------------------------------------------------------------------

    def delete_dns_zone(self) -> bool:
        '''
        Delete the forward authoritative DNS zone for the site, if it
        exists.  All records inside the zone are removed with it.

        When cfg.keep_zone is True this step is skipped and False is
        returned so the caller can reflect that in the summary.

        Returns:
            True if the zone was deleted (or would be in dry-run),
            False if it was not found or keep_zone is set.
        '''
        fqdn = self.cfg.dns_zone
        deleted = False

        if self.cfg.keep_zone:
            logger.info('--keep-zone set — skipping deletion of zone: %s', fqdn)
        else:
            logger.info(
                '%sLooking up DNS zone: %s  view=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                fqdn, self.cfg.dns_view,
            )

            data = self.client.get(
                '/dns/auth_zone',
                params={
                    '_filter': (
                        f'fqdn=="{fqdn}." and '
                        f'view=="{self._view_id}"'
                    ),
                },
            )
            results = data.get('results', [])
            if not results:
                logger.info('  DNS zone not found — nothing to delete: %s', fqdn)
            else:
                zone = results[0]
                zone_id = zone['id']
                logger.info(
                    '%sDeleting DNS zone: %s  id=%s',
                    '[DRY-RUN] ' if self.cfg.dry_run else '',
                    fqdn, zone_id,
                )
                if not self.cfg.dry_run:
                    self.client.delete(f'/{zone_id}')
                deleted = True

        return deleted

    # ------------------------------------------------------------------
    # Step 6c: Delete reverse DNS zones for subnets
    # ------------------------------------------------------------------

    def delete_reverse_zones(self, subnets: list[dict]) -> list[dict]:
        '''
        Delete reverse (in-addr.arpa) DNS zones for all provided subnets,
        if they exist in the configured DNS view.

        Zones that are not found are silently skipped — it is valid to
        decommission a site that was provisioned without reverse zones.

        Args:
            subnets: List of subnet resource dicts (must have 'address'
                     and 'cidr')

        Returns:
            List of dicts describing each deleted (or dry-run) zone
        '''
        deleted: list[dict] = []
        for subnet in subnets:
            try:
                fqdn = reverse_zone_fqdn(subnet['address'], int(subnet['cidr']))
            except (KeyError, ValueError) as exc:
                logger.warning('Cannot compute reverse zone for subnet %s: %s',
                               subnet.get('id'), exc)
                continue

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
            if not results:
                logger.debug('Reverse zone not found (skipping): %s', fqdn)
                continue

            zone = results[0]
            zone_id = zone.get('id', '')
            logger.info(
                '%sDeleting reverse zone: %s  id=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                fqdn, zone_id,
            )
            if not self.cfg.dry_run:
                self.client.delete(f'/{zone_id}')
            deleted.append({'id': zone_id, 'fqdn': fqdn})

        return deleted

    # ------------------------------------------------------------------
    # Step 6b: Delete DHCP ranges within a subnet
    # ------------------------------------------------------------------

    def delete_dhcp_ranges(self, subnet: dict) -> list[dict]:
        '''
        Delete all DHCP ranges that belong to the given subnet.

        Args:
            subnet: Subnet resource dict (must have 'id')

        Returns:
            List of dicts describing each deleted (or dry-run) DHCP range
        '''
        all_ranges = self.client.get_all('/ipam/range')
        ranges = [
            r for r in all_ranges
            if r.get('space') == self._space_id
            and _in_subnet(r.get('start', ''), subnet)
        ]
        deleted: list[dict] = []
        for r in ranges:
            range_id = r.get('id', '')
            logger.info(
                '%sDeleting DHCP range %s-%s  id=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                r.get('start', ''), r.get('end', ''), range_id,
            )
            if not self.cfg.dry_run:
                self.client.delete(f'/{range_id}')
            deleted.append({
                'id':    range_id,
                'start': r.get('start', ''),
                'end':   r.get('end', ''),
            })
        return deleted

    # ------------------------------------------------------------------
    # Step 7: Delete subnets
    # ------------------------------------------------------------------

    def delete_subnets(self, subnets: list[dict]) -> list[dict]:
        '''
        Delete all subnets in the provided list.

        Args:
            subnets: List of subnet resource dicts from find_subnets()

        Returns:
            List of dicts describing each deleted (or dry-run) subnet
        '''
        removed: list[dict] = []
        for subnet in subnets:
            subnet_cidr = f'{subnet["address"]}/{subnet["cidr"]}'
            subnet_id = subnet['id']
            logger.info(
                '%sDeleting subnet: %s  name=%s  id=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                subnet_cidr, subnet.get('name', ''), subnet_id,
            )
            if not self.cfg.dry_run:
                self.client.delete(f'/{subnet_id}')
            removed.append({
                'address': subnet_cidr,
                'name':    subnet.get('name', ''),
                'id':      subnet_id,
            })
        return removed

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def decommission(self) -> DecommissionResult:
        '''
        Run the full site decommission sequence and return a result
        object summarising everything that was removed.

        Returns:
            DecommissionResult with details of all removed resources
        '''
        result = DecommissionResult(
            site=self.cfg.site,
            ip_space=self.cfg.ip_space,
            dry_run=self.cfg.dry_run,
        )

        # Step 1: Resolve IP space
        self.resolve_ip_space()

        # Step 2: Resolve DNS view
        self.resolve_dns_view()

        # Step 3: Enumerate the site's subnets (by Site tag). The pool block
        # is shared and never tagged for the site, so there is no block to
        # find or reset.
        subnets = self.find_subnets()
        if not subnets:
            logger.warning('No subnets tagged Site=%s found; will still remove any zone/records',
                           self.cfg.site)

        # Step 4: Delete DNS zone
        result.dns_zone_fqdn = self.cfg.dns_zone
        result.dns_zone_deleted = self.delete_dns_zone()

        # Step 5: Delete DHCP ranges in each subnet
        for subnet in subnets:
            result.dhcp_ranges_deleted.extend(self.delete_dhcp_ranges(subnet))

        # Step 6: Delete reverse DNS zones for subnets (silently skips absent zones)
        result.reverse_zones_deleted = self.delete_reverse_zones(subnets)

        # Step 7: Delete subnets — this releases the host addresses so the
        # host records (whose DHCP-bound addresses are otherwise "in use")
        # can then be removed.
        result.subnets_deleted = self.delete_subnets(subnets)

        # Step 8: Delete the now-addressless IPAM host records (and their
        # auto-generated DNS A/PTR), matched by the site DNS zone.
        result.hosts_deleted = self.delete_hosts()

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_subnet(address: str, subnet: dict) -> bool:
    '''
    Return True if address falls within the subnet's IP range.

    Uses a simple integer comparison on the 32-bit IPv4 representation
    so no external libraries are required.

    Args:
        address: Dotted-quad IPv4 address string
        subnet:  Subnet resource dict with 'address' and 'cidr' keys

    Returns:
        True if the address is within the subnet, False otherwise
    '''
    in_subnet = False
    try:
        def to_int(ip: str) -> int:
            parts = ip.split('.')
            result = 0
            for part in parts:
                result = (result << 8) | int(part)
            return result

        cidr = int(subnet['cidr'])
        mask = (0xFFFFFFFF << (32 - cidr)) & 0xFFFFFFFF
        net_int  = to_int(subnet['address']) & mask
        addr_int = to_int(address) & mask
        in_subnet = addr_int == net_int
    except (ValueError, KeyError):
        in_subnet = False

    return in_subnet


def confirm_decommission(cfg: DecommissionConfig) -> bool:
    '''
    Print a prominent warning and prompt the operator for explicit
    confirmation before any destructive changes are made.

    Args:
        cfg: DecommissionConfig describing what will be removed

    Returns:
        True if the operator confirmed, False if they declined
    '''
    print()
    print('!' * 60)
    print('  WARNING — Destructive operation')
    print('!' * 60)
    print(f'  Site      : {cfg.site}')
    print(f'  IP space  : {cfg.ip_space}')
    print(f'  DNS zone  : {cfg.dns_zone}')
    print(f'  DNS view  : {cfg.dns_view}')
    if cfg.keep_zone:
        print('  DNS zone  : WILL BE KEPT (--keep-zone)')
    else:
        print('  DNS zone  : WILL BE DELETED')
    print()
    print('  The following will be permanently removed:')
    print('    • All IPAM host records in site subnets')
    if not cfg.keep_zone:
        print('    • The site DNS authoritative zone and all its records')
    print('    • All site subnets')
    print('  The shared pool address block is left unchanged.')
    print()

    answer = input('  Type the site name to confirm, or press Enter to abort: ').strip()
    return answer == cfg.site


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_result(result: DecommissionResult) -> None:
    '''
    Print a human-readable decommission summary to stdout.

    Args:
        result: DecommissionResult from SiteDecommissioner.decommission()
    '''
    mode = '[DRY-RUN] ' if result.dry_run else ''
    print()
    print('=' * 60)
    print(f'{mode}Site Decommission Summary')
    print('=' * 60)
    print(f'  Site     : {result.site}')
    print(f'  IP space : {result.ip_space}')
    print()

    if result.hosts_deleted:
        print(f'  Hosts removed ({len(result.hosts_deleted)}):')
        for h in result.hosts_deleted:
            print(f'    {h["fqdn"]:<45}  id={h["id"]}')
    else:
        print('  Hosts removed : none found')
    print()

    if result.dns_zone_deleted:
        print(f'  DNS zone deleted : {result.dns_zone_fqdn}')
    else:
        print(f'  DNS zone         : kept / not found ({result.dns_zone_fqdn})')
    print()

    if result.subnets_deleted:
        print(f'  Subnets removed ({len(result.subnets_deleted)}):')
        for s in result.subnets_deleted:
            print(f'    {s["address"]:<22}  {s["name"]:<28}  id={s["id"]}')
    else:
        print('  Subnets removed : none found')
    print()

    print('  Pool block    : left unchanged (shared by other sites)')
    print('=' * 60)

    if result.dry_run:
        print('DRY-RUN complete. Rerun without --dry-run to execute.')
    else:
        print('Decommission complete.')
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
    # YAML template (same format as provision_site.py)
    parser.add_argument(
        '-t', '--template',
        default=None,
        metavar='FILE',
        help='Path to a YAML site definition template (same file used to provision)',
    )

    # Site name — optional when a template is provided
    parser.add_argument(
        '-s', '--site',
        default=None,
        metavar='NAME',
        help='Short site name to decommission (must match the Site tag on the block)',
    )

    # Optional overrides
    opt_grp = parser.add_argument_group('optional overrides')
    opt_grp.add_argument(
        '--ip-space',
        default=None,
        metavar='SPACE',
        help='IP space name (overrides INI default)',
    )
    opt_grp.add_argument(
        '--dns-parent',
        default=None,
        metavar='ZONE',
        help='Parent DNS zone used to derive the site zone FQDN (overrides INI default)',
    )
    opt_grp.add_argument(
        '--dns-view',
        default=None,
        metavar='VIEW',
        help='DNS view name (overrides INI default)',
    )
    opt_grp.add_argument(
        '--keep-zone',
        action='store_true',
        default=False,
        help='Leave the site DNS zone intact (skip zone deletion step)',
    )

    # Execution control
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Preview all steps without making any changes',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='Skip the interactive confirmation prompt',
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

    Reads INI configuration, builds DecommissionConfig, optionally
    prompts for confirmation, runs the decommissioner, and prints a
    summary.
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
    ini = dict(cfg_file['DEFAULTS']) if cfg_file.has_section('DEFAULTS') else {}

    # Load YAML template (empty dict if none supplied)
    template: dict = {}
    if args.template:
        template = load_yaml_template(args.template)

    # Extract template sections (all optional keys)
    site_sec = template.get('site', {}) or {}
    net_sec  = template.get('network', {}) or {}
    dns_sec  = template.get('dns', {}) or {}

    # Resolve each value: CLI > YAML template > env var > INI > error
    def resolve(cli_val: Optional[str], yaml_val, ini_key: str, label: str) -> str:
        if cli_val:
            resolved = cli_val
        elif yaml_val:
            resolved = str(yaml_val)
        else:
            env_val = env_config(ini_key)
            if env_val:
                resolved = env_val
            else:
                resolved = ini.get(ini_key, '')
                if not resolved:
                    logger.error(
                        'Required value "%s" not supplied via CLI flag, YAML template, '
                        'or INI [DEFAULTS].%s',
                        label, ini_key,
                    )
                    sys.exit(1)
        return resolved

    site       = resolve(args.site,       site_sec.get('name'),    'site',       '--site / site.name')
    ip_space   = resolve(args.ip_space,   net_sec.get('ip_space'), 'ip_space',   '--ip-space / network.ip_space')
    dns_parent = resolve(args.dns_parent, dns_sec.get('parent'),   'dns_parent', '--dns-parent / dns.parent')
    dns_view   = resolve(args.dns_view,   dns_sec.get('view'),     'dns_view',   '--dns-view / dns.view')

    cfg = DecommissionConfig(
        site=site.lower(),
        ip_space=ip_space,
        dns_parent=dns_parent,
        dns_view=dns_view,
        keep_zone=args.keep_zone,
        dry_run=args.dry_run,
        force=args.force,
    )

    mode_label = '[DRY-RUN] ' if cfg.dry_run else ''
    if not args.json_output:
        print(f'\n{mode_label}Decommissioning site: {cfg.site}')
        if args.template:
            print(f'  Template: {args.template}')

    # Confirmation gate — skipped only in dry-run mode or with explicit --force.
    # --json no longer bypasses confirmation on its own: a non-interactive run
    # that makes destructive changes must opt in with --force.
    if not cfg.dry_run and not cfg.force:
        if args.json_output or not sys.stdin.isatty():
            logger.error(
                'Refusing to decommission non-interactively without --force. '
                'Re-run with --force, or use --dry-run to preview.'
            )
            sys.exit(1)
        if not confirm_decommission(cfg):
            print('Aborted.')
            sys.exit(0)

    # Initialise API client
    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)

    # Run decommissioner
    decommissioner = SiteDecommissioner(client, cfg)
    try:
        result = decommissioner.decommission()
    except UDDIError as exc:
        logger.error('Decommission aborted on API error: %s', exc)
        sys.exit(1)

    # Output result
    if args.json_output:
        print(json.dumps(dataclasses.asdict(result)))
    else:
        print_result(result)
    return 0
