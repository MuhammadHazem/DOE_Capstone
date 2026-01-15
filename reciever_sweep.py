import os
import csv
import json
import time
import socket
import threading
import subprocess
import tkinter as tk
from tkinter import ttk


TX_IP = "10.42.0.1"       
TX_CMD_PORT = 5006        
RX_LISTEN_URL_BASE = "srt://0.0.0.0:7000?mode=listener"

REF_FILE = "/home/muhammad/Downloads/PacificRim.mp4"
REF_START = "00:06:00"
TEST_DURATION = 60

TX_SETTLE_SECONDS = 2.0

SRT_CONNECT_TIMEOUT_US = 5_000_000      
RECORD_HARD_TIMEOUT_SEC = TEST_DURATION + 20
METRICS_HARD_TIMEOUT_SEC = 300             

WORKDIR = "sweeps"
CAPTURE_DIR = os.path.join(WORKDIR, "captures")
METRICS_DIR = os.path.join(WORKDIR, "metrics")
RESULTS_CSV = os.path.join(WORKDIR, "results.csv")

MODEL_PATH = "/usr/local/share/model/vmaf_v0.6.1.json"

os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(METRICS_DIR, exist_ok=True)


class DiscreteSlider(ttk.Frame):
    def __init__(self, parent, title, points, on_change, initial=None):
        super().__init__(parent)
        self.points = points
        self.on_change = on_change

        ttk.Label(self, text=title).grid(row=0, column=0, sticky="w")

        self.value_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.value_var, width=20).grid(row=0, column=1, sticky="e")

        self.scale = ttk.Scale(
            self,
            from_=0,
            to=len(points) - 1,
            orient="horizontal",
            command=self._on_drag
        )
        self.scale.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.columnconfigure(0, weight=1)
        self.scale.bind("<ButtonRelease-1>", self._on_release)

        if initial is None:
            initial_idx = 0
        else:
            initial_idx = points.index(initial) if initial in points else 0

        self._set_index(initial_idx, fire=False)

    def _snap_index(self, x):
        return int(round(float(x)))

    def _set_index(self, idx, fire=True):
        idx = max(0, min(len(self.points) - 1, idx))
        self.scale.set(idx)
        val = self.points[idx]
        self.value_var.set(str(val))
        if fire:
            self.on_change(val)

    def _on_drag(self, x):
        idx = self._snap_index(x)
        idx = max(0, min(len(self.points) - 1, idx))
        self.value_var.set(str(self.points[idx]))

    def _on_release(self, _event):
        idx = self._snap_index(self.scale.get())
        self._set_index(idx, fire=True)

def tx_send(cmd: str):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(cmd.encode("utf-8"), (TX_IP, TX_CMD_PORT))
    finally:
        s.close()

def run_cmd_with_timeout(cmd, timeout_sec: int, step_name: str):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        raise RuntimeError(f"{step_name} timed out after {timeout_sec}s.\nSTDERR:\n{err}")

    if p.returncode != 0:
        raise RuntimeError(f"{step_name} failed (rc={p.returncode}).\nSTDERR:\n{err}")
    return out, err

def build_rx_url(latency_ms: int, pkt_size: int):
    return (
        f"{RX_LISTEN_URL_BASE}"
        f"&latency={latency_ms}"
        f"&pkt_size={pkt_size}"
        f"&timeout={SRT_CONNECT_TIMEOUT_US}"
    )

def record_stream(run_id: str, latency_ms: int, pkt_size: int):
    url = build_rx_url(latency_ms, pkt_size)
    out_ts = os.path.join(CAPTURE_DIR, f"{run_id}.ts")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-fflags", "+genpts+igndts",
        "-err_detect", "ignore_err",
        
        "-rw_timeout", "8000000",

        "-i", url,
        "-t", str(TEST_DURATION),
        "-c", "copy",
        "-mpegts_flags", "+resend_headers",
        "-f", "mpegts",
        out_ts
    ]
    run_cmd_with_timeout(cmd, RECORD_HARD_TIMEOUT_SEC, "record_stream")
    return out_ts

