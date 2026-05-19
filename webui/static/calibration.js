/* ===================================================================
 * calibration.js — Écran calibration.
 * Remplace les SVG mockés des 3 feeds par des <img> MJPEG live,
 * et branche les boutons capture/recalib au backend.
 *
 * La checklist locale (toggle visuel) est conservée à l'identique :
 * la calibration métier (homographie, RANSAC) n'est pas exposée par
 * le backend actuel ; en STEP 3 on garde le comportement démo et on
 * note l'amélioration future dans webui/FUTURE.md.
 * ================================================================= */

'use strict';

(function () {
  const { ws, $, $$, esc } = window.DartApp;

  // ─── Remplace les SVG mockés par des <img src="/api/cam/.../mjpeg"> ──
  $$('.cam-feed__svg').forEach(svgEl => {
    const slot = svgEl.dataset.feed;        // "A" | "B" | "C"
    if (!slot) return;
    const img = document.createElement('img');
    img.alt = `Flux Cam-${slot}`;
    img.src = `/api/cam/${slot}/mjpeg`;
    // Reprend la classe pour conserver le styling de la maquette.
    img.className = 'cam-feed__svg';
    img.style.width = '100%';
    img.style.height = '100%';
    img.style.objectFit = 'cover';
    img.style.display = 'block';
    svgEl.parentNode.replaceChild(img, svgEl);
  });

  // ─── Hydratation des métadonnées par cam depuis system_status ──────
  function applySystemStatus(status) {
    if (!status || !status.cams) return;
    status.cams.forEach(cam => {
      const feed = $(`.cam-feed[data-cam="${cam.id}"]`);
      if (!feed) return;
      // Mise à jour du segment + master flag dans le footer
      const segEl = $('.cam-feed__seg', feed);
      if (segEl) {
        const masterTag = cam.master ? ' · Master' : '';
        segEl.textContent = `Face · Seg ${cam.seg ?? '?'}${masterTag}`;
      }
      // Indicateur ok/ko sur le bord
      feed.classList.toggle('is-down', !cam.ok);
      // Label "60 FPS" → fps réelle
      const lbl = $('.cam-feed__label', feed);
      if (lbl) lbl.textContent = `CAM-${cam.id}${cam.master ? ' · MASTER' : ''} · ${cam.fps || '?'} FPS`;
    });

    // Résiduel RANSAC
    const residEl = $('.calib-residual__val');
    if (residEl && status.calibration) {
      residEl.textContent = (status.calibration.residual_mm || 0).toFixed(2);
    }
  }

  ws.on('system_status', applySystemStatus);
  // Snapshot initial peut aussi contenir un système (déjà poussé séparément
  // mais on rejoue le dernier connu pour réactivité).
  ws.on('_status', ({ connected }) => {
    if (connected && ws.lastStatus) applySystemStatus(ws.lastStatus);
  });

  // ─── Checklist locale (toggle visuel uniquement, comme démo) ───────
  $$('.checklist__row').forEach(row => {
    const box = $('.checklist__box', row);
    const status = $('.checklist__status', row);
    box.addEventListener('click', () => {
      const done = row.classList.toggle('is-done');
      box.setAttribute('aria-checked', done);
      box.textContent = done ? '✓' : '';
      status.textContent = done ? 'OK' : 'À faire';
      updateChecklistCount();
    });
  });

  function updateChecklistCount() {
    const total = $$('.checklist__row').length;
    const ok    = $$('.checklist__row.is-done').length;
    const el = $('#checklistProgress');
    if (el) el.textContent = `${ok} / ${total} OK`;
  }
  updateChecklistCount();

  // ─── Boutons d'action ──────────────────────────────────────────────
  $('#captureBtn')?.addEventListener('click', () => {
    ws.send('capture_reference');
    // Flash visuel sur les 3 feeds (reprise du comportement démo)
    $$('.cam-feed').forEach(c => {
      c.style.outline = '3px solid var(--pink, #ff5b9c)';
      setTimeout(() => c.style.outline = '', 350);
    });
  });

  $('#recalibBtn')?.addEventListener('click', () => {
    ws.send('start_recalibration');
    // Reset visuel de la checklist (comportement démo conservé)
    $$('.checklist__row').forEach(r => {
      r.classList.remove('is-done');
      const box = $('.checklist__box', r);
      box.textContent = '';
      box.setAttribute('aria-checked', 'false');
      $('.checklist__status', r).textContent = 'À faire';
    });
    updateChecklistCount();
  });
})();
