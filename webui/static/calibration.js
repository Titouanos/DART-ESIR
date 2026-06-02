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

    // Schéma "FIG. 01 — VUE DESSUS" : 3 <text> dans le SVG portent les
    // labels par cam. Le design les codait en dur (CAM-A · SEG 11, etc.) ;
    // on les hydrate depuis system_status. Sélection par préfixe du
    // textContent initial (pas de modif HTML requise).
    const schemaTexts = $$('.calib-schema__svg text');
    schemaTexts.forEach(t => {
      const orig = (t._origText || (t._origText = t.textContent || '')).toUpperCase();
      for (const cam of status.cams) {
        if (orig.startsWith(`CAM-${cam.id}`)) {
          const master = cam.master ? ' · MASTER' : '';
          t.textContent = `CAM-${cam.id}${master} · SEG ${cam.seg ?? '?'}`;
          break;
        }
      }
    });

    // Recalibration interactive : désactive le bouton en mode headless.
    // Le backend (main.py --headless) ne peut PAS lancer cv2.namedWindow,
    // donc on prévient au lieu d'envoyer une commande qui sera refusée.
    const recalibBtn = $('#recalibBtn');
    if (recalibBtn) {
      const isHeadless = !!status.headless;
      recalibBtn.disabled = isHeadless;
      recalibBtn.title = isHeadless
        ? 'Indisponible : main.py tourne en --headless. Relance avec écran HDMI.'
        : 'Lancer la recalibration interactive (4 points par caméra)';
      recalibBtn.classList.toggle('is-disabled-headless', isHeadless);
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
    // Garde locale : si le bouton est marqué "headless-disabled", on
    // refuse côté front avant même d'envoyer une commande qui sera ack'ée
    // en erreur (évite le toast d'erreur inutile + un round-trip WS).
    if (e.currentTarget.disabled) return;
    ws.send('start_recalibration');
    // Note : la checklist n'est PAS reset visuellement ici. Si l'ack
    // revient ok=true, l'effet visuel sera déclenché par le handler
    // d'ack ci-dessous. Sinon (refus headless), la checklist reste
    // dans son état précédent — l'utilisateur n'a rien perdu.
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
})();
