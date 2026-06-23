#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Read-only DNS inspection for Infoblox Universal DDI.

    For each zone named in a dns template, reports whether the zone exists
    in the configured view and lists its current records.  Makes no
    changes.

 Usage:
    query_dns.py [-h] -t TEMPLATE [--view VIEW] [--json]
                 [-c CONFIG] [--api-key KEY] [--no-verify-ssl]
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
import json
import logging
import sys

from uddi_client import UDDIClient, UDDIError
from uddi_utils import (
    env_config,
    find_zone,
    load_yaml_template,
    read_config,
    resolve_credentials,
    resolve_dns_view,
    setup_logging,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rdata_str(rdata: dict) -> str:
    '''
    Render an rdata mapping as a compact human-readable string.

    Args:
        rdata: Record rdata dict from the API

    Returns:
        Single-value string, or 'k=v k=v' for multi-field rdata
    '''
    if not isinstance(rdata, dict):
        text = str(rdata)
    elif len(rdata) == 1:
        text = str(next(iter(rdata.values())))
    else:
        text = ' '.join(f'{k}={v}' for k, v in rdata.items())
    return text


# ---------------------------------------------------------------------------
# DNS querier
# ---------------------------------------------------------------------------

class DnsQuerier:
    '''
    Reads the current state of a dns template's zones and records.
    '''

    def __init__(self, client: UDDIClient, view: str, fqdns: list) -> None:
        self.client = client
        self.view = view
        self.fqdns = fqdns
        self._view_id = ''
        return

    def query(self) -> dict:
        '''
        Resolve the view and report each template zone's state.

        Returns:
            Report dict: {type, view, zones: [{fqdn, id, found, records}]}
        '''
        self._view_id = resolve_dns_view(self.client, self.view)
        zones_out = []
        for fqdn in self.fqdns:
            zone = find_zone(self.client, fqdn, self._view_id)
            records_out = []
            if zone:
                records = self.client.get_all(
                    '/dns/record', params={'_filter': f'zone=="{zone.get("id")}"'},
                )
                for rec in records:
                    records_out.append({
                        'name': rec.get('name_in_zone', '') or '@',
                        'type': rec.get('type', ''),
                        'rdata': _rdata_str(rec.get('rdata', {})),
                        'ttl': rec.get('ttl', ''),
                    })
            zones_out.append({
                'fqdn':    fqdn,
                'id':      zone.get('id', '') if zone else '',
                'found':   bool(zone),
                'records': records_out,
            })
        logger.info('Queried %d zone(s)', len(zones_out))
        return {'type': 'dns', 'view': self.view, 'zones': zones_out}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_result(result: dict) -> None:
    '''
    Print a human-readable DNS report to stdout.

    Args:
        result: Report dict from DnsQuerier.query()
    '''
    print()
    print('=' * 60)
    print('DNS Report')
    print('=' * 60)
    print(f'  View : {result.get("view")}')
    print()
    for zone in result.get('zones', []):
        status = 'found' if zone['found'] else 'NOT FOUND'
        print(f'  Zone: {zone["fqdn"]}  ({status})  id={zone["id"]}')
        for rec in zone['records']:
            label = f'{rec["name"]}'
            print(f'      {rec["type"]:<6} {label:<24} {rec["rdata"]}')
        if zone['found'] and not zone['records']:
            print('      (no records)')
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
        description='Read-only DNS inspection for Infoblox Universal DDI',
    )
    parser.add_argument('-V', '--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-t', '--template', required=True, metavar='FILE',
                        help='Path to a YAML dns template')
    parser.add_argument('--view', default=None, metavar='VIEW',
                        help='DNS view name (overrides template/INI)')
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
    view = (
        args.view
        or template.get('view')
        or env_config('dns_view')
        or ini.get('dns_view', '')
        or 'default'
    )
    fqdns = [
        str(z.get('fqdn', '')).strip().rstrip('.')
        for z in (template.get('zones') or [])
        if isinstance(z, dict) and str(z.get('fqdn', '')).strip()
    ]
    if not fqdns:
        logger.error('dns template has no zones to query')
        sys.exit(1)

    if not args.json:
        print(f'\nQuerying DNS: {args.template}')

    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)
    querier = DnsQuerier(client, view, fqdns)
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
