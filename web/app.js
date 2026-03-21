/* ═══════════════════════════════════════════════════════════════
   GIS Road Master — Web UI JavaScript  v2.0
═══════════════════════════════════════════════════════════════ */

// ── Global State ─────────────────────────────────────────────────
const state = {
  fileLoaded:    false,
  processed:     false,
  busy:          false,
  layers:        { polygons: true, centerlines: true },
  mapExtent:     null,   // [xmin, ymin, xmax, ymax]
  hasBox:        false,
  shapeOverride: false,
  hintCount:     0,
};

// ── Coordinate Conversion ─────────────────────────────────────────
function pxToGIS(px, py, canvasW, canvasH) {
  if (!state.mapExtent) return [0, 0];
  const [xmin, ymin, xmax, ymax] = state.mapExtent;
  return [
    xmin + (px / canvasW) * (xmax - xmin),
    ymax - (py / canvasH) * (ymax - ymin),
  ];
}

function gisToPx(gx, gy, canvasW, canvasH, extent) {
  const [xmin, ymin, xmax, ymax] = extent || state.mapExtent;
  return [
    (gx - xmin) / (xmax - xmin) * canvasW,
    (ymax - gy) / (ymax - ymin) * canvasH,
  ];
}

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
  document.querySelectorAll('.btn').forEach(b => {
    if (!b.classList.contains('modal-close-btn')) b.disabled = true;
  });
}

function clearBusy() {
  state.busy = false;
  document.getElementById('map-spinner').style.display = 'none';
  updateButtonStates();
}

function updateButtonStates() {
  const hasFile  = state.fileLoaded;
  const hasLines = state.processed;
  const hasBox   = state.hasBox;

  // Workflow buttons
  document.getElementById('btn-process').disabled          = !hasFile;
  document.getElementById('btn-shape-builder').disabled    = !hasFile;
  document.getElementById('btn-hint-tool').disabled        = !hasFile;
  document.getElementById('btn-draw-box').disabled         = !hasLines;
  document.getElementById('btn-precision-editor').disabled = !hasLines || !hasBox;
  document.getElementById('btn-precision-apply').disabled  = !hasLines || !hasBox;
  document.getElementById('btn-pencil-tool').disabled      = !hasFile;
  document.getElementById('btn-export-geojson').disabled   = !hasLines;
  document.getElementById('btn-export-shp').disabled       = !hasLines;
  document.getElementById('btn-export-fbx').disabled       = !hasLines;
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

// ── Auto-Tune toggle ─────────────────────────────────────────────
function onAutoTuneChange() {
  const isAuto = document.getElementById('auto-tune').checked;
  document.getElementById('algo-select-field').style.opacity = isAuto ? '0.4' : '1';
  document.getElementById('algo-select').disabled = isAuto;
}

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
    if (r.extent) {
      state.mapExtent = r.extent;
    }
  } catch (e) {
    console.error('Map refresh failed', e);
  }
}

function setMapImage(imgSrc, extent) {
  const img = document.getElementById('map-img');
  img.src = imgSrc;
  img.style.display = 'block';
  document.getElementById('map-placeholder').style.display = 'none';
  document.getElementById('map-toolbar').style.display = 'flex';
  if (extent) state.mapExtent = extent;
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

    reportEl.innerHTML += `
      <div class="method-bar-row">
        <span class="method-name">${name}</span>
        <div class="method-bar-track">
          <div class="method-bar-fill bar-${cls}" style="width:${pct}%"></div>
        </div>
        <span class="method-count">${n}</span>
      </div>`;

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

    document.getElementById('file-info').style.display  = 'flex';
    document.getElementById('fi-name').textContent       = r.name;
    document.getElementById('fi-name').title             = r.path;
    document.getElementById('fi-rows').textContent       = r.rows.toLocaleString();
    document.getElementById('fi-crs').textContent        = r.crs;

    if (r.plans && r.plans.length) {
      buildChecklist('plans-list', r.plans);
      document.getElementById('panel-filters').style.display = 'block';
    }
    if (r.road_types && r.road_types.length) {
      buildChecklist('types-list', r.road_types);
      document.getElementById('panel-filters').style.display = 'block';
    }

    document.getElementById('panel-algo').style.display     = 'block';
    document.getElementById('panel-workflow').style.display  = 'block';
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
    const rect = document.getElementById('map-container').getBoundingClientRect();
    const r = await api('/api/process', {
      smooth:    parseFloat(document.getElementById('s-smooth').value),
      minlen:    parseFloat(document.getElementById('s-minlen').value),
      prune:     parseFloat(document.getElementById('s-prune').value),
      cutback:   parseFloat(document.getElementById('s-cutback').value),
      straight:  parseFloat(document.getElementById('s-straight').value),
      algo:      document.getElementById('algo-select').value,
      use_auto:  document.getElementById('auto-tune').checked,
      plans:     getChecked('plans-list'),
      types:     getChecked('types-list'),
      width:     Math.floor(rect.width)  || 800,
      height:    Math.floor(rect.height) || 600,
    });

    if (!r.ok) { toast(r.msg, 'error'); setStatus('Processing failed', 'error'); return; }

    state.processed = true;

    const counter = document.getElementById('segment-counter');
    document.getElementById('segment-count').textContent = r.count;
    counter.style.display = 'flex';

    setStatus(`${r.count} segments extracted`, 'success');
    renderMethodReport(r.method_report || {});

    if (r.image) setMapImage(r.image, r.extent);
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

// ── Export GeoJSON ────────────────────────────────────────────────
document.getElementById('btn-export-geojson').addEventListener('click', async () => {
  setBusy('Saving GeoJSON…');
  try {
    const r = await api('/api/export_geojson');
    if (!r.ok) { toast(r.msg, 'error'); return; }
    if (r.cancelled) { return; }
    toast(`Saved GeoJSON to ${r.path}`, 'success');
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
    toast(`FBX exported — ${r.count} curves.`, 'success', 7000);
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

// ═══════════════════════════════════════════════════════════════
// MODAL SYSTEM
// ═══════════════════════════════════════════════════════════════

function openModal(id) {
  document.getElementById(id).style.display = 'flex';
}

function closeModal(id) {
  document.getElementById(id).style.display = 'none';
}

// Keyboard: Escape closes top-most modal
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const modals = ['modal-pencil', 'modal-precision', 'modal-hint-tool', 'modal-shape-builder'];
    for (const id of modals) {
      const el = document.getElementById(id);
      if (el && el.style.display !== 'none') {
        closeModal(id);
        return;
      }
    }
    // Also cancel box drawing
    if (boxDrawing.active) cancelBoxDraw();
  }
});

// ═══════════════════════════════════════════════════════════════
// POINT-IN-POLYGON (for shape builder)
// ═══════════════════════════════════════════════════════════════

function pointInRing(px, py, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1];
    const xj = ring[j][0], yj = ring[j][1];
    if (((yi > py) !== (yj > py)) && (px < (xj - xi) * (py - yi) / (yj - yi) + xi))
      inside = !inside;
  }
  return inside;
}

