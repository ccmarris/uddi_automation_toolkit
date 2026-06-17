// UDDI Toolkit Web UI — app.js
'use strict';

// ── Global state ────────────────────────────────────────────────────────────

let currentTemplate = null;   // filename of template currently loaded on server
let currentTab      = 'builder';

// Builder form state
let subnets  = [];
let hosts    = [];
let tags     = [];
let nextId   = 1;

// ── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  loadConfig();
  refreshTemplates();
  seedBuilder();
  builderUpdate();
  updateExecControls();
});

// ── Config banner ────────────────────────────────────────────────────────────

function loadConfig() {
  fetch('/api/config')
    .then(r => r.json())
    .then(cfg => {
      document.getElementById('status-dot').className = 'ok';
      document.getElementById('status-dot').title = 'Server connected';
      const parts = [];
      if (cfg.url)        parts.push(cfg.url);
      if (cfg.ip_space)   parts.push('space: ' + cfg.ip_space);
      if (cfg.dns_parent) parts.push('parent: ' + cfg.dns_parent);
      document.getElementById('config-info').textContent = parts.join('  ·  ');
    })
    .catch(() => {
      const dot = document.getElementById('status-dot');
      dot.className = 'err';
      dot.title = 'Cannot reach server';
    });
}

// ── Tab switching ────────────────────────────────────────────────────────────

function switchTab(tab) {
  currentTab = tab;

  // When switching to Raw YAML, sync from builder
  if (tab === 'yaml') {
    const yaml = buildYAML();
    const raw  = document.getElementById('raw-editor');
    // Only overwrite if textarea is empty or matches what builder would generate
    if (!raw.value || raw.dataset.fromBuilder === 'true') {
      raw.value = yaml;
      raw.dataset.fromBuilder = 'true';
    }
    if (currentTemplate) {
      document.getElementById('tmpl-name').value = currentTemplate;
    }
  }

  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.getElementById('tab-btn-' + tab).classList.add('active');
}

// ── Template list (sidebar) ──────────────────────────────────────────────────

// Track which folders are collapsed: Set of folder paths (e.g. 'emea')
const collapsedFolders = new Set();

// Full template list cached for client-side filtering
let _templateList = [];

function refreshTemplates() {
  fetch('/api/templates')
    .then(r => r.json())
    .then(list => {
      _templateList = list || [];
      const q = document.getElementById('tmpl-search').value;
      if (q.trim()) {
        _renderFiltered(q.trim());
      } else {
        renderTemplateTree(_templateList);
      }
    })
    .catch(err => toast('Failed to load templates: ' + err.message));
}

function filterTemplates(query) {
  if (!query.trim()) {
    renderTemplateTree(_templateList);
  } else {
    _renderFiltered(query.trim());
  }
}

function _renderFiltered(query) {
  const lower = query.toLowerCase();
  const container = document.getElementById('tmpl-list');
  container.innerHTML = '';

  const matches = _templateList.filter(e =>
    e.type === 'file' && e.name.toLowerCase().includes(lower)
  );

  if (!matches.length) {
    container.innerHTML = '<div style="padding:14px 16px;font-size:12px;color:rgba(255,255,255,0.3)">No matches</div>';
    return;
  }

  matches.forEach(tmpl => {
    const label = tmpl.name;
    const el = document.createElement('div');
    el.className = 'tmpl-item' + (tmpl.name === currentTemplate ? ' active' : '');
    el.title = tmpl.name;
    el.dataset.tmplName = tmpl.name;
    el.onclick = () => loadTemplate(tmpl.name);

    // Highlight matching portion
    const idx = label.toLowerCase().indexOf(lower);
    if (idx >= 0) {
      el.appendChild(document.createTextNode(label.slice(0, idx)));
      const mark = document.createElement('span');
      mark.className = 'tmpl-search-match';
      mark.textContent = label.slice(idx, idx + query.length);
      el.appendChild(mark);
      el.appendChild(document.createTextNode(label.slice(idx + query.length)));
    } else {
      el.textContent = label;
    }
    container.appendChild(el);
  });
}

function _buildTree(list) {
  // Convert flat [{name, type}] into nested {dirs:{}, files:[]}
  const root = { dirs: {}, files: [] };

  // Ensure dir entries exist first (handles empty folders)
  list.filter(e => e.type === 'dir').forEach(e => {
    const parts = e.name.split('/');
    let node = root;
    parts.forEach(dir => {
      if (!node.dirs[dir]) node.dirs[dir] = { dirs: {}, files: [] };
      node = node.dirs[dir];
    });
  });

  // Then place files
  list.filter(e => e.type === 'file').forEach(tmpl => {
    const parts = tmpl.name.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const dir = parts[i];
      if (!node.dirs[dir]) node.dirs[dir] = { dirs: {}, files: [] };
      node = node.dirs[dir];
    }
    node.files.push(tmpl);
  });

  return root;
}

function renderTemplateTree(list) {
  const container = document.getElementById('tmpl-list');
  container.innerHTML = '';
  if (!list || list.error || !list.length) {
    container.innerHTML = '<div style="padding:14px 16px;font-size:12px;color:rgba(255,255,255,0.3)">No templates found</div>';
    return;
  }
  const tree = _buildTree(list);
  _renderNode(container, tree, '');
}

