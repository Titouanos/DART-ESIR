#!/usr/bin/env python3
"""
DartVision — service d'annonce vocale (process séparé).

Écoute les events de jeu en UDP (127.0.0.1:9877, émis par le bridge) et les
annonce à l'oral en français → lecture sur le sink audio par défaut
(l'enceinte Bluetooth une fois appairée et définie défaut).

Synthèse : **Piper** (TTS neuronal, voix « tom » FR — qualité commentateur)
si présent, sinon repli sur espeak-ng. Piper lit le texte sur stdin et écrit
un WAV. Voir [[dartvision-audio]].

Lecture via `paplay` (couche Pulse) et NON `pw-play` : sur ce Pi la métadonnée
PipeWire `default.audio.sink` n'est pas renseignée → `pw-play` échoue avec
« no target node available ». `paplay` passe par la couche pulse qui connaît
le sink par défaut (celui de `pactl info`). Voir [[dartvision-pi-deploy]].

Découplé comme le service LED : fire-and-forget côté bridge, le jeu tourne
même si l'audio est absent.
"""

import json
import socket
import subprocess
import sys
import tempfile
import os
import time
import queue
import threading

UDP_PORT = 9877
WAV = os.path.join(tempfile.gettempdir(), "dart_announce.wav")

# Events dont l'annonce ne doit JAMAIS être sautée pour cause de retard.
PRIORITY_EVENTS = {"bust", "game_over", "player_change", "test_audio"}
# Une annonce de lancer plus vieille que ça (= la voix a pris du retard sur le
# jeu) est sautée pour recoller à l'affichage plutôt que de débiter le passé.
STALE_S = 4.0

# État voix, piloté par l'UI via l'event UDP audio_config (relayé par le bridge).
_audio_enabled = True
_audio_volume = 80   # 0..100, appliqué au volume linéaire paplay (0..65536)

# Piper TTS (voix naturelle). Si le binaire/modèle sont absents → repli espeak.
PIPER_BIN = "/home/rt/piper-tts/piper/piper"
PIPER_MODEL = "/home/rt/piper-tts/voices/fr_FR-tom-medium.onnx"
USE_PIPER = os.path.isfile(PIPER_BIN) and os.path.isfile(PIPER_MODEL)

NUM_FR = {
    0: "zéro", 25: "bull",
}


def seg_phrase(payload):
    """Construit la phrase FR d'un lancer à partir du score_data."""
    label = (payload.get("label") or "").upper()
    ring = (payload.get("ring") or "").lower()
    num = payload.get("number", 0)
    mult = payload.get("multiplier", 1)
    if label == "MISS" or payload.get("score", 0) == 0 and ring in ("outside", "miss", ""):
        return "manqué"
    if "double_bull" in ring or label == "D-BULL":
        return "double bull, cinquante"
    if "bull" in ring or label in ("S-BULL", "BULL"):
        return "bull, vingt-cinq"
    pre = {2: "double ", 3: "triple "}.get(mult, "")
    return f"{pre}{num}"


def synth(text):
    """Synthétise `text` → WAV. Piper si dispo (voix naturelle), sinon espeak."""
    if USE_PIPER:
        # Piper lit le texte sur stdin et écrit le WAV demandé.
        subprocess.run([PIPER_BIN, "--model", PIPER_MODEL, "--output_file", WAV],
                       input=text.encode("utf-8"), check=True, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["espeak-ng", "-v", "fr", "-s", "150", "-w", WAV, text],
                       check=True, timeout=10)


def say(text):
    """Synthèse FR → lecture sur le sink par défaut (paplay)."""
    if not text:
        return
    try:
        synth(text)
        # paplay (couche pulse) envoie vers le sink par défaut (= enceinte BT).
        # --volume : 0..65536 (linéaire PA, 65536 = 100%). check=True → un échec
        # de lecture remonte dans le journal (pas avalé comme avec pw-play).
        vol = max(0, min(65536, int(_audio_volume / 100 * 65536)))
        subprocess.run(["paplay", f"--volume={vol}", WAV], check=True, timeout=15)
    except Exception as e:
        print(f"[ANNOUNCE] échec '{text}': {e}", flush=True)


