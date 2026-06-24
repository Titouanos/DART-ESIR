#!/usr/bin/env python3
"""
DartVision — service LED (process séparé, python3 SYSTÈME avec spidev).

Écoute les events de jeu émis en UDP par le bridge (127.0.0.1:9876) et joue
des effets sur le ruban WS2812B selon score / bust / phase de tour / victoire.

DRIVER : on réutilise l'encodage SPI brut éprouvé de led_test.py (le seul qui
marche sur ce Pi 5 + ce ruban WS2812B) — surtout PAS neopixel_spi/Blinka, qui
sortait du garbage invisible (mauvais timing/ordre). Chaque bit WS2812 est codé
sur 3 bits SPI à 2.4 MHz (1->0b110, 0->0b100), ordre GRB, 64 octets de reset.

⚠️ ALIMENTATION PARTAGÉE Pi/ruban : trop de LEDs trop fort = chute de tension =
REBOOT du Pi. D'où MAX_BRIGHTNESS bas (échelle 0-255) et montées progressives.
led_test.py tournait en boucle à 24/255 sans rebooter → on reste dans ces eaux.

Un seul process doit piloter le ruban : ne pas lancer led_test.py en parallèle.
"""

import json
import socket
import sys
import time

try:
    import spidev
except ImportError:
    sys.exit("spidev introuvable — lance avec le python3 SYSTÈME, pas le venv.")

# ───────────────────────── RÉGLAGES ─────────────────────────
NUM_LEDS = 120
SPI_HZ = 2_400_000          # 3 bits SPI / symbole WS2812 → ~1.25 µs
MAX_BRIGHTNESS = 28         # plafond DUR anti-brownout (0-255). Monter SEULEMENT avec alim dédiée.
AMBIENT_BRIGHTNESS = 14     # éclairage d'ambiance discret entre lancers
AMBIENT_COLOR = (255, 180, 90)   # blanc chaud
UDP_PORT = 9876
RESET_BYTES = 64
# ──────────────────────────────────────────────────────────────


def _encode_byte(value):
    bits = 0
    for i in range(8):
        bit = (value >> (7 - i)) & 1
        bits = (bits << 3) | (0b110 if bit else 0b100)
    return [(bits >> 16) & 0xFF, (bits >> 8) & 0xFF, bits & 0xFF]


_LUT = [_encode_byte(b) for b in range(256)]


