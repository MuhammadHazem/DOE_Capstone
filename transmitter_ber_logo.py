import cv2
import subprocess
import time

VIDEO = "/home/muhammad/Downloads//PacificRim.mp4"
LOGO_PATH = "/home/muhammad/Downloads//logo.jpg"

SRT_URL_DISPLAY = "srt://10.42.0.45:7000?mode=caller"
SRT_URL_ANALYSIS = "srt://10.42.0.45:7001?mode=caller"

width = 1920
height = 1080

logo = cv2.imread(LOGO_PATH)
if logo is None:
    raise Exception("Logo not found")

h, w = logo.shape[:2]

TARGET_WIDTH = 300
scale = TARGET_WIDTH / w

new_w = int(w * scale)
new_h = int(h * scale)

logo = cv2.resize(logo, (new_w, new_h))

LOGO_X = width - new_w
LOGO_Y = height - new_h

cap = cv2.VideoCapture(VIDEO)

ffmpeg = subprocess.Popen([
    "ffmpeg",
    "-loglevel", "error",
    "-y",
    "-f", "rawvideo",
    "-pix_fmt", "bgr24",
    "-s", f"{width}x{height}",
    "-r", "30",
    "-i", "-",
    "-c:v", "libx264",
    "-preset", "slow",
    "-tune", "zerolatency",
    "-map", "0:v",
    "-f", "tee",
    f"[onfail=ignore:f=mpegts]{SRT_URL_DISPLAY}|[onfail=ignore:f=mpegts]{SRT_URL_ANALYSIS}"
], stdin=subprocess.PIPE)

while True:
    ret, frame = cap.read()

    start_frame = int(6 * 60 * 30)
    if not ret:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        continue

    frame = cv2.resize(frame, (width, height))

    frame[LOGO_Y:LOGO_Y+new_h, LOGO_X:LOGO_X+new_w] = logo

    try:
        ffmpeg.stdin.write(frame.tobytes())
        time.sleep(1 / 30)
    except BrokenPipeError:
        print("Stream ended")
        break