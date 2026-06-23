#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Re-tag an IPAM address block's lifecycle Status for Infoblox Universal
    DDI.

    Mainly a recovery helper: a block left in Status=decommissioned is
    picked up by neither provision_site.py (wants Status=available) nor
    decommission_site.py (wants Status=allocated).  This sets the Status
    tag back to a chosen value (default: available) so the block re-enters
    the discovery pool.

    Blocks are matched either by exact address/cidr or by their Site tag,
    within the configured IP space.

 Usage:
    retag_block.py [-h] (--address ADDR --cidr CIDR | --site SITE)
                   [--status {available,allocated,decommissioned}]
                   [--ip-space SPACE] [--dry-run]
                   [-c CONFIG] [--api-key KEY] [--no-verify-ssl]
                   [--json] [-d | -v] [-V]

 Examples:
    # Return a specific block to the available pool
    retag_block.py --address 10.20.0.0 --cidr 16 -v

    # Return whatever block is tagged Site=london back to available
    retag_block.py --site london -v

 Author: Chris Marrison

 Date Last Updated: 20260623

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
import json
import logging
import sys

from uddi_client import UDDIClient, UDDIError
from uddi_utils import (
    env_config,
    read_config,
    resolve_credentials,
    resolve_ip_space,
    setup_logging,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retag logic
# ---------------------------------------------------------------------------

def find_blocks(client: UDDIClient, space_id: str, address: str,
                cidr, site: str) -> list:
    '''
    Find candidate blocks by address/cidr or by Site tag within a space.

    Args:
        client:   UDDIClient instance
        space_id: IP space resource ID
        address:  Block base address (with cidr) or '' to match by site
        cidr:     Prefix length (with address) or None
        site:     Site tag value to match, or '' to match by address/cidr

    Returns:
        List of address-block resource dicts (may be empty)
    '''
    if site:
        flt = f'space=="{space_id}" and tags.Site=="{site}"'
    else:
        flt = f'space=="{space_id}" and address=="{address}" and cidr=={int(cidr)}'
    return client.get_all('/ipam/address_block', params={'_filter': flt})


def retag(client: UDDIClient, block: dict, status: str, dry_run: bool) -> dict:
    '''
    Set a block's Status tag (and clean site fields when returning to pool).

    Args:
        client:  UDDIClient instance
        block:   Address-block resource dict
        status:  New Status tag value
        dry_run: When True, plan only

    Returns:
        Dict describing the change applied (or planned)
    '''
    tags = dict(block.get('tags', {}))
    tags['Status'] = status
    if status == 'available':
        tags['Site'] = 'unassigned'
        tags['Location'] = ''
        tags['Provisioned'] = ''
        tags['Decommissioned'] = ''
    addr = f'{block.get("address", "")}/{block.get("cidr", "")}'
    logger.info(
        '%sSetting Status=%s on block %s  id=%s',
        '[DRY-RUN] ' if dry_run else '',
        status, addr, block.get('id', ''),
    )
    if not dry_run:
        client.patch(f'/{block["id"]}', body={'tags': tags})
    return {'address': addr, 'id': block.get('id', ''), 'status': status}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_result(changed: list, status: str, dry_run: bool) -> None:
    '''
    Print a human-readable summary to stdout.

    Args:
        changed: List of change dicts from retag()
        status:  Status value applied
        dry_run: Whether this was a dry-run
    '''
    mode = '[DRY-RUN] ' if dry_run else ''
    print()
    print('=' * 60)
    print(f'{mode}Block Re-tag Summary  (Status -> {status})')
    print('=' * 60)
    if changed:
        for c in changed:
            print(f'  {c["address"]:<22}  id={c["id"]}')
    else:
        print('  No matching blocks found.')
    print('=' * 60)
    print()
    return


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parseargs() -> argparse.Namespace:
    '''
    Parse command-line arguments.

    Returns:
        Parsed argparse Namespace
    '''
    parser = argparse.ArgumentParser(
        description='Re-tag an IPAM address block lifecycle Status for Infoblox Universal DDI',
    )
    parser.add_argument('-V', '--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--address', default=None, metavar='ADDR',
                        help='Block base address (use with --cidr)')
    parser.add_argument('--cidr', default=None, type=int, metavar='CIDR',
                        help='Block prefix length (use with --address)')
    parser.add_argument('--site', default=None, metavar='NAME',
                        help='Match block(s) by Site tag instead of address/cidr')
    parser.add_argument('--status', default='available',
                        choices=['available', 'allocated', 'decommissioned'],
                        help='Status tag value to set (default: available)')
    parser.add_argument('--ip-space', default=None, metavar='SPACE',
                        help='IP space name (overrides INI default)')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='Preview without making changes')
    parser.add_argument('-c', '--config', default='uddi.ini', metavar='FILE',
                        help='Path to INI configuration file (default: uddi.ini)')
    parser.add_argument('--api-key', default='', metavar='KEY',
                        help='API key (overrides INI and INFOBLOX_PORTAL_KEY / UDDI_API_KEY env vars)')
    parser.add_argument('--no-verify-ssl', dest='verify_ssl', action='store_false', default=True,
                        help='Disable SSL certificate verification (for lab / self-signed certs)')
    parser.add_argument('--json', dest='json_output', action='store_true', default=False,
                        help='Emit a single JSON object to stdout instead of human-readable output')

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
    Main entry point: resolve credentials/space, match the block(s), retag.
    '''
    args = parseargs()
    setup_logging(debug=args.debug, verbose=args.verbose)
    logger.debug('Arguments: %s', args)

    if not args.site and not (args.address and args.cidr):
        logger.error('Specify either --site, or both --address and --cidr')
        sys.exit(1)

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
    ip_space = args.ip_space or env_config('ip_space') or ini.get('ip_space', '')
    if not ip_space:
        logger.error('No ip_space supplied via --ip-space, env, or INI [DEFAULTS].ip_space')
        sys.exit(1)

    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)
    try:
        space_id = resolve_ip_space(client, ip_space)
        blocks = find_blocks(client, space_id, args.address or '', args.cidr, args.site or '')
        changed = [retag(client, b, args.status, args.dry_run) for b in blocks]
    except UDDIError as exc:
        logger.error('Re-tag failed on API error: %s', exc)
        sys.exit(1)

    if args.json_output:
        print(json.dumps({'status': args.status, 'changed': changed, 'dry_run': args.dry_run}))
    else:
        print_result(changed, args.status, args.dry_run)
    return


if __name__ == '__main__':
    main()
