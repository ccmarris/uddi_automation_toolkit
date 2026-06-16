#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Sequential batch provisioning or decommissioning of multiple sites
    for Infoblox Universal DDI.

    Invokes provision_site.py or decommission_site.py as subprocesses
    for each template, so per-site failures are isolated — one failure
    does not prevent the remaining sites from being processed.

 Usage:
    batch_provision.py --action {provision,decommission}
                       [--templates-dir DIR | --templates FILE [FILE ...]]
                       [--dry-run] [--force] [--no-rollback]
                       [--stop-on-error]
                       [-c CONFIG] [-d | -v] [-V]

 Examples:
    # Dry-run all templates in the templates/ directory
    batch_provision.py --action provision --templates-dir templates --dry-run -v

    # Provision specific templates
    batch_provision.py --action provision --templates templates/site-london.yaml templates/site-paris.yaml -v

    # Decommission all templates non-interactively
    batch_provision.py --action decommission --templates-dir templates --force -v

    # Stop on first failure
    batch_provision.py --action provision --templates-dir templates --stop-on-error -v

 Requirements:
    Python 3.8+ — provision_site.py and decommission_site.py must be in
    the same directory as this script.

 Configuration:
    Shares the same INI file as provision_site.py (default: uddi.ini)

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
import glob
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from uddi_utils import setup_logging

logger = logging.getLogger(__name__)

# Directory containing this script — used to locate sibling scripts
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BatchConfig:
    '''
    Holds all parameters for a batch provisioning or decommissioning run.

    Attributes:
        templates:     Resolved list of YAML template file paths to process
        action:        'provision' or 'decommission'
        dry_run:       Forward --dry-run to each child script
        force:         Forward --force to decommission_site.py (skip confirmation)
        no_rollback:   Forward --no-rollback to provision_site.py
        config:        Path to INI configuration file
        verbose:       Forward -v to each child script
        debug:         Forward -d to each child script
        stop_on_error: Abort batch after first failure
    '''
    templates: list
    action: str
    dry_run: bool = False
    force: bool = False
    no_rollback: bool = False
    config: str = 'uddi.ini'
    verbose: bool = False
    debug: bool = False
    stop_on_error: bool = False


@dataclass
class BatchResult:
    '''
    Collects per-template outcomes across the batch run.

    Attributes:
        succeeded: Template paths that completed with exit code 0
        failed:    Template paths that completed with non-zero exit code
        skipped:   Template paths skipped due to --stop-on-error
    '''
    succeeded: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------

def resolve_templates(
    templates_dir: str,
    templates: list,
) -> list:
    '''
    Build the ordered, deduplicated list of YAML template paths to process.

    Globs *.yaml and *.yml from templates_dir (if given), then appends
    any explicitly listed templates.  Validates that each resolved path
    exists; logs a warning and skips it if not.

    Args:
        templates_dir: Directory to glob for templates (may be None)
        templates:     Explicit list of template paths (may be empty)

    Returns:
        Sorted, deduplicated list of valid template paths
    '''
    found: list = []

    if templates_dir:
        for pattern in ('*.yaml', '*.yml'):
            found.extend(
                sorted(glob.glob(os.path.join(templates_dir, pattern)))
            )

    found.extend(templates or [])

    # Deduplicate while preserving order
    seen = set()
    deduped: list = []
    for path in found:
        if path not in seen:
            seen.add(path)
            deduped.append(path)

    # Validate existence
    valid: list = []
    for path in deduped:
        if os.path.isfile(path):
            valid.append(path)
        else:
            logger.warning('Template not found — skipping: %s', path)

    return valid


# ---------------------------------------------------------------------------
# Per-template execution
# ---------------------------------------------------------------------------

