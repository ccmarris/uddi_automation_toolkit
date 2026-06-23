#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    DNS zone and record decommissioning for Infoblox Universal DDI.

    Reverses provision_dns.py from a YAML dns template:

      * Zones created by the template (create: true) are deleted, which
        removes all records within them.
      * For pre-existing zones the template only added records to
        (create: false), just those template-defined records are deleted
        and the zone is left intact.

    Supports --dry-run to preview and --force to skip confirmation.

 Usage:
    decommission_dns.py [-h] -t TEMPLATE [--view VIEW]
                        [--dry-run] [--force]
                        [-c CONFIG] [--api-key KEY] [--no-verify-ssl]
                        [--json] [-d | -v] [-V]

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
import dataclasses
import json
import logging
import sys
from dataclasses import dataclass, field

from uddi_client import UDDIClient, UDDIError
from uddi_utils import (
    build_record_body,
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
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ZoneDef:
    '''
    Zone definition (subset needed for teardown).
    '''
    fqdn: str
    create: bool = True
    records: list = field(default_factory=list)


@dataclass
class DnsDecommissionConfig:
    '''
    Holds all parameters needed to decommission a dns template.
    '''
    view: str
    zones: list = field(default_factory=list)
    dry_run: bool = False
    force: bool = False


@dataclass
class DnsDecommissionResult:
    '''
    Accumulates zones and records removed during teardown.
    '''
    view: str = ''
    zones_deleted: list = field(default_factory=list)
    records_deleted: list = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# DNS decommissioner
# ---------------------------------------------------------------------------

class DnsDecommissioner:
    '''
    Deletes DNS zones and/or records defined by a dns template.
    '''

    def __init__(self, client: UDDIClient, cfg: DnsDecommissionConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._view_id: str = ''
        return

    def _delete_zone(self, zdef: ZoneDef, zone: dict, result: DnsDecommissionResult) -> None:
        '''
        Delete an entire zone (records cascade with it).

        Args:
            zdef:   ZoneDef being removed
            zone:   Resolved zone resource dict
            result: DnsDecommissionResult updated with the deletion
        '''
        zone_id = zone.get('id', '')
        logger.info(
            '%sDeleting zone: %s  id=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            zdef.fqdn, zone_id,
        )
        if not self.cfg.dry_run:
            self.client.delete(f'/{zone_id}')
        result.zones_deleted.append({'fqdn': zdef.fqdn, 'id': zone_id})
        return

    def _delete_records(self, zdef: ZoneDef, zone: dict, result: DnsDecommissionResult) -> None:
        '''
        Delete only the template-defined records from an existing zone.

        Args:
            zdef:   ZoneDef whose records to remove
            zone:   Resolved zone resource dict
            result: DnsDecommissionResult updated per record removed
        '''
        zone_id = zone.get('id', '')
        live = self.client.get_all('/dns/record', params={'_filter': f'zone=="{zone_id}"'})
        live_by_key: dict = {}
        for rec in live:
            key = (
                rec.get('name_in_zone', ''),
                rec.get('type', ''),
                json.dumps(rec.get('rdata', {}), sort_keys=True),
            )
            live_by_key[key] = rec.get('id', '')

        for rec in zdef.records:
            try:
                body = build_record_body(zone_id, rec)
            except (ValueError, TypeError) as exc:
                logger.warning('  Skipping invalid record in %s: %s', zdef.fqdn, exc)
                continue
            label = body['name_in_zone'] or '@'
            key = (body['name_in_zone'], body['type'], json.dumps(body['rdata'], sort_keys=True))
            rec_id = live_by_key.get(key, '')
            if not rec_id and not self.cfg.dry_run:
                logger.info('  Record not found (skipping): %s %s.%s', body['type'], label, zdef.fqdn)
                continue
            logger.info(
                '%sDeleting %s record %s.%s  id=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                body['type'], label, zdef.fqdn, rec_id or '(dry-run)',
            )
            if not self.cfg.dry_run:
                self.client.delete(f'/{rec_id}')
            result.records_deleted.append({
                'zone': zdef.fqdn, 'name': label, 'type': body['type'], 'id': rec_id or '(dry-run)',
            })
        return

    def decommission(self) -> DnsDecommissionResult:
        '''
        Resolve the view and tear down each zone per its create flag.

        Returns:
            DnsDecommissionResult describing everything removed
        '''
        result = DnsDecommissionResult(view=self.cfg.view, dry_run=self.cfg.dry_run)
        self._view_id = resolve_dns_view(self.client, self.cfg.view)
        for zdef in self.cfg.zones:
            zone = {} if self.cfg.dry_run else find_zone(self.client, zdef.fqdn, self._view_id)
            if not self.cfg.dry_run and not zone:
                logger.info('  Zone not found — nothing to remove: %s', zdef.fqdn)
                continue
            if zdef.create:
                self._delete_zone(zdef, zone or {'id': '(dry-run)'}, result)
            else:
                self._delete_records(zdef, zone or {'id': '(dry-run)'}, result)
        return result


# ---------------------------------------------------------------------------
# Confirmation + output
# ---------------------------------------------------------------------------

def confirm_decommission(cfg: DnsDecommissionConfig) -> bool:
    '''
    Prompt the operator for explicit confirmation before deleting.

    Args:
        cfg: DnsDecommissionConfig describing what will be removed

    Returns:
        True if the operator confirmed, False otherwise
    '''
    print()
    print('!' * 60)
    print('  WARNING — Destructive operation')
    print('!' * 60)
    print(f'  View  : {cfg.view}')
    print('  Zones with create=true will be DELETED (with all their records).')
    print('  Records in create=false zones will be deleted individually.')
    print()
    for zdef in cfg.zones:
        action = 'delete zone' if zdef.create else f'delete {len(zdef.records)} record(s)'
        print(f'    {zdef.fqdn:<40}  {action}')
    print()
    answer = input(f'  Type "{cfg.view}" to confirm, or press Enter to abort: ').strip()
    return answer == cfg.view


def print_result(result: DnsDecommissionResult) -> None:
    '''
    Print a human-readable decommission summary to stdout.

    Args:
        result: DnsDecommissionResult from decommission()
    '''
    mode = '[DRY-RUN] ' if result.dry_run else ''
    print()
    print('=' * 60)
    print(f'{mode}DNS Decommission Summary')
    print('=' * 60)
    print(f'  View : {result.view}')
    print()
    if result.zones_deleted:
        print(f'  Zones removed ({len(result.zones_deleted)}):')
        for z in result.zones_deleted:
            print(f'    {z["fqdn"]:<40}  id={z["id"]}')
    else:
        print('  Zones removed : none')
    print()
    if result.records_deleted:
        print(f'  Records removed ({len(result.records_deleted)}):')
        for r in result.records_deleted:
            label = f'{r["name"]}.{r["zone"]}'
            print(f'    {r["type"]:<6} {label:<48}  id={r["id"]}')
    else:
        print('  Records removed : none')
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

def parseargs() -> argparse.Namespace:
    '''
    Parse command-line arguments.

    Returns:
        Parsed argparse Namespace
    '''
    parser = argparse.ArgumentParser(
        description='DNS zone and record decommissioning for Infoblox Universal DDI',
    )
    parser.add_argument('-V', '--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-t', '--template', required=True, metavar='FILE',
                        help='Path to a YAML dns template')
    parser.add_argument('--view', default=None, metavar='VIEW',
                        help='DNS view name (overrides template/INI)')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='Preview all steps without making any changes')
    parser.add_argument('--force', action='store_true', default=False,
                        help='Skip the interactive confirmation prompt')
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
    Main entry point: read config + template, build config, confirm, run.
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

    zones: list = []
    for z in template.get('zones') or []:
        if not isinstance(z, dict):
            continue
        zones.append(ZoneDef(
            fqdn=str(z.get('fqdn', '')).strip().rstrip('.'),
            create=bool(z.get('create', True)),
            records=list(z.get('records') or []),
        ))
    if not zones:
        logger.error('dns template has no zones to decommission')
        sys.exit(1)

    cfg = DnsDecommissionConfig(
        view=view,
        zones=zones,
        dry_run=args.dry_run,
        force=args.force,
    )

    mode_label = '[DRY-RUN] ' if cfg.dry_run else ''
    if not args.json_output:
        print(f'\n{mode_label}Decommissioning DNS: {args.template}')

    if not cfg.dry_run and not cfg.force:
        if args.json_output:
            logger.error(
                'Refusing to decommission non-interactively without --force. '
                'Re-run with --force (and --json) or --dry-run.'
            )
            sys.exit(1)
        if not confirm_decommission(cfg):
            print('Aborted.')
            sys.exit(0)

    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)
    decommissioner = DnsDecommissioner(client, cfg)
    try:
        result = decommissioner.decommission()
    except UDDIError as exc:
        logger.error('Decommission aborted on API error: %s', exc)
        sys.exit(1)

    if args.json_output:
        print(json.dumps(dataclasses.asdict(result)))
    else:
        print_result(result)
    return


if __name__ == '__main__':
    main()
