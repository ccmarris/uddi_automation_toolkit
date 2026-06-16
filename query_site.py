#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Read-only site inspection script for Infoblox Universal DDI.

    Queries the current state of a provisioned site and produces a
    detailed report covering:

      1. The allocated address block and its tags
      2. All subnets carved from that block
      3. IPAM hosts within each subnet (with IP addresses)
      4. The forward DNS authoritative zone for the site

    No changes are made to the infrastructure — this script is
    safe to run at any time.

    Site identity is resolved from (highest to lowest precedence):

      1. CLI flags  (-s, --ip-space, ...)
      2. YAML template  (--template site.yaml)
      3. INI configuration defaults  (uddi.ini)

 Usage:
    query_site.py [-h] [-t TEMPLATE]
                  [-s SITE] [--dns-parent DNS_PARENT] [--dns-view DNS_VIEW]
                  [--ip-space IP_SPACE] [--json]
                  [-c CONFIG] [-d] [-v]

 Examples:
    # Human-readable report from a template
    query_site.py -t templates/site-london.yaml -v

    # Report using just the site name (other params from INI)
    query_site.py -s london -v

    # Machine-readable JSON output
    query_site.py -t templates/site-london.yaml --json

    # Pipe JSON output through jq
    query_site.py -t templates/site-london.yaml --json | python -m json.tool

 Requirements:
    Python 3.8+ with requests and PyYAML modules

    pip install requests pyyaml

 Configuration:
    Uses the same INI file as provision_site.py (default: uddi.ini):

      [UDDI]
      api_key  = <your BloxOne/Universal DDI API key>
      url      = https://csp.infoblox.com

      [DEFAULTS]
      ip_space    = my-ip-space
      dns_parent  = internal.example.com
      dns_view    = default

 Author: Chris Marrison

 Date Last Updated: 20260615

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
__version__ = '1.0.0'
__author__ = 'Chris Marrison'
__author_email__ = 'chris@infoblox.com'

import argparse
import dataclasses
import ipaddress
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

from uddi_client import UDDIClient
from uddi_utils import load_yaml_template, read_config, resolve_credentials, setup_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class QueryConfig:
    '''
    Holds all parameters needed to query a single site.

    Attributes:
        site:        Short site identifier (e.g. london)
        ip_space:    IP space name containing the allocated block
        dns_parent:  Parent DNS zone (e.g. internal.example.com)
        dns_view:    DNS view name for zone lookup
        output_json: Emit machine-readable JSON instead of formatted text
    '''
    site: str
    ip_space: str
    dns_parent: str
    dns_view: str
    output_json: bool = False

    @property
    def dns_zone(self) -> str:
        '''Fully-qualified DNS zone name for the site.'''
        return f'site-{self.site}.{self.dns_parent}'


@dataclass
class QueryResult:
    '''
    Holds the complete current state of a provisioned site.

    Attributes:
        block_id:      API resource ID of the address block
        block_address: CIDR notation of the block (e.g. 10.20.0.0/16)
        block_tags:    All tags currently on the block
        subnets:       List of subnet dicts, each with a nested 'hosts' list
        dns_zone_found: Whether the site DNS zone was found
        dns_zone_fqdn: FQDN of the site zone (may be '' if not found)
        dns_zone_id:   API resource ID of the zone (may be '' if not found)
    '''
    block_id: str = ''
    block_address: str = ''
    block_tags: dict = field(default_factory=dict)
    subnets: list = field(default_factory=list)
    dns_zone_found: bool = False
    dns_zone_fqdn: str = ''
    dns_zone_id: str = ''


# ---------------------------------------------------------------------------
# YAML template loader
# ---------------------------------------------------------------------------

