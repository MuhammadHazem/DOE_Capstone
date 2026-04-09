import subprocess
import numpy as np
import cv2
import signal
import sys
import lgpio
import time
import threading
import datetime

ENABLE_ANALYSIS = True

DISPLAY_URL = "srt://0.0.0.0:7000?mode=listener"
ANALYSIS_URL = "srt://0.0.0.0:7001?mode=listener"

FRAME_W = 1280
FRAME_H = 720
FRAME_SIZE = FRAME_W * FRAME_H * 3

AVG_WINDOW = 30
ANALYZE_EVERY = 3

GLOBAL_FRAME_COUNT = 0

ANALYSIS_LOG = "analysis_log.csv"
GPIO_LOG = "gpio_log.csv"

with open(ANALYSIS_LOG, "w") as f:
    f.write("frame,error,avg_error,integrity,prev_integrity,control\n")

with open(GPIO_LOG, "w") as f:
    f.write("frame,timestamp,pin,state\n")

LOGO_PATH = "/home/muhammad/Downloads/logo.png"

logo = cv2.imread(LOGO_PATH)
if logo is None:
    raise Exception("Logo not found")

h, w = logo.shape[:2]

TARGET_WIDTH = 200
scale = TARGET_WIDTH / w

new_w = int(w * scale)
new_h = int(h * scale)

logo = cv2.resize(logo, (new_w, new_h))
logo_gray = cv2.cvtColor(logo, cv2.COLOR_BGR2GRAY)

LOGO_X = FRAME_W - new_w
LOGO_Y = FRAME_H - new_h

PIN_RIGHT = 17
PIN_LEFT = 27

chip = lgpio.gpiochip_open(0)
lgpio.gpio_claim_output(chip, PIN_RIGHT)
lgpio.gpio_claim_output(chip, PIN_LEFT)

lgpio.gpio_write(chip, PIN_RIGHT, 0)
lgpio.gpio_write(chip, PIN_LEFT, 0)

display_proc = subprocess.Popen([
    "ffplay",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-framedrop",
    DISPLAY_URL
], start_new_session=True)

analysis_proc = None

if ENABLE_ANALYSIS:
    analysis_proc = subprocess.Popen([
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "quiet",
        "-i", ANALYSIS_URL,
        "-vf", "scale=1280:720",
        "-pix_fmt", "bgr24",
        "-f", "rawvideo",
        "-"
    ], stdout=subprocess.PIPE, bufsize=10**8, start_new_session=True)

def read_exact(pipe, size):
    data = b''
    while len(data) < size:
        packet = pipe.read(size - len(data))
        if not packet:
            return None
        data += packet
    return data

def gpio_pulse(pin, width_ms=10):
    global GLOBAL_FRAME_COUNT

    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")

    lgpio.gpio_write(chip, pin, 1)
    with open(GPIO_LOG, "a") as f:
        f.write(f"{GLOBAL_FRAME_COUNT},{ts},{pin},HIGH\n")

    time.sleep(width_ms / 1000.0)

    lgpio.gpio_write(chip, pin, 0)
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")
    with open(GPIO_LOG, "a") as f:
        f.write(f"{GLOBAL_FRAME_COUNT},{ts},{pin},LOW\n")

def gpio_pulse_async(pin, width_ms=50):
    threading.Thread(target=gpio_pulse, args=(pin, width_ms), daemon=True).start()

def run_analysis():
    frame_counter = 0
    error_sum = 0
    frame_index = 0
    
    last_avg_error = None
    last_integrity = None

    prev_integrity = None
    prev_for_log = None
    last_direction = 1
    TOL = 0.3
    control = 0

    global GLOBAL_FRAME_COUNT

    while True:
        raw = read_exact(analysis_proc.stdout, FRAME_SIZE)
        if raw is None:
            break

        frame_index += 1
        GLOBAL_FRAME_COUNT += 1
        frame_counter += 1

        if frame_index % ANALYZE_EVERY != 0:
            continue

        frame = np.frombuffer(raw, np.uint8).reshape((FRAME_H, FRAME_W, 3))

        roi = frame[LOGO_Y:LOGO_Y+new_h, LOGO_X:LOGO_X+new_w]

        gray_recv = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        diff = cv2.absdiff(gray_recv, logo_gray)
        _, diff = cv2.threshold(diff, 15, 255, cv2.THRESH_TOZERO)

        error = np.mean(diff)

        error_sum += error

        if frame_counter == AVG_WINDOW:
            num_samples = frame_counter // ANALYZE_EVERY
            avg_error = error_sum / max(1, num_samples)
            
            integrity = 100 * (1 - avg_error / 255)

            last_avg_error = avg_error
            last_integrity = integrity

            delta = 0

            if prev_integrity is None:
                control = 0
            else:
                delta = integrity - prev_integrity

                if delta > TOL:
                    control = last_direction
                elif delta < -TOL:
                    control = -last_direction
                else:
                    control = 0

            if control == 1:
                gpio_pulse_async(PIN_RIGHT, 10)
            elif control == -1:
                gpio_pulse_async(PIN_LEFT, 10)

            if control != 0:
                last_direction = control

            prev_for_log = prev_integrity
            prev_integrity = integrity

            print(
                f"Integrity: {integrity:.2f}%   "
                f"Delta: {delta:.3f}   "
                f"Control: {control}"
            )

            frame_counter = 0
            error_sum = 0

        with open(ANALYSIS_LOG, "a") as f:
            f.write(
                f"{GLOBAL_FRAME_COUNT},"
                f"{error:.4f},"
                f"{'' if last_avg_error is None else f'{last_avg_error:.4f}'},"
                f"{'' if last_integrity is None else f'{last_integrity:.4f}'},"
                f"{'' if prev_for_log is None else f'{prev_for_log:.4f}'},"
                f"{control}\n"
            )

def shutdown(sig, frame):
    try:
        display_proc.terminate()
        if analysis_proc:
            analysis_proc.terminate()
    except:
        pass

    lgpio.gpio_write(chip, PIN_RIGHT, 0)
    lgpio.gpio_write(chip, PIN_LEFT, 0)
    lgpio.gpiochip_close(chip)

    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)

if ENABLE_ANALYSIS:
    run_analysis()
else:
    signal.pause()