def run_template(template_path: str, cfg: BatchConfig) -> tuple:
    '''
    Execute provision_site.py or decommission_site.py for a single template.

    Args:
        template_path: Path to the YAML site template
        cfg:           BatchConfig controlling flags and script choice

    Returns:
        Tuple of (returncode: int, stdout: str, stderr: str)
    '''
    script_name = (
        'provision_site.py' if cfg.action == 'provision'
        else 'decommission_site.py'
    )
    script_path = os.path.join(SCRIPT_DIR, script_name)

    cmd = [sys.executable, script_path, '-t', template_path, '-c', cfg.config]

    if cfg.dry_run:
        cmd.append('--dry-run')
    if cfg.debug:
        cmd.append('-d')
    elif cfg.verbose:
        cmd.append('-v')

    if cfg.action == 'provision' and cfg.no_rollback:
        cmd.append('--no-rollback')

    if cfg.action == 'decommission' and cfg.force:
        cmd.append('--force')

    logger.debug('Running: %s', ' '.join(cmd))

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    return (proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------

def batch_run(cfg: BatchConfig) -> BatchResult:
    '''
    Process all templates in cfg.templates sequentially.

    On failure: logs the error and continues unless --stop-on-error is set.

    Args:
        cfg: BatchConfig controlling the run

    Returns:
        BatchResult with succeeded / failed / skipped lists
    '''
    result = BatchResult()
    remaining = list(cfg.templates)

    for template in remaining:
        logger.info(
            'Processing [%s]: %s',
            cfg.action, template,
        )
        print(f'  [{cfg.action}] {template} ...', flush=True)

        rc, stdout, stderr = run_template(template, cfg)

        if rc == 0:
            result.succeeded.append(template)
            # Always print child script output so the user can follow progress
            if stdout:
                sys.stdout.write(stdout)
        else:
            result.failed.append(template)
            logger.error('FAILED [%s]: %s', template, stderr.strip() or stdout.strip())
            if stderr:
                sys.stderr.write(stderr)
            elif stdout:
                sys.stdout.write(stdout)

            if cfg.stop_on_error:
                # Mark the rest as skipped
                idx = remaining.index(template)
                result.skipped = remaining[idx + 1:]
                logger.error('--stop-on-error set — aborting batch after first failure')
                break

    return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_batch_result(result: BatchResult, action: str, dry_run: bool) -> None:
    '''
    Print a human-readable batch summary to stdout.

    Args:
        result:  BatchResult from batch_run()
        action:  'provision' or 'decommission'
        dry_run: Whether this was a dry-run
    '''
    mode = '[DRY-RUN] ' if dry_run else ''
    n_ok   = len(result.succeeded)
    n_fail = len(result.failed)
    n_skip = len(result.skipped)

    print()
    print('=' * 60)
    print(f'{mode}Batch {action} summary')
    print('=' * 60)
    print(f'  Succeeded : {n_ok}')
    print(f'  Failed    : {n_fail}')
    if n_skip:
        print(f'  Skipped   : {n_skip}  (--stop-on-error)')

    if result.failed:
        print()
        print('  Failures:')
        for path in result.failed:
            print(f'    {path}')

    if result.skipped:
        print()
        print('  Skipped:')
        for path in result.skipped:
            print(f'    {path}')

    print('=' * 60)
    if dry_run:
        print('DRY-RUN complete. Rerun without --dry-run to execute.')
    print()


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
        description='Sequential batch provisioning/decommissioning for Infoblox Universal DDI',
        epilog=(
            'Each template is processed independently; a failure on one site '
            'does not prevent the remaining sites from being processed unless '
            '--stop-on-error is set.'
        ),
    )

    parser.add_argument(
        '-V', '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )

    parser.add_argument(
        '--action',
        required=True,
        choices=['provision', 'decommission'],
        help='Operation to perform on each template',
    )

    # Template source — mutually exclusive
    tmpl_grp = parser.add_mutually_exclusive_group(required=True)
    tmpl_grp.add_argument(
        '--templates-dir',
        default=None,
        metavar='DIR',
        help='Directory containing .yaml / .yml site templates to process',
    )
    tmpl_grp.add_argument(
        '--templates',
        nargs='+',
        default=None,
        metavar='FILE',
        help='One or more YAML site template paths to process',
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Forward --dry-run to each child script (preview only)',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='Forward --force to decommission_site.py (skip confirmation)',
    )
    parser.add_argument(
        '--no-rollback',
        action='store_true',
        default=False,
        help='Forward --no-rollback to provision_site.py',
    )
    parser.add_argument(
        '--stop-on-error',
        action='store_true',
        default=False,
        help='Abort batch after the first failed template',
    )
    parser.add_argument(
        '-c', '--config',
        default='uddi.ini',
        metavar='FILE',
        help='Path to INI configuration file (default: uddi.ini in current working directory)',
    )

    log_grp = parser.add_mutually_exclusive_group()
    log_grp.add_argument(
        '-d', '--debug',
        action='store_true',
        default=False,
        help='Enable DEBUG logging',
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

    Resolves template list, runs the batch, and prints a summary.
    Exits with code 1 if any template failed.
    '''
    args = parseargs()
    setup_logging(debug=args.debug, verbose=args.verbose)

    logger.debug('Arguments: %s', args)

    templates = resolve_templates(
        templates_dir=args.templates_dir,
        templates=args.templates or [],
    )

    if not templates:
        logger.error('No valid templates found to process.')
        sys.exit(1)

    mode_label = '[DRY-RUN] ' if args.dry_run else ''
    print(f'\n{mode_label}Batch {args.action}: {len(templates)} template(s)')

    cfg = BatchConfig(
        templates=templates,
        action=args.action,
        dry_run=args.dry_run,
        force=args.force,
        no_rollback=args.no_rollback,
        config=args.config,
        verbose=args.verbose,
        debug=args.debug,
        stop_on_error=args.stop_on_error,
    )

    result = batch_run(cfg)
    print_batch_result(result, args.action, args.dry_run)

    if result.failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