function _renderNode(container, node, pathPrefix) {
  // Render subdirectories first, then files at this level
  Object.keys(node.dirs).sort().forEach(dirName => {
    const dirPath = pathPrefix ? pathPrefix + '/' + dirName : dirName;
    const collapsed = collapsedFolders.has(dirPath);

    // Folder row
    const folderEl = document.createElement('div');
    folderEl.className = 'tmpl-folder';
    folderEl.dataset.path = dirPath;
    folderEl.innerHTML =
      '<span class="tmpl-folder-arrow">' + (collapsed ? '▶' : '▼') + '</span>' +
      '<span class="tmpl-folder-name">' + dirName + '</span>' +
      '<button class="tmpl-folder-menu-btn" title="Folder options">⋯</button>';
    folderEl.querySelector('.tmpl-folder-name').onclick =
      folderEl.querySelector('.tmpl-folder-arrow').onclick =
        () => _toggleFolder(dirPath);
    folderEl.querySelector('.tmpl-folder-menu-btn').onclick = (e) => {
      e.stopPropagation();
      _showFolderMenu(e, dirPath);
    };
    container.appendChild(folderEl);

    // Folder children container
    const childEl = document.createElement('div');
    childEl.className = 'tmpl-folder-children' + (collapsed ? ' collapsed' : '');
    childEl.dataset.folderPath = dirPath;
    _renderNode(childEl, node.dirs[dirName], dirPath);
    container.appendChild(childEl);
  });

  // Files at this level
  node.files.forEach(tmpl => {
    const label = tmpl.name.split('/').pop();  // basename only
    const el = document.createElement('div');
    el.className = 'tmpl-item' + (tmpl.name === currentTemplate ? ' active' : '');
    el.textContent = label;
    el.title = tmpl.name;
    el.dataset.tmplName = tmpl.name;
    el.onclick = () => loadTemplate(tmpl.name);
    container.appendChild(el);
  });
}

function _toggleFolder(dirPath) {
  if (collapsedFolders.has(dirPath)) {
    collapsedFolders.delete(dirPath);
  } else {
    collapsedFolders.add(dirPath);
  }
  // Update arrow and visibility without a full re-fetch
  document.querySelectorAll('.tmpl-folder').forEach(el => {
    if (el.dataset.path === dirPath) {
      const collapsed = collapsedFolders.has(dirPath);
      el.querySelector('.tmpl-folder-arrow').textContent = collapsed ? '▶' : '▼';
    }
  });
  document.querySelectorAll('.tmpl-folder-children').forEach(el => {
    if (el.dataset.folderPath === dirPath) {
      el.classList.toggle('collapsed', collapsedFolders.has(dirPath));
    }
  });
}

function _templateApiPath(name) {
  // Encode each path segment separately so '/' is preserved in the URL
  return '/api/templates/' + name.split('/').map(encodeURIComponent).join('/');
}

function _markActive(name) {
  document.querySelectorAll('.tmpl-item').forEach(el => {
    el.classList.toggle('active', el.dataset.tmplName === name);
  });
}

// ── Template CRUD ────────────────────────────────────────────────────────────

function loadTemplate(name) {
  fetch(_templateApiPath(name))
    .then(r => r.json())
    .then(data => {
      if (data.error) { toast('Error: ' + data.error); return; }
      const raw = document.getElementById('raw-editor');
      raw.value = data.content;
      raw.dataset.fromBuilder = 'false';
      document.getElementById('tmpl-name').value = data.name;
      document.getElementById('btn-delete').style.display = '';
      currentTemplate = data.name;
      setBadge(data.name);
      _markActive(data.name);
      switchTab('yaml');
    })
    .catch(err => toast('Failed to load: ' + err.message));
}

function newTemplate() {
  clearBuilder();
  seedBuilder();
  builderUpdate();
  currentTemplate = null;
  setBadge('(unsaved)');
  document.getElementById('tmpl-name').value = '';
  document.getElementById('btn-delete').style.display = 'none';
  _markActive(null);
  const raw = document.getElementById('raw-editor');
  raw.value = '';
  raw.dataset.fromBuilder = 'true';
  switchTab('builder');
}

function saveRawTemplate() {
  const name    = document.getElementById('tmpl-name').value.trim();
  const content = document.getElementById('raw-editor').value;
  _saveContent(name, content);
}

function builderSave() {
  if (currentTemplate) {
    _saveContent(currentTemplate, buildYAML());
  } else {
    builderSaveAs();
  }
}

function builderSaveAs() {
  const site = v('site-name').trim();
  const suggestion = currentTemplate || (site ? site + '.yaml' : 'site-new.yaml');
  const name = prompt('Save template as (use folder/name.yaml for subdirectories):', suggestion);
  if (!name) return;
  const finalName = name.endsWith('.yaml') || name.endsWith('.yml') ? name : name + '.yaml';
  document.getElementById('tmpl-name').value = finalName;
  _saveContent(finalName, buildYAML());
}