// ═══════════════════════════════════════════════════════════════
// SHAPE BUILDER
// ═══════════════════════════════════════════════════════════════

const shapeBuild = {
  polys:      [],   // [{idx, rings, sel, excl}]
  extent:     null, // [xmin, ymin, xmax, ymax]
  canvas:     null,
  ctx:        null,
  dragStart:  null,
  dragRect:   null,
  isDragging: false,
};

function drawPolygon(ctx, ring, color, alpha) {
  if (!ring || ring.length < 2) return;
  ctx.beginPath();
  ring.forEach(([x, y], i) => i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y));
  ctx.closePath();
  ctx.globalAlpha = alpha;
  ctx.fillStyle = color;
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.strokeStyle = 'rgba(255,255,255,0.35)';
  ctx.lineWidth = 1;
  ctx.stroke();
}

function shapeGIStoCanvas(gx, gy) {
  const [xmin, ymin, xmax, ymax] = shapeBuild.extent;
  const cw = shapeBuild.canvas.width;
  const ch = shapeBuild.canvas.height;
  return [
    (gx - xmin) / (xmax - xmin) * cw,
    (ymax - gy) / (ymax - ymin) * ch,
  ];
}

function shapeRingToCanvas(ring) {
  return ring.map(([gx, gy]) => shapeGIStoCanvas(gx, gy));
}

function shapeRedraw() {
  const { canvas, ctx, polys, dragRect } = shapeBuild;
  if (!ctx) return;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#060912';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  polys.forEach(p => {
    const color = p.excl ? '#8b1a1a'
                : p.sel  ? '#e67e22'
                :           '#2980b9';
    const alpha = p.excl ? 0.6 : p.sel ? 0.75 : 0.55;
    p.rings.forEach(ring => {
      const canvasRing = shapeRingToCanvas(ring);
      drawPolygon(ctx, canvasRing, color, alpha);
    });
  });

  // Draw drag selection box
  if (dragRect) {
    ctx.save();
    ctx.strokeStyle = '#00d4ff';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(dragRect.x, dragRect.y, dragRect.w, dragRect.h);
    ctx.fillStyle = 'rgba(0,212,255,0.06)';
    ctx.fillRect(dragRect.x, dragRect.y, dragRect.w, dragRect.h);
    ctx.restore();
  }
}

function shapeUpdateStatus() {
  const selCount = shapeBuild.polys.filter(p => p.sel && !p.excl).length;
  const totalCount = shapeBuild.polys.length;
  document.getElementById('shape-builder-status').textContent =
    `${totalCount} polygons, ${selCount} selected`;
}

async function openShapeBuilder() {
  openModal('modal-shape-builder');
  document.getElementById('shape-builder-status').textContent = 'Loading…';
  shapeBuild.canvas = document.getElementById('shape-canvas');
  shapeBuild.ctx    = shapeBuild.canvas.getContext('2d');

  // Size canvas to fill modal
  const wrap = shapeBuild.canvas.parentElement;
  shapeBuild.canvas.width  = wrap.clientWidth;
  shapeBuild.canvas.height = wrap.clientHeight;

  try {
    const r = await api('/api/get_shape_data');
    if (!r.ok) { toast(r.msg, 'error'); return; }
    shapeBuild.polys  = r.polys;
    shapeBuild.extent = r.extent;
    shapeRedraw();
    shapeUpdateStatus();
  } catch (e) {
    toast(`Shape builder error: ${e.message}`, 'error');
  }
}

document.getElementById('btn-shape-builder').addEventListener('click', openShapeBuilder);

