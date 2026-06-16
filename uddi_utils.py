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
__version__ = '1.1.0'
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
