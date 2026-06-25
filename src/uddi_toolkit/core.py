#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Shared utility functions for the UDDI Automation Toolkit.

    Provides common helpers used across provision_site.py,
    decommission_site.py, query_site.py, batch_provision.py, and
    web_server.py so they do not need to maintain their own copies.

    Functions
    ---------
    load_yaml_template   -- load and validate a YAML site template file
    read_config          -- read the INI configuration file (soft — no sys.exit)
    resolve_credentials  -- resolve api_key, base_url, verify_ssl from
                            CLI flag > env var > INI file > default
    setup_logging        -- configure root logger from debug/verbose flags
    reverse_zone_fqdn    -- compute in-addr.arpa FQDN for a subnet

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
__version__ = '1.5.0'
__author__ = 'Chris Marrison'
__author_email__ = 'chris@infoblox.com'

import configparser
import ipaddress
import logging
import os
import sys

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable configuration map
# ---------------------------------------------------------------------------

# Maps INI [DEFAULTS] key names to their UDDI_ env var equivalents.
# Used by provision_site.py, decommission_site.py, and query_site.py to
# support zero-INI-file operation (all config injected as env vars).
#
# Resolution order in each script: CLI flag > YAML template > env var > INI
ENV_CONFIG_MAP: dict[str, str] = {
    'ip_space':    'UDDI_IP_SPACE',
    'dns_parent':  'UDDI_DNS_PARENT',
    'dns_view':    'UDDI_DNS_VIEW',
    'owner':       'UDDI_OWNER',
    'subnet_size': 'UDDI_SUBNET_SIZE',
}


def env_config(key: str) -> str:
    '''
    Return the environment variable value for a config key, or ''.

    Args:
        key: INI [DEFAULTS] key name (e.g. 'ip_space', 'dns_parent')

    Returns:
        The env var value stripped of surrounding whitespace, or '' if
        the key has no mapping or the env var is unset/empty.
    '''
    env_var = ENV_CONFIG_MAP.get(key, '')
    return os.environ.get(env_var, '').strip() if env_var else ''


def load_yaml_template(path: str) -> dict:
    '''
    Load and parse a YAML site template file.

    Args:
        path: Filesystem path to the YAML template

    Returns:
        Parsed template as a plain dict

    Raises:
        SystemExit if the file cannot be opened or is not valid YAML
    '''
    logger.info('Loading YAML template: %s', path)
    try:
        with open(path, 'r') as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.error('YAML template not found: %s', path)
        sys.exit(1)
    except yaml.YAMLError as exc:
        logger.error('Invalid YAML in %s: %s', path, exc)
        sys.exit(1)

    if not isinstance(data, dict):
        logger.error(
            'YAML template must be a mapping (dict) at the top level: %s', path,
        )
        sys.exit(1)

    logger.debug('Template loaded: %s', data)
    return data


DEFAULT_BASE_URL = 'https://csp.infoblox.com'
INI_SECTION = 'UDDI'


def read_config(config_file: str) -> configparser.ConfigParser:
    '''
    Read the INI configuration file.

    Unlike the old hard-exit version, this returns an empty ConfigParser
    when the file is absent or unparseable so that resolve_credentials()
    can fall back to environment variables or CLI flags.

    Expected sections (all optional when using env vars):

        [UDDI]
        api_key    = <key>
        url        = https://csp.infoblox.com
        valid_cert = true

        [DEFAULTS]
        ip_space    = my-ip-space
        dns_parent  = internal.example.com
        dns_view    = default
        owner       = network-team
        subnet_size = 24

    Args:
        config_file: Path to the INI configuration file

    Returns:
        Populated (possibly empty) ConfigParser instance
    '''
    cfg = configparser.ConfigParser()
    try:
        files_read = cfg.read(config_file)
    except configparser.Error as exc:
        logger.warning('Could not parse config file %s: %s', config_file, exc)
    else:
        if not files_read:
            logger.debug('Config file not found: %s', config_file)
    return cfg


