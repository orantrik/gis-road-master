/* ═══════════════════════════════════════════════════════════════
   GIS Road Master — Web UI JavaScript
═══════════════════════════════════════════════════════════════ */

// ── State ────────────────────────────────────────────────────────
const state = {
  fileLoaded: false,
  processed:  false,
  busy:       false,
  layers:     { polygons: true, centerlines: true },
};

// ── API helpers ──────────────────────────────────────────────────
async function api(endpoint, data = {}) {
  const res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return res.json();
}

// ── Status helpers ───────────────────────────────────────────────
function setStatus(text, mode = 'idle') {
  document.getElementById('status-text').textContent = text;
  document.getElementById('sb-text').textContent = text;
  const dot  = document.getElementById('status-dot');
  const ind  = document.getElementById('sb-indicator');
  [dot, ind].forEach(el => {
    el.className = el.className.replace(/busy|success|error/g, '').trim();
    if (mode !== 'idle') el.classList.add(mode);
  });
}

function setBusy(label = 'Processing…') {
  state.busy = true;
  setStatus(label, 'busy');
  document.getElementById('spinner-label').textContent = label;
  document.getElementById('map-spinner').style.display = 'flex';
  document.querySelectorAll('.btn').forEach(b => b.disabled = true);
}

function clearBusy() {
  state.busy = false;
  document.getElementById('map-spinner').style.display = 'none';
  updateButtonStates();
}

function updateButtonStates() {
  const hasFile  = state.fileLoaded;
  const hasLines = state.processed;
  document.getElementById('btn-process').disabled    = !hasFile;
  document.getElementById('btn-export-shp').disabled = !hasLines;
  document.getElementById('btn-export-fbx').disabled = !hasLines;
}

// ── Toast notifications ──────────────────────────────────────────
function toast(msg, type = 'info', duration = 4000) {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${msg}</span>`;
  c.appendChild(t);
  setTimeout(() => {
    t.classList.add('toast-out');
    t.addEventListener('animationend', () => t.remove());
  }, duration);
}

// ── Collapsible panels ───────────────────────────────────────────
document.querySelectorAll('.panel-header.collapsible').forEach(header => {
  header.addEventListener('click', () => {
    const bodyId = header.dataset.target;
    const body   = document.getElementById(bodyId);
    const isHid  = body.classList.contains('hidden');
    body.classList.toggle('hidden', !isHid);
    header.classList.toggle('collapsed', !isHid);
  });
});

// ── Slider sync ──────────────────────────────────────────────────
function syncSlider(input, valId, decimals = 0) {
  const val = parseFloat(input.value);
  document.getElementById(valId).textContent =
    decimals > 0 ? val.toFixed(decimals) : val.toString();
  updateSliderFill(input);
}

function updateSliderFill(input) {
  const pct = ((input.value - input.min) / (input.max - input.min)) * 100;
  input.style.background =
    `linear-gradient(to right, var(--accent) ${pct}%, var(--bg-input) ${pct}%)`;
}

document.querySelectorAll('input[type="range"]').forEach(s => updateSliderFill(s));

// ── Merge toggle ─────────────────────────────────────────────────
document.getElementById('fbx-merge').addEventListener('change', function () {
  document.getElementById('maxdist-field').style.display = this.checked ? 'block' : 'none';
});

// ── Checklist helpers ─────────────────────────────────────────────
function buildChecklist(containerId, items) {
  const el = document.getElementById(containerId);
  el.innerHTML = '';
  items.forEach(item => {
    const label = document.createElement('label');
    label.className = 'check-item';
    label.innerHTML = `
      <input type="checkbox" value="${item}" checked />
      <span class="check-box"></span>
      <span class="check-label" title="${item}">${item}</span>
    `;
    el.appendChild(label);
  });
}

function selectAll(containerId) {
  document.querySelectorAll(`#${containerId} input[type="checkbox"]`)
    .forEach(cb => cb.checked = true);
}

function selectNone(containerId) {
  document.querySelectorAll(`#${containerId} input[type="checkbox"]`)
    .forEach(cb => cb.checked = false);
}

function getChecked(containerId) {
  return [...document.querySelectorAll(`#${containerId} input:checked`)]
    .map(cb => cb.value);
}

// ── Layer toggles ─────────────────────────────────────────────────
function toggleLayer(btn, layer) {
  state.layers[layer] = !state.layers[layer];
  btn.classList.toggle('toggle-active', state.layers[layer]);
  refreshMap();
}

// ── Map rendering ─────────────────────────────────────────────────
async function refreshMap() {
  if (!state.fileLoaded) return;
  try {
    const rect = document.getElementById('map-container').getBoundingClientRect();
    const r = await api('/api/map_image', {
      width:       Math.floor(rect.width)  || 800,
      height:      Math.floor(rect.height) || 600,
      show_polys:  state.layers.polygons,
      show_lines:  state.layers.centerlines,
    });
    if (r.image) {
      const img = document.getElementById('map-img');
      img.src = r.image;
      img.style.display = 'block';
      document.getElementById('map-placeholder').style.display = 'none';
      document.getElementById('map-toolbar').style.display = 'flex';
    }
  } catch (e) {
    console.error('Map refresh failed', e);
  }
}

// ── Method report ─────────────────────────────────────────────────
const METHOD_COLORS = {
  straight_skeleton: 'skeleton',
  voronoi_density:   'voronoi',
  hatching:          'hatching',
  edt_ridge:         'edt_ridge',
};

