#!/usr/bin/env python3
"""Web-based metadata editor for autocrop.

Serves a single-page app to review and adjust photo bounding boxes,
rotation, dates, locations (with map), and captions.

Usage:
    python editor.py ./album/
    python editor.py ./album/ -o ./album/cropped --port 8080
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import re as _re
import sys
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import urllib.parse
import urllib.request
from urllib.parse import urlparse, parse_qs

from PIL import Image

METADATA_FILENAME = "autocrop_meta.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}

# ---------------------------------------------------------------------------
# HTML / CSS / JS — embedded SPA
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autocrop Editor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #141210; --bg-surface: #1c1a16; --bg-card: #24221c;
  --border: rgba(255,255,255,0.08); --border-hover: rgba(255,255,255,0.18);
  --text: #ece8e0; --text-secondary: #9b9484; --text-muted: #6b6459;
  --accent: #d4a853; --accent-hover: #e0ba6a; --accent-dim: rgba(212,168,83,0.15);
  --success: #5cb870; --success-hover: #4a9e5e;
  --danger: #d45c5c; --danger-hover: #c04444;
  --radius: 12px; --radius-sm: 8px;
}
body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }

/* Header */
.header {
  background: var(--bg-surface); border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100; backdrop-filter: blur(12px);
}
.header-row1 {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 24px 0;
}
.header-row1 h1 { font-size: 15px; font-weight: 600; letter-spacing: -0.3px; color: var(--text); }
.header-actions { display: flex; gap: 8px; }
.header-row2 {
  display: flex; align-items: center; justify-content: center;
  padding: 10px 24px 12px; gap: 16px;
}
.pagination { display: flex; gap: 4px; align-items: center; }
.pagination .arrow-btn {
  width: 32px; height: 32px; display: flex; align-items: center; justify-content: center;
  background: none; border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--text-secondary); cursor: pointer; font-size: 14px; transition: all 0.15s;
}
.pagination .arrow-btn:hover:not(:disabled) { background: rgba(255,255,255,0.06); color: var(--text); border-color: var(--border-hover); }
.pagination .arrow-btn:disabled { opacity: 0.25; cursor: default; }
.page-nums { display: flex; gap: 2px; margin: 0 4px; }
.page-btn {
  min-width: 32px; height: 32px; display: flex; align-items: center; justify-content: center;
  background: none; border: 1px solid transparent; border-radius: var(--radius-sm);
  color: var(--text-secondary); cursor: pointer; font-size: 13px; font-weight: 500;
  font-family: inherit; transition: all 0.15s; padding: 0 6px;
}
.page-btn:hover { background: rgba(255,255,255,0.06); color: var(--text); }
.page-btn.current { background: var(--accent); color: #1a1714; border-color: var(--accent); font-weight: 600; }
.page-btn.ellipsis { cursor: default; color: var(--text-muted); }
.page-btn.ellipsis:hover { background: none; color: var(--text-muted); }

/* Mode toggle */
.mode-toggle {
  display: flex; background: rgba(255,255,255,0.04); border: 1px solid var(--border);
  border-radius: var(--radius-sm); overflow: hidden;
}
.mode-btn {
  padding: 6px 20px; border: none; background: none; color: var(--text-secondary);
  font-size: 13px; font-weight: 500; font-family: inherit; cursor: pointer;
  transition: all 0.15s; position: relative;
}
.mode-btn:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.mode-btn.active { background: var(--accent); color: #1a1714; }

/* Buttons */
button { font-family: inherit; transition: all 0.15s; }
.btn-primary {
  background: var(--accent); color: #1a1714; border: none;
  padding: 8px 20px; border-radius: var(--radius-sm); font-size: 13px; font-weight: 500; cursor: pointer;
}
.btn-primary:hover { background: var(--accent-hover); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(212,168,83,0.25); }
.btn-secondary {
  background: rgba(255,255,255,0.06); border: 1px solid var(--border); color: var(--text);
  padding: 8px 20px; border-radius: var(--radius-sm); font-size: 13px; font-weight: 500; cursor: pointer;
}
.btn-secondary:hover { background: rgba(255,255,255,0.1); border-color: var(--border-hover); }
.btn-danger {
  background: rgba(212,92,92,0.12); border: 1px solid rgba(212,92,92,0.25); color: #e07070;
  padding: 8px 20px; border-radius: var(--radius-sm); font-size: 13px; font-weight: 500; cursor: pointer;
}
.btn-danger:hover { background: rgba(212,92,92,0.2); border-color: rgba(212,92,92,0.4); }
.btn-success {
  background: var(--success); color: #fff; border: none;
  padding: 8px 20px; border-radius: var(--radius-sm); font-size: 13px; font-weight: 500; cursor: pointer;
}
.btn-success:hover { background: var(--success-hover); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(92,184,112,0.25); }

/* Crop view */
#crop-view { display: flex; flex-direction: column; align-items: center; padding: 24px; }
#crop-view canvas {
  max-width: 95vw; cursor: crosshair; border-radius: var(--radius);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.05);
}
.crop-toolbar { margin-top: 20px; display: flex; gap: 10px; }

/* Metadata view */
#review-view { padding: 24px 32px; display: none; max-width: 960px; margin: 0 auto; }
.photo-cards { display: flex; flex-direction: column; gap: 16px; }
.photo-card {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 20px; display: flex; gap: 20px;
  align-items: flex-start; transition: all 0.2s;
}
.photo-card:hover { border-color: var(--border-hover); box-shadow: 0 4px 20px rgba(0,0,0,0.2); }
.photo-card.skipped { opacity: 0.35; }
.photo-card .preview-wrap { position: relative; flex-shrink: 0; }
.photo-card canvas {
  border-radius: var(--radius-sm); border: 1px solid var(--border);
  box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}
.photo-card .fields { flex: 1; display: flex; flex-direction: column; gap: 10px; }
.photo-card .field-row { display: flex; align-items: center; gap: 10px; }
.photo-card label { width: 72px; font-size: 12px; color: var(--text-muted); flex-shrink: 0; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 500; }
.photo-card input[type=text], .photo-card textarea {
  flex: 1; padding: 8px 12px; background: rgba(255,255,255,0.04);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  font-size: 13px; font-family: inherit; color: var(--text); transition: border-color 0.15s;
}
.photo-card input[type=text]:focus, .photo-card textarea:focus {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim);
}
.photo-card input.date-input {
  font-family: inherit; max-width: 170px; color-scheme: dark;
  padding: 8px 12px; background: rgba(255,255,255,0.04);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--text); font-size: 13px;
}
.photo-card input.date-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.photo-card textarea { resize: vertical; min-height: 44px; }
.photo-card .loc-display {
  flex: 1; padding: 8px 12px; background: rgba(255,255,255,0.03);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  font-size: 13px; min-height: 34px; color: var(--text-secondary); cursor: default;
}
.field-hint {
  display: none; margin-left: 82px; font-size: 12px;
}
.field-hint a, .field-hint-inline a {
  color: var(--accent); cursor: pointer; text-decoration: none;
}
.field-hint a:hover, .field-hint-inline a:hover { text-decoration: underline; }
.field-hint-inline {
  display: none; font-size: 12px; margin-left: 8px; white-space: nowrap;
}
.photo-card .rotate-btns { display: flex; gap: 6px; margin-top: 8px; }
.photo-card .rotate-btns button {
  width: 36px; height: 36px; display: flex; align-items: center; justify-content: center;
  font-size: 16px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: rgba(255,255,255,0.04); cursor: pointer; color: var(--text-secondary);
  transition: all 0.15s;
}
.photo-card .rotate-btns button:hover { background: rgba(255,255,255,0.08); color: var(--text); border-color: var(--border-hover); }

/* Map modal */
.modal-overlay {
  display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
  background: rgba(0,0,0,0.6); backdrop-filter: blur(4px);
  z-index: 1000; justify-content: center; align-items: center;
}
.modal-overlay.active { display: flex; }
.modal {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius); width: 600px; max-width: 95vw; max-height: 90vh;
  overflow: hidden; display: flex; flex-direction: column;
  box-shadow: 0 24px 48px rgba(0,0,0,0.4);
}
.modal-header { padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
.modal-header h3 { font-size: 15px; font-weight: 600; }
.modal-header .close-btn { border: none; background: none; font-size: 20px; cursor: pointer; color: var(--text-muted); padding: 4px; }
.modal-header .close-btn:hover { color: var(--text); }
.modal-body { padding: 16px 20px; flex: 1; overflow: auto; }
.modal-footer { padding: 16px 20px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 8px; }
#map-search {
  width: 100%; padding: 10px 12px; background: rgba(255,255,255,0.04);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  margin-bottom: 10px; font-size: 13px; color: var(--text); font-family: inherit;
}
#map-search:focus { outline: none; border-color: var(--accent); }
#map-search::placeholder { color: var(--text-muted); }
#map-container { width: 100%; height: 350px; border-radius: var(--radius-sm); border: 1px solid var(--border); overflow: hidden; }
#map-coords { font-size: 12px; color: var(--text-muted); margin-top: 8px; font-family: "SF Mono", "Fira Code", monospace; }

/* Toast */
.toast {
  position: fixed; bottom: 24px; right: 24px; padding: 14px 24px;
  border-radius: var(--radius-sm); font-size: 13px; font-weight: 500;
  z-index: 2000; transition: all 0.3s; box-shadow: 0 8px 24px rgba(0,0,0,0.3);
  color: #fff;
}
.toast.success { background: var(--success); }
.toast.error { background: var(--danger); }
</style>
</head>
<body>

<div class="header">
  <div class="header-row1">
    <h1>Autocrop Editor</h1>
    <div class="header-actions">
      <button class="btn-secondary" onclick="saveDraft()">Save Draft</button>
      <button class="btn-success" onclick="saveAndApply()">Apply</button>
    </div>
  </div>
  <div class="header-row2">
    <div class="pagination">
      <button class="arrow-btn" id="btn-prev" onclick="navigateToPage(currentPageIdx-1)">&#9664;</button>
      <div class="page-nums" id="page-nums"></div>
      <button class="arrow-btn" id="btn-next" onclick="navigateToPage(currentPageIdx+1)">&#9654;</button>
    </div>
    <div class="mode-toggle">
      <button class="mode-btn active" id="mode-crops" onclick="setMode('crops')">Cut photos</button>
      <button class="mode-btn" id="mode-metadata" onclick="setMode('metadata')">Edit photos</button>
    </div>
  </div>
</div>

<div id="crop-view">
  <canvas id="crop-canvas"></canvas>
  <div class="crop-toolbar">
    <button class="btn-secondary" onclick="addPhoto()">+ Add Photo</button>
    <button class="btn-danger" onclick="deleteSelected()" id="btn-delete" disabled>Delete Selected</button>
  </div>
</div>

<div id="review-view">
  <div class="photo-cards" id="photo-cards"></div>
</div>

<!-- Map modal -->
<div class="modal-overlay" id="map-modal">
  <div class="modal">
    <div class="modal-header">
      <h3>Select Location</h3>
      <button class="close-btn" onclick="closeMap()">&#215;</button>
    </div>
    <div class="modal-body">
      <input type="text" id="map-search" placeholder="Search location or enter coordinates (lat, lon)..." onkeydown="if(event.key==='Enter')searchMap()">
      <div id="map-container"></div>
      <div id="map-coords">Click on map or search to select a location</div>
    </div>
    <div class="modal-footer">
      <button class="btn-secondary" onclick="closeMap()">Cancel</button>
      <button class="btn-primary" onclick="applyMapLocation()">Apply</button>
    </div>
  </div>
</div>

<script>
// =========================================================================
// State
// =========================================================================
let metadata = null;
let dirty = false;
let currentPageIdx = 0;
let currentMode = 'crops'; // 'crops' | 'metadata'
let selectedBox = -1;
let pageImage = null;

// Drag state
let dragMode = null;
let dragHandle = -1;
let dragStart = null;
let dragOrigBox = null;

// Map state
let map = null;
let mapMarker = null;
let mapPhotoIdx = -1;
let leafletLoaded = false;

const COLORS = ['#d4a853','#e07070','#5cb870','#5ba3d9','#b07cd4','#4dbfb0','#d4883d','#6b8fd4'];
const HANDLE_SIZE = 8;
const PAD = 12; // padding around image for handles at edges

// =========================================================================
// Init
// =========================================================================
async function init() {
  const resp = await fetch('/api/metadata');
  metadata = await resp.json();
  currentPageIdx = 0;
  renderPagination();
  setMode('crops');
  resolveCoordinateLocations();
}

// Track which locations the user has manually edited (pageIdx:photoIdx)
const userEditedLocations = new Set();

async function resolveCoordinateLocations() {
  // Collect unique coordinate strings that need resolving
  const coordPattern = /^-?\d+\.\d+,\s*-?\d+\.\d+$/;
  const toResolve = new Map(); // coords -> [{pageIdx, photoIdx}]

  for (let p = 0; p < metadata.pages.length; p++) {
    const photos = metadata.pages[p].photos;
    for (let i = 0; i < photos.length; i++) {
      const loc = photos[i].location;
      if (loc && coordPattern.test(loc.trim())) {
        const key = loc.trim();
        if (!toResolve.has(key)) toResolve.set(key, []);
        toResolve.get(key).push({pageIdx: p, photoIdx: i});
      }
    }
  }

  for (const [coords, targets] of toResolve) {
    try {
      const resp = await fetch('/api/reverse-geocode?coords=' + encodeURIComponent(coords));
      const data = await resp.json();
      if (data.name) {
        for (const {pageIdx, photoIdx} of targets) {
          if (userEditedLocations.has(pageIdx + ':' + photoIdx)) continue;
          const photo = metadata.pages[pageIdx].photos[photoIdx];
          // Store coords for EXIF, replace display with name
          if (!photo._coords) photo._coords = photo.location;
          photo.location_name = data.name;
          photo.location = data.name;
        }
        // Re-render current page if affected
        if (targets.some(t => t.pageIdx === currentPageIdx) && currentMode === 'metadata') {
          renderPhotoCards();
        }
      }
    } catch (e) {
      console.warn('Reverse geocode failed for', coords, e);
    }
  }
}

// =========================================================================
// Navigation
// =========================================================================
function navigateToPage(idx) {
  if (!metadata || idx < 0 || idx >= metadata.pages.length) return;
  currentPageIdx = idx;
  selectedBox = (currentMode === 'crops' && metadata.pages[idx].photos.length > 0) ? 0 : -1;
  renderPagination();
  if (currentMode === 'crops') {
    loadPageImage();
  } else {
    loadPageImage(); // need image for previews
  }
}

function renderPagination() {
  if (!metadata) return;
  const n = metadata.pages.length;
  document.getElementById('btn-prev').disabled = (currentPageIdx <= 0);
  document.getElementById('btn-next').disabled = (currentPageIdx >= n - 1);

  const container = document.getElementById('page-nums');
  container.innerHTML = '';

  // Google-style pagination
  const pages = getPageRange(n, currentPageIdx);
  for (const p of pages) {
    const btn = document.createElement('button');
    btn.className = 'page-btn';
    if (p === '...') {
      btn.className += ' ellipsis';
      btn.textContent = '\u2026';
    } else {
      btn.textContent = p + 1;
      if (p === currentPageIdx) btn.className += ' current';
      btn.onclick = () => navigateToPage(p);
    }
    container.appendChild(btn);
  }
}

function getPageRange(total, current) {
  if (total <= 10) return Array.from({length: total}, (_, i) => i);

  const pages = [];
  const addRange = (from, to) => {
    for (let i = from; i <= to; i++) pages.push(i);
  };

  // Always show first 2
  addRange(0, 1);

  // Window around current
  const winStart = Math.max(2, current - 2);
  const winEnd = Math.min(total - 3, current + 2);

  if (winStart > 2) pages.push('...');
  addRange(winStart, winEnd);
  if (winEnd < total - 3) pages.push('...');

  // Always show last 2
  addRange(total - 2, total - 1);

  return pages;
}

// =========================================================================
// Mode toggle
// =========================================================================
function setMode(mode) {
  currentMode = mode;
  document.getElementById('mode-crops').className = 'mode-btn' + (mode === 'crops' ? ' active' : '');
  document.getElementById('mode-metadata').className = 'mode-btn' + (mode === 'metadata' ? ' active' : '');

  if (mode === 'crops') {
    document.getElementById('crop-view').style.display = 'flex';
    document.getElementById('review-view').style.display = 'none';
    selectedBox = (metadata.pages[currentPageIdx].photos.length > 0) ? 0 : -1;
    if (pageImage) { setupCanvas(); drawCrop(); }
    else loadPageImage();
  } else {
    document.getElementById('crop-view').style.display = 'none';
    document.getElementById('review-view').style.display = 'block';
    if (pageImage) renderPhotoCards();
    else loadPageImage();
  }
}

// =========================================================================
// Image loading
// =========================================================================
function loadPageImage() {
  const page = metadata.pages[currentPageIdx];
  const img = new window.Image();
  img.onload = () => {
    pageImage = img;
    if (currentMode === 'crops') { setupCanvas(); drawCrop(); }
    else renderPhotoCards();
  };
  img.src = `/images/${encodeURIComponent(page.source)}?w=2000`;
}

// =========================================================================
// Crop Editor
// =========================================================================
function setupCanvas() {
  const canvas = document.getElementById('crop-canvas');
  const maxW = window.innerWidth * 0.95 - 2 * PAD;
  const maxH = window.innerHeight * 0.72 - 2 * PAD;
  const scale = Math.min(maxW / pageImage.naturalWidth, maxH / pageImage.naturalHeight, 1);
  const imgW = Math.round(pageImage.naturalWidth * scale);
  const imgH = Math.round(pageImage.naturalHeight * scale);
  canvas.width = imgW + 2 * PAD;
  canvas.height = imgH + 2 * PAD;
  canvas.style.width = canvas.width + 'px';
  canvas.style.height = canvas.height + 'px';

  canvas.onmousedown = onCanvasMouseDown;
  canvas.onmousemove = onCanvasMouseMove;
  canvas.ondblclick = onCanvasDblClick;
}

function drawCrop() {
  const canvas = document.getElementById('crop-canvas');
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  const page = metadata.pages[currentPageIdx];
  const photos = page.photos;

  const imgW = w - 2 * PAD, imgH = h - 2 * PAD;
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(pageImage, PAD, PAD, imgW, imgH);

  // Dark overlay with cutouts (even-odd)
  ctx.beginPath();
  ctx.rect(PAD, PAD, imgW, imgH);
  for (const photo of photos) {
    if (photo.skip) continue;
    const [x1, y1, x2, y2] = bboxToPixels(photo.bbox, imgW, imgH);
    ctx.moveTo(x1, y1);
    ctx.lineTo(x1, y2);
    ctx.lineTo(x2, y2);
    ctx.lineTo(x2, y1);
    ctx.closePath();
  }
  ctx.fillStyle = 'rgba(0,0,0,0.5)';
  ctx.fill('evenodd');

  // Borders and handles
  photos.forEach((photo, i) => {
    if (photo.skip) return;
    const [x1, y1, x2, y2] = bboxToPixels(photo.bbox, imgW, imgH);
    const color = COLORS[i % COLORS.length];
    const isSelected = (i === selectedBox);

    ctx.strokeStyle = color;
    ctx.lineWidth = isSelected ? 3 : 1.5;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

    if (isSelected) {
      const corners = [[x1,y1],[x2,y1],[x2,y2],[x1,y2]];
      for (const [cx, cy] of corners) {
        ctx.beginPath();
        ctx.arc(cx, cy, HANDLE_SIZE, 0, Math.PI * 2);
        ctx.fillStyle = '#fff';
        ctx.fill();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      const midpoints = [[(x1+x2)/2, y1], [x2, (y1+y2)/2], [(x1+x2)/2, y2], [x1, (y1+y2)/2]];
      for (const [mx, my] of midpoints) {
        ctx.beginPath();
        ctx.arc(mx, my, HANDLE_SIZE * 0.7, 0, Math.PI * 2);
        ctx.fillStyle = '#fff';
        ctx.fill();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    }
  });

  updateDeleteButton();
}

function updateDeleteButton() {
  const photos = metadata.pages[currentPageIdx].photos;
  const nonSkipped = photos.filter(p => !p.skip).length;
  const btn = document.getElementById('btn-delete');
  btn.disabled = (selectedBox < 0 || nonSkipped <= 1);
}

function bboxToPixels(bbox, imgW, imgH) {
  return [PAD + bbox[0]/100*imgW, PAD + bbox[1]/100*imgH, PAD + bbox[2]/100*imgW, PAD + bbox[3]/100*imgH];
}
function pixelsToBbox(x1, y1, x2, y2, imgW, imgH) {
  return [(x1 - PAD)/imgW*100, (y1 - PAD)/imgH*100, (x2 - PAD)/imgW*100, (y2 - PAD)/imgH*100];
}

function getCanvasPos(e) {
  const r = document.getElementById('crop-canvas').getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

const EDGE_THRESHOLD = 8;

function hitTest(mx, my) {
  const canvas = document.getElementById('crop-canvas');
  const imgW = canvas.width - 2 * PAD, imgH = canvas.height - 2 * PAD;
  const photos = metadata.pages[currentPageIdx].photos;

  const order = [];
  if (selectedBox >= 0) order.push(selectedBox);
  for (let i = photos.length - 1; i >= 0; i--) {
    if (i !== selectedBox) order.push(i);
  }

  for (const i of order) {
    if (photos[i].skip) continue;
    const [x1,y1,x2,y2] = bboxToPixels(photos[i].bbox, imgW, imgH);

    const corners = [[x1,y1],[x2,y1],[x2,y2],[x1,y2]];
    for (let c = 0; c < corners.length; c++) {
      if (Math.hypot(mx - corners[c][0], my - corners[c][1]) < 12)
        return { type: 'corner', boxIdx: i, handleIdx: c };
    }

    const midpoints = [[(x1+x2)/2, y1], [x2, (y1+y2)/2], [(x1+x2)/2, y2], [x1, (y1+y2)/2]];
    for (let e = 0; e < midpoints.length; e++) {
      if (Math.hypot(mx - midpoints[e][0], my - midpoints[e][1]) < 12)
        return { type: 'edge', boxIdx: i, handleIdx: e };
    }

    const insideOuter = mx >= x1 - EDGE_THRESHOLD && mx <= x2 + EDGE_THRESHOLD &&
                        my >= y1 - EDGE_THRESHOLD && my <= y2 + EDGE_THRESHOLD;
    const insideInner = mx >= x1 + EDGE_THRESHOLD && mx <= x2 - EDGE_THRESHOLD &&
                        my >= y1 + EDGE_THRESHOLD && my <= y2 - EDGE_THRESHOLD;
    if (insideOuter && !insideInner) {
      const dTop = Math.abs(my - y1), dBot = Math.abs(my - y2);
      const dLeft = Math.abs(mx - x1), dRight = Math.abs(mx - x2);
      const minD = Math.min(dTop, dBot, dLeft, dRight);
      let edgeIdx = minD === dTop ? 0 : minD === dRight ? 1 : minD === dBot ? 2 : 3;
      return { type: 'edge', boxIdx: i, handleIdx: edgeIdx };
    }

    if (mx >= x1 && mx <= x2 && my >= y1 && my <= y2)
      return { type: 'box', boxIdx: i };
  }
  return null;
}

function getCursorForHit(hit) {
  if (!hit) return 'crosshair';
  if (hit.type === 'box') return 'move';
  if (hit.type === 'corner') return [0,2].includes(hit.handleIdx) ? 'nwse-resize' : 'nesw-resize';
  if (hit.type === 'edge') return [0,2].includes(hit.handleIdx) ? 'ns-resize' : 'ew-resize';
  return 'crosshair';
}

function onCanvasMouseDown(e) {
  const [mx, my] = getCanvasPos(e);
  const hit = hitTest(mx, my);
  if (!hit) { selectedBox = -1; drawCrop(); return; }

  selectedBox = hit.boxIdx;
  if (hit.type === 'corner' || hit.type === 'edge') {
    dragMode = 'resize';
    dragHandle = hit.handleIdx;
    dragStart = [mx, my];
    dragOrigBox = [...metadata.pages[currentPageIdx].photos[selectedBox].bbox];
    if (hit.type === 'edge') dragHandle += 4;
  } else if (hit.type === 'box') {
    dragMode = 'move';
    dragStart = [mx, my];
    dragOrigBox = [...metadata.pages[currentPageIdx].photos[selectedBox].bbox];
  }
  if (dragMode) {
    window.addEventListener('mousemove', onCanvasMouseMove);
    window.addEventListener('mouseup', onCanvasMouseUp);
  }
  drawCrop();
}

function onCanvasMouseMove(e) {
  const canvas = document.getElementById('crop-canvas');
  const [mx, my] = getCanvasPos(e);

  if (!dragMode) {
    canvas.style.cursor = getCursorForHit(hitTest(mx, my));
    return;
  }

  const imgW = canvas.width - 2 * PAD, imgH = canvas.height - 2 * PAD;
  const dx = mx - dragStart[0], dy = my - dragStart[1];
  const ob = dragOrigBox;
  const photo = metadata.pages[currentPageIdx].photos[selectedBox];
  const pxDx = dx / imgW * 100, pxDy = dy / imgH * 100;

  if (dragMode === 'move') {
    const bw = ob[2] - ob[0], bh = ob[3] - ob[1];
    let nx1 = Math.max(0, Math.min(100 - bw, ob[0] + pxDx));
    let ny1 = Math.max(0, Math.min(100 - bh, ob[1] + pxDy));
    photo.bbox = [nx1, ny1, nx1 + bw, ny1 + bh];
  } else if (dragMode === 'resize') {
    let [x1, y1, x2, y2] = [...ob];
    const h_idx = dragHandle;
    if (h_idx === 0) { x1 += pxDx; y1 += pxDy; }
    else if (h_idx === 1) { x2 += pxDx; y1 += pxDy; }
    else if (h_idx === 2) { x2 += pxDx; y2 += pxDy; }
    else if (h_idx === 3) { x1 += pxDx; y2 += pxDy; }
    else if (h_idx === 4) { y1 += pxDy; }
    else if (h_idx === 5) { x2 += pxDx; }
    else if (h_idx === 6) { y2 += pxDy; }
    else if (h_idx === 7) { x1 += pxDx; }

    x1 = Math.max(0, Math.min(x1, 99)); y1 = Math.max(0, Math.min(y1, 99));
    x2 = Math.max(1, Math.min(x2, 100)); y2 = Math.max(1, Math.min(y2, 100));
    if (x2 - x1 < 2) { if ([0,3,7].includes(h_idx)) x1 = x2 - 2; else x2 = x1 + 2; }
    if (y2 - y1 < 2) { if ([0,1,4].includes(h_idx)) y1 = y2 - 2; else y2 = y1 + 2; }
    photo.bbox = [x1, y1, x2, y2];
  }
  drawCrop();
}

function onCanvasMouseUp() {
  if (dragMode) dirty = true;
  dragMode = null; dragHandle = -1; dragStart = null; dragOrigBox = null;
  window.removeEventListener('mousemove', onCanvasMouseMove);
  window.removeEventListener('mouseup', onCanvasMouseUp);
}

function onCanvasDblClick(e) {
  const [mx, my] = getCanvasPos(e);
  if (!hitTest(mx, my)) addPhoto(mx, my);
}

function addPhoto(cx, cy) {
  const canvas = document.getElementById('crop-canvas');
  const imgW = canvas.width - 2 * PAD, imgH = canvas.height - 2 * PAD;
  let px = 25, py = 25;
  if (cx !== undefined) { px = (cx - PAD)/imgW*100; py = (cy - PAD)/imgH*100; }
  const size = 15;
  const bbox = [
    Math.max(0, px - size), Math.max(0, py - size),
    Math.min(100, px + size), Math.min(100, py + size),
  ];
  metadata.pages[currentPageIdx].photos.push({
    bbox, top_side: 'top', date: null, location: null, location_name: null, caption: null, skip: false,
  });
  dirty = true;
  selectedBox = metadata.pages[currentPageIdx].photos.length - 1;
  drawCrop();
}

function deleteSelected() {
  if (selectedBox < 0) return;
  const photos = metadata.pages[currentPageIdx].photos;
  const nonSkipped = photos.filter(p => !p.skip).length;
  if (nonSkipped <= 1) {
    showToast('At least 1 photo required per page', 'error');
    return;
  }
  photos.splice(selectedBox, 1);
  selectedBox = -1;
  dirty = true;
  drawCrop();
}

// =========================================================================
// Metadata view
// =========================================================================
function renderPhotoCards() {
  const container = document.getElementById('photo-cards');
  container.innerHTML = '';
  const page = metadata.pages[currentPageIdx];

  page.photos.forEach((photo, i) => {
    if (photo.skip) return;
    const card = document.createElement('div');
    card.className = 'photo-card';
    card.id = `card-${i}`;

    // Preview canvas
    const wrap = document.createElement('div');
    wrap.className = 'preview-wrap';
    const preview = document.createElement('canvas');
    renderPreview(preview, photo);
    wrap.appendChild(preview);

    // Rotation buttons
    const rotDiv = document.createElement('div');
    rotDiv.className = 'rotate-btns';
    const btnL = document.createElement('button');
    btnL.textContent = '\u21BA';
    btnL.title = 'Rotate left';
    btnL.onclick = () => rotatePhoto(i, 1);
    const btnR = document.createElement('button');
    btnR.textContent = '\u21BB';
    btnR.title = 'Rotate right';
    btnR.onclick = () => rotatePhoto(i, -1);
    rotDiv.appendChild(btnL);
    rotDiv.appendChild(btnR);
    wrap.appendChild(rotDiv);

    // Fields
    const fields = document.createElement('div');
    fields.className = 'fields';

    // Date
    fields.appendChild(makeDateField(photo, i));

    // Location (read-only + map button)
    const locRow = document.createElement('div');
    locRow.className = 'field-row';
    const locLabel = document.createElement('label');
    locLabel.textContent = 'Location';
    locRow.appendChild(locLabel);
    const locDisplay = document.createElement('div');
    locDisplay.className = 'loc-display';
    locDisplay.id = `loc-display-${i}`;
    locDisplay.textContent = photo.location_name || (photo.location || '\u2014');
    locRow.appendChild(locDisplay);
    const mapBtn = document.createElement('button');
    mapBtn.textContent = '\uD83D\uDDFA';
    mapBtn.title = 'Select on map';
    mapBtn.style.cssText = 'border:1px solid var(--border);border-radius:var(--radius-sm);padding:6px 10px;cursor:pointer;background:rgba(255,255,255,0.04);font-size:16px;transition:all 0.15s;';
    mapBtn.onclick = () => openMap(i);
    locRow.appendChild(mapBtn);
    fields.appendChild(locRow);

    const locHint = document.createElement('div');
    locHint.className = 'field-hint';
    locHint.id = `loc-hint-${i}`;
    fields.appendChild(locHint);

    // Caption
    fields.appendChild(makeFieldTextarea('Caption', photo.caption || '', (v) => { photo.caption = v || null; dirty = true; }));

    card.appendChild(wrap);
    card.appendChild(fields);
    container.appendChild(card);
  });
}

function dateToHtml(d) {
  if (!d) return '';
  const parts = d.split(':');
  if (parts.length === 3) return parts.join('-');
  if (parts.length === 2) return parts[0] + '-' + parts[1] + '-01';
  if (parts.length === 1 && parts[0].length === 4) return parts[0] + '-01-01';
  return '';
}
function dateFromHtml(d) {
  if (!d) return null;
  return d.replace(/-/g, ':');
}
function dateDisplayLabel(d) {
  if (!d) return '';
  const parts = d.split(':');
  if (parts.length === 3) return parts.reverse().join('.');
  if (parts.length === 2) return parts[1] + '.' + parts[0];
  return d;
}

function makeDateField(photo, photoIdx) {
  const row = document.createElement('div');
  row.className = 'field-row';
  const lbl = document.createElement('label');
  lbl.textContent = 'Date';
  row.appendChild(lbl);
  const input = document.createElement('input');
  input.type = 'date';
  input.className = 'date-input';
  input.value = dateToHtml(photo.date);
  input.onchange = () => {
    const oldDate = photo.date;
    photo.date = dateFromHtml(input.value);
    dirty = true;
    showDateSuggestions(photoIdx, oldDate, photo.date);
  };
  row.appendChild(input);

  const hint = document.createElement('span');
  hint.className = 'field-hint-inline';
  hint.id = `date-hint-${photoIdx}`;
  row.appendChild(hint);
  return row;
}

function showDateSuggestions(changedIdx, oldDate, newDate) {
  if (!newDate || newDate === oldDate) return;
  const page = metadata.pages[currentPageIdx];
  page.photos.forEach((photo, i) => {
    if (i === changedIdx || photo.skip) return;
    const hint = document.getElementById(`date-hint-${i}`);
    if (!hint) return;
    // Show hint if photo still has the old value OR already had a pending hint
    const dominated = (photo.date || null) === (oldDate || null) || hint.style.display === 'block';
    if (dominated && photo.date !== newDate) {
      hint.innerHTML = '';
      const a = document.createElement('a');
      a.textContent = `Use ${dateDisplayLabel(newDate)}`;
      a.onclick = () => {
        photo.date = newDate;
        dirty = true;
        const card = document.getElementById(`card-${i}`);
        if (card) {
          const dateInput = card.querySelector('.date-input');
          if (dateInput) dateInput.value = dateToHtml(newDate);
        }
        hint.style.display = 'none';
      };
      hint.appendChild(a);
      hint.style.display = 'block';
    }
  });
}

function makeFieldTextarea(label, value, onChange) {
  const row = document.createElement('div');
  row.className = 'field-row';
  const lbl = document.createElement('label');
  lbl.textContent = label;
  lbl.style.alignSelf = 'flex-start';
  row.appendChild(lbl);
  const ta = document.createElement('textarea');
  ta.value = value;
  ta.rows = 2;
  ta.onchange = () => onChange(ta.value.trim());
  row.appendChild(ta);
  return row;
}

function renderPreview(canvas, photo) {
  if (!pageImage) return;
  const iw = pageImage.naturalWidth, ih = pageImage.naturalHeight;
  const [sx1, sy1, sx2, sy2] = [
    photo.bbox[0]/100*iw, photo.bbox[1]/100*ih,
    photo.bbox[2]/100*iw, photo.bbox[3]/100*ih,
  ];
  const cropW = sx2 - sx1, cropH = sy2 - sy1;
  const ts = photo.top_side || 'top';
  const rotated = (ts === 'left' || ts === 'right');
  const outW = rotated ? cropH : cropW;
  const outH = rotated ? cropW : cropH;

  const maxDim = 300;
  const scale = Math.min(maxDim / outW, maxDim / outH, 1);
  canvas.width = Math.round(outW * scale);
  canvas.height = Math.round(outH * scale);

  const ctx = canvas.getContext('2d');
  ctx.save();
  if (ts === 'left') {
    ctx.translate(canvas.width, 0);
    ctx.rotate(Math.PI / 2);
    ctx.drawImage(pageImage, sx1, sy1, cropW, cropH, 0, 0, canvas.height, canvas.width);
  } else if (ts === 'right') {
    ctx.translate(0, canvas.height);
    ctx.rotate(-Math.PI / 2);
    ctx.drawImage(pageImage, sx1, sy1, cropW, cropH, 0, 0, canvas.height, canvas.width);
  } else if (ts === 'bottom') {
    ctx.translate(canvas.width, canvas.height);
    ctx.rotate(Math.PI);
    ctx.drawImage(pageImage, sx1, sy1, cropW, cropH, 0, 0, canvas.width, canvas.height);
  } else {
    ctx.drawImage(pageImage, sx1, sy1, cropW, cropH, 0, 0, canvas.width, canvas.height);
  }
  ctx.restore();
}

const TOP_SIDES = ['top', 'right', 'bottom', 'left'];
function rotatePhoto(idx, dir) {
  const photo = metadata.pages[currentPageIdx].photos[idx];
  const cur = TOP_SIDES.indexOf(photo.top_side || 'top');
  photo.top_side = TOP_SIDES[(cur + dir + 4) % 4];
  dirty = true;
  const card = document.getElementById(`card-${idx}`);
  if (card) {
    const canvas = card.querySelector('canvas');
    renderPreview(canvas, photo);
  }
}

// =========================================================================
// Map
// =========================================================================
function loadLeaflet() {
  return new Promise((resolve) => {
    if (leafletLoaded) { resolve(); return; }
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
    document.head.appendChild(css);
    const js = document.createElement('script');
    js.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    js.onload = () => { leafletLoaded = true; resolve(); };
    document.head.appendChild(js);
  });
}

async function openMap(photoIdx) {
  mapPhotoIdx = photoIdx;
  const photo = metadata.pages[currentPageIdx].photos[photoIdx];
  document.getElementById('map-modal').classList.add('active');
  await loadLeaflet();

  if (!map) {
    map = L.map('map-container').setView([45, 60], 4);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '\u00a9 OpenStreetMap'
    }).addTo(map);
    map.on('click', (e) => setMapMarker(e.latlng.lat, e.latlng.lng));
  }

  if (photo.location) {
    const parts = photo.location.split(',').map(s => parseFloat(s.trim()));
    if (parts.length === 2 && !isNaN(parts[0]) && !isNaN(parts[1])) {
      map.setView(parts, 12);
      setMapMarker(parts[0], parts[1]);
    }
  } else {
    if (mapMarker) { map.removeLayer(mapMarker); mapMarker = null; }
    document.getElementById('map-coords').textContent = 'Click on map or search to select a location';
  }

  setTimeout(() => map.invalidateSize(), 100);
  document.getElementById('map-search').value = '';
  document.getElementById('map-search').focus();
}

function setMapMarker(lat, lng) {
  if (mapMarker) map.removeLayer(mapMarker);
  mapMarker = L.marker([lat, lng]).addTo(map);
  document.getElementById('map-coords').textContent = `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
}

async function searchMap() {
  const q = document.getElementById('map-search').value.trim();
  if (!q) return;
  const coordMatch = q.match(/^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$/);
  if (coordMatch) {
    const lat = parseFloat(coordMatch[1]), lng = parseFloat(coordMatch[2]);
    if (lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180) {
      map.setView([lat, lng], 14);
      setMapMarker(lat, lng);
      return;
    }
  }
  try {
    const resp = await fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(q)}&limit=1`);
    const data = await resp.json();
    if (data.length > 0) {
      const lat = parseFloat(data[0].lat), lng = parseFloat(data[0].lon);
      map.setView([lat, lng], 14);
      setMapMarker(lat, lng);
    } else {
      document.getElementById('map-coords').textContent = 'Location not found';
    }
  } catch (e) {
    document.getElementById('map-coords').textContent = 'Search error: ' + e.message;
  }
}

function closeMap() {
  document.getElementById('map-modal').classList.remove('active');
}

async function applyMapLocation() {
  if (!mapMarker || mapPhotoIdx < 0) { closeMap(); return; }
  const lat = mapMarker.getLatLng().lat;
  const lng = mapMarker.getLatLng().lng;
  const photo = metadata.pages[currentPageIdx].photos[mapPhotoIdx];
  const oldLoc = photo.location;
  photo.location = `${lat.toFixed(6)}, ${lng.toFixed(6)}`;

  try {
    const resp = await fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lng}`);
    const data = await resp.json();
    if (data.address) {
      const city = data.address.city || data.address.town || data.address.village || data.address.state || '';
      const country = data.address.country || '';
      photo.location_name = [city, country].filter(Boolean).join(', ') || data.display_name;
    } else {
      photo.location_name = `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
    }
  } catch (e) {
    photo.location_name = `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
  }

  const display = document.getElementById(`loc-display-${mapPhotoIdx}`);
  if (display) display.textContent = photo.location_name;
  dirty = true;
  userEditedLocations.add(currentPageIdx + ':' + mapPhotoIdx);
  showLocationSuggestions(mapPhotoIdx, oldLoc, photo.location, photo.location_name);
  closeMap();
}

function showLocationSuggestions(changedIdx, oldLoc, newLoc, newLocName) {
  if (!newLoc || newLoc === oldLoc) return;
  const page = metadata.pages[currentPageIdx];
  page.photos.forEach((photo, i) => {
    if (i === changedIdx || photo.skip) return;
    const hint = document.getElementById(`loc-hint-${i}`);
    if (!hint) return;
    const dominated = (photo.location || null) === (oldLoc || null) || hint.style.display === 'block';
    if (dominated && photo.location !== newLoc) {
      hint.innerHTML = '';
      const a = document.createElement('a');
      a.textContent = `Use ${newLocName}`;
      a.onclick = () => {
        photo.location = newLoc;
        photo.location_name = newLocName;
        dirty = true;
        userEditedLocations.add(currentPageIdx + ':' + i);
        const display = document.getElementById(`loc-display-${i}`);
        if (display) display.textContent = newLocName;
        hint.style.display = 'none';
      };
      hint.appendChild(a);
      hint.style.display = 'block';
    }
  });
}

// =========================================================================
// Save / Apply
// =========================================================================
function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + (type || '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 3000);
}

async function saveDraft() {
  try {
    await fetch('/api/metadata', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(metadata) });
    dirty = false;
    showToast('Draft saved!', 'success');
  } catch (e) {
    showToast('Save failed: ' + e.message, 'error');
  }
}

async function saveAndApply() {
  try {
    await fetch('/api/metadata', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(metadata) });
    showToast('Metadata saved. Applying...', 'success');
    const resp = await fetch('/api/apply', { method: 'POST' });
    const result = await resp.json();
    if (result.status === 'ok') {
      dirty = false;
      showToast(`Done! ${result.count} photo(s) saved to ${result.output}`, 'success');
    } else {
      showToast('Apply failed: ' + (result.error || 'unknown error'), 'error');
    }
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// =========================================================================
// Keyboard
// =========================================================================
document.addEventListener('keydown', (e) => {
  const tag = document.activeElement.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;

  if (e.key === 'ArrowLeft') { navigateToPage(currentPageIdx - 1); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { navigateToPage(currentPageIdx + 1); e.preventDefault(); }
  else if (e.key === 'Escape') { closeMap(); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && selectedBox >= 0) { deleteSelected(); }
});

// =========================================================================
// Unsaved changes guard
// =========================================================================
window.addEventListener('beforeunload', (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ''; }
});

// =========================================================================
// Boot
// =========================================================================
init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Empty metadata generation
# ---------------------------------------------------------------------------

def _enrich_from_exif(data: dict, input_dir: Path) -> None:
    """Fill empty date/location/caption on photos from source image EXIF."""
    try:
        from edit_meta import read_file_metadata
    except ImportError:
        return

    for page in data.get("pages", []):
        source = page.get("source")
        if not source:
            continue
        # Check if any photo needs enrichment
        photos = page.get("photos", [])
        needs = any(
            not p.get("date") or not p.get("location") or not p.get("caption")
            for p in photos if not p.get("skip")
        )
        if not needs:
            continue

        img_path = input_dir / source
        if not img_path.exists():
            continue
        try:
            exif = read_file_metadata(str(img_path))
        except Exception:
            continue

        exif_date = exif.get("date")
        exif_gps = exif.get("gps")
        exif_caption = exif.get("caption")
        exif_location = f"{exif_gps[0]:.6f}, {exif_gps[1]:.6f}" if exif_gps else None

        for photo in photos:
            if photo.get("skip"):
                continue
            if not photo.get("date") and exif_date:
                photo["date"] = exif_date
            if not photo.get("location") and exif_location:
                photo["location"] = exif_location
            if not photo.get("caption") and exif_caption:
                photo["caption"] = exif_caption


def _generate_empty_metadata(input_dir: Path) -> dict:
    """Generate default metadata when autocrop_meta.json doesn't exist.

    Scans for images, creates one full-page photo per image, and pre-fills
    fields from EXIF if available.
    """
    try:
        from edit_meta import read_file_metadata
    except ImportError:
        read_file_metadata = None

    image_files = sorted(
        f for f in input_dir.iterdir()
        if f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
    )

    pages = []
    for img_file in image_files:
        photo = {
            "bbox": [0, 0, 100, 100],
            "top_side": "top",
            "date": None,
            "location": None,
            "location_name": None,
            "caption": None,
            "skip": False,
        }

        # Try reading EXIF
        if read_file_metadata:
            try:
                exif = read_file_metadata(str(img_file))
                if exif.get("date"):
                    photo["date"] = exif["date"]
                if exif.get("gps"):
                    lat, lon = exif["gps"]
                    photo["location"] = f"{lat:.6f}, {lon:.6f}"
                if exif.get("caption"):
                    photo["caption"] = exif["caption"]
            except Exception:
                pass

        pages.append({"source": img_file.name, "photos": [photo]})

    return {"version": 2, "pages": pages}


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class EditorHandler(BaseHTTPRequestHandler):
    input_dir: Path
    output_dir: str
    meta_path: Path

    def log_message(self, fmt, *args):
        pass  # silence default logging

    def handle(self):
        try:
            super().handle()
        except BrokenPipeError:
            pass

    def _send(self, code: int, body: bytes, content_type: str = "text/html"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(code, body, "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")

        elif path == "/api/metadata":
            try:
                if self.meta_path.exists():
                    with open(self.meta_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    _enrich_from_exif(data, self.input_dir)
                else:
                    data = _generate_empty_metadata(self.input_dir)
                self._send_json(data)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/reverse-geocode":
            coords = qs.get("coords", [None])[0]
            if not coords:
                self._send_json({"error": "missing coords"}, 400)
                return
            try:
                lat, lon = [x.strip() for x in coords.split(",")]
                params = urllib.parse.urlencode({
                    "lat": lat, "lon": lon, "format": "json", "zoom": 10,
                    "accept-language": "ru,en",
                })
                url = f"https://nominatim.openstreetmap.org/reverse?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "autocrop-editor/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                addr = data.get("address", {})
                parts = []
                for key in ("city", "town", "village", "county", "state", "country"):
                    if key in addr:
                        parts.append(addr[key])
                        if len(parts) >= 2:
                            break
                name = ", ".join(parts) if parts else data.get("display_name", coords)
                self._send_json({"name": name})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path.startswith("/images/"):
            filename = urllib.parse.unquote(path[len("/images/"):])
            filepath = self.input_dir / filename
            if not filepath.exists():
                self._send(404, b"Not found")
                return

            # Optional downscale
            max_w = int(qs.get("w", [0])[0])
            if max_w > 0:
                try:
                    img = Image.open(filepath)
                    if img.width > max_w:
                        ratio = max_w / img.width
                        new_h = int(img.height * ratio)
                        img = img.resize((max_w, new_h), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=85)
                    self._send(200, buf.getvalue(), "image/jpeg")
                    return
                except Exception:
                    pass  # fallback to serving raw file

            mime = mimetypes.guess_type(str(filepath))[0] or "application/octet-stream"
            with open(filepath, "rb") as f:
                self._send(200, f.read(), mime)
        else:
            self._send(404, b"Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b""

        if path == "/api/metadata":
            try:
                data = json.loads(body)
                with open(self.meta_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self._send_json({"status": "ok"})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/apply":
            try:
                result = subprocess.run(
                    [sys.executable, "crop_exif.py", str(self.input_dir),
                     "-o", self.output_dir],
                    capture_output=True, text=True, cwd=str(Path(__file__).parent),
                )
                # Count photos from output
                lines = result.stdout.strip().split("\n")
                count_line = [l for l in lines if "Extracted" in l]
                count = 0
                if count_line:
                    import re
                    m = re.search(r"(\d+) photo", count_line[-1])
                    if m:
                        count = int(m.group(1))
                if result.returncode != 0:
                    self._send_json({"status": "error", "error": result.stderr or result.stdout}, 500)
                else:
                    self._send_json({"status": "ok", "count": count, "output": self.output_dir})
            except Exception as e:
                self._send_json({"status": "error", "error": str(e)}, 500)
        else:
            self._send(404, b"Not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Web-based metadata editor for autocrop")
    parser.add_argument("input", help="Directory containing scanned pages and autocrop_meta.json")
    parser.add_argument("-o", "--output", default=None, help="Output directory for cropped photos")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)), help="HTTP server port (default: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    if not input_dir.is_dir():
        print(f"Error: {input_dir} is not a directory", file=sys.stderr)
        sys.exit(1)
    meta_path = input_dir / METADATA_FILENAME
    if not meta_path.exists():
        print(f"Note: {METADATA_FILENAME} not found, starting with empty metadata")

    output_dir = args.output or str(input_dir / "cropped")

    # Configure handler
    EditorHandler.input_dir = input_dir
    EditorHandler.output_dir = output_dir
    EditorHandler.meta_path = meta_path

    port = args.port
    while True:
        try:
            server = HTTPServer(("localhost", port), EditorHandler)
            break
        except OSError:
            print(f"Port {port} is busy, trying {port + 1}...")
            port += 1
    url = f"http://localhost:{port}"
    print(f"Autocrop Editor running at {url}")
    print(f"  Input:  {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