// Shape canvas events
(function () {
  const canvas = document.getElementById('shape-canvas');

  canvas.addEventListener('mousedown', (e) => {
    if (e.button === 2) return; // right handled separately
    shapeBuild.dragStart = { x: e.offsetX, y: e.offsetY };
    shapeBuild.isDragging = false;
  });

  canvas.addEventListener('mousemove', (e) => {
    if (!shapeBuild.dragStart) return;
    const dx = e.offsetX - shapeBuild.dragStart.x;
    const dy = e.offsetY - shapeBuild.dragStart.y;
    if (Math.abs(dx) > 4 || Math.abs(dy) > 4) {
      shapeBuild.isDragging = true;
      shapeBuild.dragRect = {
        x: Math.min(shapeBuild.dragStart.x, e.offsetX),
        y: Math.min(shapeBuild.dragStart.y, e.offsetY),
        w: Math.abs(dx),
        h: Math.abs(dy),
      };
      shapeRedraw();
    }
  });

  canvas.addEventListener('mouseup', async (e) => {
    const start = shapeBuild.dragStart;
    shapeBuild.dragStart = null;

    if (shapeBuild.isDragging && shapeBuild.dragRect) {
      // Box-select by centroid
      const dr = shapeBuild.dragRect;
      shapeBuild.dragRect   = null;
      shapeBuild.isDragging = false;
      // Find polygons whose centroid falls in box
      shapeBuild.polys.forEach(p => {
        if (p.excl) return;
        let cx = 0, cy = 0, count = 0;
        p.rings.forEach(ring => {
          ring.forEach(([gx, gy]) => {
            const [px, py] = shapeGIStoCanvas(gx, gy);
            cx += px; cy += py; count++;
          });
        });
        if (count > 0) {
          cx /= count; cy /= count;
          if (cx >= dr.x && cx <= dr.x + dr.w && cy >= dr.y && cy <= dr.y + dr.h) {
            p.sel = !p.sel;
            api('/api/shape_toggle', { idx: p.idx, action: 'sel' });
          }
        }
      });
      shapeRedraw();
      shapeUpdateStatus();
      return;
    }

    shapeBuild.isDragging = false;
    shapeBuild.dragRect   = null;

    if (e.button === 0) {
      // Left click: toggle selection
      const hit = shapeHitTest(e.offsetX, e.offsetY);
      if (hit !== null) {
        const p = shapeBuild.polys.find(x => x.idx === hit);
        if (p && !p.excl) {
          p.sel = !p.sel;
          await api('/api/shape_toggle', { idx: hit, action: 'sel' });
          shapeRedraw();
          shapeUpdateStatus();
        }
      }
    }
  });

  canvas.addEventListener('contextmenu', async (e) => {
    e.preventDefault();
    const hit = shapeHitTest(e.offsetX, e.offsetY);
    if (hit !== null) {
      const p = shapeBuild.polys.find(x => x.idx === hit);
      if (p) {
        p.excl = !p.excl;
        if (p.excl) p.sel = false;
        await api('/api/shape_toggle', { idx: hit, action: 'excl' });
        shapeRedraw();
        shapeUpdateStatus();
      }
    }
  });
})();

function shapeHitTest(mx, my) {
  // Return idx of first polygon containing point
  for (let i = shapeBuild.polys.length - 1; i >= 0; i--) {
    const p = shapeBuild.polys[i];
    for (const ring of p.rings) {
      const cRing = shapeRingToCanvas(ring);
      if (pointInRing(mx, my, cRing)) return p.idx;
    }
  }
  return null;
}

async function shapeSelectAll() {
  shapeBuild.polys.forEach(p => { if (!p.excl) p.sel = true; });
  // Send batch toggle (just refresh from server)
  await api('/api/shape_reset');
  const r = await api('/api/get_shape_data');
  if (r.ok) {
    shapeBuild.polys  = r.polys;
    shapeBuild.extent = r.extent;
    shapeBuild.polys.forEach(p => { p.sel = true; });
    // Sync each selection
    for (const p of shapeBuild.polys) {
      await api('/api/shape_toggle', { idx: p.idx, action: 'sel' });
    }
  }
  shapeRedraw();
  shapeUpdateStatus();
}

async function shapeDeselectAll() {
  shapeBuild.polys.forEach(p => { p.sel = false; });
  const r = await api('/api/shape_reset');
  if (r.ok) {
    const r2 = await api('/api/get_shape_data');
    if (r2.ok) { shapeBuild.polys = r2.polys; shapeBuild.extent = r2.extent; }
  }
  shapeRedraw();
  shapeUpdateStatus();
}

async function shapeMergeSelected() {
  const selected = shapeBuild.polys.filter(p => p.sel && !p.excl).map(p => p.idx);
  if (selected.length < 2) { toast('Select at least 2 polygons to merge', 'info'); return; }
  const r = await api('/api/shape_merge', { indices: selected });
  if (!r.ok) { toast(r.msg, 'error'); return; }
  // Reload
  const r2 = await api('/api/get_shape_data');
  if (r2.ok) { shapeBuild.polys = r2.polys; shapeBuild.extent = r2.extent; }
  shapeRedraw();
  shapeUpdateStatus();
  toast(`Merged into ${r.count} polygons`, 'success');
}

async function shapeUndo() {
  const r = await api('/api/shape_undo');
  if (!r.ok) { toast(r.msg, 'info'); return; }
  const r2 = await api('/api/get_shape_data');
  if (r2.ok) { shapeBuild.polys = r2.polys; shapeBuild.extent = r2.extent; }
  shapeRedraw();
  shapeUpdateStatus();
}

async function shapeReset() {
  const r = await api('/api/shape_reset');
  if (!r.ok) { toast(r.msg, 'error'); return; }
  const r2 = await api('/api/get_shape_data');
  if (r2.ok) { shapeBuild.polys = r2.polys; shapeBuild.extent = r2.extent; }
  shapeRedraw();
  shapeUpdateStatus();
  toast('Shape reset to original', 'info');
}

async function shapeClearOverride() {
  await api('/api/shape_clear');
  state.shapeOverride = false;
  const lbl = document.getElementById('shape-status');
  lbl.textContent = 'no shape override';
  lbl.className = 'status-label';
  toast('Shape override cleared', 'info');
}

async function shapeUsePolygons() {
  const nonExcl = shapeBuild.polys.filter(p => !p.excl).map(p => p.idx);
  if (nonExcl.length === 0) { toast('No polygons to use', 'info'); return; }
  const r = await api('/api/shape_use', { indices: nonExcl });
  if (!r.ok) { toast(r.msg, 'error'); return; }
  state.shapeOverride = true;
  const lbl = document.getElementById('shape-status');
  lbl.textContent = `${r.count} polygons committed`;
  lbl.className = 'status-label active';
  closeModal('modal-shape-builder');
  toast(`Shape builder: ${r.count} polygons committed`, 'success');
}

