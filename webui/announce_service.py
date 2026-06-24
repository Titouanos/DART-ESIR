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

UDP_PORT = 9877
WAV = os.path.join(tempfile.gettempdir(), "dart_announce.wav")

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
        # check=True → un échec de lecture remonte dans le journal au lieu
        # d'être avalé silencieusement (cas vécu avec pw-play).
        subprocess.run(["paplay", WAV], check=True, timeout=15)
    except Exception as e:
        print(f"[ANNOUNCE] échec '{text}': {e}", flush=True)


def handle(event_type, p):
    if event_type == "throw":
        # bust géré par l'event bust ; ici on annonce juste le segment touché
        say(seg_phrase(p))
    elif event_type == "bust":
        say(f"{p.get('player','')}, bust, zéro point")
    elif event_type == "turn_end":
        if not p.get("busted"):
            say(f"{p.get('player','')}, {p.get('total', 0)} points")
    elif event_type == "player_change":
        say(f"{p.get('name','')}, à toi de jouer")
    elif event_type == "game_over":
        say(f"{p.get('winner_name','')} remporte la partie ! Bravo !")
    elif event_type == "test_audio":
        say("Test de l'annonce vocale. Le son fonctionne.")


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", UDP_PORT))
    print(f"[ANNOUNCE] service démarré, écoute UDP :{UDP_PORT}", flush=True)
    if "--selftest" in sys.argv:
        say("Test de l'annonce vocale. Le son fonctionne.")
    while True:
        try:
            data, _ = sock.recvfrom(8192)
            ev = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        try:
            handle(ev.get("type"), ev.get("payload") or {})
        except Exception as e:
            print(f"[ANNOUNCE] erreur: {e}", flush=True)


if __name__ == "__main__":
    main()
