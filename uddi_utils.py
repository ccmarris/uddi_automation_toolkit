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
    load_yaml_template  -- load and validate a YAML site template file
    read_config         -- read and validate the INI configuration file
    setup_logging       -- configure root logger from debug/verbose flags
    reverse_zone_fqdn   -- compute in-addr.arpa FQDN for a subnet

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

import configparser
import ipaddress
import logging
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


def read_config(config_file: str) -> configparser.ConfigParser:
    '''
    Read and validate the INI configuration file.

    Expected sections:

        [UDDI]
        api_key = <key>
        url     = https://csp.infoblox.com

        [DEFAULTS]
        ip_space    = my-ip-space
        dns_parent  = internal.example.com
        dns_view    = default
        owner       = network-team
        subnet_size = 24

    Args:
        config_file: Path to the INI configuration file

    Returns:
        Populated ConfigParser instance

    Raises:
        SystemExit if the file cannot be read or required keys are missing
    '''
    cfg = configparser.ConfigParser()
    if not cfg.read(config_file):
        logger.error('Configuration file not found: %s', config_file)
        sys.exit(1)

    required = [('UDDI', 'api_key'), ('UDDI', 'url')]
    for section, key in required:
        if not cfg.has_option(section, key):
            logger.error(
                'Missing required config [%s] %s in %s',
                section, key, config_file,
            )
            sys.exit(1)

    return cfg


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