// Resize shape canvas on window resize
window.addEventListener('resize', () => {
  const modal = document.getElementById('modal-shape-builder');
  if (modal && modal.style.display !== 'none' && shapeBuild.canvas) {
    const wrap = shapeBuild.canvas.parentElement;
    shapeBuild.canvas.width  = wrap.clientWidth;
    shapeBuild.canvas.height = wrap.clientHeight;
    shapeRedraw();
  }
});

// ═══════════════════════════════════════════════════════════════
// ROUTING HINTS TOOL
// ═══════════════════════════════════════════════════════════════

const hintTool = {
  canvas:     null,
  ctx:        null,
  bgImage:    null,
  extent:     null,
  strokes:    [],      // committed strokes: [[gx,gy], ...]
  current:    [],      // current stroke vertices (GIS coords)
  mousePos:   null,
};

function hintGIStoPx(gx, gy) {
  const [xmin, ymin, xmax, ymax] = hintTool.extent || state.mapExtent;
  const cw = hintTool.canvas.width;
  const ch = hintTool.canvas.height;
  return [
    (gx - xmin) / (xmax - xmin) * cw,
    (ymax - gy) / (ymax - ymin) * ch,
  ];
}

function hintPxToGIS(px, py) {
  const [xmin, ymin, xmax, ymax] = hintTool.extent || state.mapExtent;
  const cw = hintTool.canvas.width;
  const ch = hintTool.canvas.height;
  return [
    xmin + (px / cw) * (xmax - xmin),
    ymax - (py / ch) * (ymax - ymin),
  ];
}

