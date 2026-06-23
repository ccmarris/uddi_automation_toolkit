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
import json
import logging
import os
import subprocess
import sys

from uddi_utils import (
    detect_drift,
    load_yaml_template,
    setup_logging,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = 'uddi.ini'
QUERY_SCRIPT = os.path.join(os.path.dirname(__file__), 'query_site.py')

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


def parseargs() -> argparse.Namespace:
    '''
    Parse command-line arguments.
    '''
    p = argparse.ArgumentParser(
        description='Detect configuration drift for a UDDI site template',
    )
    p.add_argument(
        '-t', '--template',
        required=True,
        help='Path to YAML site template',
    )
    p.add_argument(
        '-c', '--config',
        default=DEFAULT_CONFIG,
        help=f'Path to INI credentials file (default: {DEFAULT_CONFIG})',
    )
    p.add_argument(
        '--json',
        action='store_true',
        dest='json_output',
        help='Emit machine-readable JSON result to stdout',
    )
    p.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug logging',
    )
    p.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose (INFO) logging',
    )
    return p.parse_args()


def run_query(template_path: str, config: str) -> dict:
    '''
    Execute query_site.py --json and return the parsed result dict.

    Returns an empty dict on error (caller passes to detect_drift which
    will report site-not-found).
    '''
    cmd = [
        sys.executable,
        QUERY_SCRIPT,
        '-t', template_path,
        '-c', config,
        '--json',
    ]
    logger.debug('Running: %s', ' '.join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)

    result = {}
    if proc.returncode != 0:
        logger.warning('query_site.py exited %d: %s', proc.returncode,
                       (proc.stderr or proc.stdout).strip())
    else:
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            logger.warning('Failed to parse query output: %s', exc)

    return result


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


def main() -> None:
    args = parseargs()
    setup_logging(debug=args.debug, verbose=args.verbose)

    template = load_yaml_template(args.template)
    site_name = str((template.get('site') or {}).get('name', '')).strip()

    live = run_query(args.template, args.config)
    result = detect_drift(template, live, site_name=site_name)

    if args.json_output:
        print(json.dumps(result))
    else:
        print_report(result)

    if not result.get('found'):
        sys.exit(2)
    elif result.get('drifted'):
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
