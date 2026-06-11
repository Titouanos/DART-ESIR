/* ===================================================================
 * recalib.js — Modal recalibration WEB partagé entre /calibration et /game.
 *
 * Flow par cam (A → B → C) :
 *   1. Affiche le flux RAW (pas warpé) plein écran
 *   2. User clique 4 points : Triple 20, 6, 3, 11
 *   3. POST /api/calibration/compute → preview warpée
 *   4. User confirme → POST /api/calibration/save → reload backend
 *   5. Passe à la cam suivante, ou propose de recapturer la référence
 *
 * Expose : window.DartApp.openRecalibModal()
 * Dépend de : window.DartApp.{ws, $, $$, esc} fournis par app.js
 * ================================================================= */

'use strict';

(function () {
  const { ws, $, $$ } = window.DartApp;

  const CALIB_POINT_LABELS = [
    'Triple 20 (HAUT)',
    'Triple 6 (DROITE)',
    'Triple 3 (BAS)',
    'Triple 11 (GAUCHE)',
  ];
  const CALIB_POINT_COLORS = [
    'var(--pink, #ff5b9c)',
    'var(--blue, #2e3aa8)',
    'var(--ink, #171717)',
    'var(--pink, #ff5b9c)',
  ];
  const RECALIB_SLOTS = ['A', 'B', 'C'];

  let recalibState = null;
  let cssInjected = false;

  function injectCSS() {
    if (cssInjected) return;
    cssInjected = true;
    const css = `
      .recalib-overlay {
        position: fixed; inset: 0; z-index: 9000;
        background: rgba(15, 15, 18, 0.92);
        display: flex; flex-direction: column;
        font-family: var(--f-mono);
      }
      .recalib-head {
        display: grid;
        grid-template-columns: 1fr auto 1fr;
        align-items: center; gap: 16px;
        padding: 14px 22px;
        background: var(--paper);
        border-bottom: 2px solid var(--ink);
      }
      .recalib-head__title {
        font-family: var(--f-display);
        font-weight: 900; font-size: 22px;
        text-transform: uppercase; letter-spacing: 0.04em;
      }
      .recalib-head__step {
        text-align: center; font-size: 12px;
        letter-spacing: 0.18em; text-transform: uppercase;
        color: var(--ink-soft);
      }
      .recalib-head__step strong { color: var(--ink); font-weight: 700; }
      .recalib-close {
        justify-self: end;
        background: var(--ink); color: var(--paper);
        border: none; padding: 8px 16px;
        font-family: var(--f-mono); font-size: 12px;
        letter-spacing: 0.18em; text-transform: uppercase;
        cursor: pointer;
      }
      .recalib-close:hover { background: var(--danger, #c8341a); }

      .recalib-body {
        flex: 1; display: grid;
        grid-template-columns: 1fr 360px;
        gap: 18px; padding: 18px;
        min-height: 0;
      }
      .recalib-canvas {
        position: relative; background: #000;
        border: 1.5px solid var(--ink);
        display: flex; align-items: center; justify-content: center;
        overflow: hidden; min-height: 0;
      }
      .recalib-imgbox {
        position: relative; display: inline-block;
        line-height: 0; cursor: crosshair;
      }
      .recalib-imgbox img {
        display: block;
        max-width: calc(100vw - 460px);
        max-height: calc(100vh - 100px);
      }
      .recalib-imgbox svg {
        position: absolute; inset: 0;
        width: 100%; height: 100%; pointer-events: none;
      }

      .recalib-side {
        background: var(--paper);
        border: 1.5px solid var(--ink);
        padding: 18px;
        display: flex; flex-direction: column; gap: 14px;
        overflow-y: auto;
      }
      .recalib-instructions {
        font-family: var(--f-display);
        font-weight: 800; font-size: 24px;
        text-transform: uppercase; line-height: 1.1;
      }
      .recalib-instructions__sub {
        margin-top: 6px;
        font-family: var(--f-mono);
        font-size: 11px; letter-spacing: 0.15em;
        color: var(--ink-soft); text-transform: none;
      }
      .recalib-points {
        display: grid; gap: 8px;
        font-size: 12px; letter-spacing: 0.12em;
        text-transform: uppercase;
      }
      .recalib-points__row {
        display: grid; grid-template-columns: 32px 1fr auto;
        gap: 10px; align-items: center;
        padding: 8px 10px; background: rgba(0,0,0,0.05);
        opacity: 0.5;
      }
      .recalib-points__row.is-current { opacity: 1; background: var(--ink); color: var(--paper); }
      .recalib-points__row.is-done    { opacity: 0.85; background: rgba(46,58,168,0.20); }
      .recalib-points__row .dot {
        width: 16px; height: 16px; border-radius: 50%;
        border: 1.5px solid currentColor;
      }
      .recalib-actions { display: flex; gap: 10px; flex-wrap: wrap; }
      .recalib-actions .btn {
        flex: 1; min-width: 140px;
        padding: 12px 16px;
        font-family: var(--f-condensed, sans-serif);
        font-size: 16px; letter-spacing: 0.08em;
        text-transform: uppercase;
        background: var(--paper); color: var(--ink);
        border: 1.5px solid var(--ink);
        cursor: pointer;
      }
      .recalib-actions .btn--primary { background: var(--ink); color: var(--paper); }
      .recalib-actions .btn--danger  { background: var(--danger, #c8341a); color: var(--paper); border-color: var(--danger, #c8341a); }
      .recalib-actions .btn:disabled { opacity: 0.4; cursor: not-allowed; }
      .recalib-preview {
        border: 1.5px solid var(--ink); background: #000;
        padding: 4px; margin-top: 8px;
      }
      .recalib-preview img { width: 100%; display: block; }
      .recalib-state {
        font-size: 11px; letter-spacing: 0.15em;
        text-transform: uppercase;
        color: var(--ink-soft);
        text-align: center; min-height: 14px;
      }
      .recalib-state.is-error { color: var(--danger, #c8341a); }
      .recalib-state.is-ok    { color: var(--success, #1e7a3a); }
      @media (max-width: 1100px) {
        .recalib-body { grid-template-columns: 1fr; }
        .recalib-side { max-height: 40vh; }
      }
    `;
    const s = document.createElement('style');
    s.id = 'recalib-css';
    s.textContent = css;
    document.head.appendChild(s);
  }

  function openRecalibModal() {
    if (document.getElementById('recalib-overlay')) return;   // déjà ouvert
    injectCSS();
    recalibState = { slotIdx: 0, points: [], lastCompute: null,
                     busy: false, imgReady: false };
    const overlay = document.createElement('div');
    overlay.className = 'recalib-overlay';
    overlay.id = 'recalib-overlay';
    overlay.innerHTML = `
      <div class="recalib-head">
        <span class="recalib-head__title">Recalibration</span>
        <span class="recalib-head__step" id="recalib-step">CAM <strong>A</strong> · 1 SUR 3</span>
        <button class="recalib-close" id="recalib-close-btn" type="button">Annuler ✕</button>
      </div>
      <div class="recalib-body">
        <div class="recalib-canvas" id="recalib-canvas">
          <div class="recalib-imgbox" id="recalib-imgbox">
            <img id="recalib-img" alt="Flux RAW">
            <svg id="recalib-overlay-svg" xmlns="http://www.w3.org/2000/svg"></svg>
          </div>
        </div>
        <aside class="recalib-side">
          <div>
            <div class="recalib-instructions" id="recalib-instr">CLIQUE LE TRIPLE 20</div>
            <div class="recalib-instructions__sub" id="recalib-instr-sub">
              C'est le centre du segment 20 sur l'anneau triple, partie haute.
              Vise le COIN EXTÉRIEUR du double ring (le fil le plus large) au pixel près.
            </div>
          </div>
          <div class="recalib-points" id="recalib-points-list"></div>
          <div class="recalib-actions">
            <button class="btn btn--danger" id="recalib-restart" type="button">↺ Recommencer cette cam</button>
            <button class="btn" id="recalib-confirm" type="button" disabled>✓ Confirmer & passer</button>
          </div>
          <div class="recalib-state" id="recalib-state"></div>
          <div id="recalib-preview-wrap" style="display:none">
            <div class="recalib-instructions__sub">PREVIEW (board attendu en cyan) :</div>
            <div class="recalib-preview"><img id="recalib-preview-img" alt="Preview warpée"></div>
          </div>
        </aside>
      </div>
    `;
    document.body.appendChild(overlay);

    setupSlot(0);

    $('#recalib-close-btn').addEventListener('click', closeModal);
    $('#recalib-imgbox').addEventListener('click', onCanvasClick);
    $('#recalib-restart').addEventListener('click', restartCurrentSlot);
    $('#recalib-confirm').addEventListener('click', confirmCurrentSlot);
    document.addEventListener('keydown', keyHandler);
    $('#recalib-img').addEventListener('load', redrawOverlay);
  }

  function keyHandler(e) {
    if (!document.getElementById('recalib-overlay')) return;
    if (e.key === 'Escape') closeModal();
  }

  function closeModal() {
    const o = document.getElementById('recalib-overlay');
    if (o) o.remove();
    document.removeEventListener('keydown', keyHandler);
    recalibState = null;
  }

  function setupSlot(slotIdx) {
    recalibState.slotIdx = slotIdx;
    recalibState.points = [];
    recalibState.lastCompute = null;
    recalibState.busy = true;
    recalibState.imgReady = false;
    const slot = RECALIB_SLOTS[slotIdx];
    const img = $('#recalib-img');

    try { img.removeAttribute('src'); } catch {}
    img.src = '';
    setTimeout(() => {
      const ts = Date.now();
      img.src = `/api/cam/${slot}/raw.mjpeg?t=${ts}`;
    }, 50);

    const stepEl = $('#recalib-step');
    if (stepEl) {
      stepEl.innerHTML = `CAM <strong>${slot}</strong> · ${slotIdx + 1} SUR ${RECALIB_SLOTS.length}`;
    }
    updateInstr(0);
    updatePointsList();
    $('#recalib-confirm').disabled = true;
    $('#recalib-confirm').classList.remove('btn--primary');
    $('#recalib-confirm').textContent = '✓ Confirmer & passer';
    $('#recalib-preview-wrap').style.display = 'none';
    setState(`Chargement du flux Cam-${slot}…`);

    const svg = $('#recalib-overlay-svg');
    if (svg) svg.innerHTML = '';

    waitForImgReady(slot);
  }

  function waitForImgReady(slot) {
    const img = $('#recalib-img');
    let tries = 0;
    const MAX_TRIES = 100;
    const timer = setInterval(() => {
      tries++;
      if (!recalibState || recalibState.slotIdx == null
          || RECALIB_SLOTS[recalibState.slotIdx] !== slot) {
        clearInterval(timer);
        return;
      }
      if (img.naturalWidth > 0 && img.naturalHeight > 0) {
        clearInterval(timer);
        recalibState.imgReady = true;
        recalibState.busy = false;
        setState('');
        redrawOverlay();
      } else if (tries >= MAX_TRIES) {
        clearInterval(timer);
        setState(`Timeout : flux Cam-${slot} n'a pas chargé. Vérifie main.py.`, 'error');
      }
    }, 100);
  }

  function updateInstr(pointIdx) {
    const instr = $('#recalib-instr');
    const sub = $('#recalib-instr-sub');
    if (pointIdx < 4) {
      instr.textContent = `CLIQUE ${CALIB_POINT_LABELS[pointIdx]}`;
      sub.textContent =
        'Vise le coin EXTÉRIEUR du double-ring (le fil le plus large) — '
        + 'tu peux zoomer Ctrl+molette si besoin.';
    } else {
      instr.textContent = '4 POINTS POSÉS';
      sub.textContent = 'Vérifie la preview à droite. "Confirmer" sauvegarde + passe à la cam suivante.';
    }
  }

  function updatePointsList() {
    const list = $('#recalib-points-list');
    if (!list) return;
    list.innerHTML = CALIB_POINT_LABELS.map((label, i) => {
      const isDone = i < recalibState.points.length;
      const isCurrent = i === recalibState.points.length;
      const cls = ['recalib-points__row'];
      if (isDone)    cls.push('is-done');
      if (isCurrent) cls.push('is-current');
      const color = CALIB_POINT_COLORS[i];
      return `
        <div class="${cls.join(' ')}">
          <span class="dot" style="background:${color}"></span>
          <span>${label}</span>
          <span>${isDone ? '✓' : (isCurrent ? '→' : '·')}</span>
        </div>
      `;
    }).join('');
  }

  function onCanvasClick(e) {
    if (!recalibState) return;
    if (recalibState.points.length >= 4) return;
    if (recalibState.busy) {
      if (!recalibState.imgReady) setState('Patiente, flux pas encore prêt…');
      return;
    }
    const img = $('#recalib-img');
    const rect = img.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) {
      setState('Image pas affichée — bug layout, ouvre la console.', 'error');
      return;
    }
    const nw = img.naturalWidth, nh = img.naturalHeight;
    if (!nw || !nh) {
      setState('Flux pas chargé (naturalWidth=0). Refresh la page.', 'error');
      return;
    }
    const dx = e.clientX - rect.left;
    const dy = e.clientY - rect.top;
    if (dx < 0 || dx > rect.width || dy < 0 || dy > rect.height) return;
    const nx = dx * (nw / rect.width);
    const ny = dy * (nh / rect.height);
    recalibState.points.push([nx, ny]);
    redrawOverlay();
    updatePointsList();
    updateInstr(recalibState.points.length);
    if (recalibState.points.length === 4) computeCurrentSlot();
  }

  function redrawOverlay() {
    const svg = $('#recalib-overlay-svg');
    const img = $('#recalib-img');
    if (!svg || !img) return;
    const nw = img.naturalWidth, nh = img.naturalHeight;
    if (!nw || !nh) return;
    svg.setAttribute('viewBox', `0 0 ${nw} ${nh}`);
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    if (!recalibState) { svg.innerHTML = ''; return; }
    let html = '';
    recalibState.points.forEach((pt, i) => {
      const color = CALIB_POINT_COLORS[i];
      const r = Math.max(8, nw / 80);
      const stroke = Math.max(1.5, nw / 600);
      const fontSize = Math.max(12, nw / 50);
      html += `
        <circle cx="${pt[0]}" cy="${pt[1]}" r="${r}"
                fill="none" stroke="${color}" stroke-width="${stroke * 1.5}"/>
        <circle cx="${pt[0]}" cy="${pt[1]}" r="${r * 0.3}" fill="${color}"/>
        <text x="${pt[0] + r + 6}" y="${pt[1] + fontSize * 0.35}"
              fill="${color}" stroke="rgba(0,0,0,0.4)" stroke-width="0.4"
              font-family="IBM Plex Mono"
              font-size="${fontSize}" font-weight="700">${i + 1}</text>
      `;
    });
    if (recalibState.points.length >= 2) {
      const dashUnit = Math.max(4, nw / 300);
      for (let i = 0; i < recalibState.points.length - 1; i++) {
        const a = recalibState.points[i], b = recalibState.points[i + 1];
        html += `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}"
                       stroke="#fff" stroke-width="${Math.max(1, nw/800)}"
                       stroke-dasharray="${dashUnit} ${dashUnit}"
                       opacity="0.5"/>`;
      }
      if (recalibState.points.length === 4) {
        const a = recalibState.points[3], b = recalibState.points[0];
        html += `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}"
                       stroke="#fff" stroke-width="${Math.max(1, nw/800)}"
                       stroke-dasharray="${dashUnit} ${dashUnit}"
                       opacity="0.5"/>`;
      }
    }
    svg.innerHTML = html;
  }

  window.addEventListener('resize', () => {
    if (recalibState) redrawOverlay();
  });

  async function computeCurrentSlot() {
    if (!recalibState) return;
    recalibState.busy = true;
    setState('CALCUL EN COURS…', '');
    const slot = RECALIB_SLOTS[recalibState.slotIdx];
    const img = $('#recalib-img');
    const nw = img.naturalWidth || 1280;
    const nh = img.naturalHeight || 720;
    try {
      const res = await fetch('/api/calibration/compute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          slot, points: recalibState.points,
          display_w: nw, display_h: nh,
          raw_w: nw, raw_h: nh,
        }),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const errJson = await res.json();
          if (errJson.detail) {
            detail = typeof errJson.detail === 'string'
              ? errJson.detail
              : JSON.stringify(errJson.detail);
          }
        } catch {}
        throw new Error(detail);
      }
      const data = await res.json();
      recalibState.lastCompute = data;
      $('#recalib-preview-img').src = 'data:image/jpeg;base64,' + data.preview_b64;
      $('#recalib-preview-wrap').style.display = 'block';
      $('#recalib-confirm').disabled = false;
      $('#recalib-confirm').classList.add('btn--primary');
      setState(`Segment détecté: ${data.cam_position_segment}. Vérifie la preview avant Confirmer.`, 'ok');
    } catch (e) {
      console.error('[recalib] compute failed:', e);
      setState(`Erreur compute: ${e.message}`, 'error');
      recalibState.points = [];
      updatePointsList();
      updateInstr(0);
      redrawOverlay();
    } finally {
      recalibState.busy = false;
    }
  }

  function restartCurrentSlot() {
    if (!recalibState) return;
    setupSlot(recalibState.slotIdx);
  }

  async function confirmCurrentSlot() {
    if (!recalibState || !recalibState.lastCompute) return;
    recalibState.busy = true;
    setState('Sauvegarde…', '');
    const slot = RECALIB_SLOTS[recalibState.slotIdx];
    try {
      const res = await fetch('/api/calibration/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          slot,
          homography: recalibState.lastCompute.homography,
          cam_position_segment: recalibState.lastCompute.cam_position_segment,
          points: recalibState.lastCompute.points,
        }),
      });
      if (!res.ok) throw new Error('save failed');
      const data = await res.json();
      setState(`Sauvegardé. ${data.reloaded ? '✓ Reloaded' : '⚠ Reload requis'}.`, 'ok');
    } catch (e) {
      setState(`Erreur save: ${e.message}`, 'error');
      recalibState.busy = false;
      return;
    }
    $('#recalib-confirm').disabled = true;
    const next = recalibState.slotIdx + 1;
    if (next < RECALIB_SLOTS.length) {
      setTimeout(() => {
        try { setupSlot(next); }
        catch (e) {
          console.error('[recalib] setupSlot crash:', e);
          setState(`Crash transition: ${e.message}`, 'error');
        }
      }, 600);
    } else {
      setState('Recalibration des 3 cams terminée — Pense à recapturer la référence.', 'ok');
      setTimeout(() => {
        closeModal();
        if (window.confirm('Recapturer la référence maintenant (board doit être vide) ?')) {
          ws.send('capture_reference');
        }
      }, 1500);
    }
  }

  function setState(text, kind) {
    const el = $('#recalib-state');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'recalib-state'
      + (kind === 'error' ? ' is-error' : '')
      + (kind === 'ok' ? ' is-ok' : '');
  }

  // Expose au reste de l'app
  window.DartApp.openRecalibModal = openRecalibModal;
})();
