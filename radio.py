# --------------------------------------------------
# MY RADIO PROJECT
# --------------------------------------------------

from flask import Flask, jsonify, request
import subprocess
import socket
import json
import os
import time
import threading
import signal
import sys
import math
from luma.core.interface.serial import spi
from luma.oled.device import ssd1309
from luma.core.render import canvas
from PIL import ImageFont
from gpiozero import RotaryEncoder, Button, PWMLED

# --------------------------------------------------
# GLOBALS
# --------------------------------------------------

APP = Flask(__name__)
LAST_VOLUME_UPDATE = time.time()
LAST_URL = None
SYSTEM_STATE = "boot"   # boot / running / shutdown / idle
STATUS_LED = PWMLED(26, frequency=1000)
SOCKET_PATH = "/tmp/mpv-socket"
MPV_PATH = "/usr/bin/mpv"
MPV_PROCESS = None
LAST_SCREEN = None
CURRENT_ARTIST = ""
CURRENT_TITLE = ""
UI_STATE = "NOW_PLAYING"
UI_DATA = {}
UI_TIMEOUT = 2.0
UI_LAST_INTERACTION = time.time()
RUNNING = True
MUTED = False
LAST_NONZERO_VOLUME = 50
STATE_FILE = "/home/pi/radio_state.json"
VOLUME_DELTA = 0
LAST_TURN_TIME = time.time()
LAST_STATE = None

# --------------------------------------------------
# LOCKS 'n STUFF
# -------------------------------------------------

lock = threading.Lock()
display_lock = threading.Lock()

class VolumeState:
    vol = 50

volume_worker = VolumeState()

# --------------------------------------------------
# ENCODERS
# --------------------------------------------------

vol_encoder = RotaryEncoder(a=5, b=6, max_steps=0)

station_encoder = RotaryEncoder(a=23, b=24, max_steps=0)

vol_button = Button(13, hold_time=1.2)

shutdown_button = Button(17, hold_time=1.5)

# --------------------------------------------------
# STATION PRESETS
# --------------------------------------------------

STATIONS = {
    "Triple J": "https://mediaserviceslive.akamaized.net/hls/live/2038308/triplejnsw/index.m3u8",
    "BBC": "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service",
#    "Folk Alley": "http://freshgrass.streamguys1.com/folkalley-128mp3",
    "PBS": "https://28793.live.streamtheworld.com/3PBS_FMAACHIGH/HLS/playlist.m3u8",
    "ABC Classic": "http://www.abc.net.au/res/streaming/audio/mp3/classic_fm.pls",
    "ABC Jazz": "https://streaming.abc-cdn.net.au/audio/hls/abcjazz.m3u8",
    "Radio National": "https://streaming.abc-cdn.net.au/audio/hls/rnnsw.m3u8",
    "Aardvark Blues": "https://ais-sa5.cdnstream1.com/b77280_128mp3",
    "Folk Alley": "http://freshgrass.streamguys1.com/folkalley-64aac"
}

STATION_LIST = list(STATIONS.keys())
CURRENT_STATION_INDEX = 0

# --------------------------------------------------
# DISPLAY SETUP
# --------------------------------------------------

serial = spi(device=0, port=0, gpio_DC=25, bus_speed_hz=8000000)
device = ssd1309(serial)

def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception as e:
        print(f"Font load failed: {path} -> {e}")
        return ImageFont.load_default()

# UI fonts tuned for 128x64 OLED
font_station = load_font(
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoCondensed-Bold.ttf", 14
)

font_title = load_font(
    "/usr/share/fonts/truetype/terminus/TerminusTTF-4.46.0.ttf", 12
)

font_big = load_font(
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoCondensed-Bold.ttf", 28
)

font_status = load_font(
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 9
)

SCROLL_TITLE = {
    "text": "",
    "progress": 0,
    "last_update": 0
}

SCROLL_ARTIST = {
    "text": "",
    "offset": 0,
    "last_update": 0
}

# --------------------------------------------------
# MPV MANAGEMENT
# --------------------------------------------------

def start_mpv():
    global MPV_PROCESS

    if MPV_PROCESS and MPV_PROCESS.poll() is None:
        return

    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    print("Starting mpv...")

    MPV_PROCESS = subprocess.Popen([
        MPV_PATH,
        "--no-video",
        "--ao=alsa",
        "--audio-device=alsa/hw:CARD=DigiAMP,DEV=0",
        "--idle=yes",
        "--volume-max=100",
        "--cache=yes",
        "--cache-secs=10",              # much more reasonable
        "--demuxer-readahead-secs=5",   # small forward buffer
        "--network-timeout=30",         # less trigger-happy
        "--input-ipc-server=" + SOCKET_PATH,
        "--log-file=/dev/null",
        "--msg-level=ffmpeg=warn",
    ])

    for _ in range(10):
        if os.path.exists(SOCKET_PATH):
            print("mpv ready")
            return
        time.sleep(0.5)

    print("ERROR: mpv socket not created")

