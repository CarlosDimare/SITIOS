#!/usr/bin/env python3
"""
TV Argentina — reproductor local sin CORS ni worker.
Usa python-vlc (embedded), mpv, vlc, o ffplay según disponibilidad.

Uso:
    python3 tv-app.py
    python3 tv-app.py --print-urls        # solo mostrar URLs
    python3 tv-app.py --print-urls trece telefe   # canales específicos

Dependencias: pip install requests (python-vlc opcional para ventana embebida)
"""

import re, sys, json, time, subprocess, shutil, tempfile
from pathlib import Path
import requests

# ─── Stream Cache ──────────────────────────────────────────────────────
CACHE_FILE = Path(tempfile.gettempdir()) / "tv_argentina_streams.json"
CACHE_TTL = 180  # 3 minutos

def _cache_get(key):
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            entry = data.get(key)
            if entry and time.time() - entry["t"] < CACHE_TTL:
                return entry["url"]
        except: pass
    return None

def _cache_set(key, url):
    try:
        data = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
        data[key] = {"url": url, "t": time.time()}
        CACHE_FILE.write_text(json.dumps(data))
    except: pass

# ─── Channels ──────────────────────────────────────────────────────────
CHANNELS = {
    "trece": {
        "name": "Canal 13",
        "url": "https://livetrx01.vodgc.net/eltrecetv/index.m3u8",
        "referrer": "https://vodgc.net/",
    },
    "telefe": {
        "name": "Telefe",
        "la14": "telefe",
    },
    "america": {
        "name": "América TV",
        "la14": "america",
    },
    "tvpublica": {
        "name": "TV Pública",
        "la14": "tvpublica",
    },
}

def resolve_stream(ch_id):
    """Devuelve (url, referrer) para reproducir."""
    ch = CHANNELS[ch_id]

    if url := ch.get("url"):
        return url, ch.get("referrer", "")

    if la14 := ch.get("la14"):
        cached = _cache_get(la14)
        if cached:
            return cached, ""
        r = requests.get(
            f"https://la14hd.com/vivo/canal.php?stream={la14}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        m = re.search(r'playbackURL\s*=\s*"([^"]+)"', r.text)
        if not m:
            m = re.search(r"playbackURL\s*=\s*'([^']+)'", r.text)
        if not m:
            raise RuntimeError(f"No se encontró playbackURL para {la14}")
        url = m.group(1).replace("\\/", "/")
        _cache_set(la14, url)
        return url, ""

    raise RuntimeError(f"Canal {ch_id}: sin fuente")

# ─── Player ────────────────────────────────────────────────────────────
class Player:
    def __init__(self):
        self._proc = None
        self._vlc_instance = None
        self._vlc_player = None
        self._backend = self._detect()

    def _detect(self):
        try:
            import vlc as _
            return "vlc-py"
        except ImportError:
            pass
        for cmd in ["mpv", "vlc", "ffplay"]:
            if shutil.which(cmd):
                return cmd
        return None

    @property
    def backend(self):
        return self._backend or "nada"

    def play(self, url, referrer="", hwnd=None):
        self.stop()
        print(f"[{self._backend}] {url}")
        if self._backend == "vlc-py":
            self._play_vlc_py(url, referrer, hwnd)
        elif self._backend == "mpv":
            self._play_mpv(url, referrer)
        elif self._backend == "vlc":
            self._play_vlc_cli(url, referrer)
        elif self._backend == "ffplay":
            self._play_ffplay(url, referrer)
        else:
            raise RuntimeError("No hay reproductor. Instalá mpv, vlc, o `pip install python-vlc`")

    def _play_vlc_py(self, url, referrer, hwnd):
        import vlc
        opts = ["--no-video-title-show", "--no-osd", "--network-caching=2000"]
        if referrer:
            opts += [f"--http-referrer={referrer}"]
        self._vlc_instance = vlc.Instance(*opts)
        self._vlc_player = self._vlc_instance.media_player_new()
        media = self._vlc_instance.media_new(url)
        self._vlc_player.set_media(media)
        if hwnd:
            if sys.platform == "win32":
                self._vlc_player.set_hwnd(hwnd)
            else:
                self._vlc_player.set_xwindow(hwnd)
        self._vlc_player.play()

    def _play_mpv(self, url, referrer):
        cmd = ["mpv", "--no-terminal", "--cache=2048", url]
        if referrer:
            cmd.insert(-1, f"--http-header-fields=Referer: {referrer}")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _play_vlc_cli(self, url, referrer):
        cmd = ["vlc", "--intf", "qt", "--no-video-title-show", "--network-caching=2000", url]
        if referrer:
            cmd.insert(-1, f"--http-referrer={referrer}")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _play_ffplay(self, url, referrer):
        cmd = ["ffplay", "-nodisp", "-loglevel", "quiet", url]
        if referrer:
            cmd.insert(-1, f"-headers")
            cmd.insert(-1, f"Referer: {referrer}\r\n")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self._vlc_player:
            try: self._vlc_player.stop()
            except: pass
            try: self._vlc_player.release()
            except: pass
            self._vlc_player = None
            self._vlc_instance = None
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except:
                self._proc.kill()
            self._proc = None

    def close(self):
        self.stop()

# ─── GUI (tkinter) ────────────────────────────────────────────────────
def launch_gui():
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("Falta tkinter. En Ubuntu: sudo apt install python3-tk")
        sys.exit(1)

    root = tk.Tk()
    root.title("TV Argentina")
    root.geometry("800x550")
    root.minsize(640, 400)

    player = Player()
    status = tk.StringVar(value="Listo")
    status_label = ttk.Label(root, textvariable=status, relief=tk.SUNKEN, anchor=tk.W, padding=(6,2))
    status_label.pack(fill=tk.X, side=tk.BOTTOM)

    # Canvas para VLC embebido
    vf = ttk.Frame(root, relief=tk.SUNKEN, borderwidth=2)
    vf.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0,6))
    canvas = tk.Canvas(vf, bg="black", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    # Botones
    top = ttk.Frame(root, padding=6)
    top.pack(fill=tk.X)
    bf = ttk.Frame(top)
    bf.pack()

    def switch(ch_id):
        ch = CHANNELS[ch_id]
        status.set(f"Conectando a {ch['name']}...")
        root.update()
        try:
            url, referrer = resolve_stream(ch_id)
            player.play(url, referrer, canvas.winfo_id())
            status.set(f"▶ {ch['name']}  ({player.backend})")
        except Exception as e:
            status.set(f"✗ {ch['name']}: {e}")
            messagebox.showerror("Error", str(e))

    for ch_id, ch in CHANNELS.items():
        btn = ttk.Button(bf, text=ch["name"], command=lambda c=ch_id: switch(c))
        btn.pack(side=tk.LEFT, padx=3, pady=4)

    ttk.Button(bf, text="⏹ Detener", command=player.stop).pack(side=tk.LEFT, padx=10)
    ttk.Label(top, text=f"Backend: {player.backend}", foreground="gray").pack(side=tk.RIGHT, padx=6)

    root.protocol("WM_DELETE_WINDOW", lambda: (player.close(), root.destroy()))
    root.after(500, lambda: switch("trece"))
    root.mainloop()

# ─── Entry ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--print-urls" in sys.argv:
        targets = [a for a in sys.argv[1:] if a in CHANNELS] or list(CHANNELS)
        for ch_id in targets:
            try:
                url, _ = resolve_stream(ch_id)
                print(f"{CHANNELS[ch_id]['name']}: {url}")
            except Exception as e:
                print(f"{ch_id}: ERROR {e}")
        sys.exit(0)
    launch_gui()
