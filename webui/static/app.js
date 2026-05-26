/* ===================================================================
 * DART-ESIR — app.js
 * Shared layer: WebSocket client (DartWS), connection-status pill,
 * disconnect-freeze, live clock. Chaque page (setup/game/calib/end) ajoute
 * son propre script pour la logique spécifique.
 *
 * Aucune dépendance, vanilla JS. Compatible Chromium >=90 / iOS 15+.
 * ================================================================= */

'use strict';

// ───────────────────────────────────────────────────────────────────
// DartWS : client WS avec reconnect exponentiel + dispatcher pub/sub
// ───────────────────────────────────────────────────────────────────
class DartWS {
  constructor() {
    this.url = `ws://${location.host}/ws`;
    this.ws = null;
    this.connected = false;
    this.handlers = Object.create(null);
    // Backoff : 1s, 2s, 4s, 8s, 16s, 30s, 30s, …
    this.retryDelay = 1000;
    this.maxDelay   = 30000;
    this._retryTimer = null;
    // Dernier snapshot reçu — utile aux pages qui veulent se rendre dès load
    // sans attendre un nouvel event.
    this.lastSnapshot = null;
    // Dernier system_status reçu, idem.
    this.lastStatus = null;
  }

  /** Enregistre un callback `(payload) => void` pour un type d'event WS. */
  on(type, cb) {
    (this.handlers[type] || (this.handlers[type] = [])).push(cb);
    return this;
  }

  /** Envoie une commande au serveur. Retourne false si WS pas prêt. */
  send(type, payload = {}) {
    if (!this.connected || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn(`[ws] send(${type}) ignoré: pas connecté`);
      return false;
    }
    this.ws.send(JSON.stringify({ type, payload }));
    return true;
  }

  connect() {
    if (this._retryTimer) { clearTimeout(this._retryTimer); this._retryTimer = null; }
    const sock = new WebSocket(this.url);
    this.ws = sock;

    sock.onopen = () => {
      console.log('[ws] connecté');
      this.connected = true;
      this.retryDelay = 1000;  // reset backoff
      this._emit('_status', { connected: true });
    };

    sock.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); }
      catch (e) { console.warn('[ws] message non-JSON', e); return; }
      if (msg.type === 'snapshot') this.lastSnapshot = msg.payload;
      if (msg.type === 'system_status') this.lastStatus = msg.payload;
      this._emit(msg.type, msg.payload);
    };

    sock.onclose = () => {
      // Si on a déjà été remplacé par une nouvelle socket entre temps, ignore
      if (this.ws !== sock) return;
      console.log(`[ws] fermée, reconnexion dans ${this.retryDelay}ms`);
      this.connected = false;
      this._emit('_status', { connected: false });
      this._retryTimer = setTimeout(() => this.connect(), this.retryDelay);
      this.retryDelay = Math.min(this.retryDelay * 2, this.maxDelay);
    };

    sock.onerror = (e) => {
      // Ne pas appeler _emit ici — onclose va suivre.
      console.warn('[ws] erreur', e);
    };
  }

  _emit(type, payload) {
    const list = this.handlers[type];
    if (!list) return;
    for (const cb of list) {
      try { cb(payload); }
      catch (e) { console.error(`[ws] handler ${type} crashed`, e); }
    }
  }
}

// Instance unique partagée par toutes les pages.
const ws = new DartWS();