def send_command(command):
    start_mpv()

    for _ in range(3):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(SOCKET_PATH)
                client.send((json.dumps(command) + "\n").encode())
                response = client.recv(4096)
                return json.loads(response.decode())
        except Exception as e:
            print(f"Retrying mpv ({e})...")
            time.sleep(1)
            start_mpv()

    return {"error": "mpv not responding"}

# --------------------------------------------------
# DISPLAY HELPERS
# --------------------------------------------------

def draw_centered(draw, text, y, font):
    w = draw.textlength(text, font=font)
    x = (128 - w) // 2
    draw.text((x, y), text, font=font, fill=255)

def draw_centered_in_region(draw, text, x, y, width, font):
    text_width = draw.textlength(text, font=font)
    tx = x + max(0, (width - text_width) // 2)
    draw.text((tx, y), text, font=font, fill=255)

def clear_display():
    with canvas(device) as draw:
        pass

def draw_scrolling_text(draw, text, y, font, state, speed=0.07):
    now = time.time()

    if state["text"] != text:
        state["text"] = text
        state["progress"] = 0
        state["last_update"] = now
        state["start_time"] = now

    text_width = draw.textlength(text, font=font)

    # No scroll needed
    if text_width <= 124:
        x = (128 - text_width) // 2
        draw.text((x, y), text, font=font, fill=255)
        return False

    if now - state["start_time"] < 1.5:
        progress = 0
    else:
        elapsed = now - state["start_time"] - 1.5
        progress = (elapsed * speed) % 1.0

    # easing applied here
    eased = ease_in_out(progress)

    x = -int(eased * (text_width + 20))

    draw.text((x, y), text, font=font, fill=255)
    draw.text((x + text_width + 20, y), text, font=font, fill=255)

    return True

def ease_in_out(t):
    # smooth cosine easing (0 → 1 → 0 velocity curve)
    return 0.5 - 0.5 * math.cos(math.pi * t)

def set_ui_state(state, data=None):
    global UI_STATE, UI_DATA, UI_LAST_INTERACTION

    UI_STATE = state
    UI_DATA = data or {}
    UI_LAST_INTERACTION = time.time()

def draw_volume_icon(draw, x, y, level):
    # speaker base
    draw.rectangle((x, y+4, x+3, y+8), fill=255)

    # volume bars
    if level > 20:
        draw.rectangle((x+7, y+5, x+8, y+7), fill=255)
    if level > 40:
        draw.rectangle((x+10, y+4, x+11, y+8), fill=255)
    if level > 60:
        draw.rectangle((x+13, y+3, x+14, y+9), fill=255)
    if level > 80:
        draw.rectangle((x+16, y+2, x+17, y+10), fill=255)
    if level > 90:
        draw.rectangle((x+19, y+1, x+20, y+11), fill=255)

def show_volume(volume):
    set_ui_state("VOLUME", {"volume": volume})

def draw_muted_label(draw, x, y, text, font, padding=3):
    text_width = draw.textlength(text, font=font)
    text_height = font.size

    # White background
    draw.rectangle(
        (x - padding,
        y - padding,
        x + text_width + padding,
        y + text_height + padding),
        fill=255
    )

    # Black text
    draw.text((x, y), text, font=font, fill=0)

def show_station():
    station = STATION_LIST[CURRENT_STATION_INDEX]
    set_ui_state("STATION", {"station": station})

# Class for Display Manager
class DisplayManager:
    def __init__(self, device, render_callback):
        self.device = device
        self.render_callback = render_callback

        # --- config ---
        self.active_contrast = 80
        self.idle_contrast = 30
        self.dim_timeout = 60          # seconds
        self.blank_timeout = 300       # seconds (set None to disable)

        # --- state ---
        self.last_activity = time.time()
        self.blank = False

        self.lock = threading.Lock()
        self.running = True

    def notify_activity(self):
        global UI_LAST_INTERACTION

        with self.lock:
            self.last_activity = time.time()

            UI_LAST_INTERACTION = time.time()

            if self.blank:
                self.blank = False
                self.device.show()

            self.device.contrast(self.active_contrast)

    def stop(self):
        self.running = False

    def run(self, state_provider):

        from luma.core.render import canvas  # make sure this import exists

        while self.running:
            now = time.time()

            with self.lock:
                idle_time = now - self.last_activity

                # --- blanking ---
                if self.blank_timeout and idle_time > self.blank_timeout:
                    if not self.blank:
                        self.device.hide()
                        self.blank = True
                    time.sleep(0.5)
                    continue

                # --- dimming ---
                if idle_time > self.dim_timeout:
                    self.device.contrast(self.idle_contrast)
                else:
                    self.device.contrast(self.active_contrast)

            global UI_STATE, UI_DATA

            if UI_STATE != "NOW_PLAYING":
                if time.time() - UI_LAST_INTERACTION > UI_TIMEOUT:
                    UI_STATE = "NOW_PLAYING"
                    UI_DATA = {}

            # --- render ---
            try:
                with canvas(self.device) as draw:
                    self.render_callback(draw, None)
            except Exception as e:
                print("Display error:", e)

            time.sleep(0.1)  # ~10 FPS

def get_display_state():
    return {
        "station": current_station,
        "volume": current_volume
    }

def render_display(draw, state):

    with display_lock:

        if UI_STATE == "VOLUME":
            volume = UI_DATA.get("volume", 0)
            draw_centered(draw, f"{volume}", 0, font_big)
            draw_centered(draw, "VOLUME", 32, font_title)
            bar_width = int((volume / 100) * 120)
            draw.rectangle((4, 52, 124, 62), outline=255)
            draw.rectangle((4, 52, 4 + bar_width, 62), fill=255)

        elif UI_STATE == "STATION":
            station = UI_DATA.get("station", "")
            draw_centered(draw, station, 22, font_station)

        elif UI_STATE == "NOW_PLAYING":

            station = STATION_LIST[CURRENT_STATION_INDEX]
            volume = getattr(volume_worker, "vol", 50)

            # Icons
            draw_volume_icon(draw, 2, 0, volume)

            if MUTED:
                draw_muted_label(draw, 100, 0, "MUTED", font_status)

            # Station name
            draw_centered(draw, station, 22, font_station)

            if CURRENT_TITLE == "":
                draw_centered(draw, "Live", 44, font_title)

            elif station != CURRENT_TITLE:
                # Track title
                draw_scrolling_text(
                    draw,
                    CURRENT_TITLE,
                    44,
                    font_title,
                    SCROLL_TITLE
                )

display = DisplayManager(device, render_display)

threading.Thread(
    target=display.run,
    args=(get_display_state,),
    daemon=True
).start()

# --------------------------------------------------
# API
# --------------------------------------------------

@APP.route("/play/<station>")
def play_station(station):
    global LAST_URL

    if station not in STATIONS:
        return jsonify({"error": "Unknown station"}), 404

    LAST_URL = STATIONS[station]

    send_command({
        "command": ["loadfile", LAST_URL, "replace"]
    })

    display.notify_activity()

    return jsonify({"status": f"Playing {station}"})

@APP.route("/preset/<int:preset>")
def play_preset(preset):
    global LAST_URL, CURRENT_STATION_INDEX

    try:
        # Convert 1-based → 0-based
        index = preset - 1

        if index < 0 or index >= len(STATION_LIST):
            return jsonify({"error": "Unknown preset"}), 404

        CURRENT_STATION_INDEX = index

        station = STATION_LIST[index]
        LAST_URL = STATIONS[station]

        send_command({
            "command": ["loadfile", LAST_URL, "replace"]
        })

        show_station() # claud review
        save_state()

        display.notify_activity()

        return jsonify({"status": f"Playing {station}"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@APP.route("/playurl")
def play_url():
    global LAST_URL

    url = request.args.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400

    LAST_URL = url

    send_command({
        "command": ["loadfile", url, "replace"]
    })

    display.notify_activity()

    return jsonify({"status": "playing"})

@APP.route("/pause")
def pause():
    send_command({"command": ["cycle", "pause"]})
    return jsonify({"status": "Toggled pause"})

@APP.route("/volume/<value>")
def volume(value):
    try:
        if value.startswith("+") or value.startswith("-"):
            send_command({
                "command": ["add", "volume", int(value)]
            })
        else:
            level = max(0, min(100, int(value)))
            send_command({
                "command": ["set_property", "volume", level]
            })

        # 🔥 SYNC BACK FROM MPV
        result = send_command({
            "command": ["get_property", "volume"]
        })

        volume_worker.vol = max(0, min(100, int(value)))
        show_volume(volume_worker.vol)

        display.notify_activity()

        return jsonify({"status": "ok"})
    except:
        return jsonify({"error": "bad value"}), 400

@APP.route("/status")
def status():
    pause = send_command({"command": ["get_property", "pause"]})
    volume = send_command({"command": ["get_property", "volume"]})

    return jsonify({
        "pause": pause.get("data"),
        "volume": volume.get("data")
    })

@APP.route("/volume_now")
def volume_now():
    volume = send_command({"command": ["get_property", "volume"]})
    return jsonify({
        "volume": volume.get("data")
    })

@APP.route("/nowplaying")
def now_playing():
    return STATION_LIST[CURRENT_STATION_INDEX]

@APP.route("/nowplaying2")
def now_playing2():
    result = send_command({
        "command": ["get_property", "metadata"]
    })
    return jsonify(result)

@APP.route("/mute")
def mute():
    vol_press()
    return jsonify({"status": "ok"})

@APP.route("/title")
def title():
    result = CURRENT_TITLE
    return result

# -----------------------------
# BUTTON CONTROL
# -----------------------------

def shutdown_hold():
    global SYSTEM_STATE, RUNNING

    print("Shutdown triggered...")
    SYSTEM_STATE = "shutdown"

    # ✅ play shutdown animation explicitly
    play_shutdown_led()

    # ✅ now stop workers cleanly
    RUNNING = False
    display.stop()

    time.sleep(0.2)

    try:
        device.hide()
    except:
        pass

    clear_display()
    save_state()

    time.sleep(0.4)
    os.system("sudo shutdown -h now")

def play_shutdown_led():
    # fast blink pattern, blocking but short
    for _ in range(6):
        STATUS_LED.value = 1.0
        time.sleep(0.1)
        STATUS_LED.off()
        time.sleep(0.1)

# Process actions
shutdown_button.when_held = shutdown_hold

# -----------------------------
# VOLUME CONTROL
# -----------------------------

def apply_volume_curve(vol):
    # Convert linear 0–100 into logarithmic curve for mpv
    vol = max(0, min(100, vol))

    # Normalise 0–1
    x = vol / 100.0

    # Log curve (tweak 4.0 to taste)
    curved = math.log10(1 + 5 * x)  # smooth log curve

    return int(curved * 100)

def vol_clockwise():
    global VOLUME_DELTA, LAST_TURN_TIME

    now = time.time()
    speed = now - LAST_TURN_TIME
    LAST_TURN_TIME = now

    step = 4 if speed < 0.08 else 1

    with lock:
        VOLUME_DELTA += step

    display.notify_activity()

def vol_counter():
    global VOLUME_DELTA, LAST_TURN_TIME

    now = time.time()
    speed = now - LAST_TURN_TIME
    LAST_TURN_TIME = now

    step = 4 if speed < 0.08 else 1

    with lock:
        VOLUME_DELTA -= step

    display.notify_activity()

def vol_press():
    global MUTED, LAST_NONZERO_VOLUME

    if not MUTED:
        LAST_NONZERO_VOLUME = volume_worker.vol
        send_command({"command": ["set_property", "mute", True]})
        MUTED = True
    else:
        send_command({"command": ["set_property", "mute", False]})
        volume_worker.vol = LAST_NONZERO_VOLUME
        send_command({
            "command": ["set_property", "volume",
                        apply_volume_curve(volume_worker.vol)]
        })
        MUTED = False

    display.notify_activity()

# Process actions
vol_button.when_pressed = vol_press
vol_encoder.when_rotated_clockwise = vol_counter
vol_encoder.when_rotated_counter_clockwise = vol_clockwise

# -----------------------------
# STATION CONTROL
# -----------------------------

def station_clockwise():
    global CURRENT_STATION_INDEX, LAST_URL

    CURRENT_STATION_INDEX = (CURRENT_STATION_INDEX + 1) % len(STATION_LIST)
    station = STATION_LIST[CURRENT_STATION_INDEX]

    LAST_URL = STATIONS[station]
    send_command({"command": ["loadfile", LAST_URL, "replace"]})

    # show station number
    show_station()

    save_state()

    display.notify_activity()

def station_counter():
    global CURRENT_STATION_INDEX, LAST_URL

    CURRENT_STATION_INDEX = (CURRENT_STATION_INDEX - 1) % len(STATION_LIST)
    station = STATION_LIST[CURRENT_STATION_INDEX]

    LAST_URL = STATIONS[station]
    send_command({"command": ["loadfile", LAST_URL, "replace"]})

    # show station number
    show_station()

    save_state()

    display.notify_activity()

# Process actions
station_encoder.when_rotated_clockwise = station_clockwise
station_encoder.when_rotated_counter_clockwise = station_counter

# --------------------------------------------------
# SAVE LAST STATION
# --------------------------------------------------

def save_state():
    try:
        print("Saving state...") 
        with open(STATE_FILE, "w") as f:
            json.dump({
                "station_index": CURRENT_STATION_INDEX,
                "volume": getattr(volume_worker, "vol", 50)
            }, f)
    except Exception as e:
        print("Save state error:", e)

def load_state():
    global CURRENT_STATION_INDEX

    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)

                CURRENT_STATION_INDEX = data.get("station_index", 0)

                # restore volume
                volume_worker.vol = data.get("volume", 50)

    except Exception as e:
        print("Load state error:", e)

# --------------------------------------------------
# WORKERS
# --------------------------------------------------

def volume_worker_thread():
    global VOLUME_DELTA, LAST_VOLUME_UPDATE, MUTED

    while RUNNING:
        time.sleep(0.03)

        # 1. Pull pending encoder delta
        with lock:
            delta = VOLUME_DELTA
            VOLUME_DELTA = 0

        # 2. Apply delta ONCE
        if delta != 0:

            if MUTED:
                send_command({"command": ["set_property", "mute", False]})
                MUTED = False

            volume_worker.vol = max(0, min(100, volume_worker.vol + delta))

            # 3. Send curved volume to mpv
            mpv_vol = apply_volume_curve(volume_worker.vol)
            send_command({
                "command": ["set_property", "volume", mpv_vol]
            })

            # 4. Update display state
            LAST_VOLUME_UPDATE = time.time()
            show_volume(volume_worker.vol)

def watchdog():
    global LAST_URL

    while RUNNING:
        time.sleep(10)

        try:
            idle = send_command({
                "command": ["get_property", "idle-active"]
            }).get("data")

            if idle and LAST_URL:
                send_command({
                    "command": ["loadfile", LAST_URL, "replace"]
                })

        except Exception as e:
            print("Watchdog error:", e)

def led_worker():
    global SYSTEM_STATE, LAST_STATE

    while RUNNING:
        if SYSTEM_STATE != LAST_STATE:

            if SYSTEM_STATE == "boot":
                # slow blink
                for _ in range(4):
                    if SYSTEM_STATE != "boot":
                        break
                    STATUS_LED.value = 1.0
                    time.sleep(0.3)
                    STATUS_LED.off()
                    time.sleep(0.3)

            elif SYSTEM_STATE == "running":
                # dim steady
                STATUS_LED.value = 0.2

            else:
                STATUS_LED.off()

            LAST_STATE = SYSTEM_STATE

        time.sleep(0.05)

def metadata_worker():
    global CURRENT_ARTIST, CURRENT_TITLE

    while RUNNING:
        try:
            result = send_command({
                "command": ["get_property", "metadata"]
            })

            data = result.get("data", {})

            artist = data.get("artist", "")
            title = data.get("title") or data.get("icy-title", "")

            if title:
                CURRENT_ARTIST = artist
                CURRENT_TITLE = title
            else:
                CURRENT_ARTIST = ""
                CURRENT_TITLE = ""

        except:
            pass

        time.sleep(3)  # update every few seconds

# Define worker threads
threading.Thread(target=volume_worker_thread, daemon=True).start()
threading.Thread(target=watchdog, daemon=True).start()
threading.Thread(target=led_worker, daemon=True).start()
threading.Thread(target=metadata_worker, daemon=True).start()

# --------------------------------------------------
# CLEAUP
# --------------------------------------------------

def cleanup(sig, frame):
    global RUNNING
    print("Exiting cleanly...")

    RUNNING = False
    display.stop()    # ✅ important

    time.sleep(0.2)

    try:
        device.hide()
    except:
        pass

    try:
        clear_display()
    except:
        pass

    save_state()
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# --------------------------------------------------
# START
# --------------------------------------------------

if __name__ == "__main__":
    SYSTEM_STATE = "boot"

    load_state()

    if STATION_LIST:

        station = STATION_LIST[CURRENT_STATION_INDEX]
        LAST_URL = STATIONS[station]

        print(f"Auto-playing: {station}")

        send_command({
            "command": ["loadfile", LAST_URL, "replace"]
    })

    show_station()

    send_command({
        "command": ["set_property", "volume", getattr(volume_worker, "vol", 50)]
    })

    time.sleep(0.5)
    print("Radio ready")
    SYSTEM_STATE = "running"
    APP.run(host="0.0.0.0", port=5000)
