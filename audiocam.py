#!/usr/bin/env python3
"""
AUDIOCAM PRO v5.2 — Estilo audiocam2d + Zoom nativo + Galería.

Cambios vs v5.1:
  • Visual estilo oscuro con acentos rojo/dorado (audiocam2d)
  • Zoom nativo: pinch-to-zoom, presets, indicador, barra de progreso
  • Galería: al tocar thumbnail se abre galería de grabaciones guardadas
  • Viewer: vista previa de videos guardados en la PC
  • Guardado en PC (no descarga en teléfono)
"""

import socket, threading, queue, os, logging, tempfile, subprocess, shutil, sys, time
import tkinter as tk
from tkinter import ttk
import numpy as np
from flask import Flask, render_template_string, jsonify, Response, request
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

try:
    import pyaudio

    HAS_PA = True
except ImportError:
    HAS_PA = False

RATE = 44100
CHANNELS = 1
CHUNK = 1024
PORT = 5000

audio_level = 0
audio_level_lock = threading.Lock()
server_running = False
subscribers = []
sub_lock = threading.Lock()
has_ffmpeg = bool(shutil.which("ffmpeg"))

app = Flask(__name__)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB

SAVE_DIR = os.path.join(os.getcwd(), "audiocam_recordings")
os.makedirs(SAVE_DIR, exist_ok=True)

# ═══════════════════════════════════════════
# AudioWorklet — Jitter Buffer + Latency Report
# ═══════════════════════════════════════════
WORKLET_JS = r"""
class P extends AudioWorkletProcessor {
  constructor() {
    super();
    this.SZ = 44100 * 4;
    this.B  = new Float32Array(this.SZ);
    this.w = 0; this.r = 0; this.n = 0;
    this.MX = (44100 * 0.6)|0;
    this.HI = (44100 * 0.15)|0;
    this.ok = false;
    this.go = true;
    this.ls = 0;
    this.C  = 0;

    this.port.onmessage = (e) => {
      const d = e.data;
      if (!(d instanceof Float32Array)) return;
      const B = this.B, S = this.SZ;
      let w = this.w;
      for (let i = 0, L = d.length; i < L; i++) {
        B[w] = d[i]; if (++w >= S) w = 0;
      }
      this.w = w;
      this.n = Math.min(this.n + d.length, S);
      if (this.n > this.MX) {
        const drop = this.n - this.MX;
        this.r = (this.r + drop) % S;
        this.n = this.MX;
      }
    };
  }

  process(inputs, outputs) {
    const o = outputs[0][0];
    if (!o) return true;
    const L = o.length;

    /* Reportar latencia cada ~0.5s */
    if (++this.C >= 150) {
      this.port.postMessage({ t: 'lat', ms: (this.n / 44100 * 1000) | 0 });
      this.C = 0;
    }

    if (!this.ok || !this.go) {
      if (this.n >= this.HI) { this.ok = true; this.go = true; }
      else { o.fill(0); return true; }
    }

    if (this.n >= L) {
      const B = this.B, S = this.SZ;
      let r = this.r;
      for (let i = 0; i < L; i++) { o[i] = B[r]; if (++r >= S) r = 0; }
      this.ls = o[L - 1]; this.r = r; this.n -= L;
      return true;
    }

    const B = this.B, S = this.SZ;
    let r = this.r, i = 0;
    while (i < this.n) { o[i] = B[r]; if (++r >= S) r = 0; i++; }
    if (i > 0) this.ls = o[i - 1];
    this.r = r; this.n = 0;
    const fl = Math.min(L - i, 128), lv = this.ls;
    for (let j = 0; j < fl; j++) o[i + j] = lv * (1 - j / fl);
    i += fl;
    while (i < L) o[i++] = 0;
    this.go = false;
    return true;
  }
}
registerProcessor('p', P);
"""