def resolve_credentials(
    api_key_flag: str,
    ini_file: str,
    verify_ssl_override: bool | None = None,
) -> tuple[str, str, bool]:
    '''
    Resolve API key, base URL, and SSL verification from multiple sources.

    Priority order:

        API key:    --api-key flag  >  INFOBLOX_PORTAL_KEY / UDDI_API_KEY env var  >  INI file
        base URL:   INFOBLOX_PORTAL_URL / BLOXONE_CSP_URL env var  >  INI file  >  default
        verify SSL: --no-verify-ssl flag  >  INI valid_cert  >  True (default)

    The INI file is always read first (to pick up base_url / valid_cert) even
    when the API key comes from a higher-priority source.

    Args:
        api_key_flag:       Value of --api-key CLI argument; empty string if not supplied.
        ini_file:           Path to the INI credentials file.
        verify_ssl_override: Explicit SSL flag from --no-verify-ssl; None means use INI/default.

    Returns:
        Tuple of (api_key, base_url, verify_ssl).  api_key is an empty string
        if no source supplies one — the caller must check and abort if required.
    '''
    cfg = read_config(ini_file)
    ini = cfg[INI_SECTION] if cfg.has_section(INI_SECTION) else {}

    # base_url: ini is baseline; env vars override for CI/CD portability
    base_url = DEFAULT_BASE_URL
    if ini.get('url'):
        base_url = ini['url'].strip('\'"').rstrip('/')
    for env_var in ('INFOBLOX_PORTAL_URL', 'BLOXONE_CSP_URL'):
        val = os.environ.get(env_var, '')
        if val:
            base_url = val.rstrip('/')
            logger.debug('Using base URL from %s', env_var)
            break

    # verify_ssl: ini sets default; explicit flag wins
    verify_ssl = True
    if ini.get('valid_cert', '').strip('\'"').lower() in ('false', '0', 'no'):
        verify_ssl = False
    if verify_ssl_override is not None:
        verify_ssl = verify_ssl_override
        logger.debug('SSL verification overridden to: %s', verify_ssl)

    # API key: CLI flag > env var > INI
    api_key = ''
    if api_key_flag:
        logger.debug('Using API key from --api-key flag')
        api_key = api_key_flag
    else:
        for env_var in ('INFOBLOX_PORTAL_KEY', 'UDDI_API_KEY'):
            val = os.environ.get(env_var, '')
            if val:
                logger.debug('Using API key from %s environment variable', env_var)
                api_key = val
                break
        else:
            if ini.get('api_key'):
                logger.debug('Using API key from INI file: %s', ini_file)
                api_key = ini['api_key'].strip('\'"')

    return api_key, base_url, verify_ssl


def setup_logging(debug: bool = False, verbose: bool = False) -> None:
    '''
    Configure the root logger from debug/verbose flags.

    Args:
        debug:   Enable DEBUG level (overrides verbose)
        verbose: Enable INFO level (default is WARNING)
    '''
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    return


def add_common_args(parser) -> None:
    '''
    Add the credential, config, and logging flags shared by every command.

    Args:
        parser: argparse parser (or subparser) to add the common flags to
    '''
    parser.add_argument(
        '-c', '--config', default='uddi.ini', metavar='FILE',
        help='Path to INI configuration file (default: uddi.ini)',
    )
    parser.add_argument(
        '--api-key', default='', metavar='KEY',
        help='API key (overrides INI and INFOBLOX_PORTAL_KEY / UDDI_API_KEY env vars)',
    )
    parser.add_argument(
        '--no-verify-ssl', dest='verify_ssl', action='store_false', default=True,
        help='Disable SSL certificate verification (for lab / self-signed certs)',
    )
    log_grp = parser.add_mutually_exclusive_group()
    log_grp.add_argument(
        '-d', '--debug', action='store_true', default=False,
        help='Enable DEBUG logging (shows all API calls)',
    )
    log_grp.add_argument(
        '-v', '--verbose', action='store_true', default=False,
        help='Enable INFO logging',
    )
    return


def reverse_zone_fqdn(address: str, cidr: int) -> str:
    '''
    Compute the in-addr.arpa zone name for a subnet.

    Examples:
        10.20.1.0/24  ->  1.20.10.in-addr.arpa
        10.20.0.0/16  ->  20.10.in-addr.arpa
        10.0.0.0/8    ->   10.in-addr.arpa

    Args:
        address: Network base address string (e.g. '10.20.1.0')
        cidr:    Prefix length (e.g. 24)

    Returns:
        in-addr.arpa zone FQDN string

    Note:
        Returns a single classful zone name based on /8, /16, or /24
        boundaries.  Prefixes that are not on those boundaries (e.g. /23,
        /22) map to the zone of their network address only and do not
        fully cover the subnet; RFC 2317 classless delegation for prefixes
        longer than /24 is not implemented.
    '''
    net = ipaddress.ip_network(f'{address}/{cidr}', strict=False)
    octets = str(net.network_address).split('.')
    if cidr <= 8:
        relevant = octets[:1]
    elif cidr <= 16:
        relevant = octets[:2]
    else:
        relevant = octets[:3]
    return '.'.join(reversed(relevant)) + '.in-addr.arpa'