class Strip:
    """Driver WS2812B via SPI brut (repris de led_test.py, éprouvé)."""

    def __init__(self, count, bus=0, device=0, brightness=AMBIENT_BRIGHTNESS):
        self.count = count
        self.brightness = brightness
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = SPI_HZ
        self.spi.mode = 0
        self.pixels = [(0, 0, 0)] * count

    def set(self, i, r, g, b):
        if 0 <= i < self.count:
            self.pixels[i] = (r, g, b)

    def fill(self, r, g, b):
        self.pixels = [(r, g, b)] * self.count

    def show(self):
        scale = max(0, min(MAX_BRIGHTNESS, self.brightness))
        data = []
        for (r, g, b) in self.pixels:
            data += _LUT[(g * scale) // 255]   # WS2812B = ordre GRB
            data += _LUT[(r * scale) // 255]
            data += _LUT[(b * scale) // 255]
        data += [0] * RESET_BYTES
        self.spi.writebytes2(data)

    def clear(self):
        self.fill(0, 0, 0)
        self.show()


strip = Strip(NUM_LEDS)


# ───────────────────── helpers d'effets ─────────────────────
def set_all(color, bri):
    strip.brightness = max(0, min(MAX_BRIGHTNESS, bri))
    strip.fill(*color)
    strip.show()


def ambient():
    set_all(AMBIENT_COLOR, AMBIENT_BRIGHTNESS)


def ramp_to(color, target, dur=0.25, steps=16):
    target = max(0, min(MAX_BRIGHTNESS, target))
    strip.fill(*color)
    for k in range(1, steps + 1):
        strip.brightness = int(target * k / steps)
        strip.show()
        time.sleep(dur / steps)


def flash(color, peak=MAX_BRIGHTNESS, hold=0.25):
    ramp_to(color, peak, dur=0.15)
    time.sleep(hold)
    for k in range(12, -1, -1):
        strip.brightness = int(peak * k / 12)
        strip.show()
        time.sleep(0.02)
    ambient()


def pulse(color, times=3, peak=MAX_BRIGHTNESS):
    for _ in range(times):
        ramp_to(color, peak, dur=0.10, steps=8)
        for k in range(8, -1, -1):
            strip.brightness = int(peak * k / 8)
            strip.show()
            time.sleep(0.015)
    ambient()


def wipe(color, dur=0.5):
    strip.brightness = MAX_BRIGHTNESS
    strip.fill(0, 0, 0)
    strip.show()
    step = max(1, NUM_LEDS // 60)
    for i in range(0, NUM_LEDS, step):
        for j in range(i, min(i + step, NUM_LEDS)):
            strip.set(j, *color)
        strip.show()
        time.sleep(dur / (NUM_LEDS / step))
    time.sleep(0.15)
    ambient()


def _wheel(pos):
    pos %= 256
    if pos < 85:
        return (255 - pos * 3, pos * 3, 0)
    if pos < 170:
        pos -= 85
        return (0, 255 - pos * 3, pos * 3)
    pos -= 170
    return (pos * 3, 0, 255 - pos * 3)


def celebrate(duration=3.0):
    strip.brightness = MAX_BRIGHTNESS
    t0 = time.time()
    j = 0
    while time.time() - t0 < duration:
        for i in range(NUM_LEDS):
            strip.set(i, *_wheel((i * 256 // NUM_LEDS) + j))
        strip.show()
        j = (j + 6) & 255
        time.sleep(0.02)
    ambient()


def demo():
    """Bouton 'Test LEDs' : R/V/B/blanc tenus ~1.2s + arc-en-ciel."""
    print("[LED] DEMO test_leds démarrée", flush=True)
    for name, col in [("ROUGE", (255, 0, 0)), ("VERT", (0, 255, 0)),
                      ("BLEU", (0, 0, 255)), ("BLANC", (255, 255, 255))]:
        print(f"[LED] demo: {name}", flush=True)
        ramp_to(col, MAX_BRIGHTNESS, dur=0.25)
        time.sleep(1.2)
    celebrate(3.0)
    print("[LED] DEMO terminée → ambiance blanche", flush=True)


# ───────────────────── effets selon les events ─────────────────────
def effect_for_throw(payload):
    score = payload.get("score", 0)
    mult = payload.get("multiplier", 1)
    ring = (payload.get("ring") or "").lower()
    label = (payload.get("label") or "").upper()
    if score == 0 or label == "MISS":
        flash((255, 0, 0), hold=0.15)                  # raté → rouge
    elif "bull" in ring or "BULL" in label:
        flash((255, 215, 0), hold=0.45)                # bull → or
    elif mult == 3:
        flash((0, 255, 0), hold=0.4)                   # triple → vert
    elif mult == 2:
        flash((0, 220, 220), hold=0.35)                # double → cyan
    else:
        flash((120, 160, 255), hold=0.2)               # simple → bleu doux


def handle(event_type, payload):
    if event_type == "throw":
        effect_for_throw(payload)
    elif event_type == "bust":
        pulse((255, 0, 0), times=3)
    elif event_type == "game_over":
        celebrate(4.0)
    elif event_type == "takeout_start":
        set_all((255, 100, 0), MAX_BRIGHTNESS)         # ATTENDS (ambre fixe)
    elif event_type == "takeout_end":
        flash((0, 255, 0), hold=0.35)                  # PRÊT (vert → blanc)
    elif event_type in ("player_change", "turn_end"):
        pass
    elif event_type == "test":
        demo()


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", UDP_PORT))
    sock.settimeout(1.0)
    print(f"[LED] service démarré : {NUM_LEDS} LEDs WS2812B (spidev GRB @ "
          f"{SPI_HZ/1e6}MHz), plafond {MAX_BRIGHTNESS}/255, UDP :{UDP_PORT}",
          flush=True)
    if "--selftest" in sys.argv:
        demo()
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
            try:
                handle(ev.get("type"), ev.get("payload") or {})
            except Exception as e:
                print(f"[LED] effet '{ev.get('type')}' a échoué: {e}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        strip.clear()
        print("[LED] éteint, service arrêté", flush=True)


if __name__ == "__main__":
    main()
