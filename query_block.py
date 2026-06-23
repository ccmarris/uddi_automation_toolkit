#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Read-only address-block inspection for Infoblox Universal DDI.

    Reports the current state of the address blocks belonging to an
    address-block template — discovered by Template tag (or by exact
    address/cidr when the template has no name) — reconstructing the
    parent/child hierarchy by containment.  Makes no changes.

 Usage:
    query_block.py [-h] -t TEMPLATE [--ip-space SPACE] [--name NAME]
                   [--json] [-c CONFIG] [--api-key KEY] [--no-verify-ssl]
                   [-d | -v] [-V]

 Author: Chris Marrison

 Date Last Updated: 20260622

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
import ipaddress
import json
import logging
import sys

from uddi_client import UDDIClient, UDDIError
from uddi_utils import (
    env_config,
    load_yaml_template,
    read_config,
    resolve_credentials,
    resolve_ip_space,
    setup_logging,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten_pairs(raw_blocks: list) -> list:
    '''
    Flatten a (possibly nested) address_blocks list to (address, cidr) pairs.

    Args:
        raw_blocks: List of block mappings from the template

    Returns:
        List of (address, cidr) tuples for every block and child
    '''
    pairs: list = []
    for raw in raw_blocks or []:
        if not isinstance(raw, dict):
            continue
        addr = str(raw.get('address', '')).strip()
        cidr = raw.get('cidr')
        if addr and cidr is not None:
            pairs.append((addr, int(cidr)))
        pairs.extend(_flatten_pairs(raw.get('children') or []))
    return pairs


def _to_node(block: dict) -> dict:
    '''
    Project a raw API block resource to the report node shape.

    Args:
        block: Address-block resource dict

    Returns:
        Node dict with address/cidr/status/region/environment/tags/children
    '''
    tags = block.get('tags') or {}
    return {
        'address':     block.get('address', ''),
        'cidr':        block.get('cidr', ''),
        'status':      tags.get('Status', ''),
        'region':      tags.get('Region', ''),
        'environment': tags.get('Environment', ''),
        'tags':        tags,
        'children':    [],
    }


def build_tree(blocks: list) -> list:
    '''
    Reconstruct a parent/child tree from a flat block list by containment.

    Each block is attached to the most-specific (largest cidr) other block
    that strictly contains it; blocks with no container become roots.

    Args:
        blocks: Flat list of address-block resource dicts

    Returns:
        List of root node dicts with nested children
    '''
    nodes = []
    for block in blocks:
        try:
            net = ipaddress.ip_network(
                f'{block.get("address")}/{int(block.get("cidr"))}', strict=False,
            )
        except (TypeError, ValueError):
            continue
        nodes.append((net, _to_node(block)))

    # Parents first (smaller cidr = larger network)
    nodes.sort(key=lambda n: (int(n[0].prefixlen), int(n[0].network_address)))
    roots: list = []
    for i, (net, node) in enumerate(nodes):
        parent = None
        for other_net, other_node in nodes[:i]:
            if net != other_net and net.subnet_of(other_net):
                if parent is None or other_net.prefixlen > parent[0].prefixlen:
                    parent = (other_net, other_node)
        if parent is None:
            roots.append(node)
        else:
            parent[1]['children'].append(node)
    return roots


# ---------------------------------------------------------------------------
# Block querier
# ---------------------------------------------------------------------------

class BlockQuerier:
    '''
    Reads the current state of an address-block template's blocks.
    '''

    def __init__(self, client: UDDIClient, name: str, ip_space: str, pairs: list) -> None:
        self.client = client
        self.name = name
        self.ip_space = ip_space
        self.pairs = pairs
        self._space_id = ''
        return

    def find_blocks(self) -> list:
        '''
        Find the template's blocks by Template tag or by address/cidr.

        Returns:
            Flat list of address-block resource dicts
        '''
        found: list = []
        if self.name:
            found = self.client.get_all(
                '/ipam/address_block',
                params={
                    '_filter': (
                        f'space=="{self._space_id}" and '
                        f'tags.Template=="{self.name}"'
                    ),
                },
            )
        else:
            seen: set = set()
            for address, cidr in self.pairs:
                data = self.client.get(
                    '/ipam/address_block',
                    params={
                        '_filter': (
                            f'space=="{self._space_id}" and '
                            f'address=="{address}" and cidr=={cidr}'
                        ),
                    },
                )
                for block in data.get('results', []):
                    if block.get('id') not in seen:
                        seen.add(block.get('id'))
                        found.append(block)
        return found

    def query(self) -> dict:
        '''
        Resolve the IP space and build the nested block report.

        Returns:
            Report dict: {type, name, ip_space, blocks: [nested nodes]}
        '''
        self._space_id = resolve_ip_space(self.client, self.ip_space)
        blocks = self.find_blocks()
        logger.info('Found %d block(s)', len(blocks))
        return {
            'type':     'address-block',
            'name':     self.name,
            'ip_space': self.ip_space,
            'blocks':   build_tree(blocks),
        }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_node(node: dict, indent: int) -> None:
    '''
    Recursively print a block node and its children.

    Args:
        node:   Block node dict
        indent: Current indentation depth
    '''
    pad = '    ' + '  ' * indent
    addr = f'{node["address"]}/{node["cidr"]}'
    extra = []
    if node['status']:
        extra.append(f'status={node["status"]}')
    if node['region']:
        extra.append(f'region={node["region"]}')
    if node['environment']:
        extra.append(f'env={node["environment"]}')
    print(f'{pad}{addr:<22}  {"  ".join(extra)}')
    for child in node['children']:
        _print_node(child, indent + 1)
    return


def print_result(result: dict) -> None:
    '''
    Print a human-readable block report to stdout.

    Args:
        result: Report dict from BlockQuerier.query()
    '''
    print()
    print('=' * 60)
    print(f'Address Block Report: {result.get("name") or "(by address)"}')
    print('=' * 60)
    print(f'  IP space : {result.get("ip_space")}')
    print()
    blocks = result.get('blocks', [])
    print(f'  Blocks ({len(blocks)} root):')
    if blocks:
        for node in blocks:
            _print_node(node, 0)
    else:
        print('    (none found)')
    print('=' * 60)
    print()
    return


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
        description='Read-only address-block inspection for Infoblox Universal DDI',
    )
    parser.add_argument('-V', '--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-t', '--template', required=True, metavar='FILE',
                        help='Path to a YAML address-block template')
    parser.add_argument('--ip-space', default=None, metavar='SPACE',
                        help='IP space name (overrides template/INI)')
    parser.add_argument('--name', default=None, metavar='NAME',
                        help='Template name to match blocks (overrides template)')
    parser.add_argument('--json', action='store_true', default=False,
                        help='Output machine-readable JSON instead of formatted text')
    parser.add_argument('-c', '--config', default='uddi.ini', metavar='FILE',
                        help='Path to INI configuration file (default: uddi.ini)')
    parser.add_argument('--api-key', default='', metavar='KEY',
                        help='API key (overrides INI and INFOBLOX_PORTAL_KEY / UDDI_API_KEY env vars)')
    parser.add_argument('--no-verify-ssl', dest='verify_ssl', action='store_false', default=True,
                        help='Disable SSL certificate verification (for lab / self-signed certs)')

    log_grp = parser.add_mutually_exclusive_group()
    log_grp.add_argument('-d', '--debug', action='store_true', default=False,
                         help='Enable DEBUG logging (shows all API calls)')
    log_grp.add_argument('-v', '--verbose', action='store_true', default=False,
                         help='Enable INFO logging')

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    '''
    Main entry point: read config + template, run the query, print a report.
    '''
    args = parseargs()
    setup_logging(debug=args.debug, verbose=args.verbose)
    logger.debug('Arguments: %s', args)

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
    ini = dict(cfg_file['DEFAULTS']) if cfg_file.has_section('DEFAULTS') else {}

    template = load_yaml_template(args.template)
    name = args.name or template.get('name', '') or ''
    ip_space = (
        args.ip_space
        or template.get('ip_space')
        or env_config('ip_space')
        or ini.get('ip_space', '')
    )
    if not ip_space:
        logger.error('No ip_space supplied via --ip-space, template, or INI [DEFAULTS].ip_space')
        sys.exit(1)

    pairs = _flatten_pairs(template.get('address_blocks') or [])

    if not args.json:
        print(f'\nQuerying address blocks: {name or args.template}')

    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)
    querier = BlockQuerier(client, str(name), ip_space, pairs)
    try:
        result = querier.query()
    except UDDIError as exc:
        logger.error('Query failed on API error: %s', exc)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_result(result)
    return


if __name__ == '__main__':
    main()