# ---------------------------------------------------------------------------
# Template type
# ---------------------------------------------------------------------------

TEMPLATE_TYPES = ('site', 'address-block', 'dns')


def template_type(template: dict) -> str:
    '''
    Classify a parsed template as 'site', 'address-block', or 'dns'.

    Honours an explicit top-level ``type:`` field; otherwise infers from the
    presence of the distinguishing top-level section so legacy templates
    (written before the field existed) still resolve correctly.

    Args:
        template: Parsed YAML template dict

    Returns:
        One of 'site', 'address-block', 'dns', or 'unknown'
    '''
    explicit = str(template.get('type', '')).strip().lower()
    if explicit in TEMPLATE_TYPES:
        result = explicit
    elif template.get('address_blocks') is not None:
        result = 'address-block'
    elif template.get('zones') is not None:
        result = 'dns'
    elif template.get('site') is not None or template.get('network') is not None:
        result = 'site'
    else:
        result = 'unknown'
    return result


# ---------------------------------------------------------------------------
# Shared API lookups (used by the block / dns scripts)
# ---------------------------------------------------------------------------

def resolve_ip_space(client, name: str) -> str:
    '''
    Resolve an IP space name to its API resource ID.

    Args:
        client: UDDIClient instance
        name:   IP space name

    Returns:
        IP space resource ID string

    Raises:
        SystemExit if the space is not found
    '''
    logger.info('Resolving IP space: %s', name)
    data = client.get('/ipam/ip_space', params={'_filter': f'name=="{name}"'})
    results = data.get('results', [])
    if not results:
        logger.error('IP space not found: %s', name)
        sys.exit(1)
    space_id = results[0]['id']
    logger.debug('IP space ID: %s', space_id)
    return space_id


def resolve_dns_view(client, name: str) -> str:
    '''
    Resolve a DNS view name to its API resource ID.

    Args:
        client: UDDIClient instance
        name:   DNS view name

    Returns:
        DNS view resource ID string

    Raises:
        SystemExit if the view is not found
    '''
    logger.info('Resolving DNS view: %s', name)
    data = client.get('/dns/view', params={'_filter': f'name=="{name}"'})
    results = data.get('results', [])
    if not results:
        logger.error('DNS view not found: %s', name)
        sys.exit(1)
    view_id = results[0]['id']
    logger.debug('DNS view ID: %s', view_id)
    return view_id


def find_zone(client, fqdn: str, view_id: str) -> dict:
    '''
    Look up an authoritative DNS zone by FQDN within a view.

    Args:
        client:  UDDIClient instance
        fqdn:    Zone FQDN without the trailing dot (e.g. 'corp.example.com')
        view_id: DNS view resource ID

    Returns:
        Zone resource dict, or {} if not found
    '''
    data = client.get(
        '/dns/auth_zone',
        params={'_filter': f'fqdn=="{fqdn}." and view=="{view_id}"'},
    )
    results = data.get('results', [])
    return results[0] if results else {}


# ---------------------------------------------------------------------------
# DNS record helpers
# ---------------------------------------------------------------------------

SUPPORTED_RECORD_TYPES = ('A', 'AAAA', 'CNAME', 'MX', 'TXT', 'PTR')


