#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Address-block provisioning for Infoblox Universal DDI.

    Creates one or more IPAM address blocks (and nested child blocks)
    from a YAML address-block template.  These blocks seed the pool that
    provision_site.py later discovers by Region / Environment /
    Status=available tags.

    Every block is tagged so it can be found again at decommission time:
        Region, Environment, Status, Location, Owner, Template=<name>
    plus any per-block or template-wide tags.

    Supports --dry-run to preview the plan without making changes, and
    rolls back created blocks (deepest first) on failure.

 Usage:
    provision_block.py [-h] -t TEMPLATE [--ip-space SPACE] [--name NAME]
                       [--dry-run] [--no-rollback]
                       [-c CONFIG] [--api-key KEY] [--no-verify-ssl]
                       [--json] [-d | -v] [-V]

 Examples:
    # Preview
    provision_block.py -t templates/blocks/emea-prod-pool.yaml --dry-run -v

    # Execute
    provision_block.py -t templates/blocks/emea-prod-pool.yaml -v

 Requirements:
    Python 3.10+ with requests and PyYAML modules

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
import datetime
import ipaddress
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

from uddi_toolkit.client import UDDIClient, UDDIError
from uddi_toolkit.core import (
    env_config,
    load_yaml_template,
    read_config,
    resolve_credentials,
    resolve_ip_space,
    setup_logging,
    add_common_args,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BlockDef:
    '''
    Definition of a single address block to create.

    Attributes:
        address:     Network base address (e.g. '10.20.0.0')
        cidr:        Prefix length (e.g. 16)
        region:      Region tag value (optional)
        environment: Environment tag value (optional)
        status:      Status tag value (default 'available')
        location:    Location tag value (optional)
        comment:     Free-text comment on the block (optional)
        tags:        Per-block extra tags
        children:    Nested child blocks contained within this one
    '''
    address: str
    cidr: int
    region: str = ''
    environment: str = ''
    status: str = 'available'
    location: str = ''
    comment: str = ''
    tags: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


@dataclass
class BlockConfig:
    '''
    Holds all parameters needed to provision an address-block template.
    '''
    name: str
    ip_space: str
    dry_run: bool = False
    no_rollback: bool = False
    extra_tags: dict = field(default_factory=dict)
    blocks: list = field(default_factory=list)
    date: str = field(default_factory=lambda: datetime.date.today().isoformat())


@dataclass
class BlockResult:
    '''
    Accumulates blocks created during provisioning (in creation order).
    '''
    name: str = ''
    ip_space: str = ''
    blocks_created: list = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Template loader
# ---------------------------------------------------------------------------

def _parse_blocks(raw_blocks: list) -> list:
    '''
    Recursively convert raw YAML block dicts into BlockDef instances.

    Args:
        raw_blocks: List of block mappings from the template

    Returns:
        List of BlockDef (with nested children populated)
    '''
    parsed: list = []
    for raw in raw_blocks or []:
        if not isinstance(raw, dict):
            logger.warning('Skipping non-mapping block entry: %s', raw)
            continue
        parsed.append(BlockDef(
            address=str(raw.get('address', '')).strip(),
            cidr=raw.get('cidr'),
            region=str(raw.get('region', '')),
            environment=str(raw.get('environment', '')),
            status=str(raw.get('status', 'available')),
            location=str(raw.get('location', '')),
            comment=str(raw.get('comment', '')),
            tags={k: str(v) for k, v in (raw.get('tags') or {}).items()},
            children=_parse_blocks(raw.get('children') or []),
        ))
    return parsed


def template_to_block_config(
    template: dict,
    ini_defaults: dict,
    cli_args: argparse.Namespace,
) -> BlockConfig:
    '''
    Merge an address-block template, INI defaults, and CLI args into a
    BlockConfig.

    Precedence (highest -> lowest): CLI flag > template > env var > INI.

    Args:
        template:     Parsed YAML template dict
        ini_defaults: Dict of [DEFAULTS] key/value pairs
        cli_args:     Parsed argparse Namespace

    Returns:
        Fully populated BlockConfig

    Raises:
        SystemExit if ip_space or address_blocks cannot be resolved
    '''
    def resolve(cli_val, yaml_val, ini_key, fallback=''):
        if cli_val is not None and cli_val != '':
            value = cli_val
        elif yaml_val is not None and yaml_val != '':
            value = yaml_val
        else:
            env_val = env_config(ini_key)
            value = env_val if env_val else ini_defaults.get(ini_key, fallback)
        return value

    name = resolve(getattr(cli_args, 'name', None), template.get('name'), 'name')
    ip_space = resolve(
        getattr(cli_args, 'ip_space', None), template.get('ip_space'), 'ip_space',
    )

    errors = []
    if not ip_space:
        errors.append('ip_space / --ip-space / network ip_space')
    blocks = _parse_blocks(template.get('address_blocks') or [])
    if not blocks:
        errors.append('address_blocks (non-empty list)')
    if errors:
        logger.error(
            'Required values missing (supply via CLI, template, or INI): %s',
            ', '.join(errors),
        )
        sys.exit(1)

    extra_tags = {k: str(v) for k, v in (template.get('tags') or {}).items()}

    return BlockConfig(
        name=name,
        ip_space=ip_space,
        dry_run=getattr(cli_args, 'dry_run', False),
        no_rollback=getattr(cli_args, 'no_rollback', False),
        extra_tags=extra_tags,
        blocks=blocks,
    )


# ---------------------------------------------------------------------------
# Block provisioner
# ---------------------------------------------------------------------------

class BlockProvisioner:
    '''
    Creates address blocks (and nested children) from a BlockConfig.
    '''

    def __init__(self, client: UDDIClient, cfg: BlockConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._space_id: str = ''
        return

    def _block_tags(self, bdef: BlockDef) -> dict:
        '''
        Build the tag set applied to a block.

        Args:
            bdef: BlockDef being created

        Returns:
            Merged tag dict
        '''
        tags = {**self.cfg.extra_tags}
        if self.cfg.name:
            tags['Template'] = self.cfg.name
        if bdef.region:
            tags['Region'] = bdef.region
        if bdef.environment:
            tags['Environment'] = bdef.environment
        if bdef.status:
            tags['Status'] = bdef.status
        if bdef.location:
            tags['Location'] = bdef.location
        tags.update(bdef.tags)
        return tags

    def _exists(self, bdef: BlockDef) -> bool:
        '''
        Return True if a block with this address/cidr already exists in space.

        Args:
            bdef: BlockDef to check

        Returns:
            True if an identical block is already present
        '''
        data = self.client.get(
            '/ipam/address_block',
            params={
                '_filter': (
                    f'space=="{self._space_id}" and '
                    f'address=="{bdef.address}" and cidr=={int(bdef.cidr)}'
                ),
            },
        )
        return bool(data.get('results', []))

    def _create_block(self, bdef: BlockDef, parent_net, result: BlockResult) -> None:
        '''
        Create a single block then recurse into its children.

        Args:
            bdef:       BlockDef to create
            parent_net: Parent ip_network for containment validation, or None
            result:     BlockResult updated incrementally for rollback
        '''
        try:
            net = ipaddress.ip_network(f'{bdef.address}/{int(bdef.cidr)}', strict=False)
        except (TypeError, ValueError) as exc:
            logger.error('Invalid block %s/%s: %s', bdef.address, bdef.cidr, exc)
            sys.exit(1)

        if parent_net is not None and not (net.subnet_of(parent_net) and net != parent_net):
            logger.error('Child %s is not contained within parent %s', net, parent_net)
            sys.exit(1)

        tags = self._block_tags(bdef)
        logger.info(
            '%sCreating address block %s  status=%s%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            net, bdef.status,
            f'  parent={parent_net}' if parent_net is not None else '',
        )

        if self.cfg.dry_run:
            result.blocks_created.append({
                'address': str(net.network_address),
                'cidr':    int(bdef.cidr),
                'id':      '(dry-run)',
                'status':  bdef.status,
            })
        elif self._exists(bdef):
            logger.info('  Already exists — skipping: %s', net)
        else:
            body = {
                'address': str(net.network_address),
                'cidr':    int(bdef.cidr),
                'space':   self._space_id,
                'comment': bdef.comment,
                'tags':    tags,
            }
            api_result = self.client.post('/ipam/address_block', body)
            block = api_result.get('result', {})
            logger.info('  Created block id=%s', block.get('id'))
            result.blocks_created.append({
                'address': str(net.network_address),
                'cidr':    int(bdef.cidr),
                'id':      block.get('id', ''),
                'status':  bdef.status,
            })

        for child in bdef.children:
            self._create_block(child, net, result)
        return

    def _rollback(self, result: BlockResult) -> None:
        '''
        Delete blocks created so far, deepest (last created) first.

        Args:
            result: BlockResult populated up to the point of failure
        '''
        logger.warning('Starting rollback of created address blocks ...')
        errors = 0
        for block in reversed(result.blocks_created):
            block_id = block.get('id', '')
            if not block_id or block_id == '(dry-run)':
                continue
            try:
                logger.warning('  Rollback: deleting block %s/%s  id=%s',
                               block['address'], block['cidr'], block_id)
                self.client.delete(f'/{block_id}')
            except UDDIError:
                logger.error('  Rollback: failed to delete block id=%s', block_id)
                errors += 1
        if errors:
            logger.error('Rollback finished with %d error(s) — manual cleanup may be required', errors)
        else:
            logger.warning('Rollback complete.')
        return

    def provision(self) -> BlockResult:
        '''
        Resolve the IP space and create all blocks in the plan.

        Returns:
            BlockResult describing everything created
        '''
        result = BlockResult(
            name=self.cfg.name,
            ip_space=self.cfg.ip_space,
            dry_run=self.cfg.dry_run,
        )
        self._space_id = resolve_ip_space(self.client, self.cfg.ip_space)
        try:
            for bdef in self.cfg.blocks:
                self._create_block(bdef, None, result)
        except (SystemExit, Exception) as exc:
            if not self.cfg.dry_run and not self.cfg.no_rollback:
                logger.error('Block provisioning failed (%r) — initiating rollback', exc)
                self._rollback(result)
            raise SystemExit(1) from exc
        return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_result(result: BlockResult) -> None:
    '''
    Print a human-readable provisioning summary to stdout.

    Args:
        result: BlockResult from BlockProvisioner.provision()
    '''
    mode = '[DRY-RUN] ' if result.dry_run else ''
    print()
    print('=' * 60)
    print(f'{mode}Address Block Provisioning Summary')
    print('=' * 60)
    print(f'  Template name : {result.name}')
    print(f'  IP space      : {result.ip_space}')
    print()
    print(f'  Blocks created ({len(result.blocks_created)}):')
    for b in result.blocks_created:
        addr = f'{b["address"]}/{b["cidr"]}'
        print(f'    {addr:<22}  status={b["status"]:<14}  id={b["id"]}')
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
        parser: The argparse parser to populate
    '''
    parser.add_argument('-t', '--template', required=True, metavar='FILE',
                        help='Path to a YAML address-block template')
    parser.add_argument('--ip-space', default=None, metavar='SPACE',
                        help='IP space name (overrides template/INI)')
    parser.add_argument('--name', default=None, metavar='NAME',
                        help='Template name used to tag created blocks (overrides template)')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='Preview all steps without making any changes')
    parser.add_argument('--no-rollback', action='store_true', default=False,
                        help='Do not roll back created blocks on failure (for debugging)')
    parser.add_argument('--json', dest='json_output', action='store_true', default=False,
                        help='Emit a single JSON object to stdout instead of human-readable output')

    add_common_args(parser)
    return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    '''
    Entry point: read config + template, build BlockConfig, run.

    Args:
        args: Parsed argparse Namespace

    Returns:
        Process exit code
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
    block_cfg = template_to_block_config(template, ini_defaults, args)
    logger.info('Block config: %s', block_cfg)

    mode_label = '[DRY-RUN] ' if block_cfg.dry_run else ''
    if not args.json_output:
        print(f'\n{mode_label}Provisioning address blocks: {block_cfg.name or args.template}')

    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)
    provisioner = BlockProvisioner(client, block_cfg)
    result = provisioner.provision()

    if args.json_output:
        print(json.dumps(dataclasses.asdict(result)))
    else:
        print_result(result)
    return 0
