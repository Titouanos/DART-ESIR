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
  $$('.cam-feed__svg').forEach(svgEl => {
    const slot = svgEl.dataset.feed;        // "A" | "B" | "C"
    if (!slot) return;
    const img = document.createElement('img');
    img.alt = `Flux Cam-${slot}`;
    img.src = `/api/cam/${slot}/mjpeg`;
    img.className = 'cam-feed__svg';   // garde la classe pour le sizing
    svgEl.parentNode.replaceChild(img, svgEl);
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
