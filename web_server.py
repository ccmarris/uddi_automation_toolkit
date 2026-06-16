#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Flask web server for the UDDI Automation Toolkit.

    Provides a browser-based UI for managing site templates and executing
    provision / decommission / query operations with real-time streaming
    output.

    The web app (index.html + app.js in the static/ directory) is served
    at the root URL.  The existing site_template_builder.html continues
    to work unchanged as a standalone offline tool.

    API endpoints:
      GET  /                         Serve the web UI (index.html)
      GET  /api/templates            List templates in the templates dir
      GET  /api/templates/<name>     Return raw YAML content
      POST /api/templates/<name>     Save (create/overwrite) a template
      DEL  /api/templates/<name>     Delete a template
      POST /api/provision            Stream provision execution (SSE)
      POST /api/decommission         Stream decommission execution (SSE)
      POST /api/query                Stream query execution (SSE)
      GET  /api/config               Return non-secret config values
      GET  /api/health               Health check

    All streaming endpoints use Server-Sent Events (SSE) format.
    The final event is always: data: [EXIT:<returncode>]

 Usage:
    web_server.py [-c CONFIG] [--templates-dir DIR]
                  [-p PORT] [--host HOST] [--debug-flask]
                  [-d | -v] [-V]

 Examples:
    # Start on default port 5000
    web_server.py -v

    # Custom port and host
    web_server.py --host 0.0.0.0 --port 8080 -v

    # Flask debug mode (auto-reload on file changes)
    web_server.py --debug-flask -v

 Requirements:
    Python 3.8+ with requests, PyYAML, and Flask

    pip install requests pyyaml flask

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
import configparser
import json
import logging
import os
import subprocess
import sys

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from uddi_utils import read_config, setup_logging

logger = logging.getLogger(__name__)

# Resolved at startup from CLI args
CONFIG_FILE: str = 'uddi.ini'
TEMPLATES_DIR: str = ''
SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    static_folder=os.path.join(SCRIPT_DIR, 'static'),
    static_url_path='/static',
)


# ---------------------------------------------------------------------------
# Security helper
# ---------------------------------------------------------------------------

def safe_template_name(name: str) -> str:
    '''
    Sanitise a template name to prevent path traversal.

    Args:
        name: Raw name from URL segment (e.g. 'site-london.yaml')

    Returns:
        Basename only, with any directory components stripped

    Raises:
        ValueError if the resulting name contains '..' or is empty
    '''
    safe = os.path.basename(name)
    if not safe or '..' in safe:
        raise ValueError(f'Invalid template name: {name!r}')
    return safe


def template_path(name: str) -> str:
    '''
    Resolve the filesystem path for a template name.

    Args:
        name: Safe template filename (already sanitised)

    Returns:
        Absolute path within the templates directory
    '''
    return os.path.join(TEMPLATES_DIR, name)


# ---------------------------------------------------------------------------
# Routes — static files
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    '''Serve the main web UI.'''
    return send_from_directory(os.path.join(SCRIPT_DIR, 'static'), 'index.html')


@app.route('/site_template_builder.html')
def template_builder():
    '''Serve the standalone offline template builder.'''
    return send_from_directory(SCRIPT_DIR, 'site_template_builder.html')


# ---------------------------------------------------------------------------
# Routes — template CRUD
# ---------------------------------------------------------------------------

@app.route('/api/templates', methods=['GET'])
def list_templates():
    '''List all YAML templates in the templates directory.'''
    try:
        entries = []
        for fname in sorted(os.listdir(TEMPLATES_DIR)):
            if fname.endswith(('.yaml', '.yml')):
                fpath = os.path.join(TEMPLATES_DIR, fname)
                entries.append({
                    'name':     fname,
                    'path':     fpath,
                    'modified': os.path.getmtime(fpath),
                })
        return jsonify(entries)
    except OSError as exc:
        logger.error('Failed to list templates: %s', exc)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/templates/<path:name>', methods=['GET'])
def get_template(name: str):
    '''Return the raw YAML content of a template.'''
    try:
        safe = safe_template_name(name)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    fpath = template_path(safe)
    if not os.path.isfile(fpath):
        return jsonify({'error': f'Template not found: {safe}'}), 404

    try:
        with open(fpath, 'r') as fh:
            content = fh.read()
        return jsonify({'name': safe, 'content': content})
    except OSError as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/templates/<path:name>', methods=['POST'])