// ───────────────────────────────────────────────────────────────────
// CSS injecté : pastille WS + état désactivé des boutons d'action.
// On n'écrit pas dans style.css du design, on s'ajoute en tête de <head>.
// ───────────────────────────────────────────────────────────────────
(function injectAppCSS() {
  const css = `
    .ws-status {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 10px; margin-right: 10px;
      border: 1.5px solid var(--ink, #171717);
      font-family: 'IBM Plex Mono', monospace;
      font-size: 10px; letter-spacing: 0.18em; text-transform: uppercase;
      color: var(--ink, #171717);
      background: transparent;
      transition: background 180ms ease, color 180ms ease;
    }
    .ws-status::before {
      content: ''; display: inline-block; width: 8px; height: 8px;
      background: var(--success, #1e7a3a);
      border-radius: 50%;
    }
    .ws-status--down {
      background: var(--danger, #c8341a);
      color: var(--paper, #f3ecdb);
      border-color: var(--danger, #c8341a);
      animation: ws-pulse 1.2s ease-in-out infinite;
    }
    .ws-status--down::before {
      background: var(--paper, #f3ecdb);
    }
    @keyframes ws-pulse {
      0%, 100% { opacity: 1; }
      50%      { opacity: 0.6; }
    }
    /* Freeze des boutons d'action quand WS down.
       On ne gèle PAS les boutons de modal close, ni les inputs setup,
       ni les boutons mode/+joueur. Sélecteur ciblé sur ce qui envoie
       une commande WS. */
    body.is-disconnected .actions .btn,
    body.is-disconnected .modal__action,
    body.is-disconnected .checklist__box {
      pointer-events: none;
      opacity: 0.45;
      cursor: not-allowed;
    }
    /* La devbar du design était un helper pour tester les états sans backend.
       Côté prod (backend câblé) elle n'a plus de sens — on la masque
       proprement plutôt que de supprimer le HTML qui appartient au design. */
    .devbar { display: none !important; }
    /* Bouton désactivé indépendamment de la déco WS (ex: recalibration en
       headless). On signale visuellement avec hachures + couleur paper pour
       qu'on comprenne que c'est volontaire, pas un bug.
       !important partout : .btn--primary applique background shorthand qui
       reset background-image, et nos sélecteurs ont la même spécificité —
       sans !important, certaines règles sont écrasées. */
    .btn.is-disabled-headless,
    .btn[disabled] {
      opacity: 0.5 !important;
      cursor: not-allowed !important;
      pointer-events: none !important;
      background-color: var(--paper, #f3ecdb) !important;
      color: var(--ink, #171717) !important;
      background-image: repeating-linear-gradient(
        135deg,
        transparent 0 6px,
        rgba(23, 23, 23, 0.22) 6px 8px
      ) !important;
    }
    /* Bonus : annule l'éventuel ::after de .btn--primary (overlay pink hover)
       quand le bouton est désactivé — il ne doit pas trahir un effet "actif". */
    .btn.is-disabled-headless::after,
    .btn[disabled]::after { display: none !important; }
    /* Toast UI : message éphémère en bas d'écran, non-bloquant. */
    .app-toast {
      position: fixed; left: 50%; bottom: 60px;
      transform: translateX(-50%);
      max-width: 80vw;
      padding: 12px 24px;
      background: var(--ink, #171717); color: var(--paper, #f3ecdb);
      border: 1.5px solid var(--ink, #171717);
      font-family: 'Anton', sans-serif;
      font-size: 16px; letter-spacing: 0.08em; text-transform: uppercase;
      box-shadow: 6px 6px 0 var(--pink, #ff5b9c);
      z-index: 9999;
      animation: toast-in 180ms ease forwards;
    }
    .app-toast--out {
      animation: toast-out 220ms ease forwards;
    }
    @keyframes toast-in {
      from { opacity: 0; transform: translate(-50%, 10px); }
      to   { opacity: 1; transform: translate(-50%, 0); }
    }
    @keyframes toast-out {
      to { opacity: 0; transform: translate(-50%, 10px); }
    }
  `;
  const style = document.createElement('style');
  style.id = 'app-css';
  style.textContent = css;
  document.head.appendChild(style);
})();

// ───────────────────────────────────────────────────────────────────
// Pastille WS dans la topbar + freeze des boutons sur disconnect.
// ───────────────────────────────────────────────────────────────────
function setupConnectionPill() {
  const right = document.querySelector('.topbar__right');
  if (!right) return;
  let pill = document.getElementById('ws-status');
  if (!pill) {
    pill = document.createElement('span');
    pill.id = 'ws-status';
    pill.className = 'ws-status';
    pill.textContent = 'WS';
    pill.title = 'Connexion temps réel';
    right.insertBefore(pill, right.firstChild);
  }

  ws.on('_status', ({ connected }) => {
    pill.classList.toggle('ws-status--down', !connected);
    pill.textContent = connected ? 'WS' : 'HORS-LIGNE';
    document.body.classList.toggle('is-disconnected', !connected);
  });
}

