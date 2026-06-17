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
__version__ = '1.2.0'
__author__ = 'Chris Marrison'
__author_email__ = 'chris@infoblox.com'

import configparser
import ipaddress
import logging
import os
import sys

import yaml

logger = logging.getLogger(__name__)


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
        return cfg

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
    if api_key_flag:
        logger.debug('Using API key from --api-key flag')
        return api_key_flag, base_url, verify_ssl

    for env_var in ('INFOBLOX_PORTAL_KEY', 'UDDI_API_KEY'):
        val = os.environ.get(env_var, '')
        if val:
            logger.debug('Using API key from %s environment variable', env_var)
            return val, base_url, verify_ssl

    if ini.get('api_key'):
        logger.debug('Using API key from INI file: %s', ini_file)
        return ini['api_key'].strip('\'"'), base_url, verify_ssl

    return '', base_url, verify_ssl


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
# Template validation
# ---------------------------------------------------------------------------

def validate_template(template: dict, template_name: str = '') -> dict:
    '''
    Validate a parsed YAML site template against the expected schema.

    Checks required fields, value types, CIDR ranges, and cross-field
    references (e.g. host subnet references a defined subnet name).
    Does not contact the API — purely structural validation.

    Args:
        template:      Parsed YAML dict (from load_yaml_template or {})
        template_name: Optional filename for the result metadata

    Returns:
        Dict with keys:
            valid     -- True if no errors found
            template  -- template_name echoed back
            errors    -- list of {field, message} dicts (schema violations)
            warnings  -- list of {field, message} dicts (missing optionals)
    '''
    errors: list[dict] = []
    warnings: list[dict] = []

    def _err(field: str, msg: str) -> None:
        errors.append({'field': field, 'message': msg})

    def _warn(field: str, msg: str) -> None:
        warnings.append({'field': field, 'message': msg})

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

    return {
        'valid': len(errors) == 0,
        'template': template_name,
        'errors': errors,
        'warnings': warnings,
    }