def _label_speech(lbl):
    """Label de fléchette ('T20','D16','BULL','S5') → texte FR pour la synthèse
    (Piper/espeak lisent les nombres en français)."""
    if lbl in ("BULL", "SB", "25"):
        return "bull"
    head = {"T": "triple ", "D": "double ", "S": ""}.get(lbl[0], "")
    return head + lbl[1:]


def phrase_for(event_type, p):
    """Phrase à dire pour un event de jeu, ou None si rien à annoncer."""
    if event_type == "throw":
        # bust géré par l'event bust ; ici on annonce juste le segment touché
        return seg_phrase(p)
    if event_type == "bust":
        return f"{p.get('player','')}, bust, zéro point"
    if event_type == "turn_end":
        return None if p.get("busted") else f"{p.get('player','')}, {p.get('total', 0)} points"
    if event_type == "player_change":
        base = f"{p.get('name','')}, à toi de jouer"
        co = p.get("checkout") or []
        if co:
            return f"{base}. Pour finir : {', '.join(_label_speech(l) for l in co)}."
        return base
    if event_type == "game_over":
        return f"{p.get('winner_name','')} remporte la partie ! Bravo !"
    if event_type == "test_audio":
        return "Test de l'annonce vocale. Le son fonctionne."
    return None


# File d'annonces alimentée par le thread récepteur, consommée par le thread
# lecteur. Découpler la réception UDP de la lecture (bloquante : synth Piper +
# paplay ≈ 2-3 s) évite que les events s'empilent dans le buffer du socket et
# que la voix débite le jeu avec plusieurs secondes de retard.
_announce_q: "queue.Queue" = queue.Queue()
# Vrai entre un game_over et le game_reset suivant : on n'annonce plus les
# events de jeu résiduels (la voix doit s'arrêter à la fin de la partie).
_game_over = threading.Event()


def _drain(q):
    """Vide la file sans bloquer."""
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def _player_loop():
    """Consomme la file et joue les annonces, une à la fois."""
    while True:
        ts, event_type, p = _announce_q.get()
        # Resynchro : si la voix a pris du retard, on saute les annonces de
        # lancer/total périmées (un event plus récent attend déjà) pour
        # recoller à l'affichage. Les events prioritaires sont toujours dits.
        if event_type not in PRIORITY_EVENTS and (time.monotonic() - ts) > STALE_S:
            continue
        try:
            say(phrase_for(event_type, p))
        except Exception as e:
            print(f"[ANNOUNCE] erreur lecture: {e}", flush=True)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", UDP_PORT))
    print(f"[ANNOUNCE] service démarré, écoute UDP :{UDP_PORT}"
          f" (piper={'oui' if USE_PIPER else 'non, espeak'})", flush=True)

    threading.Thread(target=_player_loop, daemon=True).start()

    if "--selftest" in sys.argv:
        _announce_q.put((time.monotonic(), "test_audio", {}))

    while True:
        try:
            data, _ = sock.recvfrom(8192)
            ev = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        event_type = ev.get("type")
        p = ev.get("payload") or {}

        if event_type == "audio_config":
            # Réglage voix depuis l'UI (on/off + volume). Pris en compte par le
            # thread lecteur (globals) ; pas mis en file.
            global _audio_enabled, _audio_volume
            if "enabled" in p:
                _audio_enabled = bool(p["enabled"])
            if "volume" in p:
                _audio_volume = max(0, min(100, int(p["volume"])))
            print(f"[ANNOUNCE] config: voix={'on' if _audio_enabled else 'off'}"
                  f" volume={_audio_volume}", flush=True)
            continue

        # Voix coupée : on n'annonce rien (sauf test_audio, action explicite UI).
        if not _audio_enabled and event_type != "test_audio":
            continue

        if event_type == "game_reset":
            # Nouvelle partie : on réautorise les annonces et on vide la file.
            _game_over.clear()
            _drain(_announce_q)
            continue
        if event_type == "game_over":
            # Fin de partie : on jette tout ce qui attend (la voix ne doit pas
            # continuer à débiter les lancers passés) et on n'annonce que le
            # vainqueur. Les events de jeu suivants sont ignorés jusqu'au reset.
            _drain(_announce_q)
            _game_over.set()
            _announce_q.put((time.monotonic(), event_type, p))
            continue
        if _game_over.is_set() and event_type != "test_audio":
            continue

        _announce_q.put((time.monotonic(), event_type, p))


if __name__ == "__main__":
    main()
