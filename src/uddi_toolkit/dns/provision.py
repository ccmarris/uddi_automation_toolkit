#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    DNS zone and record provisioning for Infoblox Universal DDI.

    Creates authoritative DNS zones (forward or reverse) and standalone
    DNS records (A, AAAA, CNAME, MX, TXT, PTR) from a YAML dns template.
    Unlike site provisioning — which creates A/PTR records implicitly via
    IPAM host auto-generation — this manages DNS records directly through
    the /dns/record endpoint.

    Zone creation is idempotent (existing zones are reused); records are
    skipped when an identical name/type/rdata already exists.  Supports
    --dry-run and rolls back created records then created zones on failure.

 Usage:
    provision_dns.py [-h] -t TEMPLATE [--view VIEW]
                     [--dry-run] [--no-rollback]
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

from uddi_toolkit.client import UDDIClient, UDDIError
from uddi_toolkit.core import (
    build_record_body,
    env_config,
    find_zone,
    load_yaml_template,
    read_config,
    resolve_credentials,
    resolve_dns_view,
    setup_logging,
    add_common_args,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ZoneDef:
    '''
    Definition of a DNS zone and its records.

    Attributes:
        fqdn:         Zone FQDN without trailing dot (e.g. 'corp.example.com')
        kind:         'forward' or 'reverse'
        primary_type: Zone primary type (default 'cloud')
        create:       Create the zone if absent (default True); False means
                      add records only into a pre-existing zone
        comment:      Free-text comment on the zone
        records:      List of record definition dicts (name/type/rdata/ttl)
    '''
    fqdn: str
    kind: str = 'forward'
    primary_type: str = 'cloud'
    create: bool = True
    comment: str = ''
    records: list = field(default_factory=list)


@dataclass
class DnsConfig:
    '''
    Holds all parameters needed to provision a dns template.
    '''
    view: str
    dry_run: bool = False
    no_rollback: bool = False
    extra_tags: dict = field(default_factory=dict)
    zones: list = field(default_factory=list)


@dataclass
class DnsResult:
    '''
    Accumulates zones and records created (in creation order).
    '''
    view: str = ''
    zones_created: list = field(default_factory=list)
    records_created: list = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Template loader
# ---------------------------------------------------------------------------

def template_to_dns_config(
    template: dict,
    ini_defaults: dict,
    cli_args: argparse.Namespace,
) -> DnsConfig:
    '''
    Merge a dns template, INI defaults, and CLI args into a DnsConfig.

    Args:
        template:     Parsed YAML template dict
        ini_defaults: Dict of [DEFAULTS] key/value pairs
        cli_args:     Parsed argparse Namespace

    Returns:
        Fully populated DnsConfig

    Raises:
        SystemExit if the view or zones cannot be resolved
    '''
    view = (
        getattr(cli_args, 'view', None)
        or template.get('view')
        or env_config('dns_view')
        or ini_defaults.get('dns_view', '')
        or 'default'
    )

    zones: list = []
    for z in template.get('zones') or []:
        if not isinstance(z, dict):
            logger.warning('Skipping non-mapping zone entry: %s', z)
            continue
        zones.append(ZoneDef(
            fqdn=str(z.get('fqdn', '')).strip().rstrip('.'),
            kind=str(z.get('kind', 'forward')).strip().lower(),
            primary_type=str(z.get('primary_type', 'cloud')),
            create=bool(z.get('create', True)),
            comment=str(z.get('comment', '')),
            records=list(z.get('records') or []),
        ))

    if not zones:
        logger.error('dns template has no zones to provision')
        sys.exit(1)

    extra_tags = {k: str(v) for k, v in (template.get('tags') or {}).items()}

    return DnsConfig(
        view=view,
        dry_run=getattr(cli_args, 'dry_run', False),
        no_rollback=getattr(cli_args, 'no_rollback', False),
        extra_tags=extra_tags,
        zones=zones,
    )


# ---------------------------------------------------------------------------
# DNS provisioner
# ---------------------------------------------------------------------------

class DnsProvisioner:
    '''
    Creates DNS zones and records from a DnsConfig.
    '''

    def __init__(self, client: UDDIClient, cfg: DnsConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._view_id: str = ''
        return

    def _existing_record_keys(self, zone_id: str) -> set:
        '''
        Return the set of (name_in_zone, type, rdata-json) keys already in zone.

        Args:
            zone_id: Auth zone resource ID

        Returns:
            Set of canonical record keys for idempotency checks
        '''
        keys: set = set()
        records = self.client.get_all(
            '/dns/record', params={'_filter': f'zone=="{zone_id}"'},
        )
        for rec in records:
            key = (
                rec.get('name_in_zone', ''),
                rec.get('type', ''),
                json.dumps(rec.get('rdata', {}), sort_keys=True),
            )
            keys.add(key)
        return keys

    def _ensure_zone(self, zdef: ZoneDef, result: DnsResult) -> str:
        '''
        Ensure the zone exists, creating it when permitted.

        Args:
            zdef:   ZoneDef to ensure
            result: DnsResult updated when a zone is created

        Returns:
            Zone resource ID, or '' if absent and not created (dry-run/skip)
        '''
        logger.info(
            '%sEnsuring %s zone: %s  view=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            zdef.kind, zdef.fqdn, self.cfg.view,
        )
        zone_id = ''
        if self.cfg.dry_run:
            zone_id = '(dry-run)'
        else:
            existing = find_zone(self.client, zdef.fqdn, self._view_id)
            if existing:
                logger.info('  Zone already exists: id=%s', existing.get('id'))
                zone_id = existing.get('id', '')
            elif not zdef.create:
                logger.warning(
                    '  Zone %s does not exist and create=false — skipping its records',
                    zdef.fqdn,
                )
            else:
                body = {
                    'fqdn':         zdef.fqdn,
                    'view':         self._view_id,
                    'primary_type': zdef.primary_type,
                }
                if zdef.comment:
                    body['comment'] = zdef.comment
                if self.cfg.extra_tags:
                    body['tags'] = dict(self.cfg.extra_tags)
                api_result = self.client.post('/dns/auth_zone', body)
                zone = api_result.get('result', {})
                zone_id = zone.get('id', '')
                logger.info('  Created zone id=%s', zone_id)
                result.zones_created.append({'fqdn': zdef.fqdn, 'id': zone_id})
        return zone_id

    def _create_records(self, zdef: ZoneDef, zone_id: str, result: DnsResult) -> None:
        '''
        Create the zone's records, skipping any that already exist.

        Args:
            zdef:    ZoneDef whose records to create
            zone_id: Resolved zone resource ID
            result:  DnsResult updated for each record created
        '''
        existing = set() if (self.cfg.dry_run or not zone_id or zone_id == '(dry-run)') \
            else self._existing_record_keys(zone_id)
        for rec in zdef.records:
            try:
                body = build_record_body(zone_id, rec)
            except (ValueError, TypeError) as exc:
                logger.error('  Skipping invalid record in %s: %s', zdef.fqdn, exc)
                continue
            label = body['name_in_zone'] or '@'
            logger.info(
                '%sCreating %s record %s.%s -> %s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                body['type'], label, zdef.fqdn, body['rdata'],
            )
            if self.cfg.dry_run:
                result.records_created.append({
                    'zone': zdef.fqdn, 'name': label, 'type': body['type'], 'id': '(dry-run)',
                })
                continue
            key = (body['name_in_zone'], body['type'], json.dumps(body['rdata'], sort_keys=True))
            if key in existing:
                logger.info('  Record already exists — skipping')
                continue
            api_result = self.client.post('/dns/record', body)
            record = api_result.get('result', {})
            logger.info('  Created record id=%s', record.get('id'))
            result.records_created.append({
                'zone': zdef.fqdn, 'name': label, 'type': body['type'], 'id': record.get('id', ''),
            })
        return

    def _rollback(self, result: DnsResult) -> None:
        '''
        Delete created records then created zones, in reverse order.

        Args:
            result: DnsResult populated up to the point of failure
        '''
        logger.warning('Starting rollback of created DNS resources ...')
        errors = 0
        for rec in reversed(result.records_created):
            rec_id = rec.get('id', '')
            if not rec_id or rec_id == '(dry-run)':
                continue
            try:
                logger.warning('  Rollback: deleting record %s.%s id=%s',
                               rec.get('name', ''), rec.get('zone', ''), rec_id)
                self.client.delete(f'/{rec_id}')
            except UDDIError:
                logger.error('  Rollback: failed to delete record id=%s', rec_id)
                errors += 1
        for zone in reversed(result.zones_created):
            zone_id = zone.get('id', '')
            if not zone_id or zone_id == '(dry-run)':
                continue
            try:
                logger.warning('  Rollback: deleting zone %s id=%s', zone.get('fqdn', ''), zone_id)
                self.client.delete(f'/{zone_id}')
            except UDDIError:
                logger.error('  Rollback: failed to delete zone id=%s', zone_id)
                errors += 1
        if errors:
            logger.error('Rollback finished with %d error(s) — manual cleanup may be required', errors)
        else:
            logger.warning('Rollback complete.')
        return

    def provision(self) -> DnsResult:
        '''
        Resolve the view and create all zones and records in the plan.

        Returns:
            DnsResult describing everything created
        '''
        result = DnsResult(view=self.cfg.view, dry_run=self.cfg.dry_run)
        self._view_id = resolve_dns_view(self.client, self.cfg.view)
        try:
            for zdef in self.cfg.zones:
                zone_id = self._ensure_zone(zdef, result)
                if zone_id or self.cfg.dry_run:
                    self._create_records(zdef, zone_id, result)
        except (SystemExit, Exception) as exc:
            if not self.cfg.dry_run and not self.cfg.no_rollback:
                logger.error('DNS provisioning failed (%r) — initiating rollback', exc)
                self._rollback(result)
            raise SystemExit(1) from exc
        return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_result(result: DnsResult) -> None:
    '''
    Print a human-readable provisioning summary to stdout.

    Args:
        result: DnsResult from DnsProvisioner.provision()
    '''
    mode = '[DRY-RUN] ' if result.dry_run else ''
    print()
    print('=' * 60)
    print(f'{mode}DNS Provisioning Summary')
    print('=' * 60)
    print(f'  View : {result.view}')
    print()
    print(f'  Zones created ({len(result.zones_created)}):')
    for z in result.zones_created:
        print(f'    {z["fqdn"]:<40}  id={z["id"]}')
    print()
    print(f'  Records created ({len(result.records_created)}):')
    for r in result.records_created:
        label = f'{r["name"]}.{r["zone"]}'
        print(f'    {r["type"]:<6} {label:<48}  id={r["id"]}')
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
    Add module-specific command-line arguments to the parser.

    Args:
        parser: The argparse parser to populate
    '''
    parser.add_argument('-t', '--template', required=True, metavar='FILE',
                        help='Path to a YAML dns template')
    parser.add_argument('--view', default=None, metavar='VIEW',
                        help='DNS view name (overrides template/INI)')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='Preview all steps without making any changes')
    parser.add_argument('--no-rollback', action='store_true', default=False,
                        help='Do not roll back created resources on failure (for debugging)')
    parser.add_argument('--json', dest='json_output', action='store_true', default=False,
                        help='Emit a single JSON object to stdout instead of human-readable output')
    add_common_args(parser)
    return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    '''
    Run the DNS provisioning: read config + template, build DnsConfig, run.

    Args:
        args: Parsed argparse Namespace

    Returns:
        Process exit code (0 on success)
    '''
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
    ini_defaults = dict(cfg_file['DEFAULTS']) if cfg_file.has_section('DEFAULTS') else {}

    template = load_yaml_template(args.template)
    dns_cfg = template_to_dns_config(template, ini_defaults, args)
    logger.info('DNS config: %s', dns_cfg)

    mode_label = '[DRY-RUN] ' if dns_cfg.dry_run else ''
    if not args.json_output:
        print(f'\n{mode_label}Provisioning DNS: {args.template}')

    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)
    provisioner = DnsProvisioner(client, dns_cfg)
    result = provisioner.provision()

    if args.json_output:
        print(json.dumps(dataclasses.asdict(result)))
    else:
        print_result(result)
    return 0
