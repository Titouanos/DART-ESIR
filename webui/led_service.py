#!/usr/bin/env python3
"""
DartVision — service LED (process séparé, python3 SYSTÈME avec Adafruit Blinka).

Écoute les events de jeu émis en UDP par le bridge (webui/bridge.py ->
_emit_led, 127.0.0.1:9876) et joue des effets sur le ruban WS2811/WS2812
selon le score, les busts, le changement de joueur et la victoire.

Pourquoi un process séparé (et pas dans main.py) :
  - main.py tourne dans le venv, qui n'a PAS blinka/neopixel_spi ;
    python3 système les a. On évite d'installer quoi que ce soit dans le venv.
  - Découplage total : si le ruban/effets plantent, la détection et le
    scoring continuent. Le bridge envoie en UDP fire-and-forget : aucun
    impact sur le jeu si ce service est absent.

⚠️ ALIMENTATION PARTAGÉE : sur ce montage le ruban est alimenté par la même
source 5V que le Pi. Allumer trop de LEDs trop fort fait chuter la tension et
REBOOTE le Pi (constaté : 120 LEDs blanc plein @0.3 -> reboot). D'où le
plafond SAFE_MAX_BRIGHTNESS bas et les montées progressives (anti-inrush).
Si tu passes le ruban sur une alim 5V dédiée, tu peux monter le plafond.
"""

import json
import math
import socket
import sys
import time

import board
import neopixel_spi as neopixel

# ───────────────────────── RÉGLAGES ─────────────────────────
NUM_LEDS = 120
SPI_FREQ = 3_200_000          # 3.2 MHz : la seule fréquence qui adresse tout le ruban ici
PIXEL_ORDER = neopixel.RGB    # mets neopixel.GRB si rouge/vert sont inversés
SAFE_MAX_BRIGHTNESS = 0.10    # plafond DUR anti-brownout (alim partagée). Ne pas dépasser sans alim dédiée.
AMBIENT_BRIGHTNESS = 0.04     # éclairage d'ambiance discret entre les lancers (aide aussi les caméras)
AMBIENT_COLOR = (255, 200, 120)  # blanc chaud
UDP_PORT = 9876
# ──────────────────────────────────────────────────────────────

spi = board.SPI()
strip = neopixel.NeoPixel_SPI(
    spi, NUM_LEDS, brightness=SAFE_MAX_BRIGHTNESS,
    auto_write=False, frequency=SPI_FREQ, pixel_order=PIXEL_ORDER,
)


def _clamp_bri(b):
    return max(0.0, min(SAFE_MAX_BRIGHTNESS, b))


def solid(color, bri=SAFE_MAX_BRIGHTNESS):
    strip.brightness = _clamp_bri(bri)
    strip.fill(color)
    strip.show()


def ambient():
    solid(AMBIENT_COLOR, AMBIENT_BRIGHTNESS)


def ramp_to(color, target_bri, dur=0.25, steps=18):
    """Montée/descente progressive de luminosité sur une couleur (anti-inrush)."""
    target_bri = _clamp_bri(target_bri)
    strip.fill(color)
    for k in range(1, steps + 1):
        strip.brightness = target_bri * k / steps
        strip.show()
        time.sleep(dur / steps)


def flash(color, peak=SAFE_MAX_BRIGHTNESS, hold=0.25):
    """Flash couleur : montée douce → maintien → retour à l'ambiance."""
    ramp_to(color, peak, dur=0.15)
    time.sleep(hold)
    # redescente vers l'ambiance
    for k in range(12, -1, -1):
        strip.brightness = _clamp_bri(peak * k / 12)
        strip.show()
        time.sleep(0.02)
    ambient()


def pulse(color, times=2, peak=SAFE_MAX_BRIGHTNESS):
    for _ in range(times):
        ramp_to(color, peak, dur=0.10, steps=10)
        for k in range(10, -1, -1):
            strip.brightness = _clamp_bri(peak * k / 10)
            strip.show()
            time.sleep(0.015)
    ambient()


