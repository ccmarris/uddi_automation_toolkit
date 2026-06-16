#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Shared HTTP client for the Infoblox Universal DDI REST API.

    Provides a thin requests wrapper that handles authentication,
    base-URL construction, and uniform error handling.  Import and
    instantiate UDDIClient in any script that needs to talk to the API.

 Usage:
    from uddi_client import UDDIClient

    client = UDDIClient(url='https://csp.infoblox.com', api_key='…')
    data   = client.get('/ipam/ip_space', params={'_filter': 'name=="prod"'})

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

import json
import logging
import sys
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class UDDIClient:
    '''
    Thin wrapper around the Infoblox Universal DDI REST API.

    Handles authentication, base URL construction, and common
    error handling so higher-level logic stays clean.
    '''

    BASE_PATH = '/api/ddi/v1'

    def __init__(self, url: str, api_key: str) -> None:
        '''
        Initialise the client.

        Args:
            url:     Base CSP URL, e.g. https://csp.infoblox.com
                     Surrounding quotes (from INI files) are stripped
                     automatically.
            api_key: BloxOne / Universal DDI API key. Surrounding
                     quotes (from INI files) are stripped automatically.
        '''
        self.base_url = url.strip("'\"").rstrip('/') + self.BASE_PATH
        _key = api_key.strip("'\"")
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Token {_key}',
            'Content-Type': 'application/json',
        })

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        '''
        HTTP GET with error handling.

        Args:
            path:   API path relative to BASE_PATH (e.g. '/ipam/ip_space')
            params: Optional query parameters

        Returns:
            Parsed JSON response body

        Raises:
            SystemExit on HTTP error
        '''
        url = self.base_url + path
        logger.debug('GET %s  params=%s', url, params)
        response = self.session.get(url, params=params)
        self._check(response)
        return response.json()

    def post(self, path: str, body: dict) -> dict:
        '''
        HTTP POST with error handling.

        Args:
            path: API path relative to BASE_PATH
            body: Request body as a dict (will be JSON-encoded)

        Returns:
            Parsed JSON response body

        Raises:
            SystemExit on HTTP error
        '''
        url = self.base_url + path
        logger.debug('POST %s  body=%s', url, json.dumps(body))
        response = self.session.post(url, json=body)
        self._check(response)
        return response.json()

    def patch(self, path: str, body: dict) -> dict:
        '''
        HTTP PATCH with error handling.

        Args:
            path: API path relative to BASE_PATH (must include resource ID)
            body: Fields to update

        Returns:
            Parsed JSON response body

        Raises:
            SystemExit on HTTP error
        '''
        url = self.base_url + path
        logger.debug('PATCH %s  body=%s', url, json.dumps(body))
        response = self.session.patch(url, json=body)
        self._check(response)
        return response.json()

    def delete(self, path: str) -> None:
        '''
        HTTP DELETE with error handling.

        Args:
            path: API path relative to BASE_PATH (must include resource ID)

        Raises:
            SystemExit on HTTP error
        '''
        url = self.base_url + path
        logger.debug('DELETE %s', url)
        response = self.session.delete(url)
        self._check(response)

    def _check(self, response: requests.Response) -> None:
        '''
        Raise a clear error on non-2xx responses.

        Args:
            response: requests.Response to inspect

        Raises:
            SystemExit with status code and body on error
        '''
        if not response.ok:
            logger.error(
                'API error %s %s: %s',
                response.request.method,
                response.url,
                response.text,
            )
            sys.exit(1)