function renderMethodReport(counts) {
  const reportEl = document.getElementById('method-report');
  const badgesEl = document.getElementById('method-badges');
  const total    = Object.values(counts).reduce((a, b) => a + b, 0);

  if (!total) { reportEl.style.display = 'none'; badgesEl.innerHTML = ''; return; }

  reportEl.style.display  = 'flex';
  reportEl.innerHTML      = '';
  badgesEl.innerHTML      = '';

  Object.entries(counts).sort((a, b) => b[1] - a[1]).forEach(([method, n]) => {
    const cls  = METHOD_COLORS[method] || 'skeleton';
    const pct  = Math.round((n / total) * 100);
    const name = method.replace('_', ' ');

    // Bar row in sidebar
    reportEl.innerHTML += `
      <div class="method-bar-row">
        <span class="method-name">${name}</span>
        <div class="method-bar-track">
          <div class="method-bar-fill bar-${cls}" style="width:${pct}%"></div>
        </div>
        <span class="method-count">${n}</span>
      </div>`;

    // Badge in status bar
    badgesEl.innerHTML += `<span class="method-badge badge-${cls}">${n}× ${name}</span>`;
  });
}

// ── Load file ─────────────────────────────────────────────────────
async function loadFile() {
  setBusy('Loading file…');
  try {
    const r = await api('/api/load_file');
    if (!r.ok) { toast(r.msg || 'Failed to load file', 'error'); setStatus('Load failed', 'error'); return; }

    state.fileLoaded = true;

    // Update file info card
    document.getElementById('file-info').style.display  = 'flex';
    document.getElementById('fi-name').textContent       = r.name;
    document.getElementById('fi-name').title             = r.path;
    document.getElementById('fi-rows').textContent       = r.rows.toLocaleString();
    document.getElementById('fi-crs').textContent        = r.crs;

    // Build filter checklists
    if (r.plans.length) {
      buildChecklist('plans-list', r.plans);
      document.getElementById('panel-filters').style.display = 'block';
    }
    if (r.road_types.length) {
      buildChecklist('types-list', r.road_types);
      document.getElementById('panel-filters').style.display = 'block';
    }

    document.getElementById('panel-algo').style.display = 'block';
    setStatus(`Loaded ${r.rows.toLocaleString()} features`, 'success');
    toast(`Loaded: ${r.name}`, 'success');
    await refreshMap();
  } catch (e) {
    toast(`Error: ${e.message}`, 'error');
    setStatus('Error', 'error');
  } finally {
    clearBusy();
  }
}

document.getElementById('btn-load').addEventListener('click',  loadFile);
document.getElementById('btn-load2').addEventListener('click', loadFile);

// ── Process centerlines ──────────────────────────────────────────
document.getElementById('btn-process').addEventListener('click', async () => {
  setBusy('Extracting centerlines…');
  try {
    const r = await api('/api/process', {
      smooth:  parseFloat(document.getElementById('s-smooth').value),
      minlen:  parseFloat(document.getElementById('s-minlen').value),
      prune:   parseFloat(document.getElementById('s-prune').value),
      cutback: parseFloat(document.getElementById('s-cutback').value),
      algo:    document.getElementById('algo-select').value,
      plans:   getChecked('plans-list'),
      types:   getChecked('types-list'),
    });

    if (!r.ok) { toast(r.msg, 'error'); setStatus('Processing failed', 'error'); return; }

    state.processed = true;

    // Update segment counter
    const counter = document.getElementById('segment-counter');
    document.getElementById('segment-count').textContent = r.count;
    counter.style.display = 'flex';

    setStatus(`${r.count} segments extracted`, 'success');
    renderMethodReport(r.method_report || {});

    if (r.image) {
      document.getElementById('map-img').src = r.image;
      document.getElementById('map-img').style.display = 'block';
      document.getElementById('map-placeholder').style.display = 'none';
      document.getElementById('map-toolbar').style.display = 'flex';
    }
    toast(`Extracted ${r.count} centerline segments`, 'success');
  } catch (e) {
    toast(`Process error: ${e.message}`, 'error');
    setStatus('Error', 'error');
  } finally {
    clearBusy();
  }
});

// ── Export Shapefile ─────────────────────────────────────────────
document.getElementById('btn-export-shp').addEventListener('click', async () => {
  setBusy('Saving…');
  try {
    const r = await api('/api/export_shp');
    if (!r.ok) { toast(r.msg, 'error'); return; }
    if (r.cancelled) { return; }
    toast(`Saved to ${r.path}`, 'success');
    setStatus('Export complete', 'success');
  } catch (e) {
    toast(`Export error: ${e.message}`, 'error');
  } finally {
    clearBusy();
  }
});

// ── Export FBX ────────────────────────────────────────────────────
document.getElementById('btn-export-fbx').addEventListener('click', async () => {
  setBusy('Exporting FBX…');
  try {
    const r = await api('/api/export_fbx', {
      bp_path:  document.getElementById('bp-path').value.trim(),
      scale:    parseFloat(document.getElementById('fbx-scale').value),
      merge:    document.getElementById('fbx-merge').checked,
      maxdist:  parseFloat(document.getElementById('fbx-maxdist').value),
    });
    if (!r.ok) { toast(r.msg, 'error'); return; }
    if (r.cancelled) { return; }
    toast(`FBX exported — ${r.count} curves. Run the *_unreal_splines.py companion script in Unreal.`, 'success', 7000);
    setStatus('FBX export complete', 'success');
  } catch (e) {
    toast(`Export error: ${e.message}`, 'error');
  } finally {
    clearBusy();
  }
});

// ── Window resize → refresh map ──────────────────────────────────
let _resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(refreshMap, 200);
});
