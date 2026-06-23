/* ===================================================================
 * game.js — Écran principal partie en cours.
 * Hydrate les 4 cartes joueurs, la matrix stats, le journal des volées,
 * gère les overlays bust/win + boutons undo/next + menu.
 * Source unique : tout rendu vient du snapshot WS.
 * ================================================================= */

'use strict';

(function () {
  const { ws, $, $$, esc } = window.DartApp;

  // ─── Cible : génération SVG (réutilisée à l'identique de la démo) ──
  const SEGMENTS = [20, 1, 18, 4, 13, 6, 10, 15, 2, 17, 3, 19, 7, 16, 8, 11, 14, 9, 12, 5];
  const BOARD = {
    ink: '#171717', paper: '#f3ecdb', pink: '#ff5b9c', blue: '#2e3aa8',
    wire: 'rgba(243, 236, 219, 0.55)',
    darkSeg: '#171717', lightSeg: '#f3ecdb',
  };
  const R = {
    board: 124, doubleOut: 108, doubleIn: 100,
    tripleOut: 67, tripleIn: 60, outerBull: 16, bull: 6.5, numberR: 117,
  };

  function buildBoard() {
    const svg = $('.board__svg');
    if (!svg || svg.dataset.built === '1') return;
    const NS = 'http://www.w3.org/2000/svg';
    const pt = (deg, r) => { const a = deg * Math.PI / 180; return [Math.cos(a) * r, Math.sin(a) * r]; };
    const wedge = (a1, a2, rIn, rOut) => {
      const [x1, y1] = pt(a1, rOut);
      const [x2, y2] = pt(a2, rOut);
      const [x3, y3] = pt(a2, rIn);
      const [x4, y4] = pt(a1, rIn);
      return `M ${x1} ${y1} A ${rOut} ${rOut} 0 0 1 ${x2} ${y2} L ${x3} ${y3} A ${rIn} ${rIn} 0 0 0 ${x4} ${y4} Z`;
    };
    const el = (tag, attrs, text) => {
      const e = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
      if (text != null) e.textContent = text;
      return e;
    };

    svg.appendChild(el('circle', { cx: 0, cy: 0, r: R.board, fill: BOARD.ink }));
    SEGMENTS.forEach((num, i) => {
      const ca = -90 + i * 18;
      const a1 = ca - 9, a2 = ca + 9;
      const even = i % 2 === 0;
      const single = even ? BOARD.darkSeg : BOARD.lightSeg;
      const ring   = even ? BOARD.pink    : BOARD.blue;
      svg.appendChild(el('path', { d: wedge(a1, a2, R.outerBull, R.tripleIn),  fill: single }));
      svg.appendChild(el('path', { d: wedge(a1, a2, R.tripleIn,  R.tripleOut), fill: ring }));
      svg.appendChild(el('path', { d: wedge(a1, a2, R.tripleOut, R.doubleIn),  fill: single }));
      svg.appendChild(el('path', { d: wedge(a1, a2, R.doubleIn,  R.doubleOut), fill: ring }));
    });
    const g = el('g', { stroke: BOARD.wire, 'stroke-width': 0.5, fill: 'none' });
    SEGMENTS.forEach((_, i) => {
      const a1 = (-90 + i * 18 - 9);
      const [x0, y0] = pt(a1, R.outerBull);
      const [x1, y1] = pt(a1, R.doubleOut);
      g.appendChild(el('line', { x1: x0, y1: y0, x2: x1, y2: y1 }));
    });
    [R.doubleOut, R.doubleIn, R.tripleOut, R.tripleIn, R.outerBull].forEach(r =>
      g.appendChild(el('circle', { cx: 0, cy: 0, r })));
    svg.appendChild(g);
    svg.appendChild(el('circle', { cx: 0, cy: 0, r: R.outerBull, fill: BOARD.blue }));
    svg.appendChild(el('circle', { cx: 0, cy: 0, r: R.bull,      fill: BOARD.pink }));
    SEGMENTS.forEach((num, i) => {
      const ca = -90 + i * 18;
      const [nx, ny] = pt(ca, R.numberR);
      svg.appendChild(el('text', {
        x: nx, y: ny + 2.2,
        'text-anchor': 'middle',
        'font-family': "'Anton', sans-serif",
        'font-size': 9, fill: BOARD.paper, 'letter-spacing': '0.5',
      }, num));
    });
    svg.dataset.built = '1';
  }

  /** Pose une pastille papier sur le dernier impact détecté. */
  function plotLastImpact(throwPayload) {
    const svg = $('.board__svg');
    if (!svg || !throwPayload) return;
    // Retire les marqueurs précédents
    $$('.last-impact', svg).forEach(n => n.remove());
    const NS = 'http://www.w3.org/2000/svg';
    const segIdx = SEGMENTS.indexOf(throwPayload.number);
    if (segIdx < 0) return;
    const ca = -90 + segIdx * 18;
    const m = throwPayload.multiplier;
    const r = m === 3 ? (R.tripleIn + R.tripleOut) / 2
            : m === 2 ? (R.doubleIn + R.doubleOut) / 2
            :           (R.outerBull + R.tripleIn) / 2;
    const a = ca * Math.PI / 180;
    const x = Math.cos(a) * r;
    const y = Math.sin(a) * r;
    const mkEl = (tag, attrs) => {
      const e = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
      return e;
    };
    svg.appendChild(mkEl('circle', { cx: x, cy: y, r: 5, fill: 'none', stroke: BOARD.paper, 'stroke-width': 1.5, class: 'last-impact' }));
    svg.appendChild(mkEl('circle', { cx: x, cy: y, r: 2.4, fill: BOARD.paper, class: 'last-impact' }));
  }

  // ─── Couleurs d'identité joueur (selon design data-pid 0..3) ───────
  const PID_CLASSES = {
    0: 'journal__who--pink',
    1: 'journal__who--blue',
    2: 'journal__who--ink',
    3: 'journal__who--p3',
  };

  // ─── Rendu pcards (4 cartes joueurs) ───────────────────────────────
  function renderPCards(snap) {
    const main = $('main.main');
    if (main) main.style.setProperty('--player-count', String(snap.players.length));

    const container = $('.players');
    if (!container) return;
    // Rebuild from scratch — source de vérité = snapshot
    container.innerHTML = '';
    snap.players.forEach((p, i) => {
      const article = document.createElement('article');
      article.className = 'pcard' + (p.active ? ' is-active' : '');
      article.dataset.pid = String(i);
      const status = p.active ? '● AU TIR' : '○ ATTEND';
      const chips = (p.current_throws || [null, null, null]).map(c =>
        c ? `<span class="pcard__chip">${esc(c)}</span>`
          : `<span class="pcard__chip pcard__chip--empty">···</span>`
      ).join('');
      article.innerHTML = `
        <div class="pcard__top">
          <span>${status} · AVG ${(p.avg_turn || 0).toFixed(1)}</span>
          <span>J.${i + 1}</span>
        </div>
        <div class="pcard__name">${esc(p.name)}</div>
        <div class="pcard__score">${p.score}</div>
        <div class="pcard__bottom">
          ${chips}
          <span class="pcard__chk">CHK ${p.checkout_pct || 0}%</span>
        </div>
      `;
      container.appendChild(article);
    });
  }

  // ─── Rendu matrix stats ────────────────────────────────────────────
  function renderMatrix(snap) {
    const grid = $('.matrix__grid');
    if (!grid) return;
    const players = snap.players;
    // Lignes: header / Avg / 180/Ton+ / Checkout% / High checkout / Fléchettes·leg
    const rows = [
      { label: '',                  cells: players.map((p, i) => ({ pid: i, txt: `J.${i + 1}`, head: true })) },
      { label: 'Avg volée',         cells: players.map((p, i) => ({ pid: i, txt: (p.avg_turn || 0).toFixed(1) })) },
      { label: '180 / Ton+',        cells: players.map((p, i) => ({ pid: i, txt: `${p.one_eighty || 0} / ${p.tons_plus || 0}` })) },
      { label: 'Checkout %',        cells: players.map((p, i) => ({ pid: i, txt: `${p.checkout_pct || 0}%` })) },
      { label: 'High checkout',     cells: players.map((p, i) => ({ pid: i, txt: String(p.high_checkout || 0) })) },
      { label: 'Fléchettes · leg',  cells: players.map((p, i) => ({ pid: i, txt: String(p.darts_total || 0) })), last: true },
    ];
    grid.innerHTML = '';
    rows.forEach((row, ri) => {
      if (ri === 0) {
        grid.appendChild(makeSpan('matrix__col-head', ' '));
        row.cells.forEach(c => grid.appendChild(
          makeSpan(`matrix__col-head matrix__col-head--p${c.pid}`, c.txt)));
      } else {
        const cls = 'matrix__row-label' + (row.last ? ' matrix__row-label--last' : '');
        grid.appendChild(makeSpan(cls, row.label));
        row.cells.forEach(c => grid.appendChild(
          makeSpan(`matrix__cell matrix__cell--p${c.pid}`, c.txt)));
      }
    });
    // Update count caption
    const cap = $('.matrix__head .caption--plain');
    if (cap) cap.textContent = `${players.length} J.`;
  }

  function makeSpan(cls, txt) {
    const s = document.createElement('span');
    s.className = cls;
    s.textContent = txt;
    return s;
  }

  // ─── Rendu journal des volées ──────────────────────────────────────
  // Le snapshot expose `history` = liste d'entrées par fléchette
  // (player, label, score). On reagrège côté front en volées (3 fléchettes).
  function renderJournal(snap) {
    const list = $('.journal__list');
    const cap = $('.journal__head .caption--plain');
    if (!list) return;
    const history = (snap.history || []).slice();
    // Agrège par joueur consécutif : on coupe à chaque changement de player.
    // Note : ça approxime, car un bust peut couper un tour en moins de 3 lancers.
    const turns = [];
    let cur = null;
    for (const h of history) {
      if (!cur || cur.player !== h.player) {
        cur = { player: h.player, labels: [], total: 0 };
        turns.push(cur);
      }
      cur.labels.push(h.label);
      cur.total += h.score;
      if (cur.labels.length === 3) cur = null;  // tour plein, fini
    }
    // Plus récent en haut (max 20)
    const recent = turns.slice(-20).reverse();
    // Mapper player.name → pid pour la coloration
    const pidByName = {};
    snap.players.forEach((p, i) => { pidByName[p.name] = i; });

    list.innerHTML = '';
    if (!recent.length) {
      list.innerHTML = '<div class="journal__row"><span class="journal__time">—</span><span></span><span>Aucun lancer enregistré</span></div>';
    } else {
      recent.forEach(t => {
        const pid = pidByName[t.player] ?? 0;
        const cls = PID_CLASSES[pid] || 'journal__who--ink';
        const row = document.createElement('div');
        row.className = 'journal__row';
        const now = new Date();
        const hh = String(now.getHours()).padStart(2, '0');
        const mm = String(now.getMinutes()).padStart(2, '0');
        const ss = String(now.getSeconds()).padStart(2, '0');
        // Pas d'horodatage côté backend pour l'instant — on met l'heure courante
        // pour les nouveaux events. Pour les "vieux" du snapshot, "--:--:--".
        row.innerHTML = `
          <span class="journal__time">${hh}:${mm}:${ss}</span>
          <span class="journal__who ${cls}">${esc(t.player.toUpperCase())}</span>
          <span>${t.labels.map(esc).join(' · ')}${t.labels.length === 3 ? ' = ' + t.total : ''}</span>
        `;
        list.appendChild(row);
      });
    }
    if (cap) cap.textContent = `${recent.length} ENTRÉE${recent.length > 1 ? 'S' : ''}`;
  }

  // ─── Header de partie : mode, leg, tab joueur, volée n° ────────────
  function renderPlayhead(snap) {
    const modeEl = $('.playhead__mode');
    if (modeEl) modeEl.textContent = `${snap.mode.toUpperCase()} · Leg ${snap.leg?.current || 1}/${snap.leg?.total || 1}`;

    // Indicateur de tour bien visible à gauche (gros libellé)
    const capEl = $('.playhead__left .caption');
    if (capEl) capEl.textContent = `TOUR N° ${snap.turn_number || 1}`;

    const tabs = $('.jtabs');
    if (tabs) {
      // Remplace les tabs existants par autant que de joueurs
      const label = $('.jtabs__label', tabs);
      tabs.innerHTML = '';
      if (label) tabs.appendChild(label);
      else {
        const l = document.createElement('span');
        l.className = 'jtabs__label';
        l.textContent = 'Joueurs';
        tabs.appendChild(l);
      }
      snap.players.forEach((p, i) => {
        const btn = document.createElement('button');
        btn.className = 'jtab' + (i === snap.current_player ? ' is-active' : '');
        btn.setAttribute('role', 'tab');
        btn.setAttribute('aria-selected', i === snap.current_player ? 'true' : 'false');
        btn.textContent = String(i + 1);
        tabs.appendChild(btn);
      });
    }
    const dart = $('.playhead__right .caption--plain');
    if (dart) {
      const cp = snap.players[snap.current_player];
      const dn = cp ? cp.darts_in_turn + 1 : 1;
      dart.textContent = `VOLÉE N° ${snap.turn_number || 1} · DART ${Math.min(dn, 3)}/3`;
    }
  }

  // ─── Render principal depuis snapshot ──────────────────────────────
  function renderFromSnapshot(snap) {
    if (!snap || !snap.players) return;
    renderPCards(snap);
    renderMatrix(snap);
    renderJournal(snap);
    renderPlayhead(snap);
    // Overlay game_over si la partie est finie (recouvre la reco)
    if (snap.game_over && snap.winner) {
      showWin({
        winner_name: snap.winner,
        finish: '',
        darts: snap.players.find(p => p.name === snap.winner)?.darts_total || 0,
        turns: snap.players.find(p => p.name === snap.winner)?.turns || 0,
        avg: snap.players.find(p => p.name === snap.winner)?.avg_turn || 0,
        best: snap.players.find(p => p.name === snap.winner)?.best_turn || 0,
      });
    }
  }

  // ─── Overlays ──────────────────────────────────────────────────────
  let bustTimer = null;
  function showBust(payload) {
    const detail = $('.overlay--bust .overlay__detail');
    if (detail && payload) {
      detail.textContent =
        `${payload.player || ''} reste à ${payload.score_left ?? '?'} — passe à ${payload.next_name || ''}`;
    }
    document.body.classList.add('state-bust');
    if (bustTimer) clearTimeout(bustTimer);
    bustTimer = setTimeout(() => {
      document.body.classList.remove('state-bust');
      bustTimer = null;
    }, 2500);
  }

  function showWin(payload) {
    const titleEl = $('.overlay--win .overlay__title');
    if (titleEl && payload?.winner_name) {
      titleEl.textContent = payload.winner_name;
      titleEl.setAttribute('data-text', payload.winner_name);
    }
    const verdict = $('.overlay--win .overlay__verdict');
    if (verdict) verdict.textContent = `Gagne en ${payload?.darts || '?'} fléchettes`;
    const stats = $('.overlay--win .overlay__stats');
    if (stats) {
      const parts = [];
      if (payload?.finish) parts.push(`FINISH ${payload.finish}`);
      if (payload?.avg != null)  parts.push(`Moyenne ${payload.avg}`);
      if (payload?.best != null) parts.push(`Meilleur tour ${payload.best}`);
      stats.textContent = parts.join(' · ');
    }
    document.body.classList.add('state-win');
  }

  // ─── Branchement des boutons d'action ──────────────────────────────
  function setupActions() {
    $('#undoBtn')?.addEventListener('click', () => ws.send('undo_throw'));
    $('#nextBtn')?.addEventListener('click', () => ws.send('next_turn'));
    $('#menuBtn')?.addEventListener('click', () =>
      document.body.classList.add('state-menu'));

    // Menu modal : les 3 premières actions sont liées par index (ordre HTML) :
    // [0] Reset partie · [1] Recalibrer · [2] Capturer ref.
    // Test LEDs et Quitter sont liés par id (robuste si l'ordre change).
    const actions = $$('.modal__action');
    if (actions[0]) actions[0].addEventListener('click', () => {
      ws.send('reset_game');
      document.body.classList.remove('state-menu');
    });
    if (actions[1]) actions[1].addEventListener('click', () => {
      // Recalibrer depuis /game : on ouvre directement le modal partagé
      // (recalib.js) sans naviguer vers /calibration. Comme ça l'utilisateur
      // reste dans le contexte de la partie en cours.
      document.body.classList.remove('state-menu');
      if (window.DartApp.openRecalibModal) {
        window.DartApp.openRecalibModal();
      } else {
        // Fallback si recalib.js pas chargé pour une raison X
        window.location.href = '/calibration';
      }
    });
    if (actions[2]) actions[2].addEventListener('click', () => {
      ws.send('capture_reference');
      document.body.classList.remove('state-menu');
    });
    $('#testLedsBtn')?.addEventListener('click', () => {
      ws.send('test_leds');
      window.DartApp.showToast?.('Test LEDs lancé 🔆');
      document.body.classList.remove('state-menu');
    });
    $('#quitGameBtn')?.addEventListener('click', () => {
      ws.send('quit_game');
      window.location.href = '/setup';
    });

    // Overlay WIN : boutons "Voir le résumé" / "Nouvelle partie"
    // (déjà branchés via window.location.href dans le HTML — on patche
    // "Nouvelle partie" pour reset_game serveur d'abord)
    $$('.overlay--win .overlay__actions .btn').forEach(btn => {
      if (btn.textContent.includes('Nouvelle partie')) {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          ws.send('reset_game');
          window.location.href = '/setup';
        });
      } else if (btn.textContent.includes('résumé')) {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          window.location.href = '/end';
        });
      }
    });
  }

  // ─── Boot ──────────────────────────────────────────────────────────
  buildBoard();
  setupActions();

  ws.on('snapshot',      renderFromSnapshot);
  ws.on('throw',         (p) => {
    plotLastImpact(p);
    // Le scoreboard sera mis à jour par la prochaine snapshot ou state_changed.
    // Pour la réactivité immédiate on patche aussi le pcard du tireur :
    const card = $(`.pcard[data-pid="${p.pid}"]`);
    if (card) {
      const scoreEl = $('.pcard__score', card);
      if (scoreEl) scoreEl.textContent = p.remaining;
      // Ajoute la chip
      const bottom = $('.pcard__bottom', card);
      if (bottom) {
        const empties = $$('.pcard__chip--empty', bottom);
        if (empties[0]) {
          empties[0].classList.remove('pcard__chip--empty');
          empties[0].textContent = p.label;
        }
      }
    }
  });
  ws.on('bust',          showBust);
  ws.on('game_over',     showWin);
  ws.on('turn_end',      () => { /* sera reflété au prochain snapshot */ });
  ws.on('player_change', () => { /* idem */ });

  // À la reconnexion : si on a déjà reçu un snapshot, on le rejoue tout de
  // suite pour éviter le blank screen. L'app.js l'a stocké dans ws.lastSnapshot.
  ws.on('_status', ({ connected }) => {
    if (connected && ws.lastSnapshot) renderFromSnapshot(ws.lastSnapshot);
  });

  // ═══════════════════════════════════════════════════════════════════
  // OVERLAY DEBUG CAMS — voir les 3 flux + détection en temps réel
  // pendant la partie. Bouton dans la bottom action bar, overlay non
  // bloquant qu'on peut fermer pour revenir au jeu.
  // ═══════════════════════════════════════════════════════════════════
  (function installDebugCamsOverlay() {
    // CSS injecté (pas de modif HTML de la maquette)
    const css = `
      .gdbg-toggle {
        position: relative;
        background: var(--paper); color: var(--ink);
        border: 1.5px solid var(--ink);
        padding: 0 18px; font-family: var(--f-condensed);
        font-size: 20px; letter-spacing: 0.06em;
        text-transform: uppercase; cursor: pointer;
        margin-right: 10px;
      }
      .gdbg-toggle.is-on { background: var(--ink); color: var(--paper); }
      .gdbg-overlay {
        position: fixed; inset: 0; z-index: 8000;
        background: rgba(15, 15, 18, 0.92);
        display: flex; flex-direction: column;
        font-family: var(--f-mono);
      }
      .gdbg-head {
        display: flex; justify-content: space-between; align-items: center;
        padding: 12px 22px;
        background: var(--paper);
        border-bottom: 2px solid var(--ink);
      }
      .gdbg-head__title {
        font-family: var(--f-display);
        font-weight: 900; font-size: 22px;
        text-transform: uppercase; letter-spacing: 0.04em;
      }
      .gdbg-controls { display: flex; gap: 10px; align-items: center; }
      .gdbg-controls button {
        background: var(--ink); color: var(--paper);
        border: none; padding: 8px 14px;
        font-family: var(--f-mono); font-size: 11px;
        letter-spacing: 0.18em; text-transform: uppercase;
        cursor: pointer;
      }
      .gdbg-controls button.is-active { background: var(--pink); }
      .gdbg-grid {
        flex: 1; display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 14px; padding: 14px;
        min-height: 0;
      }
      .gdbg-feed {
        display: flex; flex-direction: column;
        background: #000;
        border: 1.5px solid var(--ink);
        min-height: 0;
      }
      .gdbg-feed__label {
        padding: 6px 12px;
        background: var(--paper); color: var(--ink);
        font-family: var(--f-mono); font-size: 12px;
        letter-spacing: 0.18em; text-transform: uppercase;
        border-bottom: 1.5px solid var(--ink);
      }
      .gdbg-feed__img {
        flex: 1; min-height: 0;
        display: flex; align-items: center; justify-content: center;
        overflow: hidden;
      }
      .gdbg-feed__img img {
        max-width: 100%; max-height: 100%;
        display: block;
      }
      @media (max-width: 900px) {
        .gdbg-grid { grid-template-columns: 1fr; }
      }
    `;
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);

    // Ajoute le bouton "Debug" dans la actions bar AVANT le bouton Menu
    function addToggleButton() {
      const actions = $('.actions');
      if (!actions) return;
      if ($('#gdbgToggleBtn')) return;
      const btn = document.createElement('button');
      btn.className = 'btn gdbg-toggle';
      btn.id = 'gdbgToggleBtn';
      btn.type = 'button';
      btn.innerHTML = '<span class="btn__icon">🐛</span><span>Cams</span>';
      btn.title = 'Affiche les 3 flux caméra avec overlay détection (état, tip, contour, masque diff)';
      // Insertion juste avant le bouton Menu si possible
      const menuBtn = $('#menuBtn');
      if (menuBtn) actions.insertBefore(btn, menuBtn);
      else actions.appendChild(btn);
      btn.addEventListener('click', toggleOverlay);
    }

    let dbgMode = 'debug';   // "debug" (avec overlays) ou "clean" (flux brut warpé)

    // ── Lecteur de flux MJPEG via fetch streaming ──────────────────────
    // Un <img src="….mjpeg"> est une boîte noire : si la connexion meurt ou
    // s'affame (hotspot dans la poche du joueur qui marche vers le board…),
    // l'image reste figée SANS événement et ne revient jamais. Ici on lit
    // les octets nous-mêmes : silence > 6s → reconnexion automatique.
    function feedUrl(slot) {
      return `/api/cam/${slot}/${dbgMode === 'debug' ? 'debug.mjpeg' : 'mjpeg'}?t=${Date.now()}`;
    }

    function findSeq(buf, b1, b2, from) {
      for (let k = from; k < buf.length - 1; k++) {
        if (buf[k] === b1 && buf[k + 1] === b2) return k;
      }
      return -1;
    }

    function attachStream(img) {
      const slot = img.dataset.gdbgSlot;
      let aborter = null;
      let stopped = false;
      let lastFrame = Date.now();

      async function readLoop() {
        while (!stopped && document.contains(img)) {
          aborter = new AbortController();
          try {
            const resp = await fetch(feedUrl(slot), { signal: aborter.signal });
            const reader = resp.body.getReader();
            let buf = new Uint8Array(0);
            lastFrame = Date.now();
            for (;;) {
              const { done, value } = await reader.read();
              if (done || stopped) break;
              const nb = new Uint8Array(buf.length + value.length);
              nb.set(buf); nb.set(value, buf.length); buf = nb;
              // Extrait chaque JPEG complet (SOI FFD8 … EOI FFD9)
              for (;;) {
                const soi = findSeq(buf, 0xFF, 0xD8, 0);
                if (soi === -1) { buf = new Uint8Array(0); break; }
                const eoi = findSeq(buf, 0xFF, 0xD9, soi + 2);
                if (eoi === -1) { if (soi > 0) buf = buf.slice(soi); break; }
                const jpg = buf.slice(soi, eoi + 2);
                buf = buf.slice(eoi + 2);
                const url = URL.createObjectURL(new Blob([jpg], { type: 'image/jpeg' }));
                const old = img.dataset.blobUrl;
                img.src = url;
                img.dataset.blobUrl = url;
                if (old) URL.revokeObjectURL(old);
                lastFrame = Date.now();
              }
              if (buf.length > 4000000) buf = new Uint8Array(0); // garde-fou
            }
          } catch (e) { /* coupé/aborté → on retentera */ }
          if (!stopped) await new Promise(r => setTimeout(r, 1500));
        }
      }

      // Watchdog stall : aucune frame depuis 6s → abort → readLoop reconnecte
      const wd = setInterval(() => {
        if (stopped || !document.contains(img)) {
          clearInterval(wd);
          return;
        }
        if (Date.now() - lastFrame > 6000) aborter?.abort();
      }, 2000);

      readLoop();
      return () => {
        stopped = true;
        aborter?.abort();
        clearInterval(wd);
        if (img.dataset.blobUrl) URL.revokeObjectURL(img.dataset.blobUrl);
      };
    }

    let streamStops = [];
    function startStreams() {
      stopStreams();
      $$('#gdbg-grid img[data-gdbg-slot]').forEach(img => {
        streamStops.push(attachStream(img));
      });
    }
    function stopStreams() {
      streamStops.forEach(stop => stop());
      streamStops = [];
    }

    function toggleOverlay() {
      const existing = document.getElementById('gdbg-overlay');
      if (existing) {
        stopStreams();
        existing.remove();
        $('#gdbgToggleBtn')?.classList.remove('is-on');
        return;
      }
      $('#gdbgToggleBtn')?.classList.add('is-on');
      buildOverlay();
    }

    function buildOverlay() {
      const overlay = document.createElement('div');
      overlay.className = 'gdbg-overlay';
      overlay.id = 'gdbg-overlay';
      overlay.innerHTML = `
        <div class="gdbg-head">
          <span class="gdbg-head__title">🐛 Debug cams</span>
          <div class="gdbg-controls">
            <button id="gdbg-mode-debug" type="button" class="is-active">Overlays</button>
            <button id="gdbg-mode-clean" type="button">Brut warpé</button>
            <button id="gdbg-close" type="button">Fermer ✕</button>
          </div>
        </div>
        <div class="gdbg-grid" id="gdbg-grid">
          ${['A','B','C'].map(slot => `
            <div class="gdbg-feed">
              <div class="gdbg-feed__label">CAM ${slot}</div>
              <div class="gdbg-feed__img">
                <img data-gdbg-slot="${slot}" alt="Cam ${slot}">
              </div>
            </div>
          `).join('')}
        </div>
      `;
      document.body.appendChild(overlay);

      $('#gdbg-close').addEventListener('click', toggleOverlay);
      $('#gdbg-mode-debug').addEventListener('click', () => setMode('debug'));
      $('#gdbg-mode-clean').addEventListener('click', () => setMode('clean'));
      startStreams();

      document.addEventListener('keydown', escClose);
    }

    function escClose(e) {
      if (e.key === 'Escape' && document.getElementById('gdbg-overlay')) {
        toggleOverlay();
        document.removeEventListener('keydown', escClose);
      }
    }

    function setMode(mode) {
      dbgMode = mode;
      $('#gdbg-mode-debug')?.classList.toggle('is-active', mode === 'debug');
      $('#gdbg-mode-clean')?.classList.toggle('is-active', mode === 'clean');
      startStreams();   // relance les lecteurs sur le nouvel endpoint
    }

    // L'actions bar peut être rendu dynamiquement par game.js après load.
    // On essaie d'ajouter immédiatement, sinon on attend.
    if (document.readyState === 'complete' || document.readyState === 'interactive') {
      addToggleButton();
    } else {
      window.addEventListener('load', addToggleButton);
    }
  })();
})();