def build_record_body(zone_id: str, record: dict) -> dict:
    '''
    Build a Universal DDI ``POST /dns/record`` request body from a template
    record definition.

    Accepts scalar shorthand rdata for single-value types (A, AAAA, CNAME,
    TXT, PTR) and a mapping for MX (``preference``/``pref`` and ``exchange``).
    An ``@`` or empty ``name`` denotes the zone apex.

    Args:
        zone_id: Auth zone resource ID the record belongs to
        record:  Template record dict (name, type, rdata, optional ttl)

    Returns:
        Request body dict suitable for client.post('/dns/record', body)

    Raises:
        ValueError if the record type is unsupported or rdata is malformed
    '''
    rtype = str(record.get('type', '')).strip().upper()
    if rtype not in SUPPORTED_RECORD_TYPES:
        raise ValueError(
            f'Unsupported record type {rtype!r}; supported: '
            f'{", ".join(SUPPORTED_RECORD_TYPES)}'
        )

    raw = record.get('rdata')
    if rtype in ('A', 'AAAA'):
        rdata = {'address': str(raw)}
    elif rtype == 'CNAME':
        rdata = {'cname': str(raw)}
    elif rtype == 'TXT':
        rdata = {'text': str(raw)}
    elif rtype == 'PTR':
        rdata = {'dname': str(raw)}
    else:  # MX
        if not isinstance(raw, dict):
            raise ValueError('MX rdata must be a mapping with preference and exchange')
        pref = raw.get('preference', raw.get('pref'))
        exchange = raw.get('exchange', '')
        if pref is None or not exchange:
            raise ValueError('MX rdata requires both preference and exchange')
        rdata = {'preference': int(pref), 'exchange': str(exchange)}

    name = str(record.get('name', '')).strip()
    if name == '@':
        name = ''

    body = {
        'name_in_zone': name,
        'zone':         zone_id,
        'type':         rtype,
        'rdata':        rdata,
    }
    if record.get('ttl') is not None:
        body['ttl'] = int(record['ttl'])
    return body


# ---------------------------------------------------------------------------
# Template validation
# ---------------------------------------------------------------------------

def validate_template(template: dict, template_name: str = '') -> dict:
    '''
    Validate a parsed YAML template against the schema for its type.

    Dispatches on template_type() to the appropriate structural validator
    (site, address-block, or dns).  Does not contact the API — purely
    structural validation.

    Args:
        template:      Parsed YAML dict (from load_yaml_template or {})
        template_name: Optional filename for the result metadata

    Returns:
        Dict with keys:
            valid     -- True if no errors found
            template  -- template_name echoed back
            type      -- resolved template type
            errors    -- list of {field, message} dicts (schema violations)
            warnings  -- list of {field, message} dicts (missing optionals)
    '''
    errors: list[dict] = []
    warnings: list[dict] = []
    ttype = template_type(template)

    if ttype == 'address-block':
        _validate_block(template, errors, warnings)
    elif ttype == 'dns':
        _validate_dns(template, errors, warnings)
    else:
        _validate_site(template, errors, warnings)

    return {
        'valid': len(errors) == 0,
        'template': template_name,
        'type': ttype,
        'errors': errors,
        'warnings': warnings,
    }


