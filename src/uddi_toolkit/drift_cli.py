#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Drift detection for the UDDI Automation Toolkit.

    Compares the expected state described by a YAML site template
    against the live state returned by the UDDI API (via query_site.py).
    Reports missing resources, unexpected resources, and tag mismatches.

    Exit codes:
        0 — no drift (site matches template)
        1 — drift detected (differences found)
        2 — error (site not found, API failure, or bad template)

    Usage:
        drift_detect.py -t <template.yaml> [-c uddi.ini] [--json]

 Author: Chris Marrison

 Date Last Updated: 20260617

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
__version__ = '0.1.0'
__author__ = 'Chris Marrison'
__author_email__ = 'chris@infoblox.com'

import argparse
import dataclasses
import json
import logging

from uddi_toolkit.client import UDDIClient, UDDIError
from uddi_toolkit.core import (
    add_common_args,
    detect_drift,
    load_yaml_template,
    read_config,
    resolve_credentials,
    setup_logging,
)
from uddi_toolkit.site import query as site_query

logger = logging.getLogger(__name__)

_SEVERITY_PREFIX = {
    'error':   '✗',
    'warning': '⚠',
    'info':    'ℹ',
}

_CATEGORY_LABEL = {
    'site':   'Site',
    'subnet': 'Subnets',
    'dns':    'DNS',
    'tags':   'Tags',
    'hosts':  'Hosts',
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    '''
    Add the drift command's arguments to the given parser.

    Args:
        parser: argparse subparser to populate
    '''
    parser.add_argument('-t', '--template', required=True,
                        help='Path to YAML site template')
    parser.add_argument('--json', dest='json_output', action='store_true',
                        help='Emit machine-readable JSON result to stdout')
    add_common_args(parser)
    return


def run_query(args: argparse.Namespace, template: dict) -> dict:
    '''
    Query the live site state in-process and return it as a plain dict.

    Returns an empty dict on any failure (caller passes to detect_drift,
    which reports site-not-found).

    Args:
        args:     Parsed argparse Namespace (credentials/config flags)
        template: Parsed YAML site template

    Returns:
        QueryResult as a dict, or {} on error
    '''
    verify_ssl_override = None if args.verify_ssl else False
    api_key, base_url, verify_ssl = resolve_credentials(
        args.api_key, args.config, verify_ssl_override,
    )
    live: dict = {}
    if not api_key:
        logger.warning('No API key found; treating site as not provisioned')
    else:
        cfg_file = read_config(args.config)
        ini = dict(cfg_file['DEFAULTS']) if cfg_file.has_section('DEFAULTS') else {}
        client = UDDIClient(url=base_url, api_key=api_key, verify_ssl=verify_ssl)
        try:
            query_cfg = site_query.template_to_query_config(template, ini, args)
            result = site_query.SiteQuerier(client, query_cfg).query()
            live = dataclasses.asdict(result)
        except (UDDIError, SystemExit) as exc:
            logger.warning('Live query failed (%r); treating site as not provisioned', exc)
    return live


def print_report(result: dict) -> None:
    '''
    Print a human-readable drift report to stdout.
    '''
    site = result.get('site', '(unknown)')
    found = result.get('found', False)
    drifted = result.get('drifted', False)
    block = result.get('block_address', '')
    drifts = result.get('drifts', [])
    summary = result.get('summary', {})

    header = f'Drift Report — {site}'
    print(header)
    print('─' * len(header))

    if not found:
        print('  ✗ Site not provisioned — no allocated block found')
        print()
    else:
        print(f'  Block:  {block}')
        status = '✓ No drift detected' if not drifted else f'✗ Drift detected ({summary.get("total", 0)} item(s))'
        print(f'  Status: {status}')
        print()

        if drifts:
            # Group drifts by category for readable output
            by_category: dict[str, list[dict]] = {}
            for d in drifts:
                by_category.setdefault(d['category'], []).append(d)

            for cat, items in by_category.items():
                label = _CATEGORY_LABEL.get(cat, cat.title())
                print(f'  {label}:')
                for item in items:
                    prefix = _SEVERITY_PREFIX.get(item['severity'], ' ')
                    print(f'    {prefix} [{item["field"]}] {item["message"]}')
                print()

            errors = summary.get('errors', 0)
            warnings = summary.get('warnings', 0)
            print(f'  Summary: {errors} error(s), {warnings} warning(s)')
            print()
    return


def run(args: argparse.Namespace) -> int:
    '''
    Compare a site template against live state and report drift.

    Args:
        args: Parsed argparse Namespace

    Returns:
        Exit code: 2 if not provisioned, 1 if drifted, 0 if clean
    '''
    setup_logging(debug=args.debug, verbose=args.verbose)

    template = load_yaml_template(args.template)
    site_name = str((template.get('site') or {}).get('name', '')).strip()

    live = run_query(args, template)
    result = detect_drift(template, live, site_name=site_name)

    if args.json_output:
        print(json.dumps(result))
    else:
        print_report(result)

    if not result.get('found'):
        exitcode = 2
    elif result.get('drifted'):
        exitcode = 1
    else:
        exitcode = 0
    return exitcode