function _saveContent(name, content) {
  if (!name) { toast('Enter a template filename first'); return; }
  const base = name.split('/').pop();
  if (!base.endsWith('.yaml') && !base.endsWith('.yml')) {
    toast('Filename must end in .yaml or .yml'); return;
  }
  fetch(_templateApiPath(name), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { toast('Error: ' + data.error); return; }
      currentTemplate = data.name;
      setBadge(data.name);
      document.getElementById('tmpl-name').value = data.name;
      document.getElementById('btn-delete').style.display = '';
      const raw = document.getElementById('raw-editor');
      if (raw.dataset.fromBuilder !== 'false') {
        raw.value = content;
        raw.dataset.fromBuilder = 'true';
      }
      toast('Saved: ' + data.name);
      refreshTemplates();
    })
    .catch(err => toast('Save failed: ' + err.message));
}

function deleteTemplate() {
  const name = currentTemplate || document.getElementById('tmpl-name').value.trim();
  if (!name) { toast('No template selected'); return; }
  if (!confirm('Delete template "' + name + '"?')) return;
  fetch(_templateApiPath(name), { method: 'DELETE' })
    .then(r => r.json())
    .then(data => {
      if (data.error) { toast('Error: ' + data.error); return; }
      document.getElementById('raw-editor').value = '';
      document.getElementById('tmpl-name').value = '';
      document.getElementById('btn-delete').style.display = 'none';
      currentTemplate = null;
      setBadge('(unsaved)');
      toast('Deleted: ' + name);
      refreshTemplates();
    })
    .catch(err => toast('Delete failed: ' + err.message));
}

function setBadge(text) {
  document.getElementById('current-tmpl-badge').textContent = text;
}

// ── Folder management ─────────────────────────────────────────────────────────

let _ctxMenu = null;

function _closeFolderMenu() {
  if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
}

function _showFolderMenu(e, dirPath) {
  _closeFolderMenu();
  const menu = document.createElement('div');
  menu.className = 'folder-ctx-menu';
  menu.innerHTML =
    '<button onclick="_promptRenameFolder(\'' + dirPath.replace(/'/g, "\\'") + '\')">Rename…</button>' +
    '<button onclick="_promptNewSubfolder(\'' + dirPath.replace(/'/g, "\\'") + '\')">New subfolder…</button>' +
    '<button class="danger" onclick="_confirmDeleteFolder(\'' + dirPath.replace(/'/g, "\\'") + '\')">Delete…</button>';
  menu.style.top  = e.clientY + 'px';
  menu.style.left = e.clientX + 'px';
  document.body.appendChild(menu);
  _ctxMenu = menu;
  // Close on any outside click
  setTimeout(() => document.addEventListener('click', _closeFolderMenu, { once: true }), 0);
}

function promptNewFolder() {
  const path = prompt('New folder path (e.g. emea/staging):');
  if (!path || !path.trim()) return;
  _createFolder(path.trim());
}

function _promptNewSubfolder(parentPath) {
  _closeFolderMenu();
  const name = prompt('New subfolder name inside "' + parentPath + '":');
  if (!name || !name.trim()) return;
  _createFolder(parentPath + '/' + name.trim());
}

function _createFolder(path) {
  fetch('/api/folders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { toast('Error: ' + data.error); return; }
      toast('Created: ' + data.path);
      refreshTemplates();
    })
    .catch(err => toast('Failed: ' + err.message));
}

function _promptRenameFolder(dirPath) {
  _closeFolderMenu();
  const parts = dirPath.split('/');
  const current = parts[parts.length - 1];
  const newName = prompt('Rename "' + current + '" to:', current);
  if (!newName || !newName.trim() || newName.trim() === current) return;
  const newPath = parts.slice(0, -1).concat(newName.trim()).join('/');
  fetch('/api/folders/' + dirPath.split('/').map(encodeURIComponent).join('/'), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_path: newPath }),
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { toast('Error: ' + data.error); return; }
      // If the active template was inside the renamed folder, update it
      if (currentTemplate && currentTemplate.startsWith(dirPath + '/')) {
        currentTemplate = data.new_path + currentTemplate.slice(dirPath.length);
        setBadge(currentTemplate);
        document.getElementById('tmpl-name').value = currentTemplate;
      }
      toast('Renamed to: ' + data.new_path);
      refreshTemplates();
    })
    .catch(err => toast('Failed: ' + err.message));
}

function _confirmDeleteFolder(dirPath) {
  _closeFolderMenu();
  const confirmed = confirm('Delete folder "' + dirPath + '" and ALL its contents?');
  if (!confirmed) return;
  fetch('/api/folders/' + dirPath.split('/').map(encodeURIComponent).join('/') + '?recursive=true', {
    method: 'DELETE',
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { toast('Error: ' + data.error); return; }
      if (currentTemplate && currentTemplate.startsWith(dirPath + '/')) {
        currentTemplate = null;
        setBadge('(unsaved)');
        document.getElementById('tmpl-name').value = '';
        document.getElementById('raw-editor').value = '';
        document.getElementById('btn-delete').style.display = 'none';
      }
      toast('Deleted: ' + dirPath);
      refreshTemplates();
    })
    .catch(err => toast('Failed: ' + err.message));
}

// ── Output tab switching ──────────────────────────────────────────────────────