function hintRedraw() {
  const { canvas, ctx, bgImage, strokes, current, mousePos } = hintTool;
  if (!ctx) return;
  const cw = canvas.width, ch = canvas.height;

  ctx.clearRect(0, 0, cw, ch);
  ctx.fillStyle = '#06091299';
  ctx.fillRect(0, 0, cw, ch);

  // Draw background map image
  if (bgImage) {
    ctx.globalAlpha = 0.5;
    ctx.drawImage(bgImage, 0, 0, cw, ch);
    ctx.globalAlpha = 1;
  }

  // Draw committed strokes
  ctx.strokeStyle = '#c678dd';
  ctx.lineWidth   = 2;
  ctx.setLineDash([8, 4]);
  strokes.forEach(stroke => {
    if (stroke.length < 2) return;
    ctx.beginPath();
    stroke.forEach(([gx, gy], i) => {
      const [px, py] = hintGIStoPx(gx, gy);
      i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    ctx.stroke();
  });

  // Draw current stroke
  if (current.length > 0) {
    ctx.beginPath();
    current.forEach(([gx, gy], i) => {
      const [px, py] = hintGIStoPx(gx, gy);
      i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    if (mousePos && current.length > 0) {
      ctx.lineTo(mousePos.x, mousePos.y);
    }
    ctx.stroke();

    // Draw vertex dots
    ctx.setLineDash([]);
    ctx.fillStyle = '#c678dd';
    current.forEach(([gx, gy]) => {
      const [px, py] = hintGIStoPx(gx, gy);
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  ctx.setLineDash([]);
}

async function openHintTool() {
  openModal('modal-hint-tool');
  hintTool.canvas = document.getElementById('hint-canvas');
  hintTool.ctx    = hintTool.canvas.getContext('2d');

  const wrap = hintTool.canvas.parentElement;
  hintTool.canvas.width  = wrap.clientWidth;
  hintTool.canvas.height = wrap.clientHeight;

  hintTool.extent = state.mapExtent;

  // Load background map image
  const mapImg = document.getElementById('map-img');
  if (mapImg.src && mapImg.style.display !== 'none') {
    const img = new Image();
    img.onload = () => { hintTool.bgImage = img; hintRedraw(); };
    img.src = mapImg.src;
  }

  hintTool.strokes  = [];
  hintTool.current  = [];
  hintTool.mousePos = null;

  document.getElementById('hint-tool-status').textContent = `${state.hintCount} hints`;
  hintRedraw();
}

document.getElementById('btn-hint-tool').addEventListener('click', openHintTool);

(function () {
  const canvas = document.getElementById('hint-canvas');

  canvas.addEventListener('mousemove', (e) => {
    hintTool.mousePos = { x: e.offsetX, y: e.offsetY };
    hintRedraw();
  });

  canvas.addEventListener('click', (e) => {
    const [gx, gy] = hintPxToGIS(e.offsetX, e.offsetY);
    hintTool.current.push([gx, gy]);
    hintRedraw();
  });

  canvas.addEventListener('dblclick', async (e) => {
    // Remove the duplicate point from the dblclick
    if (hintTool.current.length > 1) hintTool.current.pop();
    await hintCommitStroke();
  });

  canvas.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter') {
      await hintCommitStroke();
    } else if (e.key === 'z' || e.key === 'Z') {
      hintTool.current.pop();
      hintRedraw();
    } else if (e.key === 'Escape') {
      hintTool.current = [];
      hintRedraw();
    }
  });

  canvas.setAttribute('tabindex', '0');
})();

async function hintCommitStroke() {
  if (hintTool.current.length < 2) {
    hintTool.current = [];
    hintRedraw();
    return;
  }
  const stroke = [...hintTool.current];
  hintTool.strokes.push(stroke);
  hintTool.current = [];

  const r = await api('/api/add_hint', { coords: stroke });
  if (r.ok) {
    state.hintCount = r.count;
    document.getElementById('hint-tool-status').textContent = `${r.count} hints`;
    const lbl = document.getElementById('hint-status');
    lbl.textContent = `${r.count} hint strokes`;
    lbl.className = r.count > 0 ? 'status-label hint-set' : 'status-label';
  }
  hintRedraw();
}

async function hintClearLast() {
  hintTool.strokes.pop();
  const r = await api('/api/clear_last_hint');
  if (r.ok) {
    state.hintCount = r.count;
    document.getElementById('hint-tool-status').textContent = `${r.count} hints`;
    const lbl = document.getElementById('hint-status');
    lbl.textContent = `${r.count} hint strokes`;
    lbl.className = r.count > 0 ? 'status-label hint-set' : 'status-label';
  }
  hintRedraw();
}

async function hintClearAll() {
  hintTool.strokes = [];
  hintTool.current = [];
  await api('/api/clear_hints');
  state.hintCount = 0;
  document.getElementById('hint-tool-status').textContent = '0 hints';
  const lbl = document.getElementById('hint-status');
  lbl.textContent = '0 hint strokes';
  lbl.className = 'status-label';
  hintRedraw();
}

function hintSaveClose() {
  closeModal('modal-hint-tool');
  toast(`${state.hintCount} routing hints saved`, 'success');
}

async function onHintTolChange(input) {
  const tol = parseFloat(input.value);
  document.getElementById('val-hint-tol').textContent = tol.toFixed(6);
  updateSliderFill(input);
  await api('/api/set_hint_tol', { tol });
}

// ═══════════════════════════════════════════════════════════════
// DRAW EDIT BOX (overlay on main map)
// ═══════════════════════════════════════════════════════════════

const boxDrawing = {
  active:    false,
  startPx:   null,
  endPx:     null,
  canvas:    null,
  ctx:       null,
};

function startBoxDraw() {
  const overlay = document.getElementById('draw-overlay');
  const container = document.getElementById('map-container');
  const rect = container.getBoundingClientRect();

  overlay.width  = rect.width;
  overlay.height = rect.height;
  overlay.style.display = 'block';
  overlay.classList.add('box-mode');

  boxDrawing.active  = true;
  boxDrawing.canvas  = overlay;
  boxDrawing.ctx     = overlay.getContext('2d');
  boxDrawing.startPx = null;
  boxDrawing.endPx   = null;

  toast('Draw a rectangle on the map to set the edit box', 'info', 3000);
}

function cancelBoxDraw() {
  boxDrawing.active = false;
  const overlay = document.getElementById('draw-overlay');
  overlay.style.display = 'none';
  overlay.classList.remove('box-mode');
}

function boxRedraw() {
  const { ctx, canvas, startPx, endPx } = boxDrawing;
  if (!ctx || !startPx || !endPx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const x = Math.min(startPx.x, endPx.x);
  const y = Math.min(startPx.y, endPx.y);
  const w = Math.abs(endPx.x - startPx.x);
  const h = Math.abs(endPx.y - startPx.y);
  ctx.save();
  ctx.strokeStyle = '#e74c3c';
  ctx.lineWidth = 2;
  ctx.setLineDash([8, 4]);
  ctx.strokeRect(x, y, w, h);
  ctx.fillStyle = 'rgba(231, 76, 60, 0.08)';
  ctx.fillRect(x, y, w, h);
  ctx.restore();
}

document.getElementById('btn-draw-box').addEventListener('click', startBoxDraw);

(function () {
  const overlay = document.getElementById('draw-overlay');

  overlay.addEventListener('mousedown', (e) => {
    if (!boxDrawing.active) return;
    boxDrawing.startPx = { x: e.offsetX, y: e.offsetY };
    boxDrawing.endPx   = { x: e.offsetX, y: e.offsetY };
  });

  overlay.addEventListener('mousemove', (e) => {
    if (!boxDrawing.active || !boxDrawing.startPx) return;
    boxDrawing.endPx = { x: e.offsetX, y: e.offsetY };
    boxDrawing.ctx.clearRect(0, 0, overlay.width, overlay.height);
    boxRedraw();
  });

  overlay.addEventListener('mouseup', async (e) => {
    if (!boxDrawing.active || !boxDrawing.startPx) return;
    boxDrawing.endPx = { x: e.offsetX, y: e.offsetY };

    const cw = overlay.width;
    const ch = overlay.height;

    const [gx1, gy1] = pxToGIS(boxDrawing.startPx.x, boxDrawing.startPx.y, cw, ch);
    const [gx2, gy2] = pxToGIS(boxDrawing.endPx.x,   boxDrawing.endPx.y,   cw, ch);

    const xmin = Math.min(gx1, gx2);
    const xmax = Math.max(gx1, gx2);
    const ymin = Math.min(gy1, gy2);
    const ymax = Math.max(gy1, gy2);

    if (xmax - xmin < 1e-8 || ymax - ymin < 1e-8) {
      cancelBoxDraw();
      return;
    }

    const r = await api('/api/set_box', { xmin, ymin, xmax, ymax });
    if (!r.ok) { toast(r.msg, 'error'); cancelBoxDraw(); return; }

    state.hasBox = true;
    const lbl = document.getElementById('box-status');
    lbl.textContent = 'box set';
    lbl.className = 'status-label box-set';

    // Keep the box drawn but deactivate drawing mode
    boxDrawing.active = false;
    overlay.classList.remove('box-mode');
    // Leave overlay visible showing the box
    boxRedraw();

    updateButtonStates();
    toast('Edit box set — open Precision Editor to refine', 'success');
  });
})();

// ═══════════════════════════════════════════════════════════════
// PRECISION EDITOR
// ═══════════════════════════════════════════════════════════════

const precisionEd = {
  canvas:   null,
  ctx:      null,
  bgImage:  null,
  extent:   null,
  lines:    [],   // [{coords: [[gx,gy],...], erased: false}]
};

function precGIStoPx(gx, gy) {
  const [xmin, ymin, xmax, ymax] = precisionEd.extent;
  const cw = precisionEd.canvas.width;
  const ch = precisionEd.canvas.height;
  return [
    (gx - xmin) / (xmax - xmin) * cw,
    (ymax - gy) / (ymax - ymin) * ch,
  ];
}

function precRedraw() {
  const { canvas, ctx, bgImage, lines } = precisionEd;
  if (!ctx) return;
  const cw = canvas.width, ch = canvas.height;

  ctx.clearRect(0, 0, cw, ch);
  ctx.fillStyle = '#06091299';
  ctx.fillRect(0, 0, cw, ch);

  if (bgImage) {
    ctx.globalAlpha = 0.45;
    ctx.drawImage(bgImage, 0, 0, cw, ch);
    ctx.globalAlpha = 1;
  }

  // Draw lines
  lines.forEach((ln, idx) => {
    if (ln.erased) return;
    ctx.beginPath();
    ln.coords.forEach(([gx, gy], i) => {
      const [px, py] = precGIStoPx(gx, gy);
      i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    ctx.strokeStyle = '#2ecc71';
    ctx.lineWidth   = 2.5;
    ctx.stroke();
  });

  document.getElementById('precision-status').textContent =
    `${lines.filter(l => !l.erased).length} lines`;
}

function precLineHitTest(mx, my, threshold = 8) {
  for (let i = precisionEd.lines.length - 1; i >= 0; i--) {
    const ln = precisionEd.lines[i];
    if (ln.erased) continue;
    const coords = ln.coords;
    for (let j = 0; j < coords.length - 1; j++) {
      const [ax, ay] = precGIStoPx(coords[j][0], coords[j][1]);
      const [bx, by] = precGIStoPx(coords[j+1][0], coords[j+1][1]);
      const dist = pointToSegmentDist(mx, my, ax, ay, bx, by);
      if (dist < threshold) return i;
    }
  }
  return -1;
}

function pointToSegmentDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const len2 = dx*dx + dy*dy;
  if (len2 === 0) return Math.hypot(px - ax, py - ay);
  const t = Math.max(0, Math.min(1, ((px-ax)*dx + (py-ay)*dy) / len2));
  return Math.hypot(px - (ax + t*dx), py - (ay + t*dy));
}

async function openPrecisionEditor() {
  openModal('modal-precision');
  precisionEd.canvas = document.getElementById('precision-canvas');
  precisionEd.ctx    = precisionEd.canvas.getContext('2d');

  const wrap = precisionEd.canvas.parentElement;
  precisionEd.canvas.width  = wrap.clientWidth;
  precisionEd.canvas.height = wrap.clientHeight;

  document.getElementById('precision-status').textContent = 'Loading…';

  try {
    const rect = document.getElementById('map-container').getBoundingClientRect();
    const r = await api('/api/precision_init', {
      width:  Math.floor(rect.width)  || 800,
      height: Math.floor(rect.height) || 600,
    });
    if (!r.ok) { toast(r.msg, 'error'); closeModal('modal-precision'); return; }

    precisionEd.extent = r.extent;
    precisionEd.lines  = r.lines.map(coords => ({ coords, erased: false }));

    // Load background
    if (r.image) {
      const img = new Image();
      img.onload = () => { precisionEd.bgImage = img; precRedraw(); };
      img.src = r.image;
    } else {
      precRedraw();
    }
    toast(`${r.count} lines loaded in editor`, 'info');
  } catch (e) {
    toast(`Precision editor error: ${e.message}`, 'error');
    closeModal('modal-precision');
  }
}

document.getElementById('btn-precision-editor').addEventListener('click', openPrecisionEditor);

(function () {
  const canvas = document.getElementById('precision-canvas');

  canvas.addEventListener('click', async (e) => {
    const idx = precLineHitTest(e.offsetX, e.offsetY);
    if (idx >= 0) {
      precisionEd.lines[idx].erased = true;
      await api('/api/precision_erase', { idx });
      precRedraw();
    }
  });

  canvas.addEventListener('keydown', async (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
      await precisionUndo();
    }
  });

  canvas.setAttribute('tabindex', '0');
})();

async function precisionUndo() {
  const r = await api('/api/precision_undo');
  if (!r.ok) { toast(r.msg, 'info'); return; }
  // Find the last erased line and restore it
  for (let i = precisionEd.lines.length - 1; i >= 0; i--) {
    if (precisionEd.lines[i].erased) {
      precisionEd.lines[i].erased = false;
      break;
    }
  }
  precRedraw();
}

async function precisionApply() {
  setBusy('Applying precision edits…');
  try {
    const rect = document.getElementById('map-container').getBoundingClientRect();
    const r = await api('/api/precision_apply', {
      width:  Math.floor(rect.width)  || 800,
      height: Math.floor(rect.height) || 600,
    });
    if (!r.ok) { toast(r.msg, 'error'); return; }
    if (r.image) setMapImage(r.image, r.extent);
    state.hasBox = false;
    document.getElementById('box-status').textContent = 'no box set';
    document.getElementById('box-status').className   = 'status-label';
    // Hide overlay
    document.getElementById('draw-overlay').style.display = 'none';
    const ctx = document.getElementById('draw-overlay').getContext('2d');
    ctx.clearRect(0, 0, document.getElementById('draw-overlay').width,
                  document.getElementById('draw-overlay').height);

    closeModal('modal-precision');
    updateButtonStates();
    toast(`Precision edits applied — ${r.count} total lines`, 'success');
  } catch (e) {
    toast(`Apply error: ${e.message}`, 'error');
  } finally {
    clearBusy();
  }
}

document.getElementById('btn-precision-apply').addEventListener('click', precisionApply);

// ═══════════════════════════════════════════════════════════════
// PENCIL TOOL
// ═══════════════════════════════════════════════════════════════

const pencilTool = {
  canvas:    null,
  ctx:       null,
  bgImage:   null,
  extent:    null,
  lines:     [],    // committed lines: [[[gx,gy],...], ...]
  current:   [],    // current stroke vertices (GIS coords)
  mousePos:  null,
};

function pencilGIStoPx(gx, gy) {
  const [xmin, ymin, xmax, ymax] = pencilTool.extent || state.mapExtent;
  const cw = pencilTool.canvas.width;
  const ch = pencilTool.canvas.height;
  return [
    (gx - xmin) / (xmax - xmin) * cw,
    (ymax - gy) / (ymax - ymin) * ch,
  ];
}

function pencilPxToGIS(px, py) {
  const [xmin, ymin, xmax, ymax] = pencilTool.extent || state.mapExtent;
  const cw = pencilTool.canvas.width;
  const ch = pencilTool.canvas.height;
  return [
    xmin + (px / cw) * (xmax - xmin),
    ymax - (py / ch) * (ymax - ymin),
  ];
}

function pencilRedraw() {
  const { canvas, ctx, bgImage, lines, current, mousePos } = pencilTool;
  if (!ctx) return;
  const cw = canvas.width, ch = canvas.height;

  ctx.clearRect(0, 0, cw, ch);
  ctx.fillStyle = '#060912';
  ctx.fillRect(0, 0, cw, ch);

  if (bgImage) {
    ctx.globalAlpha = 0.45;
    ctx.drawImage(bgImage, 0, 0, cw, ch);
    ctx.globalAlpha = 1;
  }

  // Committed lines
  ctx.strokeStyle = '#00d4ff';
  ctx.lineWidth   = 2;
  ctx.setLineDash([]);
  lines.forEach(ln => {
    if (ln.length < 2) return;
    ctx.beginPath();
    ln.forEach(([gx, gy], i) => {
      const [px, py] = pencilGIStoPx(gx, gy);
      i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    ctx.stroke();
  });

  // Current stroke
  if (current.length > 0) {
    ctx.strokeStyle = 'rgba(0,212,255,0.65)';
    ctx.setLineDash([6, 3]);
    ctx.beginPath();
    current.forEach(([gx, gy], i) => {
      const [px, py] = pencilGIStoPx(gx, gy);
      i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    if (mousePos && current.length > 0) ctx.lineTo(mousePos.x, mousePos.y);
    ctx.stroke();
    ctx.setLineDash([]);

    // Vertex dots
    ctx.fillStyle = '#00d4ff';
    current.forEach(([gx, gy]) => {
      const [px, py] = pencilGIStoPx(gx, gy);
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  document.getElementById('pencil-status').textContent = `${lines.length} lines`;
}

async function openPencilTool() {
  openModal('modal-pencil');
  pencilTool.canvas = document.getElementById('pencil-canvas');
  pencilTool.ctx    = pencilTool.canvas.getContext('2d');

  const wrap = pencilTool.canvas.parentElement;
  pencilTool.canvas.width  = wrap.clientWidth;
  pencilTool.canvas.height = wrap.clientHeight;

  pencilTool.extent   = state.mapExtent;
  pencilTool.lines    = [];
  pencilTool.current  = [];
  pencilTool.mousePos = null;

  const mapImg = document.getElementById('map-img');
  if (mapImg.src && mapImg.style.display !== 'none') {
    const img = new Image();
    img.onload = () => { pencilTool.bgImage = img; pencilRedraw(); };
    img.src = mapImg.src;
  } else {
    pencilRedraw();
  }
}

document.getElementById('btn-pencil-tool').addEventListener('click', openPencilTool);

(function () {
  const canvas = document.getElementById('pencil-canvas');

  canvas.addEventListener('mousemove', (e) => {
    pencilTool.mousePos = { x: e.offsetX, y: e.offsetY };
    pencilRedraw();
  });

  canvas.addEventListener('click', (e) => {
    const [gx, gy] = pencilPxToGIS(e.offsetX, e.offsetY);
    pencilTool.current.push([gx, gy]);
    pencilRedraw();
  });

  canvas.addEventListener('dblclick', (e) => {
    // Remove duplicate from dblclick
    if (pencilTool.current.length > 1) pencilTool.current.pop();
    if (pencilTool.current.length >= 2) {
      pencilTool.lines.push([...pencilTool.current]);
    }
    pencilTool.current = [];
    pencilRedraw();
  });

  canvas.addEventListener('keydown', (e) => {
    if (e.key === 'z' || e.key === 'Z') {
      pencilTool.current.pop();
      pencilRedraw();
    } else if (e.key === 'Escape') {
      pencilTool.current = [];
      pencilRedraw();
    }
  });

  canvas.setAttribute('tabindex', '0');
})();

function pencilUndo() {
  if (pencilTool.current.length > 0) {
    pencilTool.current.pop();
  } else {
    pencilTool.lines.pop();
  }
  pencilRedraw();
}

function pencilCancelLine() {
  pencilTool.current = [];
  pencilRedraw();
}

function pencilClearAll() {
  pencilTool.lines   = [];
  pencilTool.current = [];
  pencilRedraw();
}

async function pencilAddToMap() {
  // Commit current stroke if any
  if (pencilTool.current.length >= 2) {
    pencilTool.lines.push([...pencilTool.current]);
    pencilTool.current = [];
  }
  if (pencilTool.lines.length === 0) {
    toast('Draw at least one line first', 'info');
    return;
  }
  setBusy('Adding lines to map…');
  try {
    const rect = document.getElementById('map-container').getBoundingClientRect();
    const r = await api('/api/pencil_add_lines', {
      lines:  pencilTool.lines,
      width:  Math.floor(rect.width)  || 800,
      height: Math.floor(rect.height) || 600,
    });
    if (!r.ok) { toast(r.msg, 'error'); return; }
    if (r.image) setMapImage(r.image, r.extent);
    pencilTool.lines   = [];
    pencilTool.current = [];
    closeModal('modal-pencil');
    state.processed = true;
    updateButtonStates();
    toast(`Added ${r.added} line(s) to map — ${r.total} total`, 'success');
  } catch (e) {
    toast(`Pencil error: ${e.message}`, 'error');
  } finally {
    clearBusy();
  }
}

// ═══════════════════════════════════════════════════════════════
// APPLY PRECISION EDITS BUTTON (sidebar)
// ═══════════════════════════════════════════════════════════════

// (handled by precisionApply() above, wired to btn-precision-apply)

// ═══════════════════════════════════════════════════════════════
// CANVAS RESIZE HANDLERS FOR MODALS
// ═══════════════════════════════════════════════════════════════

window.addEventListener('resize', () => {
  // Hint tool
  if (document.getElementById('modal-hint-tool').style.display !== 'none' && hintTool.canvas) {
    const wrap = hintTool.canvas.parentElement;
    hintTool.canvas.width  = wrap.clientWidth;
    hintTool.canvas.height = wrap.clientHeight;
    hintRedraw();
  }
  // Precision editor
  if (document.getElementById('modal-precision').style.display !== 'none' && precisionEd.canvas) {
    const wrap = precisionEd.canvas.parentElement;
    precisionEd.canvas.width  = wrap.clientWidth;
    precisionEd.canvas.height = wrap.clientHeight;
    precRedraw();
  }
  // Pencil tool
  if (document.getElementById('modal-pencil').style.display !== 'none' && pencilTool.canvas) {
    const wrap = pencilTool.canvas.parentElement;
    pencilTool.canvas.width  = wrap.clientWidth;
    pencilTool.canvas.height = wrap.clientHeight;
    pencilRedraw();
  }
});

// ═══════════════════════════════════════════════════════════════
// MAP ZOOM / PAN
// ═══════════════════════════════════════════════════════════════

async function _zoomRequest(action, extra = {}) {
  if (!state.fileLoaded) return;
  const rect = document.getElementById('map-container').getBoundingClientRect();
  const r = await api('/api/map_zoom', {
    action,
    width:  Math.floor(rect.width)  || 800,
    height: Math.floor(rect.height) || 600,
    ...extra,
  });
  if (r.image) setMapImage(r.image, r.extent);
}

function mapZoom(action, cx, cy) {
  const extra = (cx !== undefined && cy !== undefined) ? { cx, cy } : {};
  _zoomRequest(action, extra);
}

async function mapResetView() {
  if (!state.fileLoaded) return;
  const rect = document.getElementById('map-container').getBoundingClientRect();
  const r = await api('/api/map_reset_view', {
    width:  Math.floor(rect.width)  || 800,
    height: Math.floor(rect.height) || 600,
  });
  if (r.image) setMapImage(r.image, r.extent);
}

// ── Mouse-wheel zoom (scroll over map) ───────────────────────────
(function () {
  const container = document.getElementById('map-container');

  container.addEventListener('wheel', (e) => {
    if (!state.fileLoaded || !state.mapExtent) return;
    e.preventDefault();

    const img  = document.getElementById('map-img');
    const rect = img.getBoundingClientRect();
    const px   = e.clientX - rect.left;
    const py   = e.clientY - rect.top;
    const [cx, cy] = pxToGIS(px, py, rect.width, rect.height);

    const action = e.deltaY < 0 ? 'zoom_in' : 'zoom_out';
    mapZoom(action, cx, cy);
  }, { passive: false });

  // ── Drag-to-pan ─────────────────────────────────────────────
  let _pan = null;  // { startX, startY, extentAtStart }

  container.addEventListener('mousedown', (e) => {
    // Only pan with middle button or when box-draw mode is off
    if (!state.fileLoaded || !state.mapExtent) return;
    const overlay = document.getElementById('draw-overlay');
    if (overlay.style.display !== 'none') return; // box-draw mode active
    if (e.button !== 1 && e.button !== 0) return;  // left or middle drag
    if (e.button === 0 && e.target.tagName === 'BUTTON') return;

    const img  = document.getElementById('map-img');
    if (!img || img.style.display === 'none') return;
    const rect = img.getBoundingClientRect();

    _pan = {
      startX: e.clientX,
      startY: e.clientY,
      extent: [...state.mapExtent],
      rectW:  rect.width,
      rectH:  rect.height,
    };
    container.style.cursor = 'grabbing';
    e.preventDefault();
  });

  window.addEventListener('mousemove', (e) => {
    if (!_pan) return;
    const [xmin, ymin, xmax, ymax] = _pan.extent;
    const gisW = xmax - xmin;
    const gisH = ymax - ymin;
    const dx = -((e.clientX - _pan.startX) / _pan.rectW) * gisW;
    const dy =  ((e.clientY - _pan.startY) / _pan.rectH) * gisH;
    // Live-preview: just shift the image so panning feels instant
    const img = document.getElementById('map-img');
    img.style.transform =
      `translate(${e.clientX - _pan.startX}px, ${e.clientY - _pan.startY}px)`;
  });

  window.addEventListener('mouseup', async (e) => {
    if (!_pan) return;
    const [xmin, ymin, xmax, ymax] = _pan.extent;
    const gisW = xmax - xmin;
    const gisH = ymax - ymin;
    const dx = -((e.clientX - _pan.startX) / _pan.rectW) * gisW;
    const dy =  ((e.clientY - _pan.startY) / _pan.rectH) * gisH;
    _pan = null;
    container.style.cursor = '';
    const img = document.getElementById('map-img');
    img.style.transform = '';

    if (Math.abs(dx) < 1e-10 && Math.abs(dy) < 1e-10) return; // no movement
    await _zoomRequest('pan', { dx, dy });
  });
})();
