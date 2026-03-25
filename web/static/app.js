/* ═══════════════════════════════════════════════════════════════════════════
   DartVision — Frontend Application
   Complete game UI: setup, live scoring, manual fallback, scoreboard.
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

// ─── Dartboard constants (mirror board.py) ────────────────────────────────────
const BOARD_ORDER = [20, 1, 18, 4, 13, 6, 10, 15, 2, 17, 3, 19, 7, 16, 8, 11, 14, 9, 12, 5];
const R_BULL_DBL  = 0.0375;
const R_BULL_SGL  = 0.0935;
const R_TRIPLE_IN = 0.5824;
const R_TRIPLE_OUT= 0.6294;
const R_DOUBLE_IN = 0.9529;
const R_DOUBLE_OUT= 1.0;

// SVG dartboard geometry
const CX = 250, CY = 250, RSVG = 218;

// ─── Application State ────────────────────────────────────────────────────────
let gameMode       = '501';
let ws             = null;
let wsRetryTimeout = null;
let selectedScore  = null;   // Currently selected score for manual modal
let selectedNum    = null;   // Currently selected number (awaiting multiplier)
let openCamIndexes = [];     // Camera indexes from server
let detectionPaused= false;
let lastTipPx      = null;   // Last detected dart tip (warped coords)

// ═══════════════════════════════════════════════════════════════════════════════
// Setup Screen
// ═══════════════════════════════════════════════════════════════════════════════

function showSetup() {
  document.getElementById('setup-screen').style.display = 'flex';
  document.getElementById('game-screen').style.display  = 'none';
  document.getElementById('game-over-overlay').classList.add('hidden');
  if (ws) { ws.close(); ws = null; }
  clearTimeout(wsRetryTimeout);
}

function addPlayer() {
  const list = document.getElementById('player-list');
  const n = list.querySelectorAll('.player-input-row').length + 1;
  const row = document.createElement('div');
  row.className = 'player-input-row';
  row.innerHTML = `
    <input type="text" placeholder="Joueur ${n}" value="Joueur ${n}" class="player-name-input" />
    <button class="btn-icon danger" onclick="removePlayer(this)" title="Supprimer">✕</button>`;
  list.appendChild(row);
}

function removePlayer(btn) {
  const list = document.getElementById('player-list');
  if (list.querySelectorAll('.player-input-row').length <= 1) return;
  btn.closest('.player-input-row').remove();
}

// Mode buttons
document.querySelectorAll('.mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    gameMode = btn.dataset.mode;
  });
});

async function startGame() {
  const players = [...document.querySelectorAll('.player-name-input')]
    .map(i => i.value.trim()).filter(Boolean);

  if (!players.length) { showToast('Ajoutez au moins un joueur', 'error'); return; }

  const camVals = [
    document.getElementById('cam0-input').value,
    document.getElementById('cam1-input').value,
    document.getElementById('cam2-input').value,
  ].map(v => v.trim()).filter(v => v !== '');
  const cam_indexes = camVals.map(Number);

  const btn = document.getElementById('start-btn');
  btn.disabled = true;
  document.getElementById('start-btn-text').textContent = '⏳ Initialisation…';

  try {
    const res = await fetch('/api/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: gameMode, players, cam_indexes }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || 'Erreur serveur');
    const data = await res.json();

    openCamIndexes = data.cameras || [];
    document.getElementById('mode-badge').textContent = gameMode.toUpperCase();
    document.getElementById('setup-screen').style.display = 'none';
    document.getElementById('game-screen').style.display  = 'grid';

    buildCameraFeeds(openCamIndexes);
    buildMainBoard();
    updateScoreboard(data.scoreboard);
    connectWebSocket();

    const camsMsg = openCamIndexes.length
      ? `${openCamIndexes.length} caméra(s) détectée(s)`
      : 'Mode saisie manuelle';
    showToast(`Partie ${gameMode} lancée — ${camsMsg}`, 'success');

  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    document.getElementById('start-btn-text').textContent = '🚀 Lancer la partie';
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// Camera Feeds
// ═══════════════════════════════════════════════════════════════════════════════

function buildCameraFeeds(indexes) {
  const container = document.getElementById('cam-feeds');
  const noMsg = document.getElementById('no-cam-msg');
  container.innerHTML = '';

  if (!indexes.length) {
    noMsg.style.display = 'block';
    return;
  }
  noMsg.style.display = 'none';

  indexes.forEach(idx => {
    const div = document.createElement('div');
    div.className = 'cam-container';
    div.id = `cam-box-${idx}`;
    div.innerHTML = `
      <img src="/camera/${idx}" alt="Caméra ${idx}" />
      <div class="cam-overlay">
        <span class="cam-badge">CAM ${idx}</span>
        <div class="cam-state-dot idle" id="cam-dot-${idx}"></div>
      </div>
      <div class="cam-bottom">
        <span class="cam-state-label" id="cam-state-lbl-${idx}">idle</span>
        <button class="cam-ref-btn" onclick="captureReference(${idx})">📸 Réf.</button>
      </div>`;
    container.appendChild(div);
  });
}

function updateCamStates(camStates) {
  if (!camStates) return;
  Object.entries(camStates).forEach(([idx, st]) => {
    const dot = document.getElementById(`cam-dot-${idx}`);
    const box = document.getElementById(`cam-box-${idx}`);
    const lbl = document.getElementById(`cam-state-lbl-${idx}`);

    if (dot) dot.className = `cam-state-dot ${st}`;
    if (lbl) lbl.textContent = st;
    if (box) {
      box.className = 'cam-container';
      if (st === 'motion' || st === 'confirming') box.classList.add('detecting');
      else if (st === 'detected') box.classList.add('detected');
      else if (st === 'cooldown') box.classList.add('cooldown');
    }
  });
}

async function captureReference(idx) {
  await apiCall(`/api/reference/${idx}`, 'POST');
  showToast(`Référence capturée — caméra ${idx}`, 'success');
}

async function captureAllReferences() {
  const data = await apiCall('/api/reference', 'POST');
  showToast(`Référence capturée pour ${data.captured.length} caméra(s)`, 'success');
}

async function toggleDetection() {
  detectionPaused = !detectionPaused;
  await apiCall('/api/pause', 'POST', { paused: detectionPaused });
  const btn = document.getElementById('pause-btn');
  btn.textContent = detectionPaused ? '▶ Reprendre' : '⏸ Pause';
  btn.className = `pill-btn ${detectionPaused ? 'paused' : ''}`;

  const dot  = document.getElementById('detect-dot');
  const text = document.getElementById('detect-status-text');
  if (detectionPaused) {
    dot.className = 'detect-status-dot paused';
    text.textContent = 'Détection suspendue';
  } else {
    dot.className = 'detect-status-dot';
    text.textContent = 'En attente de lancer…';
  }

  showToast(detectionPaused ? '⏸ Détection pausée' : '▶ Détection reprise', 'info');
}

// ═══════════════════════════════════════════════════════════════════════════════
// WebSocket
// ═══════════════════════════════════════════════════════════════════════════════

function connectWebSocket() {
  if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
  clearTimeout(wsRetryTimeout);

  ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => {
    console.log('[WS] Connected');
    // Keepalive ping every 20s
    ws._pingInterval = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 20000);
  };

  ws.onmessage = ev => {
    try { handleMessage(JSON.parse(ev.data)); } catch {}
  };

  ws.onclose = () => {
    console.log('[WS] Disconnected');
    clearInterval(ws._pingInterval);
    // Auto-reconnect only if game screen is visible
    if (document.getElementById('game-screen').style.display !== 'none') {
      wsRetryTimeout = setTimeout(connectWebSocket, 2500);
    }
  };

  ws.onerror = e => console.error('[WS] Error', e);
}

function handleMessage(data) {
  switch (data.type) {

    case 'state':
      if (data.scoreboard)      updateScoreboard(data.scoreboard);
      if (data.history)         updateHistory(data.history);
      if (data.cam_states)      updateCamStates(data.cam_states);
      if (data.current_player)  updateCurrentPlayer(data.current_player, data.scoreboard);
      if (data.detection_paused !== undefined) detectionPaused = data.detection_paused;
      if (data.game_over)       showGameOver(data.winner);
      break;

    case 'detection':
      if (data.scoreboard)  updateScoreboard(data.scoreboard);
      if (data.history)     updateHistory(data.history);
      if (data.cam_states)  updateCamStates(data.cam_states);
      if (data.current_player) updateCurrentPlayer(data.current_player, data.scoreboard);
      handleDetectionMsg(data);
      _dbgOnDetection(data);
      break;

    case 'undo':
      if (data.scoreboard)  updateScoreboard(data.scoreboard);
      if (data.history)     updateHistory(data.history);
      if (data.current_player) updateCurrentPlayer(data.current_player, data.scoreboard);
      clearDartMarker();
      updateLastThrowBar(null);
      showToast('↩ Lancer annulé', 'info');
      dbgLog('undo', 'Lancer annulé');
      break;

    case 'next_turn':
      if (data.scoreboard)  updateScoreboard(data.scoreboard);
      if (data.history)     updateHistory(data.history);
      if (data.current_player) updateCurrentPlayer(data.current_player, data.scoreboard);
      clearDartMarker();
      showToast(`⏭ Tour suivant — ${data.current_player}`, 'info');
      dbgLog('system', `Tour suivant → ${data.current_player}`);
      break;

    case 'reference_captured':
      // toast already shown from API call
      break;

    case 'detection_paused':
      detectionPaused = data.paused;
      break;

    case 'config_updated':
      Object.entries(data.params || {}).forEach(([k, v]) => dbgLog('config', `${k} = ${v}`));
      break;
  }
}

function _dbgOnDetection(data) {
  if (!debugOpen) return;
  const gr  = data.game_result || {};
  const src = data.source === 'manual' ? '[manuel]' : `[${data.fusion_method ?? '?'}]`;
  if (gr.bust) {
    dbgLog('bust', `${gr.player} BUST — ${data.label} (${data.score}pts) ${src}`);
  } else {
    dbgLog('detection', `${gr.player ?? '?'}: ${data.label} ${data.score}pts ${src} cams=[${(data.fusion_cams||[]).join(',')}] conf=${data.fusion_confidence != null ? (data.fusion_confidence*100).toFixed(0)+'%' : '?'} tip=${data.tip_px ? `(${data.tip_px[0]},${data.tip_px[1]})` : '—'}`);
  }
}

function handleDetectionMsg(data) {
  const gr = data.game_result || {};

  // Update detection status
  const dot  = document.getElementById('detect-dot');
  const text = document.getElementById('detect-status-text');
  dot.className = 'detect-status-dot active';
  text.textContent = `Détecté: ${data.label} (${data.score} pts)`;
  setTimeout(() => {
    dot.className = 'detect-status-dot';
    text.textContent = detectionPaused ? 'Détection suspendue' : 'En attente de lancer…';
  }, 3000);

  // Update last throw bar
  updateLastThrowBar(data);

  // Show dart marker on main board
  if (data.tip_px) {
    lastTipPx = data.tip_px;
    showDartMarker(data.tip_px, data.label, data.score);
  }

  // Reveal "Corriger" button
  const correctBtn = document.getElementById('correct-btn');
  if (correctBtn) correctBtn.style.display = 'block';

  // Notifications
  if (gr.bust) {
    // Shake all player cards briefly
    markBusted(gr.player);
    showToast(`💥 BUST ! ${gr.player} — tour annulé`, 'error');
  } else if (gr.game_over) {
    showGameOver(gr.winner);
  } else if (gr.turn_complete) {
    showToast(`✅ Tour terminé — ${gr.player}`, 'info');
  } else {
    const icon = data.score >= 60 ? '🔥' : data.score >= 40 ? '🎯' : data.score === 0 ? '😬' : '✅';
    showToast(`${icon} ${data.label} — ${data.score} pts (${gr.player})`, 'success');
  }

  // Flash score
  const el = document.getElementById('lt-score');
  if (el) { el.classList.add('score-flash'); setTimeout(() => el.classList.remove('score-flash'), 950); }
}

// ═══════════════════════════════════════════════════════════════════════════════
// Last Throw Bar
// ═══════════════════════════════════════════════════════════════════════════════

function updateLastThrowBar(data) {
  const scoreEl  = document.getElementById('lt-score');
  const labelEl  = document.getElementById('lt-label');
  const detailEl = document.getElementById('lt-detail');
  const srcEl    = document.getElementById('lt-source');

  if (!data) {
    scoreEl.textContent  = '—';
    labelEl.textContent  = 'En attente de détection';
    detailEl.textContent = 'Lancez une fléchette ou utilisez la saisie manuelle';
    srcEl.textContent    = '—';
    srcEl.className      = 'source-badge';
    return;
  }

  scoreEl.textContent = data.score ?? '—';
  labelEl.textContent = data.label || '?';

  const methodMap = {
    manual:        'Saisie manuelle',
    single:        `Caméra ${(data.fusion_cams || [])[0] ?? '?'}`,
    ray_intersect: `Triangulation (cams ${(data.fusion_cams || []).join(', ')})`,
    agree:         `Fusion accord (cams ${(data.fusion_cams || []).join(', ')})`,
    best_confidence:`Meilleure caméra (${(data.fusion_cams || []).join(', ')})`,
  };
  let detail = methodMap[data.fusion_method] || data.fusion_method || '—';
  if (data.fusion_confidence != null && data.fusion_method !== 'manual') {
    detail += ` · confiance ${Math.round(data.fusion_confidence * 100)}%`;
  }
  detailEl.textContent = detail;

  const src = data.source === 'manual' ? 'manual' : 'auto';
  srcEl.textContent = src === 'manual' ? 'Manuel' : 'Auto';
  srcEl.className   = `source-badge ${src}`;
}

// ═══════════════════════════════════════════════════════════════════════════════
// Scoreboard
// ═══════════════════════════════════════════════════════════════════════════════

function updateScoreboard(scoreboard) {
  const section = document.getElementById('players-section');
  if (!section) return;

  // Build or update cards
  scoreboard.forEach((player, i) => {
    let card = document.getElementById(`pcard-${i}`);
    if (!card) {
      card = document.createElement('div');
      card.id = `pcard-${i}`;
      section.appendChild(card);
    }

    // Classes
    card.className = 'player-card';
    if (player.active)  card.classList.add('active');

    // Dart dots
    const dots = Array.from({ length: 3 }, (_, d) =>
      d < player.darts
        ? `<div class="dart-dot thrown" title="Fléchette ${d+1}">✓</div>`
        : `<div class="dart-dot">${d+1}</div>`
    ).join('');

    // Checkout hint
    let checkout = '';
    if (player.checkout && player.checkout.length > 0) {
      checkout = `<div class="checkout-hint">💡 Checkout: <strong>${escHtml(player.checkout.join(' → '))}</strong></div>`;
    }

    const isX01 = gameMode === '501' || gameMode === '301';

    card.innerHTML = `
      <div class="player-header">
        <div class="player-dot"></div>
        <div class="player-name-display">${escHtml(player.name)}</div>
        ${player.active ? '<span class="turn-badge">► En jeu</span>' : ''}
      </div>
      <div class="player-score-row">
        <div class="player-main-score ${isX01 ? '' : ''}">${player.score}</div>
      </div>
      <div class="player-stats">
        <div class="stat-item">
          <div class="stat-val">${player.darts}</div>
          <div class="stat-lbl">Fléchettes</div>
        </div>
        <div class="stat-item">
          <div class="stat-val">${player.avg}</div>
          <div class="stat-lbl">Moy/tour</div>
        </div>
        <div class="stat-item">
          <div class="stat-val">${player.turns}</div>
          <div class="stat-lbl">Tours</div>
        </div>
      </div>
      <div class="dart-row">${dots}</div>
      ${checkout}`;
  });

  // Remove extra cards
  const cards = section.querySelectorAll('.player-card');
  for (let i = scoreboard.length; i < cards.length; i++) cards[i].remove();
}

function markBusted(playerName) {
  document.querySelectorAll('.player-card').forEach(card => {
    if (card.querySelector('.player-name-display')?.textContent === playerName) {
      card.classList.add('busted');
      setTimeout(() => card.classList.remove('busted'), 1800);
    }
  });
}

function updateCurrentPlayer(name, scoreboard) {
  document.getElementById('current-player-name').textContent = name;

  // Dart counter
  const player = scoreboard && scoreboard.find(p => p.name === name && p.active);
  const dEl = document.getElementById('current-player-darts');
  if (dEl && player) {
    const remaining = 3 - player.darts;
    dEl.textContent = remaining > 0 ? `(${remaining} restant${remaining > 1 ? 's' : ''})` : '';
  }
}

function updateHistory(history) {
  const list = document.getElementById('history-list');
  if (!list) return;

  if (!history || !history.length) {
    list.innerHTML = '<div class="history-empty">Aucun lancer enregistré</div>';
    return;
  }

  list.innerHTML = [...history].reverse().map(item => `
    <div class="history-item">
      <span class="h-player">${escHtml(item.player)}</span>
      <span class="h-label">${escHtml(item.label)}</span>
      <span class="h-score">${item.score}</span>
    </div>`).join('');
}

// ═══════════════════════════════════════════════════════════════════════════════
// Dartboard SVG Builder
// ═══════════════════════════════════════════════════════════════════════════════

function toRad(deg) { return (deg - 90) * Math.PI / 180; }

function arcPath(riFrac, roFrac, aStart, aEnd) {
  const ri = riFrac * RSVG, ro = roFrac * RSVG;
  const a1 = toRad(aStart), a2 = toRad(aEnd);
  const x1o = CX + ro*Math.cos(a1), y1o = CY + ro*Math.sin(a1);
  const x2o = CX + ro*Math.cos(a2), y2o = CY + ro*Math.sin(a2);
  const x1i = CX + ri*Math.cos(a1), y1i = CY + ri*Math.sin(a1);
  const x2i = CX + ri*Math.cos(a2), y2i = CY + ri*Math.sin(a2);
  const lg = (aEnd - aStart) > 180 ? 1 : 0;

  if (ri < 0.5) {
    return `M ${CX} ${CY} L ${x1o.toFixed(2)} ${y1o.toFixed(2)} A ${ro.toFixed(2)} ${ro.toFixed(2)} 0 ${lg} 1 ${x2o.toFixed(2)} ${y2o.toFixed(2)} Z`;
  }
  return `M ${x1o.toFixed(2)} ${y1o.toFixed(2)} A ${ro.toFixed(2)} ${ro.toFixed(2)} 0 ${lg} 1 ${x2o.toFixed(2)} ${y2o.toFixed(2)} L ${x2i.toFixed(2)} ${y2i.toFixed(2)} A ${ri.toFixed(2)} ${ri.toFixed(2)} 0 ${lg} 0 ${x1i.toFixed(2)} ${y1i.toFixed(2)} Z`;
}

function buildDartboard(svgEl, interactive = false) {
  const ns = 'http://www.w3.org/2000/svg';
  let parts = [];

  // Outer black circle (board surround)
  parts.push(`<circle cx="${CX}" cy="${CY}" r="${RSVG * 1.07}" fill="#0a0a0a" stroke="#3a3a3a" stroke-width="6"/>`);

  // ── 20 segments ──────────────────────────────────────────────────────────
  for (let i = 0; i < 20; i++) {
    const num   = BOARD_ORDER[i];
    const aS    = i * 18 - 9;
    const aE    = i * 18 + 9;
    const even  = i % 2 === 0;

    const singleFill = even ? '#111' : '#d9cca4';
    const scoreFill  = even ? '#1a6b1a' : '#b82020';
    const wire = '#3a3a3a';
    const curs = interactive ? 'style="cursor:crosshair"' : '';

    // Inner single
    parts.push(`<path d="${arcPath(R_BULL_SGL, R_TRIPLE_IN, aS, aE)}" fill="${singleFill}" stroke="${wire}" stroke-width="0.6" ${curs}/>`);
    // Triple
    parts.push(`<path d="${arcPath(R_TRIPLE_IN, R_TRIPLE_OUT, aS, aE)}" fill="${scoreFill}" stroke="${wire}" stroke-width="0.6" ${curs}/>`);
    // Outer single
    parts.push(`<path d="${arcPath(R_TRIPLE_OUT, R_DOUBLE_IN, aS, aE)}" fill="${singleFill}" stroke="${wire}" stroke-width="0.6" ${curs}/>`);
    // Double
    parts.push(`<path d="${arcPath(R_DOUBLE_IN, R_DOUBLE_OUT, aS, aE)}" fill="${scoreFill}" stroke="${wire}" stroke-width="0.6" ${curs}/>`);

    // Segment divider wire (from bull to outer)
    const pBull  = { x: CX + R_BULL_SGL*RSVG*Math.cos(toRad(aS)), y: CY + R_BULL_SGL*RSVG*Math.sin(toRad(aS)) };
    const pOuter = { x: CX + RSVG*Math.cos(toRad(aS)),             y: CY + RSVG*Math.sin(toRad(aS)) };
    parts.push(`<line x1="${pBull.x.toFixed(1)}" y1="${pBull.y.toFixed(1)}" x2="${pOuter.x.toFixed(1)}" y2="${pOuter.y.toFixed(1)}" stroke="${wire}" stroke-width="1.2"/>`);

    // Number label
    const aLbl = i * 18;
    const rLbl = RSVG * 1.085;
    const lx   = CX + rLbl * Math.cos(toRad(aLbl));
    const ly   = CY + rLbl * Math.sin(toRad(aLbl));
    parts.push(`<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="middle" dominant-baseline="middle" fill="#ccc" font-size="15" font-weight="700" font-family="Arial,sans-serif" ${curs ? 'style="cursor:default"' : ''}>${num}</text>`);
  }

  // ── Bull rings ────────────────────────────────────────────────────────────
  const bullCurs = interactive ? 'style="cursor:crosshair"' : '';
  parts.push(`<circle cx="${CX}" cy="${CY}" r="${R_BULL_SGL*RSVG}" fill="#b82020" stroke="#3a3a3a" stroke-width="0.8" ${bullCurs}/>`);
  parts.push(`<circle cx="${CX}" cy="${CY}" r="${R_BULL_DBL*RSVG}" fill="#1a6b1a" stroke="#3a3a3a" stroke-width="0.8" ${bullCurs}/>`);

  // ── Ring wire overlays ────────────────────────────────────────────────────
  [R_BULL_DBL, R_BULL_SGL, R_TRIPLE_IN, R_TRIPLE_OUT, R_DOUBLE_IN, R_DOUBLE_OUT].forEach(r => {
    parts.push(`<circle cx="${CX}" cy="${CY}" r="${(r*RSVG).toFixed(2)}" fill="none" stroke="#3a3a3a" stroke-width="${r === R_DOUBLE_OUT ? 2.5 : 1.2}"/>`);
  });

  // ── Marker group (for dart positions) ────────────────────────────────────
  parts.push(`<g id="dart-markers-${svgEl.id}"></g>`);

  svgEl.innerHTML = parts.join('\n');

  // Click handler for interactive boards
  if (interactive) {
    svgEl.onclick = ev => {
      const coords = svgToLocal(svgEl, ev);
      const score  = scoreFromCoords(coords.x, coords.y);
      if (score) {
        applySelectedScore(score);
        showClickIndicator(svgEl, coords.x, coords.y);
      }
    };
  } else {
    // Main board: open manual modal on click
    svgEl.onclick = () => openManualScore();
  }
}

// Convert screen click to SVG coordinate space
function svgToLocal(svgEl, ev) {
  const rect = svgEl.getBoundingClientRect();
  return {
    x: (ev.clientX - rect.left) * (500 / rect.width),
    y: (ev.clientY - rect.top)  * (500 / rect.height),
  };
}

// Compute score from SVG (x, y) — mirrors board.py compute_score()
function scoreFromCoords(svgX, svgY) {
  const dx = svgX - CX, dy = svgY - CY;
  const dist = Math.sqrt(dx*dx + dy*dy);
  const rFrac = dist / RSVG;

  if (rFrac > R_DOUBLE_OUT * 1.07) return null;
  if (rFrac > R_DOUBLE_OUT) return { label: 'MISS', score: 0, number: 0, multiplier: 0, ring: 'outside' };
  if (rFrac <= R_BULL_DBL)  return { label: 'D-BULL', score: 50, number: 25, multiplier: 2, ring: 'double_bull' };
  if (rFrac <= R_BULL_SGL)  return { label: 'S-BULL', score: 25, number: 25, multiplier: 1, ring: 'single_bull' };

  // Angle: 0° = top, clockwise (atan2(dx, -dy) in math coords)
  const angleDeg = ((Math.atan2(dx, -dy) * 180 / Math.PI) + 360) % 360;
  const shifted  = (angleDeg + 9) % 360;
  const segIdx   = Math.floor(shifted / 18) % 20;
  const number   = BOARD_ORDER[segIdx];

  if (rFrac <= R_TRIPLE_IN)  return { label: `S${number}`, score: number,   number, multiplier: 1, ring: 'inner_single' };
  if (rFrac <= R_TRIPLE_OUT) return { label: `T${number}`, score: number*3, number, multiplier: 3, ring: 'triple' };
  if (rFrac <= R_DOUBLE_IN)  return { label: `S${number}`, score: number,   number, multiplier: 1, ring: 'outer_single' };
  return                            { label: `D${number}`, score: number*2, number, multiplier: 2, ring: 'double' };
}

function showClickIndicator(svgEl, x, y) {
  const old = svgEl.querySelector('.click-ind');
  if (old) old.remove();
  const ns = 'http://www.w3.org/2000/svg';
  const g = document.createElementNS(ns, 'g');
  g.className = 'click-ind';
  g.innerHTML = `
    <circle cx="${x}" cy="${y}" r="12" fill="rgba(88,166,255,0.25)" stroke="#58a6ff" stroke-width="2"/>
    <circle cx="${x}" cy="${y}" r="3" fill="#58a6ff"/>`;
  svgEl.appendChild(g);
  setTimeout(() => g.remove(), 2200);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Dart Marker (main board)
// ═══════════════════════════════════════════════════════════════════════════════

// Warped space → SVG coords conversion
// Warped: center=400, radius=360
// SVG:    center=250, radius=218
const WARP_CX = 400, WARP_CY = 400, WARP_R = 360;

function warpedToSVG(tipPx) {
  const scale = RSVG / WARP_R;
  return {
    x: CX + (tipPx[0] - WARP_CX) * scale,
    y: CY + (tipPx[1] - WARP_CY) * scale,
  };
}

function showDartMarker(tipPx, label, score) {
  const svg = document.getElementById('dartboard-svg');
  if (!svg) return;

  const markerGroup = svg.querySelector(`#dart-markers-dartboard-svg`);
  const g = markerGroup || svg;

  // Remove old markers (keep last 3)
  const old = svg.querySelectorAll('.dart-hit');
  if (old.length >= 3) old[0].remove();

  const pos = warpedToSVG(tipPx);
  const ns = 'http://www.w3.org/2000/svg';
  const el = document.createElementNS(ns, 'g');
  el.className = 'dart-hit';

  const isHigh = pos.y > CY + RSVG * 0.6;
  const textY  = isHigh ? pos.y - 16 : pos.y + 20;

  el.innerHTML = `
    <circle cx="${pos.x.toFixed(1)}" cy="${pos.y.toFixed(1)}" r="9" fill="rgba(240,198,116,0.2)" stroke="#f0c674" stroke-width="2"/>
    <circle cx="${pos.x.toFixed(1)}" cy="${pos.y.toFixed(1)}" r="3" fill="#f0c674"/>
    <text x="${pos.x.toFixed(1)}" y="${textY.toFixed(1)}" text-anchor="middle" fill="#f0c674" font-size="12" font-weight="700" font-family="Arial,sans-serif"
          stroke="#000" stroke-width="3" paint-order="stroke">${label}</text>`;
  svg.appendChild(el);
}

function clearDartMarker() {
  const svg = document.getElementById('dartboard-svg');
  if (!svg) return;
  svg.querySelectorAll('.dart-hit').forEach(el => el.remove());
}

function buildMainBoard() {
  const svg = document.getElementById('dartboard-svg');
  if (svg) buildDartboard(svg, false);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Game Controls
// ═══════════════════════════════════════════════════════════════════════════════

async function undoThrow() {
  const data = await apiCall('/api/undo', 'POST');
  if (data && !data.ok) showToast('Rien à annuler', 'warning');
}

async function nextTurn() {
  await apiCall('/api/next-turn', 'POST');
  clearDartMarker();
}

async function correctLastThrow() {
  // Undo the last throw and open manual modal
  await undoThrow();
  setTimeout(() => openManualScore(), 300);
}

function showGameOver(winner) {
  document.getElementById('winner-name').textContent = winner;
  document.getElementById('game-over-overlay').classList.remove('hidden');
  showToast(`🏆 ${winner} a remporté la partie !`, 'success');
}

// ═══════════════════════════════════════════════════════════════════════════════
// Manual Score Modal
// ═══════════════════════════════════════════════════════════════════════════════

function openManualScore() {
  selectedScore = null;
  selectedNum   = null;

  // Reset UI
  document.getElementById('sel-score').textContent = '—';
  document.getElementById('sel-label').textContent = 'Sélectionnez une zone';
  document.getElementById('sel-ring').textContent  = '';
  document.getElementById('selected-display').classList.remove('has-score');
  document.getElementById('confirm-btn').disabled = true;
  document.getElementById('mult-section').style.display = 'none';
  document.querySelectorAll('.num-btn').forEach(b => b.classList.remove('selected'));
  document.querySelectorAll('.mult-btn').forEach(b => b.classList.remove('active'));

  // Build interactive dartboard
  const svg = document.getElementById('modal-dartboard');
  buildDartboard(svg, true);

  document.getElementById('manual-modal').classList.remove('hidden');
}

function closeManualScore() {
  document.getElementById('manual-modal').classList.add('hidden');
  selectedScore = null;
  selectedNum   = null;
}

// Called when clicking the board or a quick button
function applySelectedScore(score) {
  selectedScore = score;
  document.getElementById('sel-score').textContent = score.score;
  document.getElementById('sel-label').textContent = score.label;
  document.getElementById('sel-ring').textContent  = ringLabel(score.ring);
  document.getElementById('selected-display').classList.add('has-score');
  document.getElementById('confirm-btn').disabled = false;
  // Hide multiplier row since we have a full score from click
  document.getElementById('mult-section').style.display = 'none';
  document.querySelectorAll('.num-btn').forEach(b => b.classList.remove('selected'));
  document.querySelectorAll('.mult-btn').forEach(b => b.classList.remove('active'));
}

function selectQuickScore(label, score, number, multiplier, ring) {
  applySelectedScore({ label, score, number, multiplier, ring });
}

function selectNumber(num) {
  selectedNum = num;
  document.querySelectorAll('.num-btn').forEach(b => {
    b.classList.toggle('selected', parseInt(b.textContent) === num);
  });
  document.getElementById('mult-number').textContent = num;
  document.getElementById('mult-section').style.display = 'flex';
  document.querySelectorAll('.mult-btn').forEach(b => b.classList.remove('active'));

  // Reset score display until multiplier chosen
  document.getElementById('sel-score').textContent = '?';
  document.getElementById('sel-label').textContent = `${num} — Choisissez le multiplicateur`;
  document.getElementById('sel-ring').textContent  = '';
  document.getElementById('selected-display').classList.remove('has-score');
  document.getElementById('confirm-btn').disabled = true;
  selectedScore = null;
}

function applyMultiplier(mult) {
  if (selectedNum === null) return;

  const ringMap = { 1: 'outer_single', 2: 'double', 3: 'triple' };
  const labelMap = { 1: 'S', 2: 'D', 3: 'T' };
  const label = `${labelMap[mult]}${selectedNum}`;
  const score = selectedNum * mult;
  const ring  = ringMap[mult];

  applySelectedScore({ label, score, number: selectedNum, multiplier: mult, ring });

  // Highlight active mult button
  ['mb-single', 'mb-double', 'mb-triple'].forEach((id, i) => {
    document.getElementById(id).classList.toggle('active', i + 1 === mult);
  });
}

async function confirmManualScore() {
  if (!selectedScore) return;
  const ok = document.getElementById('confirm-btn');
  ok.disabled = true;
  ok.textContent = '⏳ Envoi…';

  try {
    await apiCall('/api/throw', 'POST', selectedScore);
    closeManualScore();
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  } finally {
    ok.disabled = false;
    ok.textContent = '✅ Valider';
  }
}

function ringLabel(ring) {
  return {
    double_bull:  'Double Bull (50 pts)',
    single_bull:  'Single Bull (25 pts)',
    inner_single: 'Simple (zone intérieure)',
    triple:       'Triple',
    outer_single: 'Simple (zone extérieure)',
    double:       'Double',
    outside:      'À côté — 0 pt',
  }[ring] || ring;
}

// ═══════════════════════════════════════════════════════════════════════════════
// Toast Notifications
// ═══════════════════════════════════════════════════════════════════════════════

function showToast(msg, type = 'info') {
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ️'}</span><span class="toast-msg">${escHtml(msg)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.animation = 'toastOut 0.28s ease forwards';
    setTimeout(() => toast.remove(), 290);
  }, 3800);
}

// ═══════════════════════════════════════════════════════════════════════════════
// API Helper
// ═══════════════════════════════════════════════════════════════════════════════

async function apiCall(url, method = 'GET', body = null) {
  const opts = { method, headers: {} };
  if (body) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
  const res = await fetch(url, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ═══════════════════════════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════════════════════════

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ═══════════════════════════════════════════════════════════════════════════════
// Keyboard Shortcuts
// ═══════════════════════════════════════════════════════════════════════════════

document.addEventListener('keydown', ev => {
  // Don't capture shortcuts when typing in inputs
  if (ev.target.tagName === 'INPUT') return;

  const gameVisible = document.getElementById('game-screen').style.display !== 'none';
  const modalOpen   = !document.getElementById('manual-modal').classList.contains('hidden');

  if (modalOpen) {
    if (ev.key === 'Escape') { closeManualScore(); return; }
    if (ev.key === 'Enter')  { confirmManualScore(); return; }
    return; // Don't process other shortcuts when modal open
  }

  if (!gameVisible) return;

  switch (ev.key.toLowerCase()) {
    case 'z': undoThrow();      break;
    case 'n': nextTurn();       break;
    case 'm': openManualScore(); break;
    case ' ':
      ev.preventDefault();
      captureAllReferences();
      break;
  }
});

// ═══════════════════════════════════════════════════════════════════════════════
// Debug Panel
// ═══════════════════════════════════════════════════════════════════════════════

let debugOpen      = false;
let debugLogPaused = false;
let debugPollTimer = null;
let debugStreamMode = {};   // cam_idx -> 'normal' | 'debug'
let configDefaults  = {};   // original values from server

function toggleDebug() {
  debugOpen = !debugOpen;
  const panel = document.getElementById('debug-panel');
  const btn   = document.getElementById('dbg-toggle-btn');
  if (debugOpen) {
    panel.classList.remove('hidden');
    btn.style.borderColor = 'var(--orange)';
    btn.style.color = 'var(--orange)';
    _initDebugPanel();
    _startDebugPoll();
    dbgLog('system', 'Panneau debug ouvert');
  } else {
    closeDebug();
  }
}

function closeDebug() {
  debugOpen = false;
  document.getElementById('debug-panel').classList.add('hidden');
  const btn = document.getElementById('dbg-toggle-btn');
  if (btn) { btn.style.borderColor = ''; btn.style.color = ''; }
  _stopDebugPoll();
}

// ── Debug camera cards ─────────────────────────────────────────────────────────

async function _initDebugPanel() {
  await _buildCamCards();
  await _buildParamSliders();
}

async function _buildCamCards() {
  const grid = document.getElementById('dbg-cam-grid');
  grid.innerHTML = '';

  if (!openCamIndexes.length) {
    grid.innerHTML = '<div style="color:var(--dimmed);font-size:0.8rem">Aucune caméra active</div>';
    return;
  }

  openCamIndexes.forEach(idx => {
    debugStreamMode[idx] = 'normal';
    const card = document.createElement('div');
    card.className = 'dbg-cam-card';
    card.id = `dbg-cam-${idx}`;
    card.innerHTML = `
      <div class="dbg-cam-stream-wrap" id="dbg-stream-wrap-${idx}" onclick="toggleDebugStream(${idx})">
        <img id="dbg-stream-img-${idx}" src="/camera/${idx}" alt="cam ${idx}" />
        <span class="dbg-stream-label" id="dbg-stream-lbl-${idx}">normal</span>
      </div>
      <div class="dbg-cam-info">
        <div class="dbg-cam-name">CAM ${idx}</div>
        <div class="dbg-cam-rows" id="dbg-cam-rows-${idx}">
          <div class="dbg-row"><span class="dbg-key">état</span>
            <span class="dbg-val" id="dbg-state-${idx}">—</span></div>
          <div class="dbg-row"><span class="dbg-key">diff score</span>
            <span class="dbg-val" id="dbg-diff-${idx}">—</span></div>
          <div class="dbg-row"><span class="dbg-key">contours</span>
            <span class="dbg-val" id="dbg-ctr-${idx}">—</span></div>
          <div class="dbg-row"><span class="dbg-key">tip (px)</span>
            <span class="dbg-val" id="dbg-tip-${idx}">—</span></div>
          <div class="dbg-row"><span class="dbg-key">stable cnt</span>
            <span class="dbg-val" id="dbg-stab-${idx}">—</span></div>
          <div class="dbg-row"><span class="dbg-key">cooldown</span>
            <span class="dbg-val" id="dbg-cool-${idx}">—</span></div>
          <div class="dbg-row"><span class="dbg-key">total lancers</span>
            <span class="dbg-val" id="dbg-darts-${idx}">—</span></div>
        </div>
        <button class="dbg-stream-toggle" id="dbg-stream-btn-${idx}" onclick="toggleDebugStream(${idx})">
          🔍 Activer overlay debug
        </button>
      </div>`;
    grid.appendChild(card);
  });
}

function toggleDebugStream(idx) {
  const isDebug = debugStreamMode[idx] === 'debug';
  debugStreamMode[idx] = isDebug ? 'normal' : 'debug';

  const img = document.getElementById(`dbg-stream-img-${idx}`);
  const lbl = document.getElementById(`dbg-stream-lbl-${idx}`);
  const btn = document.getElementById(`dbg-stream-btn-${idx}`);

  const src = debugStreamMode[idx] === 'debug'
    ? `/camera/debug/${idx}?t=${Date.now()}`
    : `/camera/${idx}`;
  if (img) img.src = src;
  if (lbl) lbl.textContent = debugStreamMode[idx];
  if (btn) {
    btn.textContent  = debugStreamMode[idx] === 'debug' ? '📷 Vue normale' : '🔍 Activer overlay debug';
    btn.className    = `dbg-stream-toggle ${debugStreamMode[idx] === 'debug' ? 'active' : ''}`;
  }

  // Also update the main camera feed in the left panel
  const mainImg = document.querySelector(`#cam-box-${idx} img`);
  if (mainImg) mainImg.src = src;
}

// ── Parameter sliders ──────────────────────────────────────────────────────────

async function _buildParamSliders() {
  const grid = document.getElementById('dbg-params-grid');
  grid.innerHTML = '<div style="color:var(--dimmed);font-size:0.75rem">Chargement…</div>';

  try {
    const cfg = await apiCall('/api/config');
    configDefaults = {};
    grid.innerHTML = '';

    Object.entries(cfg).forEach(([key, info]) => {
      configDefaults[key] = info.value;
      const step = info.type === 'float' ? 0.1 : 1;
      const row = document.createElement('div');
      row.className = 'dbg-param-row';
      row.innerHTML = `
        <span class="dbg-param-name" title="${key}">${key}</span>
        <input type="range" class="dbg-param-slider" id="slider-${key}"
               min="${info.min}" max="${info.max}" step="${step}" value="${info.value}"
               oninput="onSliderInput('${key}', this.value)" />
        <span class="dbg-param-val" id="val-${key}">${info.value}</span>`;
      grid.appendChild(row);
    });
  } catch (err) {
    grid.innerHTML = `<div style="color:var(--red);font-size:0.75rem">Erreur: ${escHtml(err.message)}</div>`;
  }
}

let _sliderDebounce = {};
function onSliderInput(key, rawVal) {
  document.getElementById(`val-${key}`).textContent = rawVal;
  clearTimeout(_sliderDebounce[key]);
  _sliderDebounce[key] = setTimeout(() => _applyParam(key, rawVal), 350);
}

async function _applyParam(key, val) {
  try {
    const data = await apiCall('/api/config', 'POST', { params: { [key]: parseFloat(val) } });
    if (data.errors && data.errors[key]) {
      showToast(`Paramètre ${key}: ${data.errors[key]}`, 'error');
    } else {
      dbgLog('config', `${key} = ${val}`);
    }
  } catch (err) {
    showToast(`Erreur config: ${err.message}`, 'error');
  }
}

async function resetConfig() {
  if (!Object.keys(configDefaults).length) return;
  try {
    await apiCall('/api/config', 'POST', { params: configDefaults });
    // Reset sliders
    Object.entries(configDefaults).forEach(([key, val]) => {
      const sl = document.getElementById(`slider-${key}`);
      const vl = document.getElementById(`val-${key}`);
      if (sl) sl.value = val;
      if (vl) vl.textContent = val;
    });
    dbgLog('system', 'Paramètres réinitialisés aux valeurs par défaut');
    showToast('Paramètres réinitialisés', 'info');
  } catch {}
}

// ── Debug polling ──────────────────────────────────────────────────────────────

function _startDebugPoll() {
  _stopDebugPoll();
  debugPollTimer = setInterval(_pollDebugCameras, 500);
}

function _stopDebugPoll() {
  clearInterval(debugPollTimer);
  debugPollTimer = null;
}

async function _pollDebugCameras() {
  if (!debugOpen || !openCamIndexes.length) return;
  try {
    const data = await apiCall('/api/debug/cameras');
    Object.entries(data).forEach(([idx, info]) => {
      _updateCamDebugCard(parseInt(idx), info);
    });
  } catch {}
}

function _updateCamDebugCard(idx, info) {
  const stEl   = document.getElementById(`dbg-state-${idx}`);
  const dfEl   = document.getElementById(`dbg-diff-${idx}`);
  const ctEl   = document.getElementById(`dbg-ctr-${idx}`);
  const tipEl  = document.getElementById(`dbg-tip-${idx}`);
  const stbEl  = document.getElementById(`dbg-stab-${idx}`);
  const coolEl = document.getElementById(`dbg-cool-${idx}`);
  const drtEl  = document.getElementById(`dbg-darts-${idx}`);

  if (stEl) {
    stEl.textContent = info.state;
    stEl.className   = `dbg-val state-${info.state}`;
  }
  if (dfEl)   dfEl.textContent  = info.diff_score;
  if (ctEl)   ctEl.textContent  = info.contour_count;
  if (tipEl)  tipEl.textContent = info.tip ? `(${info.tip[0]}, ${info.tip[1]})` : '—';
  if (stbEl)  stbEl.textContent = info.stable_count;
  if (coolEl) coolEl.textContent= info.cooldown;
  if (drtEl)  drtEl.textContent = info.dart_count;
}

// ── Debug log ──────────────────────────────────────────────────────────────────

const MAX_LOG_ENTRIES = 200;

function dbgLog(type, msg) {
  if (debugLogPaused) return;
  const log = document.getElementById('dbg-log');
  if (!log) return;

  const empty = log.querySelector('.dbg-log-empty');
  if (empty) empty.remove();

  const now  = new Date();
  const time = now.toTimeString().slice(0, 8) + '.' + String(now.getMilliseconds()).padStart(3, '0');

  const entry = document.createElement('div');
  entry.className = `dbg-entry ${type}`;
  entry.innerHTML = `
    <span class="dbg-entry-time">${time}</span>
    <span class="dbg-entry-type">${escHtml(type.toUpperCase())}</span>
    <span class="dbg-entry-msg">${escHtml(msg)}</span>`;

  log.insertBefore(entry, log.firstChild);

  // Prune old entries
  const entries = log.querySelectorAll('.dbg-entry');
  if (entries.length > MAX_LOG_ENTRIES) entries[entries.length - 1].remove();
}

function toggleDebugLog() {
  debugLogPaused = !debugLogPaused;
  const btn = document.getElementById('dbg-pause-log-btn');
  if (btn) {
    btn.textContent  = debugLogPaused ? '▶ Log' : '⏸ Log';
    btn.className    = `dbg-pill ${debugLogPaused ? 'active' : ''}`;
  }
}

function clearDebugLog() {
  const log = document.getElementById('dbg-log');
  if (log) log.innerHTML = '<div class="dbg-log-empty">Journal vidé.</div>';
}


// ═══════════════════════════════════════════════════════════════════════════════
// Calibration
// ═══════════════════════════════════════════════════════════════════════════════

const CALIB_LABELS   = ['20 (haut)', '6 (droite)', '3 (bas)', '11 (gauche)'];
const CALIB_COLORS   = ['#FFD700', '#00E676', '#FF5252', '#40C4FF'];

let calibPoints     = [];   // [{x, y}] in canvas display space
let calibDisplayW   = 0;    // canvas CSS width at time of click
let calibDisplayH   = 0;    // canvas CSS height at time of click
let calibCamIdx     = null; // camera being calibrated
let calibLastPoints = null; // saved for recompute with segment override

function openCalibration() {
  calibPoints = [];
  calibLastPoints = null;
  calibCamIdx = null;
  _showCalibStep('choose');
  document.getElementById('calib-modal').classList.remove('hidden');
  _refreshCalibStatus();
}

function closeCalibration() {
  document.getElementById('calib-modal').classList.add('hidden');
  // Release camera if open
  if (calibCamIdx !== null) {
    apiCall('/api/calibration/close', 'POST').catch(() => {});
    calibCamIdx = null;
  }
}

function _showCalibStep(name) {
  ['choose', 'click', 'preview'].forEach(s => {
    document.getElementById(`calib-step-${s}`).style.display = s === name ? 'flex' : 'none';
  });
}

async function _refreshCalibStatus() {
  try {
    const data = await apiCall('/api/calibration/status');

    // ── Status chips (saved + pending) ─────────────────────────────────────────
    const bar = document.getElementById('calib-status-bar');
    if (!data.saved.length && !data.pending.length) {
      bar.innerHTML = '<span style="color:var(--dimmed);font-size:0.8rem">Aucune calibration enregistrée</span>';
    } else {
      const savedIdx   = new Set(data.saved.map(c => c.cam_index));
      const pendingIdx = new Set(data.pending.map(c => c.cam_index));
      const chips = [
        ...data.saved.map(c =>
          `<span class="calib-cam-chip">✓ Cam ${c.cam_index} — seg ${c.segment ?? '?'}</span>`),
        ...data.pending.map(c =>
          `<span class="calib-cam-chip pending">⏳ Cam ${c.cam_index} (non sauvegardé)</span>`),
      ];
      bar.innerHTML = chips.join('');
    }

    // ── Camera shortcut buttons (only when a game is active) ───────────────────
    const btnArea  = document.getElementById('calib-cam-buttons');
    const gameCams = data.game_cams || [];

    if (gameCams.length > 0) {
      const savedIdx   = new Set(data.saved.map(c => c.cam_index));
      const pendingIdx = new Set(data.pending.map(c => c.cam_index));
      btnArea.style.display = 'flex';
      const allDone = gameCams.every(i => savedIdx.has(i) || pendingIdx.has(i));
      btnArea.innerHTML = gameCams.map(idx => {
        const done    = savedIdx.has(idx);
        const pend    = pendingIdx.has(idx);
        const cls     = done ? 'calib-cam-btn done' : pend ? 'calib-cam-btn pending' : 'calib-cam-btn todo';
        const icon    = done ? '✓' : pend ? '⏳' : '📷';
        return `<button class="${cls}" onclick="calibOpenCameraIdx(${idx})">${icon} Cam ${idx}</button>`;
      }).join('') + (allDone
        ? `<div class="calib-all-done">✅ Toutes les caméras calibrées — cliquez Sauvegarder</div>`
        : '');
    } else {
      btnArea.style.display = 'none';
    }

    _updateSaveBtn();
  } catch {}
}

function calibOpenCameraIdx(idx) {
  document.getElementById('calib-cam-input').value = idx;
  calibOpenCamera();
}

async function calibOpenCamera() {
  const idx = parseInt(document.getElementById('calib-cam-input').value);
  if (isNaN(idx)) { showToast('Index caméra invalide', 'error'); return; }

  try {
    await apiCall(`/api/calibration/open/${idx}`, 'POST');
    calibCamIdx = idx;
    calibPoints = [];

    // Switch to click step
    _showCalibStep('click');
    const img = document.getElementById('calib-stream-img');
    img.src = `/camera/raw/${idx}?t=${Date.now()}`;

    // Wait for img to load then sync canvas
    img.onload = _syncCalibCanvas;
    // Also sync on resize
    new ResizeObserver(_syncCalibCanvas).observe(img);

    _updateCalibProgress();
    document.getElementById('calib-compute-btn').style.display = 'none';

    // Attach click handler to canvas
    const canvas = document.getElementById('calib-canvas');
    canvas.onclick = _handleCalibClick;

    showToast(`Caméra ${idx} ouverte — cliquez les 4 points`, 'info');
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  }
}

function _syncCalibCanvas() {
  const img    = document.getElementById('calib-stream-img');
  const canvas = document.getElementById('calib-canvas');
  const rect   = img.getBoundingClientRect();
  canvas.width  = rect.width;
  canvas.height = rect.height;
  calibDisplayW = rect.width;
  calibDisplayH = rect.height;
  _drawCalibPoints();
}

function _handleCalibClick(ev) {
  if (calibPoints.length >= 4) return;
  const canvas = document.getElementById('calib-canvas');
  const rect   = canvas.getBoundingClientRect();
  calibPoints.push({ x: ev.clientX - rect.left, y: ev.clientY - rect.top });
  calibDisplayW = rect.width;
  calibDisplayH = rect.height;
  _drawCalibPoints();
  _updateCalibProgress();

  if (calibPoints.length === 4) {
    document.getElementById('calib-compute-btn').style.display = 'block';
    document.getElementById('calib-instruction').innerHTML =
      '✅ 4 points placés — cliquez <strong>Calculer l\'homographie</strong>';
  }
}

function _drawCalibPoints() {
  const canvas = document.getElementById('calib-canvas');
  const ctx    = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Connection polygon
  if (calibPoints.length > 1) {
    ctx.beginPath();
    calibPoints.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
    if (calibPoints.length === 4) ctx.closePath();
    ctx.strokeStyle = 'rgba(255,255,255,0.35)';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  // Points
  calibPoints.forEach((p, i) => {
    const color = CALIB_COLORS[i];

    // Outer ring
    ctx.beginPath();
    ctx.arc(p.x, p.y, 10, 0, Math.PI * 2);
    ctx.fillStyle = color + '44';
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Center dot
    ctx.beginPath();
    ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();

    // Label
    ctx.font = 'bold 12px Arial, sans-serif';
    ctx.fillStyle = '#fff';
    ctx.strokeStyle = '#000';
    ctx.lineWidth = 3;
    const lx = p.x + 14, ly = p.y - 8;
    ctx.strokeText(CALIB_LABELS[i], lx, ly);
    ctx.fillText(CALIB_LABELS[i], lx, ly);
  });

  // Next point indicator (pulsing crosshair hint)
  if (calibPoints.length < 4) {
    const n = calibPoints.length;
    ctx.font = 'bold 13px Arial, sans-serif';
    ctx.fillStyle = CALIB_COLORS[n];
    ctx.strokeStyle = '#000';
    ctx.lineWidth = 3;
    const hint = `${n + 1}. Cliquez ${CALIB_LABELS[n]}`;
    ctx.strokeText(hint, 10, canvas.height - 12);
    ctx.fillText(hint, 10, canvas.height - 12);
  }
}

function _updateCalibProgress() {
  const steps = document.querySelectorAll('.cp-step');
  steps.forEach((el, i) => {
    el.classList.remove('active', 'done');
    if (i < calibPoints.length) el.classList.add('done');
    else if (i === calibPoints.length) el.classList.add('active');
  });

  const n = calibPoints.length;
  const instrEl = document.getElementById('calib-instruction');
  const nextLbl = document.getElementById('calib-next-label');
  if (n < 4 && instrEl && nextLbl) {
    nextLbl.textContent = CALIB_LABELS[n];
    instrEl.style.display = 'block';
  } else if (instrEl) {
    instrEl.style.display = 'none';
  }
}

function calibUndo() {
  if (!calibPoints.length) return;
  calibPoints.pop();
  document.getElementById('calib-compute-btn').style.display = 'none';
  _drawCalibPoints();
  _updateCalibProgress();
}

function calibReset() {
  calibPoints = [];
  document.getElementById('calib-compute-btn').style.display = 'none';
  _drawCalibPoints();
  _updateCalibProgress();
  document.getElementById('calib-instruction').style.display = 'block';
}

async function calibCompute() {
  if (calibPoints.length !== 4) return;
  calibLastPoints = [...calibPoints];
  await _doCompute();
}

async function calibRecompute() {
  if (!calibLastPoints) return;
  await _doCompute();
}

async function _doCompute() {
  const btn = document.getElementById('calib-compute-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Calcul…'; }

  const override = document.getElementById('calib-segment-override')?.value;

  try {
    const data = await apiCall('/api/calibration/compute', 'POST', {
      cam_idx:   calibCamIdx,
      points:    calibLastPoints.map(p => [p.x, p.y]),
      display_w: calibDisplayW,
      display_h: calibDisplayH,
      segment:   override ? parseInt(override) : null,
    });

    // Show preview step
    _showCalibStep('preview');
    document.getElementById('calib-segment-detected').textContent =
      `${data.segment} (auto: ${data.auto_segment})`;

    // Raw snapshot: just grab the current stream frame as preview thumbnail
    const rawImg = document.getElementById('calib-raw-snapshot');
    rawImg.src = `/camera/raw/${calibCamIdx}?t=${Date.now()}`;

    // Warped preview (base64)
    if (data.preview) {
      document.getElementById('calib-preview-img').src = 'data:image/jpeg;base64,' + data.preview;
    }

    // Show save button if pending calibrations exist
    _updateSaveBtn();

    showToast(`Homographie calculée — segment ${data.segment}`, 'success');
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '✅ Calculer l\'homographie'; }
  }
}

async function _updateSaveBtn() {
  try {
    const data = await apiCall('/api/calibration/status');
    const saveBtn = document.getElementById('calib-save-btn');
    if (saveBtn) saveBtn.style.display = data.pending.length ? 'block' : 'none';
  } catch {}
}

async function calibConfirm() {
  // Save immediately after confirming — no separate Save step needed
  try {
    const data = await apiCall('/api/calibration/save', 'POST');
    showToast(`✅ Cam ${calibCamIdx} sauvegardée (${data.total} caméra(s) au total)`, 'success');
  } catch (err) {
    showToast(`Erreur sauvegarde: ${err.message}`, 'error');
  }
  calibPoints = [];
  calibLastPoints = null;
  const nextIdx = (calibCamIdx ?? 0) + 1;
  document.getElementById('calib-cam-input').value = nextIdx;
  _showCalibStep('choose');
  _refreshCalibStatus();
}

function calibBackToClick() {
  calibPoints = calibLastPoints ? [...calibLastPoints] : [];
  _showCalibStep('click');
  _syncCalibCanvas();
  _updateCalibProgress();
  document.getElementById('calib-compute-btn').style.display =
    calibPoints.length === 4 ? 'block' : 'none';
}

async function calibSave() {
  const btn = document.getElementById('calib-save-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Sauvegarde…';
  try {
    const data = await apiCall('/api/calibration/save', 'POST');
    showToast(`💾 ${data.saved} caméra(s) sauvegardée(s) dans calibration.json`, 'success');
    _refreshCalibStatus();
    btn.style.display = 'none';
  } catch (err) {
    showToast(`Erreur: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '💾 Sauvegarder tout';
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// Initialization
// ═══════════════════════════════════════════════════════════════════════════════

window.addEventListener('load', () => {
  // Show setup screen by default
  document.getElementById('game-screen').style.display = 'none';
  document.getElementById('setup-screen').style.display = 'flex';
});