// ───────────────────────────────────────────────────────────────────
// Horloge live (HH:MM, tick toutes les 30s)
// ───────────────────────────────────────────────────────────────────
function setupClock() {
  const el = document.getElementById('clock');
  if (!el) return;
  const tick = () => {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    el.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  tick();
  setInterval(tick, 30 * 1000);
}

// ───────────────────────────────────────────────────────────────────
// Helpers d'I/O DOM mutualisés
// ───────────────────────────────────────────────────────────────────
function $(sel, root = document) { return root.querySelector(sel); }
function $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

/** Affiche un toast non-bloquant pendant `durationMs`. Auto-stack si plusieurs. */
function showToast(text, durationMs = 3000) {
  const el = document.createElement('div');
  el.className = 'app-toast';
  el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => {
    el.classList.add('app-toast--out');
    setTimeout(() => el.remove(), 250);
  }, durationMs);
}

/** Échappe une chaîne pour usage dans innerHTML (XSS-safe). */
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

// ───────────────────────────────────────────────────────────────────
// Bandeau système (utilisé par setup.html ET game.html via la topbar)
// Met à jour les LEDs cam dans .topbar__right + la card par cam dans
// le bandeau système de setup.html si présent.
// ───────────────────────────────────────────────────────────────────
function updateTopbarCams(status) {
  if (!status || !status.cams) return;
  const leds = $$('.topbar__right .cam-status .cam-led');
  status.cams.forEach((cam, i) => {
    const led = leds[i];
    if (!led) return;
    led.classList.toggle('is-ok',  !!cam.ok);
    led.classList.toggle('is-err', !cam.ok);
  });
  // Texte "N cams · …"
  const txt = $('.topbar__right .cam-status > span:last-child');
  if (txt) {
    const okCount = status.cams.filter(c => c.ok).length;
    txt.textContent = `${okCount}/${status.cams.length} cams${okCount === 0 ? ' · KO' : ''}`;
  }
}

// ───────────────────────────────────────────────────────────────────
// Modal handling : ouverture/fermeture par classe body.state-menu
// Routes la fermeture (croix, backdrop, ESC) — partagée par toutes les pages.
// ───────────────────────────────────────────────────────────────────
function setupModalRouting() {
  const closeMenu = () => document.body.classList.remove('state-menu', 'state-bust', 'state-win');

  $$('[data-action="close-state"]').forEach(el =>
    el.addEventListener('click', closeMenu));

  $$('.modal').forEach(modal => {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeMenu();
    });
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeMenu();
  });
}

// ───────────────────────────────────────────────────────────────────
// Boot commun à toutes les pages.
// ───────────────────────────────────────────────────────────────────
function appBoot() {
  setupConnectionPill();
  setupClock();
  setupModalRouting();
  // La pastille démarre en "déconnecté" jusqu'à la 1re connexion.
  document.body.classList.add('is-disconnected');
  ws.on('system_status', updateTopbarCams);
  ws.connect();
}

window.addEventListener('load', appBoot);

// ───────────────────────────────────────────────────────────────────
// Toast automatique sur ack en erreur — toute commande qui échoue côté
// serveur remonte ici. Les pages spécifiques peuvent toujours s'abonner
// à 'ack' pour réagir spécifiquement (ex: setup.js qui navigue).
// ───────────────────────────────────────────────────────────────────
ws.on('ack', (p) => {
  if (p && p.ok === false && p.msg) {
    showToast(`Erreur: ${p.msg}`, 4000);
  }
});

// ───────────────────────────────────────────────────────────────────
// Détection "partie perdue" : si un nouveau snapshot arrive avec un
// game_id différent APRÈS une déco, l'utilisateur a perdu son état
// (typiquement crash + redémarrage main.py). On le signale via toast.
// Les resets volontaires (clic Reset / Démarrer) ne déclenchent rien
// parce qu'ils ne passent pas par une phase déconnectée.
// ───────────────────────────────────────────────────────────────────
let _lastGameId = null;
let _wasDisconnected = false;

ws.on('snapshot', (snap) => {
  const newId = snap && snap.game_id;
  if (_wasDisconnected && _lastGameId && newId && newId !== _lastGameId) {
    showToast('Partie perdue — nouvelle partie démarrée par le serveur');
  }
  if (newId) _lastGameId = newId;
  _wasDisconnected = false;
});
ws.on('_status', ({ connected }) => {
  if (!connected) _wasDisconnected = true;
});

// Expose pour les scripts par-page.
window.DartApp = { ws, $, $$, esc, showToast, updateTopbarCams };