def save_template(name: str):
    '''Create or overwrite a template with the provided YAML content.'''
    try:
        safe = safe_template_name(name)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    data = request.get_json(silent=True)
    if not data or 'content' not in data:
        return jsonify({'error': 'Request body must be JSON with a "content" field'}), 400

    fpath = template_path(safe)
    try:
        with open(fpath, 'w') as fh:
            fh.write(data['content'])
        logger.info('Saved template: %s', fpath)
        return jsonify({'name': safe, 'saved': True})
    except OSError as exc:
        logger.error('Failed to save template %s: %s', safe, exc)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/templates/<path:name>', methods=['DELETE'])
def delete_template(name: str):
    '''Delete a template file.'''
    try:
        safe = safe_template_name(name)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    fpath = template_path(safe)
    if not os.path.isfile(fpath):
        return jsonify({'error': f'Template not found: {safe}'}), 404

    try:
        os.remove(fpath)
        logger.info('Deleted template: %s', fpath)
        return jsonify({'name': safe, 'deleted': True})
    except OSError as exc:
        logger.error('Failed to delete template %s: %s', safe, exc)
        return jsonify({'error': str(exc)}), 500


# ---------------------------------------------------------------------------
# Streaming execution helper
# ---------------------------------------------------------------------------

def _stream_script(cmd: list) -> Response:
    '''
    Run cmd as a subprocess and stream its output as Server-Sent Events.

    Each line of combined stdout+stderr is emitted as:
        data: <line text>\n\n

    The final event is:
        data: [EXIT:<returncode>]\n\n

    Args:
        cmd: Command and arguments list for subprocess.Popen

    Returns:
        Flask Response with content_type='text/event-stream'
    '''
    def generate():
        logger.debug('Streaming: %s', ' '.join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=SCRIPT_DIR,
        )
        for line in proc.stdout:
            yield f'data: {line}\n\n'
        proc.wait()
        yield f'data: [EXIT:{proc.returncode}]\n\n'

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


# ---------------------------------------------------------------------------
# Routes — script execution (SSE streaming)
# ---------------------------------------------------------------------------

@app.route('/api/provision', methods=['POST'])
def provision():
    '''
    Execute provision_site.py for a template and stream output.

    Expected JSON body:
        template  (str)  — template filename (in templates dir)
        dry_run   (bool) — default True for safety
        verbose   (bool) — default True
        create_zone         (bool) — optional
        create_reverse_zone (bool) — optional
    '''
    data = request.get_json(silent=True) or {}
    template_name = data.get('template', '')
    dry_run = data.get('dry_run', True)
    verbose = data.get('verbose', True)

    try:
        safe = safe_template_name(template_name)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    tmpl_path = template_path(safe)
    if not os.path.isfile(tmpl_path):
        return jsonify({'error': f'Template not found: {safe}'}), 404

    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'provision_site.py'),
        '-t', tmpl_path,
        '-c', CONFIG_FILE,
    ]
    if dry_run:
        cmd.append('--dry-run')
    if verbose:
        cmd.append('-v')
    if data.get('create_zone'):
        cmd.append('--create-zone')
    if data.get('create_reverse_zone'):
        cmd.append('--create-reverse-zone')

    return _stream_script(cmd)


@app.route('/api/decommission', methods=['POST'])
def decommission():
    '''
    Execute decommission_site.py for a template and stream output.

    Expected JSON body:
        template      (str)  — template filename
        dry_run       (bool) — default True for safety
        verbose       (bool) — default True
        force         (bool) — skip confirmation prompt
        keep_zone     (bool) — preserve DNS zone
        final_status  (str)  — 'decommissioned' or 'available'
    '''
    data = request.get_json(silent=True) or {}
    template_name = data.get('template', '')
    dry_run = data.get('dry_run', True)
    verbose = data.get('verbose', True)

    try:
        safe = safe_template_name(template_name)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    tmpl_path = template_path(safe)
    if not os.path.isfile(tmpl_path):
        return jsonify({'error': f'Template not found: {safe}'}), 404

    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'decommission_site.py'),
        '-t', tmpl_path,
        '-c', CONFIG_FILE,
    ]
    if dry_run:
        cmd.append('--dry-run')
    if verbose:
        cmd.append('-v')
    if data.get('force'):
        cmd.append('--force')
    if data.get('keep_zone'):
        cmd.append('--keep-zone')
    if data.get('final_status') in ('decommissioned', 'available'):
        cmd.extend(['--final-status', data['final_status']])

    return _stream_script(cmd)