let _outputTab = 'clean';

function switchOutputTab(tab) {
  _outputTab = tab;
  document.getElementById('otab-clean').classList.toggle('active', tab === 'clean');
  document.getElementById('otab-raw').classList.toggle('active', tab === 'raw');
  document.getElementById('output-clean-area').style.display = tab === 'clean' ? '' : 'none';
  document.getElementById('output').style.display            = tab === 'raw'   ? '' : 'none';
}

// ── Clean output parser ───────────────────────────────────────────────────────

const _LOG_RE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+[\w.]+: (.+)$/;

const _LEVEL_META = {
  INFO:     { icon: '●', cls: 'ci-info' },
  WARNING:  { icon: '▲', cls: 'ci-warn' },
  ERROR:    { icon: '✗', cls: 'ci-error' },
  CRITICAL: { icon: '✗', cls: 'ci-error' },
  PLAIN:    { icon: '·', cls: 'ci-plain' },
  DRYRUN:   { icon: '◆', cls: 'ci-dryrun' },
};

function _parseCleanLine(text) {
  const m = text.match(_LOG_RE);
  if (m) {
    const level = m[2];
    const msg   = m[3];
    if (level === 'DEBUG') return null;
    const isDryRun = msg.includes('[DRY-RUN]');
    return { level: isDryRun ? 'DRYRUN' : level, msg: isDryRun ? msg.replace('[DRY-RUN] ', '') : msg };
  }
  if (text.startsWith('[EXIT:')) return null;
  const isDryRun = text.includes('[DRY-RUN]');
  if (isDryRun) return { level: 'DRYRUN', msg: text.replace('[DRY-RUN] ', '') };
  if (text.trim()) return { level: 'PLAIN', msg: text };
  return null;
}

function _appendClean(parsed) {
  const area = document.getElementById('output-clean-area');
  const meta = _LEVEL_META[parsed.level] || _LEVEL_META.PLAIN;
  const item = document.createElement('div');
  item.className = 'clean-item ' + meta.cls;
  item.innerHTML =
    '<span class="clean-icon">' + meta.icon + '</span>' +
    '<span class="clean-msg">' + htmlEsc(parsed.msg) + '</span>';
  area.appendChild(item);
  area.parentElement.scrollTop = area.parentElement.scrollHeight;
}

function _appendCleanHeader(text) {
  const area = document.getElementById('output-clean-area');
  const hdr = document.createElement('div');
  hdr.className = 'clean-header';
  hdr.textContent = text;
  area.appendChild(hdr);
}

function _appendCleanSummary(code) {
  const area = document.getElementById('output-clean-area');
  const el = document.createElement('div');
  el.className = 'clean-summary ' + (code === 0 ? 'cs-ok' : 'cs-err');
  el.textContent = code === 0 ? '✓  Completed successfully' : '✗  Failed (exit code ' + code + ')';
  area.appendChild(el);
  area.parentElement.scrollTop = area.parentElement.scrollHeight;
}

// ── Execution ────────────────────────────────────────────────────────────────

function updateExecControls() {
  const action = document.getElementById('action-select').value;
  document.getElementById('row-force').style.display        = action === 'decommission' ? '' : 'none';
  document.getElementById('row-create-zone').style.display  = action === 'provision' ? '' : 'none';
  document.getElementById('row-reverse-zone').style.display = action === 'provision' ? '' : 'none';
  document.getElementById('row-dry-run').style.display      = action === 'query' ? 'none' : '';
  if (action !== 'query') showTerminal();
}

function execute() {
  const name = currentTemplate;
  if (!name) {
    toast('Save a template to the server first (use Save or Save As)');
    return;
  }

  const action      = document.getElementById('action-select').value;
  const dryRun      = document.getElementById('dry-run-toggle').checked;
  const force       = document.getElementById('force-toggle').checked;
  const createZone  = document.getElementById('exec-create-zone').checked;
  const reverseZone = document.getElementById('exec-reverse-zone').checked;

  document.getElementById('btn-execute').style.display = 'none';
  document.getElementById('btn-stop').style.display = '';

  if (action === 'query') {
    _executeQuery(name);
    return;
  }

  // Provision / decommission — SSE streaming to terminal
  showTerminal();
  clearOutput();
  const headerLine = '▶ ' + action.toUpperCase() + ': ' + name + (dryRun ? '  [DRY-RUN]' : '');
  appendOutput(headerLine + '\n\n', 'out-dryrun');
  _appendCleanHeader(headerLine);

  const body = { template: name, verbose: true };
  if (action === 'provision') {
    body.dry_run = dryRun;
    body.create_zone = createZone;
    body.create_reverse_zone = reverseZone;
  } else {
    body.dry_run = dryRun;
    body.force = force;
  }

  fetch('/api/' + action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(response => {
    const reader  = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    function pump() {
      return reader.read().then(({ done, value }) => {
        if (done) { execDone(); return; }
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        lines.forEach(line => {
          if (!line.startsWith('data: ')) return;
          const text = line.slice(6);
          if (text.startsWith('[EXIT:')) {
            const code = parseInt(text.slice(6), 10);
            appendOutput(
              '\n' + (code === 0 ? '✓' : '✗') + ' Exited with code ' + code + '\n',
              code === 0 ? 'out-success' : 'out-error',
            );
            _appendCleanSummary(code);
            execDone();
          } else {
            renderLine(text + '\n');
          }
        });
        return pump();
      }).catch(() => execDone());
    }
    pump();
  }).catch(err => {
    appendOutput('Connection error: ' + err.message + '\n', 'out-error');
    execDone();
  });
}

function _executeQuery(name) {
  showQueryResults();
  document.getElementById('query-results').innerHTML =
    '<div style="padding:20px;text-align:center;color:var(--ibx-muted);font-size:12px">Querying…</div>';

  fetch('/api/query-json', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ template: name }),
  })
    .then(r => r.json())
    .then(data => {
      execDone();
      if (data.error) {
        document.getElementById('query-results').innerHTML =
          '<div style="padding:16px;color:var(--ibx-danger);font-size:12px">Error: ' + htmlEsc(data.error) + '</div>';
        return;
      }
      renderQueryResult(data);
    })
    .catch(err => {
      execDone();
      document.getElementById('query-results').innerHTML =
        '<div style="padding:16px;color:var(--ibx-danger);font-size:12px">Connection error: ' + htmlEsc(err.message) + '</div>';
    });
}