def wipe(color, dur=0.5):
    """Balayage d'une couleur le long du ruban (signal de changement de joueur)."""
    strip.brightness = _clamp_bri(SAFE_MAX_BRIGHTNESS)
    strip.fill((0, 0, 0))
    strip.show()
    step = max(1, NUM_LEDS // 60)
    for i in range(0, NUM_LEDS, step):
        for j in range(i, min(i + step, NUM_LEDS)):
            strip[j] = color
        strip.show()
        time.sleep(dur / (NUM_LEDS / step))
    time.sleep(0.15)
    ambient()


def _wheel(pos):
    pos &= 255
    if pos < 85:
        return (pos * 3, 255 - pos * 3, 0)
    if pos < 170:
        pos -= 85
        return (255 - pos * 3, 0, pos * 3)
    pos -= 170
    return (0, pos * 3, 255 - pos * 3)


def celebrate(duration=4.0):
    """Arc-en-ciel défilant pour la victoire (couleurs variées = courant modéré)."""
    strip.brightness = _clamp_bri(SAFE_MAX_BRIGHTNESS)
    t0 = time.time()
    j = 0
    while time.time() - t0 < duration:
        for i in range(NUM_LEDS):
            strip[i] = _wheel((i * 256 // NUM_LEDS) + j)
        strip.show()
        j = (j + 6) & 255
        time.sleep(0.02)
    ambient()


def self_test():
    """Au démarrage : R, V, B, blanc — pour vérifier l'ordre des couleurs.
    Si tu vois autre chose que Rouge puis Vert puis Bleu, change PIXEL_ORDER."""
    for name, col in [("ROUGE", (255, 0, 0)), ("VERT", (0, 255, 0)),
                      ("BLEU", (0, 0, 255)), ("BLANC", (255, 255, 255))]:
        print(f"[LED] self-test: {name}")
        ramp_to(col, SAFE_MAX_BRIGHTNESS, dur=0.2)
        time.sleep(0.6)
    ambient()


# ───────────────────── effets selon les events ─────────────────────
def effect_for_throw(payload):
    """Couleur selon la qualité du lancer."""
    score = payload.get("score", 0)
    mult = payload.get("multiplier", 1)
    ring = (payload.get("ring") or "").lower()
    label = (payload.get("label") or "").upper()

    if score == 0 or label == "MISS":
        flash((255, 0, 0), hold=0.15)                  # raté → rouge
    elif "bull" in ring or "BULL" in label:
        flash((255, 215, 0), peak=SAFE_MAX_BRIGHTNESS, hold=0.45)  # bull → or
    elif mult == 3:
        flash((0, 255, 0), hold=0.4)                   # triple → vert vif
    elif mult == 2:
        flash((0, 220, 220), hold=0.35)                # double → cyan
    else:
        flash((120, 160, 255), hold=0.2)               # simple → bleu doux


def handle(event_type, payload):
    if event_type == "throw":
        effect_for_throw(payload)
    elif event_type == "bust":
        pulse((255, 0, 0), times=3)                    # bust → triple pulse rouge
    elif event_type == "game_over":
        celebrate(4.0)                                 # victoire → arc-en-ciel
    elif event_type in ("player_change", "turn_end"):
        wipe((80, 120, 255), dur=0.5)                  # nouveau joueur → balayage bleu


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", UDP_PORT))
    sock.settimeout(1.0)

    print(f"[LED] service démarré : {NUM_LEDS} LEDs @ {SPI_FREQ/1e6}MHz, "
          f"plafond {SAFE_MAX_BRIGHTNESS}, écoute UDP :{UDP_PORT}")
    if "--selftest" in sys.argv:
        self_test()
    else:
        ramp_to(AMBIENT_COLOR, AMBIENT_BRIGHTNESS, dur=0.4)

    try:
        while True:
            try:
                data, _ = sock.recvfrom(8192)
            except socket.timeout:
                continue
            try:
                ev = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            et = ev.get("type")
            payload = ev.get("payload") or {}
            try:
                handle(et, payload)
            except Exception as e:
                print(f"[LED] effet '{et}' a échoué: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        solid((0, 0, 0), 0.0)  # extinction propre
        print("[LED] éteint, service arrêté")


if __name__ == "__main__":
    main()