def parse_res(res_str: str):
    w, h = res_str.split("x", 1)
    return int(w), int(h)

def parse_ffmpeg_avg_metric(path: str, token: str):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        idx = txt.rfind(token)
        if idx == -1:
            return None
        s = txt[idx + len(token):].strip()
        num = ""
        for ch in s:
            if ch.isdigit() or ch in ".-":
                num += ch
            else:
                break
        return float(num) if num else None
    except Exception:
        return None

def compute_metrics(run_id: str, capture_ts_path: str, target_w: int, target_h: int):
    dist_mkv = os.path.join(CAPTURE_DIR, f"{run_id}.mkv")
    run_cmd_with_timeout([
	    "ffmpeg", "-y", "-loglevel", "error",
	    "-fflags", "+genpts",
	    "-i", capture_ts_path,
	    "-an",
	    "-vf", "setpts=N/FRAME_RATE/TB,format=yuv420p",
	    "-c:v", "libx264",
	    "-preset", "ultrafast",
	    "-crf", "18",
	    dist_mkv
   ], METRICS_HARD_TIMEOUT_SEC, "decode_capture_fix_pts")


    vmaf_json = os.path.join(METRICS_DIR, f"vmaf_{run_id}.json")
    ssim_log = os.path.join(METRICS_DIR, f"ssim_{run_id}.log")
    psnr_log = os.path.join(METRICS_DIR, f"psnr_{run_id}.log")

    EVAL_W = 1920
    EVAL_H = 1080
    FRAME_RATE = "24000/1001"
    
    vf = (
        f"[0:v]scale={EVAL_W}:{EVAL_H}:flags=bicubic,format=yuv420p,"
        f"settb=AVTB,setpts=PTS-STARTPTS,fps={FRAME_RATE},split=3[ref_vmaf][ref_ssim][ref_psnr];"
        f"[1:v]scale={EVAL_W}:{EVAL_H}:flags=bicubic,format=yuv420p,"
        f"settb=AVTB,setpts=PTS-STARTPTS,fps={FRAME_RATE},split=3[dist_vmaf][dist_ssim][dist_psnr];"
        f"[dist_vmaf][ref_vmaf]libvmaf=model=path={MODEL_PATH}:log_fmt=json:log_path={vmaf_json}:n_threads=8;"
        f"[dist_ssim][ref_ssim]ssim=stats_file={ssim_log};"
        f"[dist_psnr][ref_psnr]psnr=stats_file={psnr_log}"
    )


    out, err = run_cmd_with_timeout([
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-ss", REF_START, "-t", str(TEST_DURATION), "-i", REF_FILE,
        "-t", str(TEST_DURATION), "-i", dist_mkv,
        "-an",
        "-lavfi", vf,
        "-f", "null", "-"
    ], METRICS_HARD_TIMEOUT_SEC, "compute_metrics")
    
    print("\n------ ffmpeg stderr (compute metrics) ------\n")
    print(err)
    print("\n---------------------------------------------\n")

    with open(vmaf_json, "r", encoding="utf-8") as f:
        vmaf_data = json.load(f)

    pooled = vmaf_data.get("pooled_metrics", {})
    if isinstance(pooled, dict) and "vmaf" in pooled and "mean" in pooled["vmaf"]:
        vmaf_mean = float(pooled["vmaf"]["mean"])
    else:
        frames = vmaf_data.get("frames", [])
        vals = []
        for fr in frames:
            m = fr.get("metrics", {})
            if "vmaf" in m:
                vals.append(float(m["vmaf"]))
        vmaf_mean = (sum(vals) / len(vals)) if vals else None

    psnr_avg = parse_last_number_after(err, "average:")
    ssim_avg = parse_last_number_after(err, "All:")

    return vmaf_mean, psnr_avg, ssim_avg

def append_result(row: dict):
    exists = os.path.exists(RESULTS_CSV)
    with open(RESULTS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)