function renderQueryResult(r) {
  const site = r.site || '(unknown)';
  let html = '';

  // ── Site / block card ──
  html += `<div class="qr-card">
    <div class="qr-card-header"><span class="qr-title">Site: ${htmlEsc(site)}</span></div>
    <div class="qr-card-body">
      <dl>
        <div class="qr-kv"><dt>Block</dt><dd>${htmlEsc(r.block_address || '—')}</dd></div>
      </dl>`;
  const tags = r.block_tags || {};
  if (Object.keys(tags).length) {
    html += '<div class="qr-tags">';
    for (const [k, v] of Object.entries(tags)) {
      html += `<span class="qr-tag">${htmlEsc(k)}: ${htmlEsc(v)}</span>`;
    }
    html += '</div>';
  }
  html += '</div></div>';

  // ── DNS zone card ──
  html += `<div class="qr-card">
    <div class="qr-card-header">
      <span class="qr-title">DNS Zone</span>
      <span class="qr-badge ${r.dns_zone_found ? 'ok' : 'err'}">${r.dns_zone_found ? 'Found' : 'Not found'}</span>
    </div>`;
  if (r.dns_zone_found) {
    html += `<div class="qr-card-body">
      <dl><div class="qr-kv"><dt>FQDN</dt><dd>${htmlEsc(r.dns_zone_fqdn)}</dd></div></dl>
    </div>`;
  }
  html += '</div>';

  // ── Subnets card ──
  const subnets = r.subnets || [];
  html += `<div class="qr-card">
    <div class="qr-card-header">
      <span class="qr-title">Subnets</span>
      <span class="qr-badge">${subnets.length}</span>
    </div>
    <div class="qr-card-body">`;

  if (!subnets.length) {
    html += '<div class="qr-no-hosts">No subnets found</div>';
  } else {
    subnets.forEach(s => {
      const cidr  = `${s.address}/${s.cidr}`;
      const sname = s.name || '';
      const hosts = s.hosts || [];
      html += `<div class="qr-subnet">
        <div class="qr-subnet-header">
          <span class="qr-subnet-cidr">${htmlEsc(cidr)}</span>
          ${sname ? `<span class="qr-subnet-name">${htmlEsc(sname)}</span>` : ''}
          <span class="qr-badge" style="margin-left:auto">${hosts.length} host${hosts.length !== 1 ? 's' : ''}</span>
        </div>`;
      if (hosts.length) {
        html += '<div class="qr-hosts">';
        hosts.forEach(h => {
          html += `<div class="qr-host-row">
            <span class="qr-host-name">${htmlEsc(h.name || h.id)}</span>
            <span class="qr-host-ip">${htmlEsc(h.ip || '')}</span>
          </div>`;
        });
        html += '</div>';
      } else {
        html += '<div class="qr-hosts"><div class="qr-no-hosts">No hosts</div></div>';
      }
      html += '</div>';
    });
  }
  html += '</div></div>';

  document.getElementById('query-results').innerHTML = html;
}

function showTerminal() {
  document.getElementById('output-tabs').style.display = '';
  document.getElementById('output-area').style.display = '';
  document.getElementById('query-results').style.display = 'none';
  document.getElementById('btn-copy-output').style.display = '';
  switchOutputTab(_outputTab);
}

function showQueryResults() {
  document.getElementById('output-tabs').style.display = 'none';
  document.getElementById('output-area').style.display = 'none';
  document.getElementById('query-results').style.display = '';
  document.getElementById('btn-copy-output').style.display = 'none';
}

function renderLine(text) {
  const lower = text.toLowerCase();
  let cls = '';
  if (lower.includes('[dry-run]'))                              cls = 'out-dryrun';
  else if (lower.includes('error') || lower.includes('failed')) cls = 'out-error';
  else if (lower.includes('warn'))                              cls = 'out-warn';
  else if (lower.includes('complete') || lower.includes('created') || lower.includes('success')) cls = 'out-success';
  appendOutput(text + '\n', cls);
  const parsed = _parseCleanLine(text);
  if (parsed) _appendClean(parsed);
}