def template_to_query_config(
    template: dict,
    ini_defaults: dict,
    cli_args: argparse.Namespace,
) -> QueryConfig:
    '''
    Merge a YAML template, INI defaults, and CLI arguments into a QueryConfig.

    Precedence (highest → lowest):
        CLI flags  >  YAML template  >  INI defaults  >  hardcoded fallbacks

    Args:
        template:     Parsed YAML template dict (may be empty)
        ini_defaults: Dict of key/value pairs from [DEFAULTS] in the INI file
        cli_args:     Parsed argparse Namespace from parseargs()

    Returns:
        Fully populated QueryConfig instance

    Raises:
        SystemExit if mandatory values (site, ip_space, dns_parent) cannot
        be resolved from any source
    '''
    site_sec = template.get('site', {}) or {}
    net_sec  = template.get('network', {}) or {}
    dns_sec  = template.get('dns', {}) or {}

    def resolve(cli_val, yaml_val, ini_key, fallback=''):
        '''Return the first non-None/non-empty value in priority order.'''
        if cli_val is not None and cli_val != '':
            return cli_val
        if yaml_val is not None and yaml_val != '':
            return yaml_val
        return ini_defaults.get(ini_key, fallback)

    site = resolve(
        getattr(cli_args, 'site', None),
        site_sec.get('name'),
        'site',
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
    dns_view = resolve(
        getattr(cli_args, 'dns_view', None),
        dns_sec.get('view'),
        'dns_view',
        fallback='default',
    )

    errors = []
    for label, value in [
        ('site / --site', site),
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

    return QueryConfig(
        site=site.lower(),
        ip_space=ip_space,
        dns_parent=dns_parent,
        dns_view=dns_view,
        output_json=getattr(cli_args, 'json', False),
    )


# ---------------------------------------------------------------------------
# Site querier
# ---------------------------------------------------------------------------

class SiteQuerier:
    '''
    Queries the current state of a provisioned site from the
    Infoblox Universal DDI API.  All operations are read-only.

    Steps
    -----
    1. resolve_ip_space()       - look up IP space ID from name
    2. find_allocated_block()   - tag-based discovery of allocated block
    3. resolve_dns_view()       - look up DNS view ID from name
    4. find_subnets()           - list subnets carved from the block
    5. find_hosts_in_subnet()   - list IPAM hosts per subnet
    6. find_dns_zone()          - look up forward auth zone (not fatal if absent)
    '''

    def __init__(self, client: UDDIClient, cfg: QueryConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._space_id: str = ''
        self._view_id: str = ''

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
    # Step 2: Find allocated block by Site tag
    # ------------------------------------------------------------------

    def find_allocated_block(self) -> dict:
        '''
        Search address blocks for one allocated to this site.

        Filter: tags.Site == cfg.site AND tags.Status == "allocated"

        Returns:
            Address block resource dict

        Raises:
            SystemExit if no matching block is found
        '''
        logger.info('Searching for allocated block: Site=%s', self.cfg.site)
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
        if not results:
            logger.error(
                'No allocated block found for site "%s" in space "%s". '
                'Has this site been provisioned?',
                self.cfg.site, self.cfg.ip_space,
            )
            sys.exit(1)

        block = results[0]
        logger.info(
            'Found block: %s/%s  id=%s',
            block['address'], block['cidr'], block['id'],
        )
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
    # Step 4: Find subnets in block
    # ------------------------------------------------------------------

    def find_subnets(self, block: dict) -> list:
        '''
        List all subnets carved from the given address block.

        Args:
            block: Address block resource dict

        Returns:
            List of subnet resource dicts
        '''
        logger.info('Listing subnets in block %s', block['id'])
        data = self.client.get(
            '/ipam/subnet',
            params={'_filter': f'parent=="{block["id"]}"'},
        )
        subnets = data.get('results', [])
        logger.info('Found %d subnet(s)', len(subnets))
        return subnets

    # ------------------------------------------------------------------
    # Step 5: Find hosts in a subnet
    # ------------------------------------------------------------------

    def find_hosts_in_subnet(self, subnet: dict) -> list:
        '''
        List IPAM hosts whose primary address falls within the subnet.

        Fetches all hosts in the IP space and filters client-side using
        the ipaddress module for reliable CIDR membership checking.

        Args:
            subnet: Subnet resource dict (must have 'address' and 'cidr')

        Returns:
            List of IPAM host resource dicts within the subnet
        '''
        try:
            network = ipaddress.ip_network(
                f'{subnet["address"]}/{subnet["cidr"]}', strict=False,
            )
        except (KeyError, ValueError) as exc:
            logger.warning('Cannot compute network for subnet %s: %s', subnet.get('id'), exc)
            return []

        logger.debug('Fetching all hosts to filter for subnet %s', network)
        data = self.client.get('/ipam/host')
        all_hosts = data.get('results', [])

        matching = []
        for host in all_hosts:
            for addr_entry in host.get('addresses', []):
                try:
                    ip = ipaddress.ip_address(addr_entry.get('address', ''))
                    if ip in network:
                        matching.append(host)
                        break
                except ValueError:
                    continue

        logger.debug(
            'Found %d host(s) in subnet %s/%s',
            len(matching), subnet['address'], subnet['cidr'],
        )
        return matching

    # ------------------------------------------------------------------
    # Step 6: Find DNS zone
    # ------------------------------------------------------------------

    def find_dns_zone(self) -> dict:
        '''
        Look up the forward authoritative DNS zone for the site.

        This is not fatal — a site may have been provisioned without a
        DNS zone, or the zone may have been deleted separately.

        Returns:
            DNS zone resource dict, or {} if not found
        '''
        fqdn = self.cfg.dns_zone
        logger.info('Looking up DNS zone: %s  view=%s', fqdn, self.cfg.dns_view)
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
        if results:
            logger.info('DNS zone found: id=%s', results[0].get('id'))
            return results[0]
        logger.info('DNS zone not found: %s', fqdn)
        return {}

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def query(self) -> QueryResult:
        '''
        Run the full site query sequence and return a result object.

        Returns:
            QueryResult with the complete current state of the site
        '''
        result = QueryResult()

        # Step 1: Resolve IP space
        self.resolve_ip_space()

        # Step 2: Find allocated block
        block = self.find_allocated_block()
        result.block_id = block.get('id', '')
        result.block_address = f'{block["address"]}/{block["cidr"]}'
        result.block_tags = block.get('tags', {})

        # Step 3: Resolve DNS view
        self.resolve_dns_view()

        # Step 4 + 5: Find subnets and their hosts
        subnets_raw = self.find_subnets(block)
        for subnet in subnets_raw:
            hosts_raw = self.find_hosts_in_subnet(subnet)
            hosts_out = []
            for h in hosts_raw:
                primary_ip = ''
                for addr_entry in h.get('addresses', []):
                    primary_ip = addr_entry.get('address', '')
                    break
                hosts_out.append({
                    'id':       h.get('id', ''),
                    'name':     h.get('name', ''),
                    'ip':       primary_ip,
                    'comment':  h.get('comment', ''),
                })
            result.subnets.append({
                'id':      subnet.get('id', ''),
                'address': subnet.get('address', ''),
                'cidr':    subnet.get('cidr', ''),
                'name':    subnet.get('name', ''),
                'tags':    subnet.get('tags', {}),
                'hosts':   hosts_out,
            })

        # Step 6: Find DNS zone (not fatal if absent)
        zone = self.find_dns_zone()
        if zone:
            result.dns_zone_found = True
            result.dns_zone_fqdn = zone.get('fqdn', self.cfg.dns_zone)
            result.dns_zone_id = zone.get('id', '')

        return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_result(result: QueryResult, site: str) -> None:
    '''
    Print a human-readable site report to stdout.

    Args:
        result: QueryResult from SiteQuerier.query()
        site:   Site name for the report header
    '''
    print()
    print('=' * 60)
    print(f'Site Report: {site}')
    print('=' * 60)
    print(f'  Block       : {result.block_address}  id={result.block_id}')

    if result.block_tags:
        tag_str = '  '.join(f'{k}={v}' for k, v in sorted(result.block_tags.items()))
        print(f'  Block tags  : {tag_str}')

    print()
    print(f'  Subnets ({len(result.subnets)}):')
    for subnet in result.subnets:
        addr = f'{subnet["address"]}/{subnet["cidr"]}'
        print(f'    {addr:<22}  {subnet["name"]:<28}  id={subnet["id"]}')

        tags = subnet.get('tags', {})
        if tags:
            tag_str = '  '.join(f'{k}={v}' for k, v in sorted(tags.items()))
            print(f'      tags: {tag_str}')

        hosts = subnet.get('hosts', [])
        print(f'      hosts ({len(hosts)}):')
        for h in hosts:
            print(f'        {h["name"]:<48}  {h["ip"]:<16}  id={h["id"]}')
            if h.get('comment'):
                print(f'          comment: {h["comment"]}')

    print()
    if result.dns_zone_found:
        print(f'  DNS zone    : {result.dns_zone_fqdn}  id={result.dns_zone_id}')
    else:
        print('  DNS zone    : (not found)')

    print('=' * 60)
    print()


def print_json_result(result: QueryResult) -> None:
    '''
    Print the query result as formatted JSON to stdout.

    Args:
        result: QueryResult from SiteQuerier.query()
    '''
    print(json.dumps(dataclasses.asdict(result), indent=2))


# ---------------------------------------------------------------------------
# Configuration and CLI
# ---------------------------------------------------------------------------

def parseargs() -> argparse.Namespace:
    '''
    Parse command-line arguments.

    Returns:
        Parsed argparse Namespace
    '''
    parser = argparse.ArgumentParser(
        description='Read-only site inspection for Infoblox Universal DDI',
        epilog=(
            'Site parameters are resolved from (highest to lowest priority): '
            'CLI flags > YAML template (--template) > INI file defaults.'
        ),
    )

    parser.add_argument(
        '-V', '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )

    parser.add_argument(
        '-t', '--template',
        default=None,
        metavar='FILE',
        help='Path to a YAML site definition template',
    )

    site_grp = parser.add_argument_group(
        'site parameters',
        'Override or supplement values from the YAML template',
    )
    site_grp.add_argument(
        '-s', '--site',
        default=None,
        metavar='NAME',
        help='Short site name (e.g. london)',
    )
    site_grp.add_argument(
        '--dns-parent',
        default=None,
        metavar='ZONE',
        help='Parent DNS zone (overrides template/config)',
    )
    site_grp.add_argument(
        '--dns-view',
        default=None,
        metavar='VIEW',
        help='DNS view name (overrides template/config)',
    )
    site_grp.add_argument(
        '--ip-space',
        default=None,
        metavar='SPACE',
        help='IP space name (overrides template/config)',
    )

    parser.add_argument(
        '--json',
        action='store_true',
        default=False,
        help='Output machine-readable JSON instead of formatted text',
    )

    parser.add_argument(
        '-c', '--config',
        default='uddi.ini',
        metavar='FILE',
        help='Path to INI configuration file (default: uddi.ini in current working directory)',
    )
    parser.add_argument(
        '--api-key',
        default='',
        metavar='KEY',
        help='API key (overrides INI file and INFOBLOX_PORTAL_KEY / UDDI_API_KEY env vars)',
    )
    parser.add_argument(
        '--no-verify-ssl',
        dest='verify_ssl',
        action='store_false',
        default=True,
        help='Disable SSL certificate verification (for lab / self-signed certs)',
    )

    log_grp = parser.add_mutually_exclusive_group()
    log_grp.add_argument(
        '-d', '--debug',
        action='store_true',
        default=False,
        help='Enable DEBUG logging (shows all API calls)',
    )
    log_grp.add_argument(
        '-v', '--verbose',
        action='store_true',
        default=False,
        help='Enable INFO logging',
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    '''
    Main entry point.

    Reads configuration and optional YAML template, builds QueryConfig,
    runs the querier, and prints a site report.
    '''
    args = parseargs()
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

    cfg_file = read_config(args.config)
    ini_defaults = dict(cfg_file['DEFAULTS']) if cfg_file.has_section('DEFAULTS') else {}

    template: dict = {}
    if args.template:
        template = load_yaml_template(args.template)

    query_cfg = template_to_query_config(template, ini_defaults, args)

    logger.info('Query config: %s', query_cfg)

    if not args.json:
        print(f'\nQuerying site: {query_cfg.site}')
        if args.template:
            print(f'  Template: {args.template}')

    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)

    querier = SiteQuerier(client, query_cfg)
    result = querier.query()

    if query_cfg.output_json:
        print_json_result(result)
    else:
        print_result(result, query_cfg.site)


if __name__ == '__main__':
    main()
