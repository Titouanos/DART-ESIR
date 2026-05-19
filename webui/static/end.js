/* ===================================================================
 * end.js — Écran de fin de partie.
 * Hydrate le winner spotlight, le podium et la table de stats depuis
 * le snapshot (qui contient un game_over=true + winner).
 *
 * Note : le payload riche `game_over` (finish, best, busts, podium…) est
 * pushé une seule fois quand la partie se termine. Pour qu'un fresh load
 * de /end après refresh ait toujours les données, on les stocke dans
 * sessionStorage à la réception du game_over.
 * ================================================================= */

'use strict';

(function () {
  const { ws, $, $$, esc } = window.DartApp;

  const STORAGE_KEY = 'dartvision.lastGameOver';

  function loadStoredGameOver() {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  }
  function storeGameOver(payload) {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload)); }
    catch { /* sessionStorage indispo (mode privé etc.) */ }
  }

  function render(payload, snap) {
    if (!payload && !snap) return;
    // Récupère les infos manquantes depuis le snapshot
    const players = (snap && snap.players) || [];
    const winnerName = payload?.winner_name || snap?.winner;
    const winner = players.find(p => p.name === winnerName) || players[0];

    // Topbar : "21 fléchettes · 7 volées"
    const tbDarts = $$('.topbar__meta')[1];
    if (tbDarts && payload) {
      tbDarts.textContent = `${payload.darts || winner?.darts_total || 0} fléchettes · ${payload.turns || winner?.turns || 0} volées`;
    }

    // Winner spotlight
    const nameEl = $('.winner__name');
    if (nameEl && winnerName) {
      nameEl.textContent = winnerName;
      nameEl.setAttribute('data-text', winnerName);
    }
    const verdict = $('.winner__verdict');
    if (verdict) {
      const darts = payload?.darts || winner?.darts_total || 0;
      const avg   = payload?.avg ?? winner?.avg_turn ?? 0;
      verdict.textContent = `Gagne en ${darts} fléchettes · moyenne ${(+avg).toFixed(1)}`;
    }
    // 5 stamps : 501→0 / Best XXX / N volées / N bust / FINISH ...
    const stamps = $$('.winner__line .stamp');
    const sStart = (snap?.mode === '301' ? '301' : '501');
    const sLabels = [
      `${sStart} → 0`,
      `Best ${payload?.best ?? winner?.best_turn ?? 0}`,
      `${payload?.turns ?? winner?.turns ?? 0} volées`,
      `${payload?.busts ?? winner?.busts ?? 0} bust`,
      payload?.finish ? `FINISH ${payload.finish}` : '',
    ];
    stamps.forEach((s, i) => {
      if (sLabels[i] != null) s.textContent = sLabels[i];
      if (i === 4 && !payload?.finish) s.style.display = 'none';
    });

    // Caption "★ CHECKOUT — DOUBLE 19" : on extrait du finish si possible
    const cap = $('.winner__caption');
    if (cap && payload?.finish) {
      const last = payload.finish.split('→').map(s => s.trim()).pop();
      cap.textContent = last ? `★ CHECKOUT — ${last}` : '★ CHECKOUT';
    }

    // Podium
    const podiumList = $('.podium__list');
    if (podiumList && payload?.podium) {
      podiumList.innerHTML = '';
      payload.podium.forEach((p, i) => {
        const row = document.createElement('div');
        row.className = 'podium__row' + (i === 0 ? ' is-winner' : '');
        const rank = String(i + 1).padStart(2, '0');
        row.innerHTML = `
          <span class="podium__rank">${rank}</span>
          <span class="podium__name">${esc(p.name)}</span>
          <span class="podium__label">${esc(p.label)}</span>
          <span class="podium__score">${p.score}</span>
        `;
        podiumList.appendChild(row);
      });
    } else if (podiumList && players.length) {
      // Fallback : trie sur le score si pas de payload complet
      const sorted = [...players].map((p, i) => ({ ...p, originalIdx: i }))
        .sort((a, b) => a.score - b.score);
      podiumList.innerHTML = '';
      const labels = ['vainqueur', '2ᵉ place', '3ᵉ place', '4ᵉ place'];
      sorted.forEach((p, i) => {
        const row = document.createElement('div');
        row.className = 'podium__row' + (i === 0 ? ' is-winner' : '');
        const rank = String(i + 1).padStart(2, '0');
        row.innerHTML = `
          <span class="podium__rank">${rank}</span>
          <span class="podium__name">${esc(p.name)}</span>
          <span class="podium__label">${esc(labels[i] || `${i + 1}ᵉ`)}</span>
          <span class="podium__score">${p.score}</span>
        `;
        podiumList.appendChild(row);
      });
    }

    // Stats table
    const grid = $('.stats-grid');
    if (grid && players.length) {
      // Header (5 colonnes : Joueur / Moy. tour / Meilleur / Busts / Fléchettes)
      grid.innerHTML = `
        <span class="stats-grid__head">Joueur</span>
        <span class="stats-grid__head">Moy. tour</span>
        <span class="stats-grid__head">Meilleur</span>
        <span class="stats-grid__head">Busts</span>
        <span class="stats-grid__head">Fléchettes</span>
      `;
      const winnerNm = winnerName;
      players.forEach((p) => {
        const isWin = p.name === winnerNm;
        grid.insertAdjacentHTML('beforeend', `
          <span class="stats-grid__name${isWin ? ' stats-grid__name--winner' : ''}">${esc(p.name)}</span>
          <span class="stats-grid__val${isWin ? ' stats-grid__val--accent' : ''}">${(p.avg_turn || 0).toFixed(1)}</span>
          <span class="stats-grid__val">${p.best_turn || 0}</span>
          <span class="stats-grid__val">${p.busts || 0}</span>
          <span class="stats-grid__val">${p.darts_total || 0}</span>
        `);
      });
    }
  }

  // ─── Boutons ───────────────────────────────────────────────────────
  $('#replayBtn')?.addEventListener('click', () => {
    // Rejouer = même mode + même joueurs, scores remis à 0
    ws.send('reset_game');
    // Donne le temps au snapshot rebroadcasté d'arriver puis on bascule
    setTimeout(() => window.location.href = '/game', 400);
  });
  $('#newGameBtn')?.addEventListener('click', () => {
    window.location.href = '/setup';
  });
  $('#summaryBtn')?.addEventListener('click', () => {
    alert('Résumé détaillé tour-par-tour — pas encore implémenté.');
  });

  // ─── Branchement WS ────────────────────────────────────────────────
  ws.on('game_over', (payload) => {
    storeGameOver(payload);
    render(payload, ws.lastSnapshot);
  });
  ws.on('snapshot', (snap) => {
    // Si on charge /end directement (refresh), on prend le game_over stocké en
    // session + le snapshot pour les joueurs/stats détaillés.
    render(loadStoredGameOver(), snap);
  });
  // Premier paint : tente avec ce qui est déjà dispo
  render(loadStoredGameOver(), ws.lastSnapshot);
})();