def _validate_site(template: dict, errors: list, warnings: list) -> None:
    '''
    Validate a site template's structure, appending to errors/warnings.

    Args:
        template: Parsed YAML dict
        errors:   List to append schema-violation dicts to
        warnings: List to append missing-optional dicts to
    '''
    def _err(field: str, msg: str) -> None:
        errors.append({'field': field, 'message': msg})
        return

    def _warn(field: str, msg: str) -> None:
        warnings.append({'field': field, 'message': msg})
        return

    # ── site ──
    site = template.get('site') or {}
    if not isinstance(site, dict):
        _err('site', 'Must be a mapping')
        site = {}

    name = str(site.get('name', '')).strip()
    if not name:
        _err('site.name', 'Required and must be non-empty')
    elif ' ' in name:
        _warn('site.name', 'Contains spaces — consider hyphens for DNS compatibility')

    if not site.get('region'):
        _warn('site.region', 'Not specified — useful for block-selection filtering')
    if not site.get('environment'):
        _warn('site.environment', 'Not specified')

    # ── network ──
    net = template.get('network') or {}
    if net and not isinstance(net, dict):
        _err('network', 'Must be a mapping')
        net = {}

    if not net.get('ip_space'):
        _warn('network.ip_space',
              'Not set — falls back to INI [DEFAULTS] ip_space; runtime error if missing there too')

    subnet_size = net.get('subnet_size')
    if subnet_size is not None:
        try:
            sz = int(subnet_size)
            if not 8 <= sz <= 30:
                _err('network.subnet_size', f'CIDR prefix {sz} is outside valid range 8–30')
        except (TypeError, ValueError):
            _err('network.subnet_size', f'Must be an integer, got {subnet_size!r}')

    subnet_names: set[str] = set()
    subnets = net.get('subnets') or []
    if subnets and not isinstance(subnets, list):
        _err('network.subnets', 'Must be a list')
        subnets = []

    for i, s in enumerate(subnets):
        pfx = f'network.subnets[{i}]'
        if not isinstance(s, dict):
            _err(pfx, 'Each subnet must be a mapping')
            continue
        sname = str(s.get('name', '')).strip()
        if not sname:
            _warn(f'{pfx}.name', 'Subnet name is empty')
        else:
            if sname in subnet_names:
                _err(f'{pfx}.name', f'Duplicate subnet name {sname!r}')
            subnet_names.add(sname)
        if not s.get('purpose'):
            _warn(f'{pfx}.purpose', 'No purpose specified')
        cidr = s.get('cidr')
        if cidr is not None:
            try:
                c = int(cidr)
                if not 8 <= c <= 30:
                    _err(f'{pfx}.cidr', f'CIDR prefix {c} is outside valid range 8–30')
            except (TypeError, ValueError):
                _err(f'{pfx}.cidr', f'Must be an integer, got {cidr!r}')
        if s.get('dhcp'):
            for off_key in ('dhcp_start', 'dhcp_end'):
                val = s.get(off_key)
                if val is not None:
                    try:
                        v = int(val)
                        if not 1 <= v <= 254:
                            _err(f'{pfx}.{off_key}', f'Host offset {v} outside 1–254')
                    except (TypeError, ValueError):
                        _err(f'{pfx}.{off_key}', f'Must be an integer, got {val!r}')

    # ── dns ──
    dns = template.get('dns') or {}
    if dns and not isinstance(dns, dict):
        _err('dns', 'Must be a mapping')
        dns = {}

    if not dns.get('parent'):
        _warn('dns.parent',
              'Not set — falls back to INI [DEFAULTS] dns_parent; runtime error if missing there too')

    for bool_key in ('create_zone', 'create_reverse_zone'):
        val = dns.get(bool_key)
        if val is not None and not isinstance(val, bool):
            _err(f'dns.{bool_key}', f'Must be true or false, got {val!r}')

    # ── hosts ──
    hosts = template.get('hosts') or []
    if hosts and not isinstance(hosts, list):
        _err('hosts', 'Must be a list')
        hosts = []

    for i, h in enumerate(hosts):
        pfx = f'hosts[{i}]'
        if not isinstance(h, dict):
            _err(pfx, 'Each host must be a mapping')
            continue
        if not h.get('hostname'):
            _err(f'{pfx}.hostname', 'hostname is required')
        ref = str(h.get('subnet', '')).strip()
        if ref and subnet_names and ref not in subnet_names:
            _err(f'{pfx}.subnet',
                 f'References unknown subnet {ref!r}; defined: {sorted(subnet_names)}')

    # ── tags ──
    tags = template.get('tags') or {}
    if tags and not isinstance(tags, dict):
        _err('tags', 'Must be a mapping of key: value pairs')
    elif tags:
        for k, v in tags.items():
            if not isinstance(k, str):
                _err(f'tags', f'Tag key {k!r} must be a string')
            if v is not None and not isinstance(v, (str, int, float, bool)):
                _warn(f'tags.{k}', f'Value {v!r} is not a scalar')

    return