@app.route('/api/query', methods=['POST'])
def query():
    '''
    Execute query_site.py for a template and stream output.

    Expected JSON body:
        template     (str)  — template filename
        verbose      (bool) — default True
        json_output  (bool) — use --json flag
    '''
    data = request.get_json(silent=True) or {}
    template_name = data.get('template', '')
    verbose = data.get('verbose', True)
    json_output = data.get('json_output', False)

    try:
        safe = safe_template_name(template_name)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    tmpl_path = template_path(safe)
    if not os.path.isfile(tmpl_path):
        return jsonify({'error': f'Template not found: {safe}'}), 404

    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'query_site.py'),
        '-t', tmpl_path,
        '-c', CONFIG_FILE,
    ]
    if verbose and not json_output:
        cmd.append('-v')
    if json_output:
        cmd.append('--json')

    return _stream_script(cmd)


@app.route('/api/query-json', methods=['POST'])
def query_json():
    '''
    Execute query_site.py with --json and return parsed result.

    Expected JSON body:
        template (str) — template filename
    '''
    data = request.get_json(silent=True) or {}
    template_name = data.get('template', '')

    try:
        safe = safe_template_name(template_name)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    tmpl_path = template_path(safe)
    if not os.path.isfile(tmpl_path):
        return jsonify({'error': f'Template not found: {safe}'}), 404

    cmd = [
        sys.executable,
        os.path.join(SCRIPT_DIR, 'query_site.py'),
        '-t', tmpl_path,
        '-c', CONFIG_FILE,
        '--json',
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        return jsonify({'error': err}), 500

    try:
        return jsonify(json.loads(proc.stdout))
    except json.JSONDecodeError:
        return jsonify({'error': 'Unexpected output from query_site.py', 'raw': proc.stdout}), 500


# ---------------------------------------------------------------------------
# Routes — config and health
# ---------------------------------------------------------------------------

@app.route('/api/config', methods=['GET'])
def get_config():
    '''
    Return non-secret configuration values from the INI file.

    The api_key is explicitly excluded — it never leaves the server.
    '''
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)

    safe_config = {
        'url':         cfg.get('UDDI', 'url', fallback=''),
        'ip_space':    cfg.get('DEFAULTS', 'ip_space', fallback=''),
        'dns_parent':  cfg.get('DEFAULTS', 'dns_parent', fallback=''),
        'dns_view':    cfg.get('DEFAULTS', 'dns_view', fallback=''),
        'owner':       cfg.get('DEFAULTS', 'owner', fallback=''),
        'subnet_size': cfg.get('DEFAULTS', 'subnet_size', fallback='24'),
    }
    return jsonify(safe_config)


@app.route('/api/health', methods=['GET'])
def health():
    '''Simple health check endpoint.'''
    return jsonify({'status': 'ok', 'version': __version__})


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
        description='Web server for the UDDI Automation Toolkit',
    )

    parser.add_argument(
        '-V', '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )
    parser.add_argument(
        '-c', '--config',
        default='uddi.ini',
        metavar='FILE',
        help='Path to INI configuration file (default: uddi.ini in current working directory)',
    )
    parser.add_argument(
        '--templates-dir',
        default=None,
        metavar='DIR',
        help='Directory containing site YAML templates (default: ./templates)',
    )
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=5000,
        help='Port to listen on (default: 5000)',
    )
    parser.add_argument(
        '--host',
        default='127.0.0.1',
        help='Host address to bind to (default: 127.0.0.1)',
    )
    parser.add_argument(
        '--debug-flask',
        action='store_true',
        default=False,
        help='Enable Flask debug mode (auto-reload on file changes)',
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

    Validates configuration, resolves paths, then starts the Flask server.
    '''
    global CONFIG_FILE, TEMPLATES_DIR

    args = parseargs()
    setup_logging(debug=args.debug, verbose=args.verbose)

    logger.debug('Arguments: %s', args)

    # Resolve config file path
    CONFIG_FILE = args.config
    read_config(CONFIG_FILE)   # validate at startup; exits on error

    # Resolve templates directory
    if args.templates_dir:
        TEMPLATES_DIR = os.path.abspath(args.templates_dir)
    else:
        TEMPLATES_DIR = os.path.join(SCRIPT_DIR, 'templates')

    if not os.path.isdir(TEMPLATES_DIR):
        logger.error('Templates directory not found: %s', TEMPLATES_DIR)
        sys.exit(1)

    logger.info('Templates directory: %s', TEMPLATES_DIR)
    logger.info('Starting server at http://%s:%d/', args.host, args.port)

    print(f'\nUDDI Toolkit Web Server v{__version__}')
    print(f'  Listening : http://{args.host}:{args.port}/')
    print(f'  Templates : {TEMPLATES_DIR}')
    print(f'  Config    : {CONFIG_FILE}')
    print()

    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug_flask,
        threaded=True,
    )


if __name__ == '__main__':
    main()