function execDone() {
  document.getElementById('btn-execute').style.display = '';
  document.getElementById('btn-stop').style.display = 'none';
}

function stopExec() { execDone(); }

function appendOutput(text, cls) {
  const out  = document.getElementById('output');
  const area = out.parentElement;
  if (cls) {
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = text;
    out.appendChild(span);
  } else {
    out.appendChild(document.createTextNode(text));
  }
  area.scrollTop = area.scrollHeight;
}

function clearOutput() {
  document.getElementById('output').innerHTML = '';
  document.getElementById('output-clean-area').innerHTML = '';
  document.getElementById('query-results').innerHTML = '';
  showTerminal();
}

function copyOutput() {
  navigator.clipboard.writeText(document.getElementById('output').textContent)
    .then(() => toast('Output copied'))
    .catch(() => toast('Copy failed'));
}

// ── Builder helpers ──────────────────────────────────────────────────────────

function v(id)   { const el = document.getElementById(id); return el ? el.value : ''; }
function vb(id)  { const el = document.getElementById(id); return el ? el.checked : false; }
function htmlEsc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function esc(s) {
  if (!s) return '""';
  if (/[:#\[\]{}&*!|>'"%@`,\s]/.test(s) || s.includes('\n')) return '"' + s.replace(/"/g, '\\"') + '"';
  return s;
}

// ── Builder seed ─────────────────────────────────────────────────────────────

function seedBuilder() {
  subnets = [];
  hosts   = [];
  tags    = [];
  nextId  = 1;
  addSubnet({ name: '', purpose: 'mgmt',     cidr: 24, dhcp: false });
  addSubnet({ name: '', purpose: 'user-lan', cidr: 24, dhcp: true,  dhcp_start: 10, dhcp_end: 250 });
  addSubnet({ name: '', purpose: 'server',   cidr: 24, dhcp: false });
  addHost({ hostname: 'gw01', subnet: '', comment: 'Site gateway' });
  addTag({ key: 'Owner', value: '' });
}

function clearBuilder() {
  subnets = [];
  hosts   = [];
  tags    = [];
  nextId  = 1;
  renderSubnets();
  renderHosts();
  renderTags();
  ['site-name','site-location','net-ip-space','net-subnet-size',
   'dns-parent','dns-view'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  ['site-region','site-environment'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.selectedIndex = 0;
  });
  document.getElementById('dns-create-zone').checked  = false;
  document.getElementById('dns-reverse-zone').checked = false;
}

// ── Builder subnets ──────────────────────────────────────────────────────────

function addSubnet(data) {
  const id = nextId++;
  subnets.push({
    id,
    name:      (data && data.name)    || '',
    purpose:   (data && data.purpose) || 'general',
    cidr:      (data && data.cidr)    || 24,
    dhcp:      (data && !!data.dhcp)  || false,
    dhcp_start:(data && data.dhcp_start != null) ? data.dhcp_start : 10,
    dhcp_end:  (data && data.dhcp_end   != null) ? data.dhcp_end   : 250,
  });
  renderSubnets();
  builderUpdate();
}

function removeSubnet(id) {
  subnets = subnets.filter(s => s.id !== id);
  renderSubnets();
  renderHosts();   // subnet dropdown changes
  builderUpdate();
}

function renderSubnets() {
  const container = document.getElementById('subnets-list');
  container.innerHTML = '';
  subnets.forEach((s, i) => {
    const div = document.createElement('div');
    div.className = 'list-item';
    div.innerHTML = `
      <div class="list-item-header">
        <span class="list-item-title">Subnet ${i+1}</span>
        <button class="btn-remove" onclick="removeSubnet(${s.id})">Remove</button>
      </div>
      <div class="field-row thirds">
        <div class="field">
          <label>Name<span class="hint">prefix only</span></label>
          <input type="text" value="${htmlEsc(s.name)}" placeholder="site-mgmt"
            oninput="updateSubnet(${s.id},'name',this.value)">
        </div>
        <div class="field">
          <label>Purpose</label>
          <select onchange="updateSubnet(${s.id},'purpose',this.value)">
            ${['mgmt','user-lan','server','dmz','storage','voice','iot','general'].map(p =>
              `<option value="${p}"${s.purpose===p?' selected':''}>${p}</option>`
            ).join('')}
          </select>
        </div>
        <div class="field">
          <label>CIDR Size</label>
          <input type="number" value="${htmlEsc(s.cidr)}" min="8" max="30" style="width:80px"
            oninput="updateSubnet(${s.id},'cidr',+this.value)">
        </div>
      </div>
      <div class="toggle-field">
        <label class="toggle">
          <input type="checkbox" ${s.dhcp?'checked':''}
            onchange="updateSubnet(${s.id},'dhcp',this.checked);toggleDhcpRange(${s.id},this.checked)">
          <div class="toggle-track"></div><div class="toggle-thumb"></div>
        </label>
        <label>Enable DHCP for this subnet</label>
      </div>
      <div class="dhcp-range-fields${s.dhcp?' visible':''}" id="dhcp-range-${s.id}">
        <div class="field-row" style="margin-top:6px">
          <div class="field">
            <label>DHCP Start offset<span class="hint">host # from base</span></label>
            <input type="number" value="${s.dhcp_start}" min="1" max="254"
              oninput="updateSubnet(${s.id},'dhcp_start',+this.value)">
          </div>
          <div class="field">
            <label>DHCP End offset<span class="hint">host # from base</span></label>
            <input type="number" value="${s.dhcp_end}" min="1" max="254"
              oninput="updateSubnet(${s.id},'dhcp_end',+this.value)">
          </div>
        </div>
      </div>`;
    container.appendChild(div);
  });
}

function updateSubnet(id, key, val) {
  const s = subnets.find(s => s.id === id);
  if (s) { s[key] = val; builderUpdate(); }
  if (key === 'name') renderHosts();
}

function toggleDhcpRange(id, visible) {
  const el = document.getElementById('dhcp-range-' + id);
  if (el) el.classList.toggle('visible', visible);
}

// ── Builder hosts ────────────────────────────────────────────────────────────

function addHost(data) {
  const id = nextId++;
  hosts.push({
    id,
    hostname: (data && data.hostname) || '',
    subnet:   (data && data.subnet)   || '',
    comment:  (data && data.comment)  || '',
  });
  renderHosts();
  builderUpdate();
}

function removeHost(id) {
  hosts = hosts.filter(h => h.id !== id);
  renderHosts();
  builderUpdate();
}

function renderHosts() {
  const container = document.getElementById('hosts-list');
  container.innerHTML = '';
  hosts.forEach((h, i) => {
    const subnetOpts = subnets.map(s =>
      `<option value="${htmlEsc(s.name)}"${h.subnet===s.name?' selected':''}>${htmlEsc(s.name||'(unnamed subnet '+(subnets.indexOf(s)+1)+')')}</option>`
    ).join('');
    const div = document.createElement('div');
    div.className = 'list-item';
    div.innerHTML = `
      <div class="list-item-header">
        <span class="list-item-title">Host ${i+1}</span>
        <button class="btn-remove" onclick="removeHost(${h.id})">Remove</button>
      </div>
      <div class="field-row thirds">
        <div class="field">
          <label>Hostname</label>
          <input type="text" value="${htmlEsc(h.hostname)}" placeholder="gw01"
            oninput="updateHost(${h.id},'hostname',this.value)">
        </div>
        <div class="field">
          <label>Subnet</label>
          <select onchange="updateHost(${h.id},'subnet',this.value)">
            <option value="">— auto (first) —</option>
            ${subnetOpts}
          </select>
        </div>
        <div class="field">
          <label>Comment</label>
          <input type="text" value="${htmlEsc(h.comment)}" placeholder="optional"
            oninput="updateHost(${h.id},'comment',this.value)">
        </div>
      </div>`;
    container.appendChild(div);
  });
}

function updateHost(id, key, val) {
  const h = hosts.find(h => h.id === id);
  if (h) { h[key] = val; builderUpdate(); }
}

// ── Builder tags ─────────────────────────────────────────────────────────────

function addTag(data) {
  const id = nextId++;
  tags.push({ id, key: (data && data.key)||'', value: (data && data.value)||'' });
  renderTags();
  builderUpdate();
}

function removeTag(id) {
  tags = tags.filter(t => t.id !== id);
  renderTags();
  builderUpdate();
}

function renderTags() {
  const container = document.getElementById('tags-list');
  container.innerHTML = '';
  tags.forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'list-item';
    div.innerHTML = `
      <div class="list-item-header">
        <span class="list-item-title">Tag ${i+1}</span>
        <button class="btn-remove" onclick="removeTag(${t.id})">Remove</button>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Key</label>
          <input type="text" value="${htmlEsc(t.key)}" placeholder="Owner"
            oninput="updateTag(${t.id},'key',this.value)">
        </div>
        <div class="field">
          <label>Value</label>
          <input type="text" value="${htmlEsc(t.value)}" placeholder="network-team"
            oninput="updateTag(${t.id},'value',this.value)">
        </div>
      </div>`;
    container.appendChild(div);
  });
}