WORKER_JS = r"""
let on = true;
self.onmessage = function(e) {
  if (e.data.cmd === 'start') feed(e.data.url);
  if (e.data.cmd === 'stop') on = false;
};
async function feed(url) {
  while (on) {
    try {
      const resp = await fetch(url);
      const reader = resp.body.getReader();
      let extra = new Uint8Array(0);
      while (on) {
        const { done, value } = await reader.read();
        if (done) break;
        let buf;
        if (extra.length) {
          buf = new Uint8Array(extra.length + value.length);
          buf.set(extra); buf.set(value, extra.length);
        } else buf = value;
        const ns = (buf.length / 2) | 0, used = ns * 2;
        extra = buf.length > used ? buf.slice(used) : new Uint8Array(0);
        const f32 = new Float32Array(ns);
        const dv = new DataView(buf.buffer, buf.byteOffset, used);
        for (let i = 0; i < ns; i++) f32[i] = dv.getInt16(i * 2, true) / 32768.0;
        self.postMessage(f32, [f32.buffer]);
      }
    } catch (e) {}
    if (!on) break;
    await new Promise(r => setTimeout(r, 400));
  }
}
"""

_TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "audiocam_template.html"
)
with open(_TEMPLATE, "r", encoding="utf-8") as _f:
    HTML = _f.read()


