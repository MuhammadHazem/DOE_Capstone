import socket
import subprocess
import threading
import time
from dataclasses import dataclass

TX_LISTEN_IP = "0.0.0.0"
TX_CMD_PORT = 5006

SRT_DST_IP = "10.42.0.45"   # receiver machine IP
SRT_DST_PORT = 7000

VIDEO_FILE = "/home/muhammad/Downloads/PacificRim.mp4"
START_TS = "00:06:00"

@dataclass
class TxConfig:
    width: int = 1280
    height: int = 720
    bitrate: str = "2500k"
    maxrate: str = "3000k"
    bufsize: str = "6000k"
    gop: int = 48
    preset: str = "veryfast"
    tune_zerolatency: bool = True
    srt_latency_ms: int = 1400
    pkt_size: int = 1316

class Transmitter:
    def __init__(self):
        self.cfg = TxConfig()
        self.proc = None
        self.lock = threading.Lock()
        self.need_restart = False

    def build_ffmpeg_cmd(self):
        tune_args = ["-tune", "zerolatency"] if self.cfg.tune_zerolatency else []
        srt_url = (
            f"srt://{SRT_DST_IP}:{SRT_DST_PORT}"
            f"?mode=caller"
            f"&latency={self.cfg.srt_latency_ms}"
            f"&pkt_size={self.cfg.pkt_size}"
        )

        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-re",
            "-stream_loop", "-1",
            "-ss", START_TS,
            "-i", VIDEO_FILE,
            "-vf", f"scale={self.cfg.width}:{self.cfg.height}",
            "-c:v", "libx264",
            "-preset", self.cfg.preset,
            *tune_args,
            "-pix_fmt", "yuv420p",
            "-b:v", self.cfg.bitrate,
            "-maxrate", self.cfg.maxrate,
            "-bufsize", self.cfg.bufsize,
            "-g", str(self.cfg.gop),
            "-keyint_min", str(self.cfg.gop),
            "-sc_threshold", "0",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ac", "2",
            "-f", "mpegts",
            srt_url
        ]
        return cmd

    def is_running_locked(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            self.stop_locked()
            cmd = self.build_ffmpeg_cmd()
            self.proc = subprocess.Popen(cmd)
            self.need_restart = False

    def stop_locked(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    def restart(self):
        with self.lock:
            self.stop_locked()
            time.sleep(0.2)
            cmd = self.build_ffmpeg_cmd()
            self.proc = subprocess.Popen(cmd)
            self.need_restart = False

    def apply_setting(self, key, value):
        with self.lock:
            if key == "RES":
                if value == "480":
                    self.cfg.width, self.cfg.height = 854, 480
                elif value == "720":
                    self.cfg.width, self.cfg.height = 1280, 720
                elif "x" in value:
                    w, h = value.split("x", 1)
                    self.cfg.width, self.cfg.height = int(w), int(h)
                self.need_restart = True

            elif key == "BR":
                self.cfg.bitrate = value
                self.need_restart = True

            elif key == "MAXRATE":
                self.cfg.maxrate = value
                self.need_restart = True

            elif key == "BUFSIZE":
                self.cfg.bufsize = value
                self.need_restart = True

            elif key == "GOP":
                self.cfg.gop = int(value)
                self.need_restart = True

            elif key == "PRESET":
                self.cfg.preset = value
                self.need_restart = True

            elif key == "TUNE_ZL":
                self.cfg.tune_zerolatency = (str(value) == "1")
                self.need_restart = True

            elif key == "SRT_LAT":
                self.cfg.srt_latency_ms = int(value)
                self.need_restart = True

            elif key == "PKT":
                self.cfg.pkt_size = int(value)
                self.need_restart = True

    def maybe_restart(self):
        with self.lock:
            need = self.need_restart
            running = self.is_running_locked()

        # Important: don’t restart on APPLY unless stream is already running.
        # Otherwise APPLY before PLAY forces an SRT connect attempt and fails.
        if need and running:
            self.restart()

def udp_server_loop(tx: Transmitter):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((TX_LISTEN_IP, TX_CMD_PORT))
    print(f"[TX] listening UDP {TX_LISTEN_IP}:{TX_CMD_PORT}")

    while True:
        data, addr = sock.recvfrom(2048)
        msg = data.decode("utf-8", errors="ignore").strip()
        print(f"[TX] {addr} -> {msg}")

        if msg == "PLAY":
            tx.start()
            continue
        if msg == "STOP":
            with tx.lock:
                tx.stop_locked()
            continue
        if msg == "APPLY":
            tx.maybe_restart()
            continue

        if msg.startswith("SET_") and ":" in msg:
            left, right = msg.split(":", 1)
            key = left.replace("SET_", "")
            tx.apply_setting(key, right)

if __name__ == "__main__":
    tx = Transmitter()
    t = threading.Thread(target=udp_server_loop, args=(tx,), daemon=True)
    t.start()
    print("[TX] ready. Send PLAY to start.")
    while True:
        time.sleep(1)
