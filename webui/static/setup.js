/* ===================================================================
 * setup.js — Page de préparation : mode + joueurs + démarrage.
 * Reprend la logique de la maquette (toggle mode, add/remove joueurs)
 * et la branche au backend via DartWS.
 * ================================================================= */

'use strict';

(function () {
  const { ws, $, $$, esc } = window.DartApp;

  // ─── State local : ce qui sera envoyé au backend ───────────────────
  const state = {
    mode: '101',                              // overridé par la carte active
    players: ['Titouan', 'Kévin', 'Paul'],    // synchronisé via renumber()
  };
  const MAX_PLAYERS = 8;
  const DEFAULT_NAMES = ['Titouan', 'Kévin', 'Paul', 'Marie', 'Léa', 'Hugo', 'Sami', 'Inès'];

  // ─── DOM refs ──────────────────────────────────────────────────────
  const list    = $('#playerList');
  const addBtn  = $('#addPlayer');
  const countEl = $('#playerCount');
  const startBtn = $('#startBtn');

  // ─── Mode picker ───────────────────────────────────────────────────
  $$('.mode-card').forEach(card => {
    card.addEventListener('click', () => {
      $$('.mode-card').forEach(c => {
        c.classList.remove('is-active');
        c.setAttribute('aria-checked', 'false');
      });
      card.classList.add('is-active');
      card.setAttribute('aria-checked', 'true');
      state.mode = card.dataset.mode;
    });
  });

  // ─── Player list : renumérotation + add/remove ─────────────────────
  function renumber() {
    const rows = $$('.player-input', list);
    rows.forEach((row, i) => {
      row.dataset.idx = i;
      $('.player-input__num', row).textContent = `J.${i + 1}`;
      const nameEl = $('.player-input__name', row);
      nameEl.setAttribute('aria-label', `Nom du joueur ${i + 1}`);
      row.classList.toggle('is-active', i === 0);
    });
    countEl.textContent = `${rows.length} / ${MAX_PLAYERS} INSCRITS`;
    addBtn.toggleAttribute('disabled', rows.length >= MAX_PLAYERS);
    state.players = rows.map(r => $('.player-input__name', r).value.trim());
  }

  function bindRow(row) {
    $('.player-input__remove', row).addEventListener('click', () => {
      if ($$('.player-input', list).length <= 1) return;
      row.remove();
      renumber();
    });
    $('.player-input__name', row).addEventListener('input', renumber);
  }

  $$('.player-input', list).forEach(bindRow);

  function buildRow(name) {
    const row = document.createElement('div');
    row.className = 'player-input';
    row.innerHTML = `
      <span class="player-input__num">J.?</span>
      <input type="text" value="${esc(name)}" class="player-input__name" maxlength="14">
      <button class="player-input__remove" aria-label="Retirer ce joueur">✕</button>
    `;
    bindRow(row);
    return row;
  }

  addBtn.addEventListener('click', () => {
    const rows = $$('.player-input', list);
    if (rows.length >= MAX_PLAYERS) return;
    const taken = rows.map(r => $('.player-input__name', r).value);
    const next = DEFAULT_NAMES.find(n => !taken.includes(n)) || `J.${rows.length + 1}`;
    list.insertBefore(buildRow(next), addBtn);
    renumber();
  });

  // ─── Démarrer la partie ────────────────────────────────────────────
  startBtn.addEventListener('click', () => {
    // Une dernière sync au cas où l'input n'a pas perdu le focus.
    renumber();
    const players = state.players.filter(Boolean);
    if (!players.length) {
      alert('Ajoutez au moins un joueur.');
      return;
    }
    // setup_game crée un nouveau GameEngine côté serveur (reset complet).
    // L'ack confirme la création ; on navigue après.
    const ok = ws.send('setup_game', { mode: state.mode, players });
    if (!ok) {
      alert('WS déconnecté — impossible de démarrer.');
      return;
    }
  });

  // L'ack du setup_game déclenche la navigation. On évite de naviguer
  // avant que le serveur n'ait acté la création (sinon /game se charge
  // sur un état stale).
  ws.on('ack', (p) => {
    if (p.cmd === 'setup_game' && p.ok) {
      window.location.href = '/game';
    }
  });

  // ─── Menu modal ────────────────────────────────────────────────────
  $('#menuBtn').addEventListener('click', () =>
    document.body.classList.add('state-menu'));

  // Actions du modal : on les identifie par leur texte (le design ne
  // les a pas marqués). Sélecteur basé sur l'ordre — fragile mais
  // acceptable vu la simplicité du modal.
  const modalActions = $$('.modal__action');
  // [0] Recalibrer · [1] Capturer réf · [2] Reset joueurs · [3] Quitter (danger)
  if (modalActions[0]) modalActions[0].addEventListener('click', () => {
    window.location.href = '/calibration';
  });
  if (modalActions[1]) modalActions[1].addEventListener('click', () => {
    ws.send('capture_reference');
    document.body.classList.remove('state-menu');
  });
  if (modalActions[2]) modalActions[2].addEventListener('click', () => {
    // Reset des joueurs : on garde 1 ligne par défaut, vide le reste.
    $$('.player-input', list).forEach((r, i) => { if (i > 0) r.remove(); });
    const first = $('.player-input__name', list);
    if (first) first.value = 'Joueur 1';
    renumber();
    document.body.classList.remove('state-menu');
  });
  if (modalActions[3]) modalActions[3].addEventListener('click', () => {
    ws.send('quit_game');
    document.body.classList.remove('state-menu');
  });

  // ─── Hydratation : reflète le snapshot/status backend dans le bandeau ─
  ws.on('snapshot', (snap) => {
    // Si le serveur a déjà une partie en cours (page rouvre en cours de
    // partie), on bascule directement sur /game pour ne pas perdre l'état.
    if (snap && snap.players && snap.players.length > 0 &&
        snap.history && snap.history.length > 0 && !snap.game_over) {
      // Évite la nav si on vient d'arriver — on respecte le choix manuel.
      // En pratique : un refresh accidentel pendant une partie ramène ici.
      // Pour STEP 3 on laisse l'utilisateur cliquer "Démarrer" plutôt
      // que de l'auto-rediriger. Comportement à raffiner en STEP 4.
    }
  });

  ws.on('system_status', (status) => {
    if (!status || !status.cams) return;
    // Bandeau "FIG. 03 — SYSTÈME" : 3 items cam + calib + ref
    status.cams.forEach((cam) => {
      const item = $(`.system-item[data-cam="${cam.id}"]`);
      if (!item) return;
      const led = $('.cam-led', item);
      const val = $('.system-item__val', item);
      if (led) {
        led.classList.toggle('is-ok',  !!cam.ok);
        led.classList.toggle('is-err', !cam.ok);
      }
      if (val) {
        val.textContent = cam.ok
          ? `${cam.fps || '?'} fps · OK`
          : 'Hors-ligne · KO';
      }
      item.classList.toggle('is-warning', !cam.ok);
    });

    const calibItem = $('.system-item[data-item="calib"]');
    if (calibItem && status.calibration) {
      $('.system-item__val', calibItem).textContent =
        status.calibration.ok
          ? `Résiduel ${(status.calibration.residual_mm || 0).toFixed(2)} mm`
          : 'Calibration manquante';
      calibItem.classList.toggle('is-warning', !status.calibration.ok);
      $('.system-item__check', calibItem).textContent =
        status.calibration.ok ? '✓' : '!';
    }
    const refItem = $('.system-item[data-item="ref"]');
    if (refItem && status.reference) {
      $('.system-item__val', refItem).textContent =
        status.reference.ok
          ? `${esc(status.reference.captured_at || '--:--')} · board vide`
          : 'À recapturer';
      refItem.classList.toggle('is-warning', !status.reference.ok);
      $('.system-item__check', refItem).textContent =
        status.reference.ok ? '✓' : '!';
    }
  });
})();