def summarize_best(param_name: str):
    if not os.path.exists(RESULTS_CSV):
        return None

    rows = []
    with open(RESULTS_CSV, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if row.get("param") == param_name and row.get("vmaf") and row.get("psnr") and row.get("ssim"):
                try:
                    row["_vmaf"] = float(row["vmaf"])
                    row["_psnr"] = float(row["psnr"])
                    row["_ssim"] = float(row["ssim"])
                    rows.append(row)
                except Exception:
                    pass

    if not rows:
        return None

    best_abs = max(rows, key=lambda rr: rr["_vmaf"])
    vmax = best_abs["_vmaf"]
    near = [rr for rr in rows if rr["_vmaf"] >= 0.99 * vmax]

    def cost(rr):
        v = rr.get("value", "")
        try:
            if isinstance(v, str) and v.endswith("k"):
                return float(v[:-1])
            return float(v)
        except Exception:
            return 0.0

    best_eff = min(near, key=cost) if near else best_abs
    return best_abs, best_eff

# -------------------------
# Sweep definitions
# -------------------------
POINTS = {
    "res": ["854x480", "1280x720", "1920x1080"],
    "bitrate": ["1000k", "1500k", "2000k", "2500k", "3000k", "3500k", "4000k", "4500k", "5000k", "5500k", "6000k"],
    "vbv_profile": ["1x", "2x", "4x"],
    "gop": [24, 48, 60, 96, 120],
    "preset": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"],
    "tune_zl": [0, 1],
    "srt_latency": [200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800, 2000],
    "pkt_size": [188, 376, 564, 752, 940, 1128, 1316],
}

BASELINE = {
    "res": "1280x720",
    "bitrate": "2500k",
    "maxrate": "3000k",
    "bufsize": "6000k",
    "gop": 48,
    "preset": "veryfast",
    "tune_zl": 1,
    "srt_latency": 1400,
    "pkt_size": 1316,
}

def vbv_from_profile(bitrate_k: str, profile: str):
    b = int(str(bitrate_k).replace("k", ""))
    maxrate = int(b * 1.2)
    if profile == "1x":
        buf = maxrate
    elif profile == "2x":
        buf = maxrate * 2
    else:
        buf = maxrate * 4
    return f"{maxrate}k", f"{buf}k"

def apply_all_settings(cfg: dict):
    tx_send(f"SET_RES:{cfg['res']}")
    tx_send(f"SET_BR:{cfg['bitrate']}")
    tx_send(f"SET_MAXRATE:{cfg['maxrate']}")
    tx_send(f"SET_BUFSIZE:{cfg['bufsize']}")
    tx_send(f"SET_GOP:{cfg['gop']}")
    tx_send(f"SET_PRESET:{cfg['preset']}")
    tx_send(f"SET_TUNE_ZL:{cfg['tune_zl']}")
    tx_send(f"SET_SRT_LAT:{cfg['srt_latency']}")
    tx_send(f"SET_PKT:{cfg['pkt_size']}")
    tx_send("APPLY")

def restart_tx_for_repeatable_frames():
    tx_send("STOP")
    time.sleep(0.2)
    tx_send("PLAY")
    
    
def parse_from_text_last_number_after(text: str, token: str):
    idx = text.rfind(token)
    if idx == -1:
        return None
    s = text[idx + len(token):].strip()
    num = ""
    for ch in s:
        if ch.isdigit() or ch in ".-":
            num += ch
        else:
            break
    return float(num) if num else None

def parse_last_number_after(text: str, token: str):
    idx = text.rfind(token)
    if idx == -1:
        return None
    s = text[idx + len(token):]
    num = ""
    for ch in s.strip():
        if ch.isdigit() or ch in ".-":
            num += ch
        else:
            break
    try:
        return float(num)
    except:
        return None

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SRT Streaming Sweep Runner")
        self.geometry("760x560")

        self.status = tk.StringVar(value="Idle")
        self.running = False
        self.current = dict(BASELINE)

        header = ttk.Frame(self)
        header.pack(fill="x", padx=12, pady=10)
        ttk.Label(header, text="Status:").pack(side="left")
        ttk.Label(header, textvariable=self.status).pack(side="left", padx=(6, 0))

        controls = ttk.LabelFrame(self, text="Discrete sliders (commit on release)")
        controls.pack(fill="x", padx=12, pady=10)

        DiscreteSlider(controls, "Resolution", POINTS["res"], on_change=self.on_res, initial=BASELINE["res"]).pack(fill="x", padx=10, pady=6)
        DiscreteSlider(controls, "Bitrate", POINTS["bitrate"], on_change=self.on_bitrate, initial=BASELINE["bitrate"]).pack(fill="x", padx=10, pady=6)
        DiscreteSlider(controls, "GOP", POINTS["gop"], on_change=self.on_gop, initial=BASELINE["gop"]).pack(fill="x", padx=10, pady=6)
        DiscreteSlider(controls, "Preset", POINTS["preset"], on_change=self.on_preset, initial=BASELINE["preset"]).pack(fill="x", padx=10, pady=6)
        DiscreteSlider(controls, "Tune zerolatency (0 or 1)", POINTS["tune_zl"], on_change=self.on_tune, initial=BASELINE["tune_zl"]).pack(fill="x", padx=10, pady=6)
        DiscreteSlider(controls, "SRT latency (ms)", POINTS["srt_latency"], on_change=self.on_srt_lat, initial=BASELINE["srt_latency"]).pack(fill="x", padx=10, pady=6)
        DiscreteSlider(controls, "SRT pkt_size", POINTS["pkt_size"], on_change=self.on_pkt, initial=BASELINE["pkt_size"]).pack(fill="x", padx=10, pady=6)

        actions = ttk.LabelFrame(self, text="Sweep")
        actions.pack(fill="x", padx=12, pady=10)

        self.param_var = tk.StringVar(value="bitrate")
        ttk.Label(actions, text="Sweep parameter:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 6))
        ttk.Combobox(
            actions, textvariable=self.param_var,
            values=["res", "bitrate", "vbv_profile", "gop", "preset", "tune_zl", "srt_latency", "pkt_size"],
            state="readonly", width=20
        ).grid(row=0, column=1, sticky="w", pady=(10, 6))

        self.repeats_var = tk.IntVar(value=2)
        ttk.Label(actions, text="Repeats per point:").grid(row=0, column=2, sticky="e", padx=(30, 6), pady=(10, 6))
        ttk.Spinbox(actions, from_=1, to=5, textvariable=self.repeats_var, width=6).grid(row=0, column=3, sticky="w", pady=(10, 6))

        btns = ttk.Frame(actions)
        btns.grid(row=1, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 10))
        ttk.Button(btns, text="Apply now", command=self.apply_now).pack(side="left")
        ttk.Button(btns, text="Start TX", command=self.play_tx).pack(side="left", padx=8)
        ttk.Button(btns, text="Run sweep", command=self.run_sweep).pack(side="left", padx=8)
        ttk.Button(btns, text="Measure current", command=self.measure_current).pack(side="left", padx=8)
        ttk.Button(btns, text="Stop", command=self.stop).pack(side="left")

        note = ttk.Label(
            self,
            text="For repeatable comparisons each run restarts TX (STOP then PLAY) before recording."
        )
        note.pack(fill="x", padx=12, pady=(0, 10))

        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Label(footer, text=f"CSV: {RESULTS_CSV}").pack(side="left")

    def set_status(self, s: str):
        self.status.set(s)
        self.update_idletasks()

    def play_tx(self):
        tx_send("PLAY")
        self.set_status("Sent PLAY to transmitter")

    def apply_now(self):
        apply_all_settings(self.current)
        self.set_status("Applied to transmitter")

    def stop(self):
        self.running = False
        self.set_status("Stopping after current run")

    def on_res(self, v):
        self.current["res"] = v
        w, h = parse_res(v)
        self.set_status(f"Res set {w}x{h} (pending apply)")

    def on_bitrate(self, v):
        self.current["bitrate"] = v
        mr, bs = vbv_from_profile(v, "2x")
        self.current["maxrate"] = mr
        self.current["bufsize"] = bs
        self.set_status(f"Bitrate set {v} (pending apply)")

    def on_gop(self, v):
        self.current["gop"] = int(v)
        self.set_status(f"GOP set {v} (pending apply)")

    def on_preset(self, v):
        self.current["preset"] = v
        self.set_status(f"Preset set {v} (pending apply)")

    def on_tune(self, v):
        self.current["tune_zl"] = int(v)
        self.set_status(f"Tune zerolatency set {v} (pending apply)")

    def on_srt_lat(self, v):
        self.current["srt_latency"] = int(v)
        self.set_status(f"SRT latency set {v} (pending apply)")

    def on_pkt(self, v):
        self.current["pkt_size"] = int(v)
        self.set_status(f"pkt_size set {v} (pending apply)")

    # --- helper for "Measure current" ---
    def current_config(self):
        """Return the current slider-selected configuration.

        The sliders update `self.current` via their callbacks. This method simply
        exposes a copy so the measure button can run without relying on internal
        UI state.
        """
        return dict(self.current)


    def measure_current(self):
        """Record a short clip using the current slider values and compute VMAF/PSNR/SSIM once."""
        if self.running:
            self.set_status("Busy: already running.")
            return

        def worker():
            try:
                self.running = True
                cfg = self.current_config()

                self.set_status("Measuring current config: restarting TX")
                restart_tx_for_repeatable_frames()

                self.set_status("Measuring current config: waiting for TX")
                time.sleep(TX_SETTLE_SECONDS)

                safe_res = str(cfg["res"]).replace("/", "_").replace(" ", "_")
                run_id = f"single_{safe_res}_{int(time.time())}"

                self.set_status("Measuring current config: record")
                cap_ts = record_stream(run_id, cfg["srt_latency"], cfg["pkt_size"])

                w, h = parse_res(cfg["res"])
                self.set_status("Measuring current config: metrics")
                vmaf, psnr, ssim = compute_metrics(run_id, cap_ts, w, h)

                self.set_status(f"Done: VMAF={vmaf:.4f} PSNR={psnr:.4f} SSIM={ssim:.6f}")

                out_csv = os.path.join(OUT_DIR, "single_results.csv")
                is_new = not os.path.exists(out_csv)
                with open(out_csv, "a", newline="") as f:
                    wcsv = csv.writer(f)
                    if is_new:
                        wcsv.writerow([
                            "ts", "mode", "res", "bitrate", "maxrate", "bufsize",
                            "gop", "preset", "tune_zerolatency", "srt_latency", "pkt_size",
                            "vmaf", "psnr", "ssim"
                        ])
                    wcsv.writerow([
                        int(time.time()), "single", cfg["res"], cfg["bitrate"], cfg["maxrate"], cfg["bufsize"],
                        cfg["gop"], cfg["preset"], int(cfg["tune_zerolatency"]), cfg["srt_latency"], cfg["pkt_size"],
                        f"{vmaf:.4f}", f"{psnr:.4f}", f"{ssim:.6f}"
                    ])

                print("=== Single measure ===")
                print("config:", cfg)
                print(f"VMAF={vmaf:.4f} PSNR={psnr:.4f} SSIM={ssim:.6f}")
            except Exception as e:
                self.set_status(f"Error: {e}")
                print("ERROR:", e)
            finally:
                self.running = False

        threading.Thread(target=worker, daemon=True).start()


    def run_sweep(self):
        if self.running:
            return
        self.running = True
        param = self.param_var.get()
        repeats = int(self.repeats_var.get())
        t = threading.Thread(target=self._sweep_thread, args=(param, repeats), daemon=True)
        t.start()

    def _sweep_thread(self, param: str, repeats: int):
        try:
            points = POINTS.get(param)
            if not points:
                self.running = False
                self.set_status("Invalid parameter")
                return

            self.set_status(f"Sweeping {param} with repeats {repeats}")

            for p in points:
                if not self.running:
                    break

                for rep in range(repeats):
                    if not self.running:
                        break

                    cfg = dict(BASELINE)

                    if param == "vbv_profile":
                        cfg["bitrate"] = BASELINE["bitrate"]
                        mr, bs = vbv_from_profile(cfg["bitrate"], p)
                        cfg["maxrate"] = mr
                        cfg["bufsize"] = bs
                    else:
                        if param == "res":
                            cfg["res"] = p
                        elif param == "bitrate":
                            cfg["bitrate"] = p
                            mr, bs = vbv_from_profile(p, "2x")
                            cfg["maxrate"] = mr
                            cfg["bufsize"] = bs
                        elif param == "gop":
                            cfg["gop"] = int(p)
                        elif param == "preset":
                            cfg["preset"] = p
                        elif param == "tune_zl":
                            cfg["tune_zl"] = int(p)
                        elif param == "srt_latency":
                            cfg["srt_latency"] = int(p)
                        elif param == "pkt_size":
                            cfg["pkt_size"] = int(p)

                    self.set_status(f"{param}={p} rep {rep+1}/{repeats}: applying")
                    apply_all_settings(cfg)

                    self.set_status(f"{param}={p} rep {rep+1}/{repeats}: restarting TX")
                    restart_tx_for_repeatable_frames()

                    self.set_status(f"{param}={p} rep {rep+1}/{repeats}: waiting for TX")
                    time.sleep(TX_SETTLE_SECONDS)

                    safe_p = str(p).replace("/", "_").replace(" ", "_")
                    run_id = f"{param}_{safe_p}_r{rep+1}_{int(time.time())}"

                    self.set_status(f"{param}={p} rep {rep+1}/{repeats}: record")
                    cap_ts = record_stream(run_id, cfg["srt_latency"], cfg["pkt_size"])

                    w, h = parse_res(cfg["res"])
                    self.set_status(f"{param}={p} rep {rep+1}/{repeats}: metrics")
                    vmaf, psnr, ssim = compute_metrics(run_id, cap_ts, w, h)

                    row = {
                        "ts": int(time.time()),
                        "param": param,
                        "value": str(p),
                        "repeat": rep + 1,
                        "res": cfg["res"],
                        "bitrate": cfg["bitrate"],
                        "maxrate": cfg["maxrate"],
                        "bufsize": cfg["bufsize"],
                        "gop": cfg["gop"],
                        "preset": cfg["preset"],
                        "tune_zl": cfg["tune_zl"],
                        "srt_latency": cfg["srt_latency"],
                        "pkt_size": cfg["pkt_size"],
                        "vmaf": "" if vmaf is None else f"{vmaf:.4f}",
                        "psnr": "" if psnr is None else f"{psnr:.4f}",
                        "ssim": "" if ssim is None else f"{ssim:.6f}",
                    }
                    append_result(row)

            summary = summarize_best(param)
            if summary:
                best_abs, best_eff = summary
                print("\n=== Sweep summary:", param, "===")
                print("Best absolute (max VMAF):")
                print(" value =", best_abs["value"], "VMAF =", best_abs["vmaf"], "PSNR =", best_abs["psnr"], "SSIM =", best_abs["ssim"])
                print("Best efficient (within 1 percent of best VMAF):")
                print(" value =", best_eff["value"], "VMAF =", best_eff["vmaf"], "PSNR =", best_eff["psnr"], "SSIM =", best_eff["ssim"])

            self.running = False
            self.set_status(f"Done sweep {param}. CSV saved.")
        except Exception as e:
            self.running = False
            self.set_status(f"Error: {e}")
            print("ERROR:", e)

if __name__ == "__main__":
    app = App()
    app.mainloop()
