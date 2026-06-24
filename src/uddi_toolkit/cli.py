#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Unified command-line entry point for the UDDI Automation Toolkit.

    Dispatches to the per-domain lifecycle modules and the supporting
    commands:

        uddi provision    {site|address-block|dns}  -t TEMPLATE ...
        uddi decommission {site|address-block|dns}  -t TEMPLATE ...
        uddi query        {site|address-block|dns}  -t TEMPLATE ...
        uddi validate  -t TEMPLATE
        uddi drift     -t TEMPLATE
        uddi batch     --action {provision|decommission} ...
        uddi retag-block (--address ADDR --cidr N | --site NAME)
        uddi web       [--host H] [--port P]

 Author: Chris Marrison

 Date Last Updated: 20260623

 Copyright (c) 2026 Chris Marrison / Infoblox

------------------------------------------------------------------------
'''
__version__ = '2.0.0'
__author__ = 'Chris Marrison'
__author_email__ = 'chris@infoblox.com'

import argparse
import json
import sys

from uddi_toolkit import __version__ as pkg_version
from uddi_toolkit.core import add_common_args, load_yaml_template, setup_logging, validate_template
from uddi_toolkit.site import provision as site_provision
from uddi_toolkit.site import decommission as site_decommission
from uddi_toolkit.site import query as site_query
from uddi_toolkit.block import provision as block_provision
from uddi_toolkit.block import decommission as block_decommission
from uddi_toolkit.block import query as block_query
from uddi_toolkit.dns import provision as dns_provision
from uddi_toolkit.dns import decommission as dns_decommission
from uddi_toolkit.dns import query as dns_query
from uddi_toolkit import batch, retag, drift_cli


# Lifecycle verb -> template type -> implementing module
LIFECYCLE = {
    'provision': {
        'site':          site_provision,
        'address-block': block_provision,
        'dns':           dns_provision,
    },
    'decommission': {
        'site':          site_decommission,
        'address-block': block_decommission,
        'dns':           dns_decommission,
    },
    'query': {
        'site':          site_query,
        'address-block': block_query,
        'dns':           dns_query,
    },
}


# ---------------------------------------------------------------------------
# validate (no API calls)
# ---------------------------------------------------------------------------

def _add_validate_args(parser: argparse.ArgumentParser) -> None:
    '''Add arguments for the validate command.'''
    parser.add_argument('-t', '--template', required=True, metavar='FILE',
                        help='Path to a YAML template to validate')
    parser.add_argument('--json', dest='json_output', action='store_true', default=False,
                        help='Emit the validation result as JSON')
    return


def _run_validate(args: argparse.Namespace) -> int:
    '''Validate a template's schema without contacting the API.'''
    template = load_yaml_template(args.template)
    result = validate_template(template, template_name=args.template)
    if args.json_output:
        print(json.dumps(result))
    else:
        status = 'VALID' if result['valid'] else 'INVALID'
        print(f'{status} ({result["type"]}): {args.template}')
        for err in result['errors']:
            print(f'  ✗ [{err["field"]}] {err["message"]}')
        for warn in result['warnings']:
            print(f'  ⚠ [{warn["field"]}] {warn["message"]}')
    return 0 if result['valid'] else 1


# ---------------------------------------------------------------------------
# web (flask imported lazily so non-web commands don't require it)
# ---------------------------------------------------------------------------

def _add_web_args(parser: argparse.ArgumentParser) -> None:
    '''Add arguments for the web command (without importing Flask).'''
    parser.add_argument('--templates-dir', default=None, metavar='DIR',
                        help='Directory containing site YAML templates (default: ./templates)')
    parser.add_argument('-p', '--port', type=int, default=5000,
                        help='Port to listen on (default: 5000)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Host address to bind to (default: 127.0.0.1)')
    parser.add_argument('--debug-flask', action='store_true', default=False,
                        help='Enable Flask debug mode (auto-reload on file changes)')
    add_common_args(parser)
    return


def _run_web(args: argparse.Namespace) -> int:
    '''Start the web server (imports Flask lazily).'''
    from uddi_toolkit.web import server as web_server
    return web_server.run(args)


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    '''
    Build the top-level argument parser with all subcommands.

    Returns:
        Configured argparse.ArgumentParser
    '''
    parser = argparse.ArgumentParser(
        prog='uddi',
        description='Infoblox Universal DDI automation toolkit',
    )
    parser.add_argument('-V', '--version', action='version', version=f'%(prog)s {pkg_version}')
    sub = parser.add_subparsers(dest='command', required=True)

    # Lifecycle verbs with per-type subcommands
    for verb, types in LIFECYCLE.items():
        vp = sub.add_parser(verb, help=f'{verb} a site / address-block / dns template')
        tsub = vp.add_subparsers(dest='type', required=True)
        for tname, module in types.items():
            tp = tsub.add_parser(tname, help=f'{verb} a {tname} template')
            module.add_arguments(tp)
            tp.set_defaults(func=module.run)

    # validate
    vp = sub.add_parser('validate', help='Validate a template schema (no API calls)')
    _add_validate_args(vp)
    vp.set_defaults(func=_run_validate)

    # drift
    dp = sub.add_parser('drift', help='Detect drift between a site template and live state')
    drift_cli.add_arguments(dp)
    dp.set_defaults(func=drift_cli.run)

    # batch
    bp = sub.add_parser('batch', help='Batch provision/decommission a set of templates')
    batch.add_arguments(bp)
    bp.set_defaults(func=batch.run)

    # retag-block
    rp = sub.add_parser('retag-block', help='Re-tag an address block lifecycle Status')
    retag.add_arguments(rp)
    rp.set_defaults(func=retag.run)

    # web
    wp = sub.add_parser('web', help='Start the browser-based web UI')
    _add_web_args(wp)
    wp.set_defaults(func=_run_web)

    return parser


def main(argv: list | None = None) -> int:
    '''
    Parse arguments and dispatch to the selected command.

    Args:
        argv: Optional argument list (defaults to sys.argv[1:])

    Returns:
        Process exit code from the dispatched command
    '''
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    return result if isinstance(result, int) else 0


if __name__ == '__main__':
    sys.exit(main())
