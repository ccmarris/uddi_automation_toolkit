#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Address-block decommissioning for Infoblox Universal DDI.

    Deletes the IPAM address blocks created by provision_block.py from an
    address-block template.  Blocks are discovered by their Template tag
    (or, when the template has no name, by exact address/cidr match) and
    deleted deepest child first so parents are emptied before removal.

    Supports --dry-run to preview and --force to skip the confirmation
    prompt for non-interactive use.

 Usage:
    decommission_block.py [-h] -t TEMPLATE [--ip-space SPACE] [--name NAME]
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
class DecommissionBlockConfig:
    '''
    Holds all parameters needed to decommission an address-block template.
    '''
    name: str
    ip_space: str
    pairs: list = field(default_factory=list)   # [(address, cidr)] fallback match
    dry_run: bool = False
    force: bool = False


@dataclass
class DecommissionBlockResult:
    '''
    Accumulates blocks deleted during teardown.
    '''
    name: str = ''
    ip_space: str = ''
    blocks_deleted: list = field(default_factory=list)
    dry_run: bool = False


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


# ---------------------------------------------------------------------------
# Block decommissioner
# ---------------------------------------------------------------------------

class BlockDecommissioner:
    '''
    Deletes address blocks created from an address-block template.
    '''

    def __init__(self, client: UDDIClient, cfg: DecommissionBlockConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._space_id: str = ''
        return

    def find_blocks(self) -> list:
        '''
        Find the blocks belonging to this template.

        Uses the Template tag when the template has a name, otherwise matches
        each template-defined address/cidr exactly within the IP space.

        Returns:
            List of address-block resource dicts (de-duplicated)
        '''
        found: list = []
        if self.cfg.name:
            found = self.client.get_all(
                '/ipam/address_block',
                params={
                    '_filter': (
                        f'space=="{self._space_id}" and '
                        f'tags.Template=="{self.cfg.name}"'
                    ),
                },
            )
        else:
            seen: set = set()
            for address, cidr in self.cfg.pairs:
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
        logger.info('  Found %d block(s) to remove', len(found))
        return found

    def delete_blocks(self, blocks: list) -> list:
        '''
        Delete the given blocks, most-specific (largest cidr) first so that
        child blocks are removed before their parents.

        Args:
            blocks: Address-block resource dicts

        Returns:
            List of dicts describing each deleted (or dry-run) block
        '''
        ordered = sorted(blocks, key=lambda b: int(b.get('cidr', 0)), reverse=True)
        deleted: list = []
        for block in ordered:
            block_id = block.get('id', '')
            addr = f'{block.get("address", "")}/{block.get("cidr", "")}'
            status = (block.get('tags') or {}).get('Status', '')
            if status == 'allocated':
                logger.warning('  Block %s is allocated to a site — deletion may fail', addr)
            logger.info(
                '%sDeleting block: %s  id=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                addr, block_id,
            )
            if not self.cfg.dry_run:
                self.client.delete(f'/{block_id}')
            deleted.append({'address': addr, 'id': block_id, 'status': status})
        return deleted

    def decommission(self) -> DecommissionBlockResult:
        '''
        Resolve the IP space, find the template's blocks, and delete them.

        Returns:
            DecommissionBlockResult describing everything removed
        '''
        result = DecommissionBlockResult(
            name=self.cfg.name,
            ip_space=self.cfg.ip_space,
            dry_run=self.cfg.dry_run,
        )
        self._space_id = resolve_ip_space(self.client, self.cfg.ip_space)
        blocks = self.find_blocks()
        result.blocks_deleted = self.delete_blocks(blocks)
        return result


# ---------------------------------------------------------------------------
# Confirmation + output
# ---------------------------------------------------------------------------

def confirm_decommission(cfg: DecommissionBlockConfig) -> bool:
    '''
    Prompt the operator for explicit confirmation before deleting blocks.

    Args:
        cfg: DecommissionBlockConfig describing what will be removed

    Returns:
        True if the operator confirmed, False otherwise
    '''
    target = cfg.name or cfg.ip_space
    print()
    print('!' * 60)
    print('  WARNING — Destructive operation')
    print('!' * 60)
    print(f'  Template name : {cfg.name or "(none — matching by address)"}')
    print(f'  IP space      : {cfg.ip_space}')
    print('  All address blocks created by this template will be deleted.')
    print()
    answer = input(f'  Type "{target}" to confirm, or press Enter to abort: ').strip()
    return answer == target


def print_result(result: DecommissionBlockResult) -> None:
    '''
    Print a human-readable decommission summary to stdout.

    Args:
        result: DecommissionBlockResult from decommission()
    '''
    mode = '[DRY-RUN] ' if result.dry_run else ''
    print()
    print('=' * 60)
    print(f'{mode}Address Block Decommission Summary')
    print('=' * 60)
    print(f'  Template name : {result.name}')
    print(f'  IP space      : {result.ip_space}')
    print()
    if result.blocks_deleted:
        print(f'  Blocks removed ({len(result.blocks_deleted)}):')
        for b in result.blocks_deleted:
            print(f'    {b["address"]:<22}  id={b["id"]}')
    else:
        print('  Blocks removed : none found')
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
        parser: The argparse parser to populate
    '''
    parser.add_argument('-t', '--template', required=True, metavar='FILE',
                        help='Path to a YAML address-block template')
    parser.add_argument('--ip-space', default=None, metavar='SPACE',
                        help='IP space name (overrides template/INI)')
    parser.add_argument('--name', default=None, metavar='NAME',
                        help='Template name to match created blocks (overrides template)')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='Preview all steps without making any changes')
    parser.add_argument('--force', action='store_true', default=False,
                        help='Skip the interactive confirmation prompt')
    parser.add_argument('--json', dest='json_output', action='store_true', default=False,
                        help='Emit a single JSON object to stdout instead of human-readable output')

    add_common_args(parser)
    return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    '''
    Entry point: read config + template, build config, confirm, run.

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

    cfg = DecommissionBlockConfig(
        name=str(name),
        ip_space=ip_space,
        pairs=_flatten_pairs(template.get('address_blocks') or []),
        dry_run=args.dry_run,
        force=args.force,
    )

    mode_label = '[DRY-RUN] ' if cfg.dry_run else ''
    if not args.json_output:
        print(f'\n{mode_label}Decommissioning address blocks: {cfg.name or args.template}')

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

    client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)
    decommissioner = BlockDecommissioner(client, cfg)
    try:
        result = decommissioner.decommission()
    except UDDIError as exc:
        logger.error('Decommission aborted on API error: %s', exc)
        sys.exit(1)

    if args.json_output:
        print(json.dumps(dataclasses.asdict(result)))
    else:
        print_result(result)
    return 0