function updateTag(id, key, val) {
  const t = tags.find(t => t.id === id);
  if (t) { t[key] = val; builderUpdate(); }
}

// ── Build YAML ───────────────────────────────────────────────────────────────

function buildYAML() {
  const siteName  = v('site-name').trim();
  const siteEnv   = v('site-environment');
  const siteReg   = v('site-region');
  const siteLoc   = v('site-location').trim();
  const ipSpace   = v('net-ip-space').trim();
  const subSize   = v('net-subnet-size').trim();
  const dnsParent = v('dns-parent').trim();
  const dnsView   = v('dns-view').trim();
  const createZone = vb('dns-create-zone');
  const revZone    = vb('dns-reverse-zone');

  let out = '';

  // ── site: ──
  out += 'site:\n';
  out += '  name:        ' + esc(siteName || 'my-site') + '\n';
  if (siteReg) out += '  region:      ' + siteReg + '\n';
  if (siteEnv) out += '  environment: ' + siteEnv + '\n';
  if (siteLoc) out += '  location:    ' + esc(siteLoc) + '\n';

  // ── network: ──
  out += '\nnetwork:\n';
  if (ipSpace)  out += '  ip_space:    ' + esc(ipSpace) + '\n';
  if (subSize)  out += '  subnet_size: ' + subSize + '\n';
  if (subnets.length) {
    out += '  subnets:\n';
    subnets.forEach(s => {
      out += '    - name:    ' + esc(s.name || (siteName ? siteName + '-' + s.purpose : s.purpose)) + '\n';
      out += '      purpose: ' + s.purpose + '\n';
      out += '      cidr:    ' + s.cidr + '\n';
      out += '      dhcp:    ' + (s.dhcp ? 'true' : 'false') + '\n';
      if (s.dhcp) {
        out += '      dhcp_start: ' + s.dhcp_start + '\n';
        out += '      dhcp_end:   ' + s.dhcp_end + '\n';
      }
    });
  }

  // ── dns: ──
  out += '\ndns:\n';
  if (dnsParent) out += '  parent:      ' + esc(dnsParent) + '\n';
  if (dnsView)   out += '  view:        ' + esc(dnsView) + '\n';
  out += '  create_zone:         ' + createZone + '\n';
  out += '  create_reverse_zone: ' + revZone + '\n';

  // ── hosts: ──
  if (hosts.length) {
    out += '\nhosts:\n';
    hosts.forEach(h => {
      out += '  - hostname: ' + esc(h.hostname || 'gw01') + '\n';
      if (h.subnet) out += '    subnet:   ' + esc(h.subnet) + '\n';
      if (h.comment) out += '    comment:  ' + esc(h.comment) + '\n';
    });
  }

  // ── tags: ──
  const validTags = tags.filter(t => t.key);
  if (validTags.length) {
    out += '\ntags:\n';
    validTags.forEach(t => {
      out += '  ' + esc(t.key) + ': ' + esc(t.value) + '\n';
    });
  }

  return out;
}

