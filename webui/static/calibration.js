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

  // Le design du zip applique DEUX couches de décoration sur chaque cam-feed :
  //   1. `.cam-feed::before`  → offset-shadow décalé (top:5/left:-6/bottom:-7)
  //      avec mix-blend-mode multiply et couleur pink/blue/ink (le 3e en noir
  //      à 45% opacity). Crée la "zone noire dépassant sous la card".
  //   2. `.cam-feed__view::after` → overlay riso teinté SUR le flux.
  // Les deux faisaient sens sur des SVG mockés clairs ; sur un vrai flux MJPEG
  // sombre, ça écrase l'image en noir. On désactive les deux via `.is-live`.
  (function injectCalibCSS() {
    const css = `
      .cam-feed.is-live::before { display: none !important; }
      .cam-feed.is-live .cam-feed__view::after { display: none !important; }
      .cam-feed.is-live img.cam-feed__svg {
        width: 100%; height: 100%; display: block; object-fit: cover;
      }
    `;
    const s = document.createElement('style');
    s.textContent = css;
    document.head.appendChild(s);
  })();

  // ─── Remplace les SVG mockés par des <img src="/api/cam/.../mjpeg"> ──
  const camImgs = [];   // [{slot, img}], pour pouvoir toggler debug ensuite
  $$('.cam-feed__svg').forEach(svgEl => {
    const slot = svgEl.dataset.feed;        // "A" | "B" | "C"
    if (!slot) return;
    const img = document.createElement('img');
    img.alt = `Flux Cam-${slot}`;
    img.dataset.slot = slot;
    img.src = `/api/cam/${slot}/mjpeg`;
    img.className = 'cam-feed__svg';   // garde la classe pour le sizing
    svgEl.parentNode.replaceChild(img, svgEl);
    camImgs.push({ slot, img });
    // Marque le parent .cam-feed comme "live" → désactive l'overlay riso
    const feed = svgEl.closest ? svgEl.closest('.cam-feed') : null;
    if (feed) feed.classList.add('is-live');
    else {
      // Fallback : remonte manuellement
      let p = img.parentElement;
      while (p && !p.classList.contains('cam-feed')) p = p.parentElement;
      if (p) p.classList.add('is-live');
    }
  });

  // ─── Bouton toggle debug : switch les <img> entre /mjpeg et /debug.mjpeg ──
  // Le flux debug annote chaque frame avec : état détecteur, contour candidat,
  // ray (axe fléchette), tip détecté, derniers impacts confirmés, mini masque
  // de diff. Précieux pour comprendre pourquoi la détection part en cacahuète.
  function swapStreams(useDebug) {
    const cacheBust = Date.now();   // évite le cache navigateur sur switch
    camImgs.forEach(({ slot, img }) => {
      const url = useDebug
        ? `/api/cam/${slot}/debug.mjpeg?t=${cacheBust}`
        : `/api/cam/${slot}/mjpeg?t=${cacheBust}`;
      img.src = url;
    });
  }

  (function installDebugToggle() {
    // On accroche le bouton dans l'actions bar à côté de "Capturer référence"
    const actions = $('.actions');
    if (!actions) return;
    const btn = document.createElement('button');
    btn.className = 'btn';
    btn.id = 'debugToggleBtn';
    btn.type = 'button';
    btn.innerHTML = '<span class="btn__icon">🐛</span><span>Debug</span>';
    btn.setAttribute('aria-pressed', 'false');
    btn.title = 'Affiche les overlays de détection sur les 3 flux (tip, contour, masque diff)';
    // Insertion après le bouton "Capturer référence" si présent, sinon début.
    const captureBtn = $('#captureBtn');
    if (captureBtn && captureBtn.nextSibling) {
      actions.insertBefore(btn, captureBtn.nextSibling);
    } else {
      actions.insertBefore(btn, actions.firstChild);
    }
    btn.addEventListener('click', () => {
      const on = btn.getAttribute('aria-pressed') !== 'true';
      btn.setAttribute('aria-pressed', String(on));
      btn.classList.toggle('btn--primary', on);
      btn.querySelector('span:last-child').textContent = on ? 'Debug ON' : 'Debug';
      swapStreams(on);
    });
  })();

  // ─── Panel tuning live des seuils de détection ──────────────────────
  // 3 sliders qui pushent un `set_tuning` debouncé sur le WS. Les valeurs
  // sont initialisées depuis `ws.lastStatus.tuning` au load (ou au prochain
  // system_status reçu). Utile pour combattre les faux positifs sans restart.
  (function installTuningPanel() {
    const calibSide = $('.calib-side');
    if (!calibSide) return;

    // CSS minimal injecté — les sliders n'existent pas dans la maquette
    // d'origine. On reste sur les tokens design (paper / ink / pink).
    const css = `
      .tuning-panel {
        position: relative;
        padding: 14px 16px;
        background: var(--paper);
        border: 1.5px solid var(--ink);
        margin-bottom: 12px;
        font-family: var(--f-mono);
      }
      .tuning-panel__head {
        display: flex; justify-content: space-between; align-items: baseline;
        margin-bottom: 10px;
      }
      .tuning-row {
        display: grid;
        grid-template-columns: 1fr 56px;
        gap: 12px;
        align-items: center;
        margin: 8px 0;
        font-size: 11px;
        letter-spacing: 0.14em;
        text-transform: uppercase;
      }
      .tuning-row__name { color: var(--ink-soft); }
      .tuning-row__val {
        text-align: right;
        font-family: var(--f-display);
        font-weight: 800;
        font-size: 18px;
        color: var(--ink);
      }
      .tuning-row__slider {
        grid-column: 1 / -1;
        width: 100%;
        margin: 2px 0 4px;
        accent-color: var(--pink);
      }
      .tuning-hint {
        font-size: 10px;
        color: var(--ink-soft);
        letter-spacing: 0.12em;
        margin-top: 8px;
        line-height: 1.5;
      }
    `;
    const s = document.createElement('style');
    s.textContent = css;
    document.head.appendChild(s);

    // Panel HTML
    const panel = document.createElement('section');
    panel.className = 'tuning-panel';
    panel.innerHTML = `
      <div class="tuning-panel__head">
        <span class="caption">FIG. 0X — TUNING LIVE</span>
        <span class="caption caption--plain" id="tuning-state">—</span>
      </div>
      <div class="tuning-row">
        <span class="tuning-row__name" title="Seuil de différence pixel-à-pixel vs référence. Bas = sensible (+faux positifs).">Diff thresh.</span>
        <span class="tuning-row__val" id="tuning-diff-val">—</span>
        <input class="tuning-row__slider" type="range" min="10" max="120" step="1"
               id="tuning-diff" data-key="diff_threshold">
      </div>
      <div class="tuning-row">
        <span class="tuning-row__name" title="Aire minimale en pixels² qu'un contour doit avoir pour être candidat. Monter = filtre les petits artefacts.">Min area</span>
        <span class="tuning-row__val" id="tuning-area-val">—</span>
        <input class="tuning-row__slider" type="range" min="40" max="800" step="10"
               id="tuning-area" data-key="min_dart_area">
      </div>
      <div class="tuning-row">
        <span class="tuning-row__name" title="Nb de frames consécutives stables avant de confirmer une fléchette. Monter = moins de faux positifs, latence +.">Stable frames</span>
        <span class="tuning-row__val" id="tuning-stable-val">—</span>
        <input class="tuning-row__slider" type="range" min="3" max="30" step="1"
               id="tuning-stable" data-key="stable_frames">
      </div>
      <div class="tuning-hint">
        ↑ <strong>faux positifs</strong> = monte diff (45–60), monte area (150–250),<br>
        monte stable (12–18).  Active 🐛 Debug pour voir l'effet en direct.
      </div>
    `;
    // Insertion juste avant la checklist (en bas du side panel)
    const checklist = $('.checklist');
    if (checklist) calibSide.insertBefore(panel, checklist);
    else calibSide.appendChild(panel);

    const els = {
      diff:        $('#tuning-diff'),
      diff_val:    $('#tuning-diff-val'),
      area:        $('#tuning-area'),
      area_val:    $('#tuning-area-val'),
      stable:      $('#tuning-stable'),
      stable_val:  $('#tuning-stable-val'),
      state:       $('#tuning-state'),
    };

    function hydrate(tuning) {
      if (!tuning) return;
      if (tuning.diff_threshold != null) {
        els.diff.value = tuning.diff_threshold;
        els.diff_val.textContent = tuning.diff_threshold;
      }
      if (tuning.min_dart_area != null) {
        els.area.value = tuning.min_dart_area;
        els.area_val.textContent = tuning.min_dart_area;
      }
      if (tuning.stable_frames != null) {
        els.stable.value = tuning.stable_frames;
        els.stable_val.textContent = tuning.stable_frames;
      }
    }

    // Debounce : on n'envoie set_tuning qu'à la fin du drag (250ms après
    // le dernier input event). Évite de saturer le WS pendant le drag.
    let pending = null;
    let debounceTimer = null;
    function scheduleApply(key, value) {
      pending = pending || {};
      pending[key] = value;
      if (debounceTimer) clearTimeout(debounceTimer);
      els.state.textContent = '…';
      debounceTimer = setTimeout(() => {
        const payload = pending;
        pending = null;
        debounceTimer = null;
        ws.send('set_tuning', payload);
      }, 250);
    }

    [els.diff, els.area, els.stable].forEach(slider => {
      slider.addEventListener('input', (e) => {
        const v = parseInt(e.target.value, 10);
        const key = e.target.dataset.key;
        // Update label immédiatement (réactivité visuelle)
        if (key === 'diff_threshold') els.diff_val.textContent = v;
        if (key === 'min_dart_area')  els.area_val.textContent = v;
        if (key === 'stable_frames')  els.stable_val.textContent = v;
        scheduleApply(key, v);
      });
    });

    ws.on('ack', (p) => {
      if (p && p.cmd === 'set_tuning') {
        els.state.textContent = p.ok ? '✓ appliqué' : '✗ erreur';
        setTimeout(() => { els.state.textContent = ''; }, 1500);
      }
    });

    // Hydratation : depuis le dernier status connu OU à la prochaine arrivée
    if (ws.lastStatus && ws.lastStatus.tuning) hydrate(ws.lastStatus.tuning);
    ws.on('system_status', (status) => {
      if (status && status.tuning && !debounceTimer) {
        // Ne pas écraser pendant un drag actif (debounceTimer = drag en cours)
        hydrate(status.tuning);
      }
    });
  })();

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

    // Schéma "FIG. 01 — SETUP RÉEL" : place dynamiquement les 3 cams sur
    // le cercle à 340 mm selon le segment qu'elles regardent. Le système
    // de coords de board.py : angle 0° = segment 20 (top), sens horaire,
    // 18°/segment. La cam est placée à l'OPPOSÉ du segment qu'elle voit
    // de face (si elle regarde le 20, elle est en bas du cercle, etc.).
    const BOARD_ORDER = [20,1,18,4,13,6,10,15,2,17,3,19,7,16,8,11,14,9,12,5];
    const CAM_RADIUS_MM = 340;
    function placeCamOnSchema(camId, seg, isMaster) {
      const segIdx = BOARD_ORDER.indexOf(seg);
      if (segIdx < 0) return;
      const segAngleDeg = segIdx * 18;           // 0° = haut (seg 20)
      const camAngleDeg = (segAngleDeg + 180) % 360;
      const rad = camAngleDeg * Math.PI / 180;
      // SVG : y vers le bas → cos = horizontal, sin = vertical inversée
      const x = Math.sin(rad) * CAM_RADIUS_MM;
      const y = -Math.cos(rad) * CAM_RADIUS_MM;
      // Update line cam → centre (l'autre extrémité reste sur 0,0)
      const line = document.querySelector(`line[data-cam-line="${camId}"]`);
      if (line) { line.setAttribute('x1', x); line.setAttribute('y1', y); }
      // Update outer dot
      const dot = document.querySelector(`circle[data-cam-dot="${camId}"]`);
      if (dot)  { dot.setAttribute('cx', x); dot.setAttribute('cy', y); }
      // Update inner dot (le 2e cercle juste après dans le DOM, sans data-attr)
      if (dot && dot.nextElementSibling
          && dot.nextElementSibling.tagName.toLowerCase() === 'circle') {
        dot.nextElementSibling.setAttribute('cx', x);
        dot.nextElementSibling.setAttribute('cy', y);
      }
      // Update label (positionné à l'extérieur du cercle pour ne pas chevaucher)
      const label = document.querySelector(`text[data-cam-label="${camId}"]`);
      if (label) {
        const lx = x + Math.sin(rad) * 28;       // décale légèrement vers l'extérieur
        const ly = y - Math.cos(rad) * 28 + (y > 0 ? 14 : -4);
        label.setAttribute('x', lx);
        label.setAttribute('y', ly);
        const master = isMaster ? ' · MASTER' : '';
        label.textContent = `CAM-${camId}${master} · SEG ${seg}`;
      }
    }
    status.cams.forEach(cam => {
      if (cam.seg != null) placeCamOnSchema(cam.id, cam.seg, !!cam.master);
    });

    // Bouton Recalibrer : actif dans les 2 modes. En headless on ouvre
    // le modal WEB (4 clics par cam) ; en mode dev avec écran on délègue
    // au flow OpenCV interactif. Le tooltip clarifie ce qui va se passer.
    const recalibBtn = $('#recalibBtn');
    if (recalibBtn) {
      const isHeadless = !!status.headless;
      recalibBtn.disabled = false;
      recalibBtn.classList.remove('is-disabled-headless');
      recalibBtn.title = isHeadless
        ? 'Ouvre le modal web : clique 4 points par caméra (Triple 20, 6, 3, 11)'
        : 'Lance la recalibration interactive OpenCV (clavier + souris côté Pi)';
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

  $('#recalibBtn')?.addEventListener('click', (e) => {
    // Bouton "Recalibrer" : 2 modes possibles selon l'env du serveur.
    //  - headless=true  → on ouvre le modal WEB (flow 4-points par cam)
    //  - headless=false → on délègue au flow OpenCV interactif (clavier
    //                     `c` côté Pi avec écran X)
    if (e.currentTarget.disabled) return;
    const headless = !!(ws.lastStatus && ws.lastStatus.headless);
    if (headless) {
      openRecalibModal();
    } else {
      ws.send('start_recalibration');
    }
  });

  // Reset visuel de la checklist UNIQUEMENT si le backend a accepté.
  ws.on('ack', (p) => {
    if (p && p.cmd === 'start_recalibration' && p.ok) {
      $$('.checklist__row').forEach(r => {
        r.classList.remove('is-done');
        const box = $('.checklist__box', r);
        box.textContent = '';
        box.setAttribute('aria-checked', 'false');
        $('.checklist__status', r).textContent = 'À faire';
      });
      updateChecklistCount();
    }
  });

  // ═══════════════════════════════════════════════════════════════════
  // MODAL RECALIBRATION WEB — flow 4-points par cam (A → B → C)
  // ═══════════════════════════════════════════════════════════════════
  // Étapes par cam :
  //   1. Affiche le flux RAW (pas warpé) en grand
  //   2. User clique sur 4 points dans l'ordre canonique :
  //        Triple 20 (haut) → 6 (droite) → 3 (bas) → 11 (gauche)
  //      Marqueurs colorés + labels affichés à chaque clic.
  //   3. À 4 points, POST /api/calibration/compute → preview warpée
  //   4. User valide ("Confirmer") → POST /api/calibration/save
  //      Sinon "Recommencer" pour réessayer la cam courante.
  //   5. Passe à la cam suivante, ou finit.

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

  // Injection CSS du modal (pas de modification du style.css du design)
  (function injectRecalibCSS() {
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
        align-items: center;
        gap: 16px;
        padding: 14px 22px;
        background: var(--paper);
        border-bottom: 2px solid var(--ink);
      }
      .recalib-head__title {
        font-family: var(--f-display);
        font-weight: 900;
        font-size: 22px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .recalib-head__step {
        text-align: center;
        font-size: 12px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
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
        position: relative;
        background: #000;
        border: 1.5px solid var(--ink);
        display: flex; align-items: center; justify-content: center;
        overflow: hidden;
        min-height: 0;
      }
      /* Wrapper qui prend EXACTEMENT la taille de l'image affichée.
         display:inline-block force le wrapper à s'adapter au contenu,
         donc le SVG par-dessus (en position absolute inset:0) est
         pile aligné sur l'image, sans dérive ni étirement. */
      .recalib-imgbox {
        position: relative;
        display: inline-block;
        line-height: 0;
        cursor: crosshair;
      }
      .recalib-imgbox img {
        display: block;
        max-width: 100%; max-height: 100%;
        max-width: calc(100vw - 460px);
        max-height: calc(100vh - 100px);
      }
      .recalib-imgbox svg {
        position: absolute; inset: 0;
        width: 100%; height: 100%;
        pointer-events: none;
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
        font-weight: 800;
        font-size: 24px;
        text-transform: uppercase;
        line-height: 1.1;
      }
      .recalib-instructions__sub {
        margin-top: 6px;
        font-family: var(--f-mono);
        font-size: 11px;
        font-weight: 400;
        letter-spacing: 0.15em;
        color: var(--ink-soft);
        text-transform: none;
      }
      .recalib-points {
        display: grid; gap: 8px;
        font-size: 12px;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }
      .recalib-points__row {
        display: grid; grid-template-columns: 32px 1fr auto;
        gap: 10px; align-items: center;
        padding: 8px 10px;
        background: rgba(0,0,0,0.05);
        opacity: 0.5;
      }
      .recalib-points__row.is-current { opacity: 1; background: var(--ink); color: var(--paper); }
      .recalib-points__row.is-done    { opacity: 0.85; background: rgba(46,58,168,0.20); }
      .recalib-points__row .dot {
        width: 16px; height: 16px; border-radius: 50%;
        border: 1.5px solid currentColor;
      }
      .recalib-actions {
        display: flex; gap: 10px; flex-wrap: wrap;
      }
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
        border: 1.5px solid var(--ink);
        background: #000;
        padding: 4px;
        margin-top: 8px;
      }
      .recalib-preview img { width: 100%; display: block; }

      .recalib-state {
        font-size: 11px;
        letter-spacing: 0.15em;
        text-transform: uppercase;
        color: var(--ink-soft);
        text-align: center;
        min-height: 14px;
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
  })();

  const RECALIB_SLOTS = ['A', 'B', 'C'];   // ordre des cams
  let recalibState = null;                  // {currentSlotIdx, points, lastCompute, ...}

  function openRecalibModal() {
    recalibState = {
      slotIdx: 0,
      points: [],            // [[x,y], ...] dans coords <img> displayed
      lastCompute: null,     // résultat /compute pour la cam courante
      busy: false,
    };
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

    setupRecalibSlot(0);

    $('#recalib-close-btn').addEventListener('click', closeRecalibModal);
    // Click handler sur l'imgbox (pas le canvas) → coords directement
    // relatives à l'image affichée, sans contamination des marges noires.
    $('#recalib-imgbox').addEventListener('click', onCanvasClick);
    $('#recalib-restart').addEventListener('click', restartCurrentSlot);
    $('#recalib-confirm').addEventListener('click', confirmCurrentSlot);
    document.addEventListener('keydown', recalibKeyHandler);
    // Quand l'image native a chargé, on connaît sa taille → on peut
    // figer le viewBox du SVG dessus.
    $('#recalib-img').addEventListener('load', redrawOverlay);
  }

  function recalibKeyHandler(e) {
    if (!document.getElementById('recalib-overlay')) return;
    if (e.key === 'Escape') closeRecalibModal();
  }

  function closeRecalibModal() {
    const o = document.getElementById('recalib-overlay');
    if (o) o.remove();
    document.removeEventListener('keydown', recalibKeyHandler);
    recalibState = null;
  }

  function setupRecalibSlot(slotIdx) {
    recalibState.slotIdx = slotIdx;
    recalibState.points = [];
    recalibState.lastCompute = null;
    recalibState.busy = true;           // bloque les clics pendant le chargement
    recalibState.imgReady = false;
    const slot = RECALIB_SLOTS[slotIdx];
    const img = $('#recalib-img');

    // Forcer un reset complet de l'élément <img>. Sans ça, sur MJPEG le
    // navigateur garde la dernière frame de la cam précédente et
    // `naturalWidth` reste à l'ancienne valeur tant que la 1ère frame de
    // la nouvelle cam n'est pas arrivée → coords du clic se calculent
    // dans le mauvais espace.
    try { img.removeAttribute('src'); } catch {}
    // Force le browser à laisser tomber la frame en mémoire pour qu'on
    // puisse vérifier naturalWidth==0 (= pas encore chargé)
    img.src = '';
    // Petit délai pour laisser le browser flush l'ancienne frame avant
    // de demander la nouvelle (sinon il peut réutiliser le cache MJPEG).
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

    // Vide l'overlay svg (les markers de la cam précédente ne doivent
    // pas rester pendant la transition)
    const svg = $('#recalib-overlay-svg');
    if (svg) svg.innerHTML = '';

    // Polling pour détecter quand la 1ère frame de la nouvelle cam est
    // dispo. MJPEG ne fire pas toujours `load` de façon fiable selon le
    // browser, donc on poll naturalWidth jusqu'à ce qu'il devienne > 0.
    waitForImgReady(slot);
  }

  function waitForImgReady(slot) {
    const img = $('#recalib-img');
    let tries = 0;
    const MAX_TRIES = 100;            // 100 * 100ms = 10s timeout
    const timer = setInterval(() => {
      tries++;
      if (!recalibState || recalibState.slotIdx == null
          || RECALIB_SLOTS[recalibState.slotIdx] !== slot) {
        // L'utilisateur a changé de cam ou fermé le modal pendant l'attente
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
      // Si l'utilisateur clique pendant le chargement, on lui signale
      // gentiment plutôt que d'ignorer silencieusement.
      if (!recalibState.imgReady) {
        setState('Patiente, flux pas encore prêt…');
      }
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

    // Coords du clic relatif à l'image AFFICHÉE
    const dx = e.clientX - rect.left;
    const dy = e.clientY - rect.top;
    if (dx < 0 || dx > rect.width || dy < 0 || dy > rect.height) return;

    // Convertit DIRECTEMENT en pixels NATIFS (raw frame). On stocke
    // toujours en espace natif → cohérent avec le viewBox SVG natif et
    // avec le backend qui attend ces coords natives.
    const nx = dx * (nw / rect.width);
    const ny = dy * (nh / rect.height);

    recalibState.points.push([nx, ny]);
    redrawOverlay();
    updatePointsList();
    updateInstr(recalibState.points.length);

    if (recalibState.points.length === 4) {
      computeCurrentSlot();
    }
  }

  function redrawOverlay() {
    const svg = $('#recalib-overlay-svg');
    const img = $('#recalib-img');
    if (!svg || !img) return;
    // viewBox = taille NATIVE de l'image (naturalWidth/Height).
    // L'SVG est positionné absolutely sur l'imgbox qui a EXACTEMENT
    // la taille de l'image affichée (display:inline-block + max-w/h).
    // Les coordonnées des points sont stockées en pixels NATIFS
    // (cf. onCanvasClick), donc on peut dessiner directement dans le
    // viewBox sans rescale, et SVG mappe natif→affiché tout seul.
    const nw = img.naturalWidth, nh = img.naturalHeight;
    if (!nw || !nh) return;
    svg.setAttribute('viewBox', `0 0 ${nw} ${nh}`);
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

    if (!recalibState) { svg.innerHTML = ''; return; }
    let html = '';
    recalibState.points.forEach((pt, i) => {
      const color = CALIB_POINT_COLORS[i];
      const r = Math.max(8, nw / 80);   // taille du marker proportionnelle à l'image
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

  // Re-draw l'overlay sur resize (l'image change de taille)
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
    // recalibState.points est DÉJÀ en pixels natifs (cf. onCanvasClick).
    // On envoie display_w = raw_w pour neutraliser le rescale backend.
    try {
      const res = await fetch('/api/calibration/compute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          slot,
          points: recalibState.points,
          display_w: nw,
          display_h: nh,
          raw_w: nw,
          raw_h: nh,
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
        } catch { /* body pas json */ }
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
    setupRecalibSlot(recalibState.slotIdx);
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
    // Slot suivant — désactive Confirmer le temps de la transition pour
    // éviter qu'un double-click ne déclenche un save dans le mauvais slot.
    $('#recalib-confirm').disabled = true;
    const next = recalibState.slotIdx + 1;
    if (next < RECALIB_SLOTS.length) {
      // Petit délai pour laisser l'utilisateur voir "Sauvegardé." puis on
      // bascule. setupRecalibSlot remettra busy=true le temps que la
      // nouvelle frame charge.
      setTimeout(() => {
        try { setupRecalibSlot(next); }
        catch (e) { console.error('[recalib] setupRecalibSlot crash:', e);
                    setState(`Crash transition: ${e.message}`, 'error'); }
      }, 600);
    } else {
      // Terminé
      setState('Recalibration des 3 cams terminée — Pense à recapturer la référence.', 'ok');
      setTimeout(() => {
        closeRecalibModal();
        // Auto-trigger capture référence après recalib (workflow naturel)
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

})();