# ═══════════════════ RUTAS FLASK ═══════════════════


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/worklet.js")
def worklet():
    return Response(
        WORKLET_JS,
        mimetype="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.route("/worker.js")
def worker_route():
    return Response(
        WORKER_JS,
        mimetype="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.route("/audio_level")
def get_level():
    with audio_level_lock:
        lv = audio_level
    return jsonify({"level": lv})


@app.route("/audio_pcm")
def audio_pcm():
    def gen():
        q = queue.Queue(maxsize=20)
        with sub_lock:
            subscribers.append(q)
        try:
            while server_running:
                try:
                    yield q.get(timeout=0.5)
                except queue.Empty:
                    continue
        finally:
            with sub_lock:
                if q in subscribers:
                    subscribers.remove(q)

    return Response(
        gen(),
        mimetype="application/octet-stream",
        headers={
            "Cache-Control": "no-cache,no-store,must-revalidate",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.route("/fix_sync", methods=["POST"])
def fix_sync():
    """
    Recibe video + delay_ms.
    Usa ffmpeg para avanzar el audio y compensar el buffer delay.
    Guarda el resultado en el PC y retorna JSON con el nombre del archivo.

    Lógica:
      - Audio en la grabación está retrasado por delay_ms
      - ffmpeg -itsoffset -{delay} adelanta el audio
      - Resultado: audio sincronizado con video
    """
    delay_ms = int(request.form.get("delay_ms", 0))
    video = request.files.get("video")

    if not video:
        return jsonify({"error": "No video file"}), 400

    # Detectar formato (iOS envía mp4, Android/desktop webm)
    ct = video.content_type or ""
    is_mp4 = "mp4" in ct
    fext = "mp4" if is_mp4 else "webm"

    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")

    def _save_to_pc(data, suffix):
        fname = f"ACAM_{ts}_{suffix}.{fext}"
        path = os.path.join(SAVE_DIR, fname)
        with open(path, "wb") as f:
            f.write(data)
        size_mb = len(data) / 1048576
        print(f"[Save] 💾 {fname} ({size_mb:.1f}MB) → {SAVE_DIR}")
        return jsonify(
            {"ok": True, "filename": fname, "path": path, "synced": suffix == "synced"}
        )

    # Si delay es imperceptible, guardar original
    if delay_ms < 30:
        return _save_to_pc(video.read(), "original")

    # Sin ffmpeg, guardar original
    if not has_ffmpeg:
        print(f"[Sync] ffmpeg no disponible, delay={delay_ms}ms sin corregir")
        return _save_to_pc(video.read(), "no_ffmpeg")

    tmpdir = tempfile.mkdtemp(prefix="audiocam_")
    try:
        inp = os.path.join(tmpdir, f"input.{fext}")
        out = os.path.join(tmpdir, f"output.{fext}")

        video.save(inp)
        delay_s = delay_ms / 1000.0

        print(f"[Sync] Corrigiendo {delay_ms}ms de delay...")

        # ffmpeg: adelantar audio por delay_s
        # -itsoffset -{delay} en el primer input (audio) → audio avanza
        # Segundo input sin offset (video) → video queda igual
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-itsoffset",
                f"-{delay_s:.3f}",
                "-i",
                inp,
                "-i",
                inp,
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-fflags",
                "+genpts",
                out,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 0 and os.path.exists(out):
            with open(out, "rb") as f:
                data = f.read()
            print(f"[Sync] ✅ Corregido: {len(data) / 1048576:.1f}MB, -{delay_ms}ms")
            return _save_to_pc(data, "synced")
        else:
            print(f"[Sync] ❌ ffmpeg error: {result.stderr[-500:]}")
            # Fallback: guardar original
            with open(inp, "rb") as f:
                data = f.read()
            return _save_to_pc(data, "unsynced")

    except subprocess.TimeoutExpired:
        print("[Sync] ❌ ffmpeg timeout")
        return jsonify({"error": "Processing timeout"}), 504
    except Exception as e:
        print(f"[Sync] ❌ Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/save_video", methods=["POST"])
def save_video():
    """Recibe video del teléfono y lo guarda en el PC."""
    video = request.files.get("video")
    if not video:
        return jsonify({"error": "No video file"}), 400
    filename = video.filename or "rec.webm"
    # Nombre seguro: reemplazar caracteres problemáticos
    safe = "".join(c for c in filename if c.isalnum() or c in "._-")
    if not safe:
        safe = "rec.webm"
    # Agregar timestamp para evitar colisiones
    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    name, ext = os.path.splitext(safe)
    final_name = f"ACAM_{ts}{ext}"
    path = os.path.join(SAVE_DIR, final_name)
    video.save(path)
    size_mb = os.path.getsize(path) / 1048576
    print(f"[Save] 💾 {final_name} ({size_mb:.1f}MB) → {SAVE_DIR}")
    return jsonify({"ok": True, "filename": final_name, "path": path})


@app.route("/gallery")
def gallery_json():
    """Lista de grabaciones guardadas en el PC."""
    try:
        files = []
        if os.path.exists(SAVE_DIR):
            for f in sorted(os.listdir(SAVE_DIR), reverse=True):
                if f.lower().endswith((".mp4", ".webm")) and not f.endswith("_temp"):
                    fp = os.path.join(SAVE_DIR, f)
                    if os.path.getsize(fp) > 0:
                        files.append({"name": f, "size": os.path.getsize(fp)})
        return jsonify(files)
    except Exception as e:
        print(f"[Gallery] {e}")
        return jsonify([])


@app.route("/saves/<path:filename>")
def serve_save(filename):
    """Streaming con Range requests para reproducción en el teléfono."""
    try:
        path = os.path.join(SAVE_DIR, os.path.basename(filename))
        if not os.path.exists(path):
            return "Not found", 404
        ext = filename.rsplit(".", 1)[-1].lower()
        mime = "video/mp4" if ext == "mp4" else "video/webm"
        file_size = os.path.getsize(path)

        range_header = request.headers.get("Range")
        if range_header:
            byte_range = range_header.replace("bytes=", "").split("-")
            start = int(byte_range[0]) if byte_range[0] else 0
            end = (
                int(byte_range[1])
                if len(byte_range) > 1 and byte_range[1]
                else file_size - 1
            )
            end = min(end, file_size - 1)
            length = end - start + 1

            def gen_chunk(s, e):
                with open(path, "rb") as f:
                    f.seek(s)
                    remaining = e - s + 1
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            return Response(
                gen_chunk(start, end),
                206,
                mimetype=mime,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                },
            )

        def gen_full():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        return Response(
            gen_full(),
            mimetype=mime,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )
    except Exception as e:
        print(f"[Serve] {e}")
        return "Error", 500


# ═══════════════ CAPTURA AUDIO ═══════════════


def audio_capture_thread(dev_idx):
    global audio_level
    if not HAS_PA:
        return
    try:
        p = pyaudio.PyAudio()
        s = p.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=dev_idx,
            frames_per_buffer=CHUNK,
        )
        print(f"[Audio] device {dev_idx} @ {RATE}Hz")
        while server_running:
            try:
                data = s.read(CHUNK, exception_on_overflow=False)
            except:
                break
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            if len(samples) > 0:
                rms = np.sqrt(np.mean(samples**2))
                with audio_level_lock:
                    audio_level = min(100, int(rms / 327.68 * 100))
            with sub_lock:
                for q in subscribers:
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        try:
                            q.get_nowait()
                        except:
                            pass
                        try:
                            q.put_nowait(data)
                        except:
                            pass
        s.stop_stream()
        s.close()
        p.terminate()
    except Exception as e:
        print(f"[Audio] {e}")


# ═══════════════ UTILIDADES ═══════════════


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"


def list_audio_devices():
    devs = []
    if not HAS_PA:
        return devs
    try:
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                devs.append((i, info["name"]))
        p.terminate()
    except:
        pass
    return devs


def generate_cert():
    if os.path.exists("cert.pem") and os.path.exists("key.pem"):
        return True
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509 import SubjectAlternativeName, DNSName, IPAddress
        import datetime, ipaddress

        lip = get_local_ip()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AUDIOCAM")])
        san = [DNSName("localhost"), IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            san.append(IPAddress(ipaddress.IPv4Address(lip)))
        except:
            pass
        cert = (
            x509.CertificateBuilder()
            .subject_name(subj)
            .issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .add_extension(SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256())
        )
        with open("key.pem", "wb") as f:
            f.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
        with open("cert.pem", "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        print("[Cert] OK")
        return True
    except ImportError:
        print("[Cert] pip install cryptography")
        return False
    except Exception as e:
        print(f"[Cert] {e}")
        return False


# ═══════════════ TKINTER GUI ═══════════════


class GUI:
    def __init__(self, root):
        self.root = root
        self._server = None
        root.title("AUDIO-wifi-CAM v5.2")
        root.geometry("440x540")
        root.resizable(False, False)
        root.configure(bg="#000000")

        s = ttk.Style()
        s.theme_use("clam")
        s.configure(
            "TCombobox",
            fieldbackground="#000",
            background="#111",
            foreground="#fff",
            arrowcolor="#CC0000",
            bordercolor="#CC0000",
        )
        s.configure(
            "R.Horizontal.TProgressbar", troughcolor="#1a1a1a", background="#CC0000"
        )

        BANNER = (
            " █████╗ ██╗   ██╗██████╗ ██╗ ██████╗\n"
            "██╔══██╗██║   ██║██╔══██╗██║██╔═══██╗\n"
            "███████║██║   ██║██║  ██║██║██║   ██║\n"
            "██╔══██║██║   ██║██║  ██║██║██║   ██║\n"
            "██║  ██║╚██████╔╝██████╔╝██║╚██████╔╝\n"
            "╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═╝ ╚═════╝\n"
            "       ➜➜➜   WIFI   ➜➜➜\n"
            " ██████╗ █████╗ ███╗   ███╗\n"
            "██╔════╝██╔══██╗████╗ ████║\n"
            "██║     ███████║██╔████╔██║\n"
            "██║     ██╔══██║██║╚██╔╝██║\n"
            "╚██████╗██║  ██║██║ ╚═╝ ██║\n"
            " ╚═════╝╚═╝  ╚═╝╚═╝     ╚═╝ ⓒdimare"
        )
        hdr = tk.Frame(root, bg="#000")
        hdr.pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(
            hdr,
            text=BANNER,
            font=("Courier New", 6, "bold"),
            bg="#000",
            fg="#CC0000",
            justify="left",
        ).pack(anchor="w")
        tk.Frame(root, bg="#fff", height=2).pack(fill="x", padx=10)

        main = tk.Frame(root, bg="#000", padx=22, pady=8)
        main.pack(fill="both", expand=True)

        af = tk.LabelFrame(
            main,
            text="  MICRÓFONO PC  ",
            font=("Arial Narrow", 8, "bold"),
            bg="#000",
            fg="#CC0000",
            padx=8,
            pady=6,
            bd=1,
            relief="flat",
            highlightbackground="#CC0000",
            highlightthickness=1,
        )
        af.pack(fill="x", pady=(10, 0))
        self.devs = list_audio_devices()
        names = [n for _, n in self.devs] or ["(sin dispositivos)"]
        self.avar = tk.StringVar(value=names[0])
        self.combo = ttk.Combobox(
            af, textvariable=self.avar, values=names, state="readonly", width=44
        )
        self.combo.pack()

        mf = tk.LabelFrame(
            main,
            text="  NIVEL  ",
            font=("Arial Narrow", 8, "bold"),
            bg="#000",
            fg="#CC0000",
            padx=8,
            pady=6,
            bd=1,
            relief="flat",
            highlightbackground="#CC0000",
            highlightthickness=1,
        )
        mf.pack(fill="x", pady=(8, 0))
        self.meter = ttk.Progressbar(
            mf, length=360, maximum=100, style="R.Horizontal.TProgressbar"
        )
        self.meter.pack()
        self.lvl = tk.Label(
            mf, text="0 %", font=("Arial Narrow", 9, "bold"), bg="#000", fg="#fff"
        )
        self.lvl.pack()

        bf = tk.Frame(main, bg="#000")
        bf.pack(pady=10)
        self.bgo = tk.Button(
            bf,
            text="▶  INICIAR",
            width=13,
            bg="#CC0000",
            fg="#fff",
            font=("Arial Narrow", 10, "bold"),
            relief="flat",
            activebackground="#990000",
            command=self.start,
            cursor="hand2",
            bd=0,
        )
        self.bgo.pack(side="left", padx=4)
        self.bst = tk.Button(
            bf,
            text="■  DETENER",
            width=13,
            bg="#111",
            fg="#fff",
            font=("Arial Narrow", 10, "bold"),
            relief="flat",
            activebackground="#CC0000",
            command=self.stop,
            state="disabled",
            cursor="hand2",
            bd=0,
        )
        self.bst.pack(side="left", padx=4)

        self.status = tk.StringVar(value="■  DETENIDO")
        tk.Label(
            main,
            textvariable=self.status,
            font=("Arial Narrow", 9, "bold"),
            bg="#000",
            fg="#fff",
        ).pack()

        footer = tk.Frame(root, bg="#111", padx=16, pady=12)
        footer.pack(fill="x", side="bottom")
        tk.Frame(root, bg="#CC0000", height=2).pack(fill="x", side="bottom")

        uf = tk.Frame(footer, bg="#111")
        uf.pack(fill="x")
        tk.Label(
            uf,
            text="ABRIR EN EL TELÉFONO",
            font=("Arial Narrow", 7, "bold"),
            bg="#111",
            fg="#CC0000",
        ).pack(anchor="w")
        self.url = tk.StringVar(value=f"https://{get_local_ip()}:{PORT}")
        tk.Label(
            uf,
            textvariable=self.url,
            font=("Courier", 11, "bold"),
            bg="#111",
            fg="#fff",
        ).pack(anchor="w")
        tk.Label(
            uf,
            text="⚠  Aceptar certificado → Avanzado → Continuar",
            font=("Arial Narrow", 7),
            bg="#111",
            fg="#888",
        ).pack(anchor="w")

        tk.Frame(footer, bg="#CC0000", height=1).pack(fill="x", pady=(8, 0))
        ff_c = "#fff" if has_ffmpeg else "#CC0000"
        ff_t = "▶  FFMPEG — SYNC ACTIVO" if has_ffmpeg else "✕  FFMPEG NO ENCONTRADO"
        tk.Label(
            footer, text=ff_t, font=("Arial Narrow", 7, "bold"), bg="#111", fg=ff_c
        ).pack(anchor="w", pady=(4, 0))
        tk.Label(
            footer,
            text=f"💾 Guardado en: {SAVE_DIR}",
            font=("Arial Narrow", 7),
            bg="#111",
            fg="#D4A017",
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            footer,
            text=f"📁 Galería: https://{get_local_ip()}:{PORT}/galeria",
            font=("Arial Narrow", 7),
            bg="#111",
            fg="#D4A017",
        ).pack(anchor="w", pady=(2, 0))

        self._smooth = 0.0
        self._tick()
        root.after(500, self.start)

    def _tick(self):
        with audio_level_lock:
            raw = audio_level
        self._smooth = self._smooth * (0.3 if raw > self._smooth else 0.85) + raw * (
            0.7 if raw > self._smooth else 0.15
        )
        lv = int(self._smooth)
        self.meter["value"] = lv
        self.lvl.config(text=f"{lv} %", fg="#CC0000" if lv >= 60 else "#fff")
        self.root.after(80, self._tick)

    def start(self):
        global server_running
        if server_running:
            return
        server_running = True
        if not generate_cert():
            server_running = False
            self.status.set("✕  CERT FAIL")
            return
        if self.devs:
            sel = self.avar.get()
            idx = next((i for i, n in self.devs if n == sel), 0)
            threading.Thread(
                target=audio_capture_thread, args=(idx,), daemon=True
            ).start()
        threading.Thread(target=self._serve, daemon=True).start()
        self.bgo.config(state="disabled")
        self.bst.config(state="normal", bg="#CC0000")
        self.combo.config(state="disabled")
        self.status.set(f"▶  {self.url.get()}")
        logger.info("[GUI] Server iniciado")

    def stop(self):
        global server_running, audio_level
        server_running = False
        with audio_level_lock:
            audio_level = 0
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
        self.bgo.config(state="normal")
        self.bst.config(state="disabled", bg="#1a1a1a")
        self.combo.config(state="readonly")
        self.status.set("■  DETENIDO")
        logger.info("[GUI] Server detenido")

    def _serve(self):
        ctx = ("cert.pem", "key.pem") if os.path.exists("cert.pem") else None
        from werkzeug.serving import make_server

        self._server = make_server("0.0.0.0", PORT, app, ssl_context=ctx, threaded=True)
        logger.info(f"[GUI] Werkzeug listening on :{PORT}")
        self._server.serve_forever()


# ═══════════════ MAIN ═══════════════

if __name__ == "__main__":
    logger.info("=== AUDIO-wifi-CAM v5.2 START ===")
    logger.info(f"Save dir: {SAVE_DIR}")
    logger.info(f"FFmpeg: {'OK' if has_ffmpeg else 'NOT FOUND'}")
    logger.info(f"PyAudio: {'OK' if HAS_PA else 'NOT INSTALLED'}")

    try:
        root = tk.Tk()
        if sys.platform == "win32":
            import ctypes

            root.after(
                100,
                lambda: ctypes.windll.user32.ShowWindow(
                    ctypes.windll.kernel32.GetConsoleWindow(), 0
                ),
            )
        GUI(root)
        root.mainloop()
    except KeyboardInterrupt:
        logger.info("Shutdown by user")
    except Exception as e:
        logger.critical(f"Fatal: {e}", exc_info=True)
        input("Presiona ENTER para salir...")