// ── YAML syntax highlighter ──────────────────────────────────────────────────

function highlightValue(v) {
  if (v === 'true' || v === 'false' || v === 'null')
    return '<span class="yaml-bool">' + htmlEsc(v) + '</span>';
  if (/^-?\d+(\.\d+)?$/.test(v))
    return '<span class="yaml-number">' + htmlEsc(v) + '</span>';
  return '<span class="yaml-string">' + htmlEsc(v) + '</span>';
}

function highlight(text) {
  return text.split('\n').map(line => {
    if (/^\s*#/.test(line))
      return '<span class="yaml-comment">' + htmlEsc(line) + '</span>';
    if (/^\s*-\s/.test(line) && !/:/.test(line))
      return '<span class="yaml-dash">' + htmlEsc(line) + '</span>';
    const kv = line.match(/^(\s*-?\s*)([^:#\s][^:]*?)(\s*:\s*)(.*)$/);
    if (kv) {
      const [, indent, key, sep, val] = kv;
      return htmlEsc(indent) +
             '<span class="yaml-key">' + htmlEsc(key) + '</span>' +
             htmlEsc(sep) +
             (val ? highlightValue(val) : '');
    }
    const listItem = line.match(/^(\s*-\s+)([^:#\s][^:]*?)(\s*:\s*)(.*)$/);
    if (listItem) {
      const [, dash, key, sep, val] = listItem;
      return '<span class="yaml-dash">' + htmlEsc(dash) + '</span>' +
             '<span class="yaml-key">' + htmlEsc(key) + '</span>' +
             htmlEsc(sep) +
             (val ? highlightValue(val) : '');
    }
    return htmlEsc(line);
  }).join('\n');
}

// ── Builder live update ───────────────────────────────────────────────────────

function builderUpdate() {
  const yaml = buildYAML();

  // YAML preview panel
  document.getElementById('yaml-output').innerHTML = highlight(yaml);

  // Keep raw editor in sync if it was generated from builder
  const raw = document.getElementById('raw-editor');
  if (raw.dataset.fromBuilder !== 'false') {
    raw.value = yaml;
    raw.dataset.fromBuilder = 'true';
  }

  // CLI hints
  const siteName = v('site-name').trim() || 'my-site';
  const tmplFile = currentTemplate || '<template.yaml>';
  document.getElementById('cli-provision').textContent =
    'provision_site.py -t ' + tmplFile + ' -v';
  document.getElementById('cli-decommission').textContent =
    'decommission_site.py -t ' + tmplFile + ' --force -v';
}

// ── Builder download / copy ───────────────────────────────────────────────────

function downloadBuilderYAML() {
  const yaml     = buildYAML();
  const siteName = v('site-name').trim() || 'site-new';
  const filename = currentTemplate || (siteName + '.yaml');
  const blob = new Blob([yaml], { type: 'text/yaml' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function copyBuilderYAML() {
  navigator.clipboard.writeText(buildYAML())
    .then(() => {
      const btn = document.getElementById('copy-yaml-btn');
      btn.textContent = 'Copied!';
      btn.classList.add('active');
      setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('active'); }, 1600);
    })
    .catch(() => toast('Copy failed'));
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2400);
}