def _validate_block(template: dict, errors: list, warnings: list) -> None:
    '''
    Validate an address-block template's structure.

    Checks the address_blocks list, each block's address/cidr, and recurses
    into nested children verifying each child network falls inside its parent.

    Args:
        template: Parsed YAML dict
        errors:   List to append schema-violation dicts to
        warnings: List to append missing-optional dicts to
    '''
    def _err(field: str, msg: str) -> None:
        errors.append({'field': field, 'message': msg})
        return

    def _warn(field: str, msg: str) -> None:
        warnings.append({'field': field, 'message': msg})
        return

    if not str(template.get('name', '')).strip():
        _warn('name', 'No template name — used to tag and later find created blocks')

    blocks = template.get('address_blocks')
    if not blocks:
        _err('address_blocks', 'Required and must be a non-empty list')
        blocks = []
    elif not isinstance(blocks, list):
        _err('address_blocks', 'Must be a list')
        blocks = []

    def _check_block(block: dict, pfx: str, parent_net) -> None:
        if not isinstance(block, dict):
            _err(pfx, 'Each block must be a mapping')
        else:
            addr = str(block.get('address', '')).strip()
            cidr = block.get('cidr')
            net = None
            if not addr:
                _err(f'{pfx}.address', 'Required')
            if cidr is None:
                _err(f'{pfx}.cidr', 'Required')
            else:
                try:
                    c = int(cidr)
                    if not 8 <= c <= 30:
                        _err(f'{pfx}.cidr', f'CIDR prefix {c} is outside valid range 8–30')
                    elif addr:
                        net = ipaddress.ip_network(f'{addr}/{c}', strict=False)
                except (TypeError, ValueError) as exc:
                    _err(f'{pfx}.cidr', f'Invalid address/cidr: {exc}')
            if net is not None and parent_net is not None:
                if not (net.subnet_of(parent_net) and net != parent_net):
                    _err(f'{pfx}', f'{net} is not contained within parent {parent_net}')
            if parent_net is None:
                if not block.get('region'):
                    _warn(f'{pfx}.region', 'No region — site discovery filters on Region')
                if not block.get('environment'):
                    _warn(f'{pfx}.environment', 'No environment — site discovery filters on Environment')
            children = block.get('children') or []
            if children and not isinstance(children, list):
                _err(f'{pfx}.children', 'Must be a list')
                children = []
            for j, child in enumerate(children):
                _check_block(child, f'{pfx}.children[{j}]', net)
        return

    for i, block in enumerate(blocks):
        _check_block(block, f'address_blocks[{i}]', None)

    return


def _validate_dns(template: dict, errors: list, warnings: list) -> None:
    '''
    Validate a dns template's structure.

    Checks the zones list, each zone's fqdn, and each record's type and rdata
    shape (via the same normalisation used at provision time).

    Args:
        template: Parsed YAML dict
        errors:   List to append schema-violation dicts to
        warnings: List to append missing-optional dicts to
    '''
    def _err(field: str, msg: str) -> None:
        errors.append({'field': field, 'message': msg})
        return

    def _warn(field: str, msg: str) -> None:
        warnings.append({'field': field, 'message': msg})
        return

    zones = template.get('zones')
    if not zones:
        _err('zones', 'Required and must be a non-empty list')
        zones = []
    elif not isinstance(zones, list):
        _err('zones', 'Must be a list')
        zones = []

    for i, zone in enumerate(zones):
        pfx = f'zones[{i}]'
        if not isinstance(zone, dict):
            _err(pfx, 'Each zone must be a mapping')
            continue
        if not str(zone.get('fqdn', '')).strip():
            _err(f'{pfx}.fqdn', 'Required and must be non-empty')
        kind = str(zone.get('kind', 'forward')).strip().lower()
        if kind not in ('forward', 'reverse'):
            _err(f'{pfx}.kind', f"Must be 'forward' or 'reverse', got {kind!r}")
        records = zone.get('records') or []
        if records and not isinstance(records, list):
            _err(f'{pfx}.records', 'Must be a list')
            records = []
        for j, rec in enumerate(records):
            rpfx = f'{pfx}.records[{j}]'
            if not isinstance(rec, dict):
                _err(rpfx, 'Each record must be a mapping')
                continue
            try:
                build_record_body('validate', rec)
            except (ValueError, TypeError) as exc:
                _err(rpfx, str(exc))

    return


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def detect_drift(template: dict, live: dict, site_name: str = '') -> dict:
    '''
    Compare a template's expected state against a live query result.

    Does not contact the API — caller is responsible for providing the
    live dict (typically from query_site.py --json output).

    Args:
        template:  Parsed YAML template dict
        live:      QueryResult as a plain dict (from `uddi query site --json`);
                   pass {} or a dict with no subnets when the site is absent
        site_name: Site identifier echoed back into the result

    Returns:
        Dict with:
            site         -- site name
            found        -- True if the site's subnets were found
            drifted      -- True if any differences were detected
            subnet_count -- number of live subnets for the site
            drifts       -- list of {category, severity, field, message}
            summary      -- {total, errors, warnings}
    '''
    drifts: list[dict] = []

    def _drift(category: str, severity: str, field: str, message: str) -> None:
        drifts.append({
            'category': category,
            'severity': severity,
            'field': field,
            'message': message,
        })
        return

    # Resolve site name from live data when not supplied
    resolved_site = site_name or live.get('site', '')

    # ── 1. Site existence (a site exists when it has subnets) ───────────────
    live_subnets = live.get('subnets') or []
    if not (live_subnets or live.get('found')):
        _drift('site', 'error', 'site',
               'Site is not provisioned — no subnets found')
        result = {
            'site': resolved_site,
            'found': False,
            'drifted': True,
            'subnet_count': 0,
            'drifts': drifts,
            'summary': {'total': 1, 'errors': 1, 'warnings': 0},
        }
    else:
        net = template.get('network') or {}
        dns = template.get('dns') or {}
        tags_tmpl = template.get('tags') or {}

        # Template tags are applied to the site's subnets (the pool block is
        # shared and untagged), so compare against a subnet's tags.
        live_tags = (live_subnets[0].get('tags') or {}) if live_subnets else {}

        # ── 2. Subnets ─────────────────────────────────────────────────────────
        expected_subnet_names = {
            str(s.get('name', '')).strip()
            for s in (net.get('subnets') or [])
            if str(s.get('name', '')).strip()
        }
        live_subnet_names = {
            str(s.get('name', '')).strip()
            for s in live_subnets
            if str(s.get('name', '')).strip()
        }

        for name in sorted(expected_subnet_names - live_subnet_names):
            _drift('subnet', 'error', f'network.subnets[{name}]',
                   f'Expected subnet {name!r} not found in API')
        for name in sorted(live_subnet_names - expected_subnet_names):
            _drift('subnet', 'warning', f'subnet:{name}',
                   f'Subnet {name!r} exists in API but is not in the template')

        # ── 3. DNS zone (forward only — reverse zones and DHCP ranges are not
        #        checked because query_site.py does not query those resources) ──
        wants_zone = bool(dns.get('create_zone'))
        zone_found = bool(live.get('dns_zone_found'))

        if wants_zone and not zone_found:
            _drift('dns', 'error', 'dns.create_zone',
                   'Template specifies create_zone: true but no DNS zone was found')
        elif not wants_zone and zone_found:
            fqdn = live.get('dns_zone_fqdn', '')
            _drift('dns', 'warning', 'dns.create_zone',
                   f'DNS zone {fqdn!r} exists in API but template does not specify create_zone: true')

        # ── 4. Template tags ───────────────────────────────────────────────────
        for key, expected_val in sorted(tags_tmpl.items()):
            live_val = live_tags.get(key)
            if live_val is None:
                _drift('tags', 'warning', f'tags.{key}',
                       f'Tag {key!r} missing from subnet tags (expected {str(expected_val)!r})')
            elif str(live_val) != str(expected_val):
                _drift('tags', 'warning', f'tags.{key}',
                       f'Tag {key!r}: expected {str(expected_val)!r}, live value is {str(live_val)!r}')

        # ── 5. Hosts ───────────────────────────────────────────────────────────
        expected_hosts = {
            str(h.get('hostname', '')).strip()
            for h in (template.get('hosts') or [])
            if str(h.get('hostname', '')).strip()
        }
        live_hosts: set[str] = set()
        for subnet in live_subnets:
            for h in subnet.get('hosts') or []:
                raw = h.get('name') or h.get('id') or ''
                # strip domain suffix to get bare hostname for comparison
                base = str(raw).split('.')[0].strip()
                if base:
                    live_hosts.add(base)

        for hostname in sorted(expected_hosts - live_hosts):
            _drift('hosts', 'warning', f'hosts[{hostname}]',
                   f'Expected host {hostname!r} not found in any subnet')
        for hostname in sorted(live_hosts - expected_hosts):
            _drift('hosts', 'info', f'host:{hostname}',
                   f'Host {hostname!r} exists in API but is not in the template')

        errors   = sum(1 for d in drifts if d['severity'] == 'error')
        warnings = sum(1 for d in drifts if d['severity'] in ('warning', 'info'))

        result = {
            'site': resolved_site,
            'found': True,
            'drifted': len(drifts) > 0,
            'subnet_count': len(live_subnets),
            'drifts': drifts,
            'summary': {
                'total': len(drifts),
                'errors': errors,
                'warnings': warnings,
            },
        }

    return result
