import os
import re
import csv
import json
import time
import uuid
import glob
import base64
import shutil
import struct
import socket
import logging
import platform
import datetime
import threading
import subprocess
import webbrowser
import urllib.parse
import urllib.request
import http.server
import webview
import requests

# pystray es opcional: si no está instalado, la bandeja del sistema se
# desactiva sola sin romper el resto del launcher (ver TrayManager).
#
# Por defecto forzamos el backend "xorg" (X11 puro, vía python-xlib, que ya
# viene como dependencia de pystray) en vez del backend "appindicator"/GTK
# que pystray elige solo en la mayoría de distros. El backend GTK corre su
# propio main loop de GLib, y pywebview levanta el suyo: ambos compitiendo
# por el mismo contexto termina en "g_application_run() cannot acquire the
# default main context because it is already acquired by another thread"
# (y el proceso se cae al arrancar). El backend xorg no toca GLib para nada,
# así que no hay con qué chocar.
# Si ya definiste PYSTRAY_BACKEND vos mismo (por ejemplo para forzar
# "appindicator" en un entorno donde sí lo necesitás), se respeta tu valor.
os.environ.setdefault("PYSTRAY_BACKEND", "xorg")
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

from paths import ASSETS_DIR, DATA_DIR

# BASE_DIR queda como alias de DATA_DIR: todo lo que antes se guardaba "al
# lado del script" ahora se guarda en una carpeta de usuario escribible y
# estable (necesario para poder empaquetar como AppImage, que monta el
# código en una ruta de solo lectura que además cambia en cada ejecución).
BASE_DIR = DATA_DIR
CONFIG_FILE = os.path.join(BASE_DIR, "backstage_apps.json")
WINE_CONFIG_FILE = os.path.join(BASE_DIR, "backstage_wine_config.json")
THEMES_REGISTRY_FILE = os.path.join(BASE_DIR, "backstage_themes.json")
LOG_FILE = os.path.join(BASE_DIR, "backstage_errors.log")

# FPStation: overlay de rendimiento en tiempo real (FPS/CPU/RAM/batería) que
# se abre en una ventana propia, siempre-encima, mientras el launcher tiene
# una app abierta. Config propia (qué métricas mostrar, posición, intervalo)
# y una carpeta donde MangoHud vuelca su log de FPS por sesión, que FPStation
# lee en vivo mientras la app está corriendo.
FPSTATION_FILE = os.path.join(BASE_DIR, "backstage_fpstation.json")
FPSTATION_LOGS_DIR = os.path.join(BASE_DIR, "fpstation_logs")
os.makedirs(FPSTATION_LOGS_DIR, exist_ok=True)

# ui/: el código fuente (index.html/app.js/style.css "de fábrica") vive
# empaquetado en ASSETS_DIR y puede ser de solo lectura. Lo que el webview
# realmente carga es una copia en DATA_DIR/ui, que sí es escribible (el
# editor de temas necesita poder pisar style.css, por ejemplo).
UI_SRC_DIR = os.path.join(ASSETS_DIR, "ui")
UI_DIR = os.path.join(BASE_DIR, "ui")
UI_INDEX = os.path.join(UI_DIR, "index.html")
STYLE_CSS_PATH = os.path.join(UI_DIR, "style.css")
STYLE_DEFAULT_BACKUP = os.path.join(BASE_DIR, "backstage_style_default.css")
STYLE_LAST_BACKUP = os.path.join(BASE_DIR, "backstage_style_backup.css")
DISCORD_CONFIG_FILE = os.path.join(BASE_DIR, "backstage_discord_config.json")
DISCORD_SESSION_FILE = os.path.join(BASE_DIR, "backstage_discord_session.json")
DISCORD_REDIRECT_PORT = 17983
DISCORD_REDIRECT_URI = f"http://localhost:{DISCORD_REDIRECT_PORT}/callback"
COVERS_DIR = os.path.join(BASE_DIR, "covers")
os.makedirs(COVERS_DIR, exist_ok=True)

APP_VERSION = "1.1.0"
UPDATE_CHECK_URL = "https://api.github.com/repos/kivppy/sakura-launcher/releases/latest"
PREFS_FILE = os.path.join(BASE_DIR, "backstage_prefs.json")


def _sync_ui_assets():
    """Copia index.html y app.js (código) del bundle a DATA_DIR/ui en cada
    arranque, así siempre corre la versión empaquetada. style.css se
    siembra una sola vez, la primera vez que se abre la app, para no
    pisar un tema que el usuario ya haya personalizado."""
    os.makedirs(UI_DIR, exist_ok=True)
    for fname in ("index.html", "app.js", "fpstation.html"):
        src = os.path.join(UI_SRC_DIR, fname)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(UI_DIR, fname))
    dst_css = os.path.join(UI_DIR, "style.css")
    if not os.path.exists(dst_css):
        src_css = os.path.join(UI_SRC_DIR, "style.css")
        if os.path.exists(src_css):
            shutil.copyfile(src_css, dst_css)


_sync_ui_assets()

# Logger propio (NO usar logging.basicConfig / root logger): pywebview y GTK
# escriben sus propios warnings/errores internos al root logger, y terminaban
# mezclados en nuestro log dando la falsa impresión de errores de la app.
logger = logging.getLogger("backstage")
logger.setLevel(logging.INFO)
logger.propagate = False
_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_handler)

# Tipos de app soportados y como se lanzan
APP_TYPES = {
    "exe": "Windows (.exe vía Wine)",
    "flatpak": "Flatpak (Application ID)",
    "appimage": "AppImage",
    "native": "Binario nativo Linux",
    "script": "Script (.sh)",
    "jar": "Java (.jar)",
}


WIN_VERSIONS = {
    "win11": "Windows 11",
    "win10": "Windows 10",
    "win81": "Windows 8.1",
    "win7": "Windows 7",
    "winxp": "Windows XP",
    "win98": "Windows 98",
    "winme": "Windows Me",
    "win95": "Windows 95",
}

RUNNERS = {
    "system_wine": {
        "label": "Wine del sistema",
        "desc": "Usa el Wine instalado en el sistema (opción por defecto)",
    },
    "custom_wine": {
        "label": "Wine personalizado",
        "desc": "Usa un binario de Wine específico (Staging, builds a medida, versiones viejas)",
    },
    "dosbox": {
        "label": "DOSBox",
        "desc": "Emulador de MS-DOS, para juegos previos a Windows 95",
    },
}

# Presets de compatibilidad pensados para juegos retro/viejos que Wine no
# corre bien con su configuración por defecto. Cada preset define qué motor
# usar, a qué versión de Windows debe hacerse pasar el prefix, y qué
# componentes extra de winetricks conviene instalar.
COMPAT_PRESETS = {
    "none": {
        "label": "Sin preset",
        "desc": "Wine estándar, sin ajustes especiales",
        "runner": "system_wine",
        "win_version": None,
        "winetricks": [],
    },
    "win9x_rts": {
        "label": "Clásicos de Windows 95/98/Me",
        "desc": "Age of Empires, Age of Kings, StarCraft, Diablo, Half-Life viejo y similares",
        "runner": "system_wine",
        "win_version": "win98",
        "winetricks": ["directx9", "d3dx9_36"],
    },
    "winxp_early2000": {
        "label": "Juegos de inicios de los 2000 (WinXP)",
        "desc": "Juegos pensados para Windows XP, DirectX 9 y Visual C++ 6",
        "runner": "system_wine",
        "win_version": "winxp",
        "winetricks": ["vcrun6", "directx9"],
    },
    "dos_classic": {
        "label": "Juegos MS-DOS",
        "desc": "Juegos anteriores a Windows 95: se ejecutan con DOSBox en vez de Wine",
        "runner": "dosbox",
        "win_version": None,
        "winetricks": [],
    },
    "custom_wine_manual": {
        "label": "Wine personalizado",
        "desc": "Elegí manualmente un binario de Wine específico para esta app (Staging, build a medida, etc.)",
        "runner": "custom_wine",
        "win_version": None,
        "winetricks": [],
    },
}


def default_wineprefix():
    return os.environ.get("WINEPREFIX", os.path.expanduser("~/.wine"))


WINE_CONFIG_DEFAULTS = {
    "esync": True,
    "fsync": False,
    "debug_off": True,
    "win_version": "win10",
}


def load_wine_config():
    cfg = dict(WINE_CONFIG_DEFAULTS)
    if os.path.exists(WINE_CONFIG_FILE):
        try:
            with open(WINE_CONFIG_FILE, "r") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_wine_config(cfg):
    with open(WINE_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)


# ---------- FPStation ----------
FPSTATION_DEFAULTS = {
    "enabled": False,          # sistema activado/desactivado (toggle simple del rail)
    "refresh_ms": 1000,        # cada cuánto se refresca el overlay
    "position": "top-right",  # top-left | top-right | bottom-left | bottom-right
    "metrics": {
        "fps": True,
        "cpu": True,
        "ram": True,
        "battery": True,
        "disk": False,
        "net": False,
    },
}


def load_fpstation_config():
    cfg = json.loads(json.dumps(FPSTATION_DEFAULTS))  # copia profunda simple
    if os.path.exists(FPSTATION_FILE):
        try:
            with open(FPSTATION_FILE, "r") as f:
                data = json.load(f)
            cfg.update({k: v for k, v in data.items() if k != "metrics"})
            if isinstance(data.get("metrics"), dict):
                cfg["metrics"].update(data["metrics"])
        except Exception:
            pass
    return cfg


def save_fpstation_config(cfg):
    with open(FPSTATION_FILE, "w") as f:
        json.dump(cfg, f, indent=4)


def _read_latest_mangohud_fps(log_dir):
    """Lee las últimas líneas del CSV que MangoHud va escribiendo en vivo
    (autostart_log=1) y devuelve el FPS más reciente. A diferencia del
    reporte final de una sesión, acá solo nos importa el último valor:
    FPStation lo vuelve a pedir cada 'refresh_ms' mientras la app corre."""
    try:
        if not os.path.isdir(log_dir):
            return None
        candidates = glob.glob(os.path.join(log_dir, "*.csv"))
        if not candidates:
            return None
        candidates.sort(key=os.path.getmtime, reverse=True)
        with open(candidates[0], "r", newline="", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            # La primera línea del CSV de MangoHud es metadata del sistema
            # (os,cpu,gpu,ram,...), no el header de columnas: hay que buscar
            # la línea real que arranca con "fps" y arrancar el reader ahí.
            header_idx = next(
                (i for i, line in enumerate(lines) if line.strip().lower().startswith("fps")), None
            )
            if header_idx is None:
                return None
            reader = csv.DictReader(lines[header_idx:])
            if not reader.fieldnames:
                return None
            last_fps = None
            for row in reader:
                for key, val in row.items():
                    if key and key.strip().lower() == "fps" and val not in (None, ""):
                        try:
                            last_fps = float(val)
                        except (TypeError, ValueError):
                            pass
            return round(last_fps, 1) if last_fps is not None else None
    except Exception:
        return None


def _read_cpu_percent(prev_sample=None, interval=None):
    """CPU global del sistema leída de /proc/stat, sin dependencias externas.
    Si se le pasa una muestra previa (totales acumulados), calcula el % de
    uso entre ambas lecturas; si no, hace un sleep corto para poder medir
    (solo se usa así la primera vez)."""
    def _read_totals():
        with open("/proc/stat", "r") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        return total, idle

    try:
        if prev_sample is None:
            t1, i1 = _read_totals()
            time.sleep(interval or 0.15)
            t2, i2 = _read_totals()
        else:
            t1, i1 = prev_sample
            t2, i2 = _read_totals()

        dt = t2 - t1
        di = i2 - i1
        percent = 0.0 if dt <= 0 else max(0.0, min(100.0, (1 - di / dt) * 100))
        return round(percent, 1), (t2, i2)
    except Exception:
        return None, prev_sample


def _read_ram_info():
    """RAM usada/total en MB, leída de /proc/meminfo (stdlib puro)."""
    try:
        info = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0])  # kB
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
        used_kb = max(0, total_kb - avail_kb)
        return {
            "used_mb": round(used_kb / 1024, 0),
            "total_mb": round(total_kb / 1024, 0),
            "percent": round((used_kb / total_kb) * 100, 1) if total_kb else None,
        }
    except Exception:
        return None


def _read_battery_info():
    """Batería vía /sys/class/power_supply (stdlib puro). Devuelve None en
    equipos de escritorio sin batería, en vez de inventar un valor."""
    try:
        base = "/sys/class/power_supply"
        if not os.path.isdir(base):
            return None
        for entry in os.listdir(base):
            type_path = os.path.join(base, entry, "type")
            if not os.path.exists(type_path):
                continue
            with open(type_path, "r") as f:
                if f.read().strip() != "Battery":
                    continue
            cap_path = os.path.join(base, entry, "capacity")
            status_path = os.path.join(base, entry, "status")
            if not os.path.exists(cap_path):
                continue
            with open(cap_path, "r") as f:
                capacity = int(f.read().strip())
            status = "Unknown"
            if os.path.exists(status_path):
                with open(status_path, "r") as f:
                    status = f.read().strip()
            return {"percent": capacity, "charging": status.lower() == "charging", "status": status}
        return None
    except Exception:
        return None


def _read_disk_info():
    """Uso del disco donde vive BASE_DIR (stdlib puro, sin psutil)."""
    try:
        st = os.statvfs(BASE_DIR)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {
            "used_gb": round(used / (1024 ** 3), 1),
            "total_gb": round(total / (1024 ** 3), 1),
            "percent": round((used / total) * 100, 1) if total else None,
        }
    except Exception:
        return None


def _read_net_info(prev_sample=None):
    """Velocidad de red aproximada (KB/s), leyendo /proc/net/dev entre dos
    muestras. Suma todas las interfaces salvo loopback."""
    try:
        rx_total = tx_total = 0
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()[2:]
        for line in lines:
            iface, _, rest = line.partition(":")
            if iface.strip() == "lo":
                continue
            fields = rest.split()
            if len(fields) < 9:
                continue
            rx_total += int(fields[0])
            tx_total += int(fields[8])

        now = time.time()
        sample = (now, rx_total, tx_total)
        if not prev_sample:
            return {"down_kbps": 0.0, "up_kbps": 0.0}, sample

        prev_time, prev_rx, prev_tx = prev_sample
        dt = max(0.001, now - prev_time)
        down_kbps = round(max(0, rx_total - prev_rx) / dt / 1024, 1)
        up_kbps = round(max(0, tx_total - prev_tx) / dt / 1024, 1)
        return {"down_kbps": down_kbps, "up_kbps": up_kbps}, sample
    except Exception:
        return None, prev_sample


PREFS_DEFAULTS = {
    "sort_by": "name",       # name | favorite | playtime | last_played | added
    "close_to_tray": True,
    "last_update_check": 0,
    "dismissed_version": "",
}


def load_prefs():
    prefs = dict(PREFS_DEFAULTS)
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                prefs.update(json.load(f))
        except Exception:
            pass
    return prefs


def save_prefs(prefs):
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=4)


def ensure_default_css_backup():
    """Guarda una copia del CSS original la primera vez que corre la app,
    para poder restaurarlo aunque el usuario edite/rompa el style.css."""
    if not os.path.exists(STYLE_DEFAULT_BACKUP) and os.path.exists(STYLE_CSS_PATH):
        try:
            shutil.copyfile(STYLE_CSS_PATH, STYLE_DEFAULT_BACKUP)
        except Exception:
            pass


def load_themes_registry():
    if os.path.exists(THEMES_REGISTRY_FILE):
        try:
            with open(THEMES_REGISTRY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_themes_registry(themes):
    with open(THEMES_REGISTRY_FILE, "w") as f:
        json.dump(themes, f, indent=4)


def load_discord_config():
    if os.path.exists(DISCORD_CONFIG_FILE):
        try:
            with open(DISCORD_CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"client_id": "", "client_secret": ""}


def save_discord_config(cfg):
    with open(DISCORD_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)


def load_discord_session():
    if os.path.exists(DISCORD_SESSION_FILE):
        try:
            with open(DISCORD_SESSION_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_discord_session(session):
    with open(DISCORD_SESSION_FILE, "w") as f:
        json.dump(session, f, indent=4)


def discord_avatar_url(data):
    if data.get("avatar"):
        ext = "gif" if str(data["avatar"]).startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{data['id']}/{data['avatar']}.{ext}?size=128"
    disc = int(data.get("discriminator") or 0)
    idx = disc % 5 if disc else (int(data["id"]) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


class DiscordRPC:
    """Cliente mínimo del protocolo IPC de Discord para Rich Presence,
    implementado directamente sobre el socket Unix (sin pypresence ni
    ninguna otra dependencia nueva). Solo hace 'Jugando a <juego>' +
    tiempo jugado; no implementa chat ni funciones sociales.

    Protocolo: cada mensaje son 8 bytes de header (opcode uint32 LE +
    largo uint32 LE) seguidos del payload JSON en UTF-8."""

    OP_HANDSHAKE = 0
    OP_FRAME = 1

    def __init__(self, client_id):
        self.client_id = client_id
        self.sock = None
        self.connected = False
        self.lock = threading.Lock()

    def _candidate_paths(self):
        base = (os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR")
                or os.environ.get("TMP") or "/tmp")
        for i in range(10):
            yield os.path.join(base, f"discord-ipc-{i}")
            # Flatpak/snap a veces exponen el socket en subcarpetas propias
            yield os.path.join(base, "app", "com.discordapp.Discord", f"discord-ipc-{i}")
            yield os.path.join(base, "snap.discord", f"discord-ipc-{i}")

    def connect(self):
        with self.lock:
            if self.connected:
                return True
            for path in self._candidate_paths():
                if not os.path.exists(path):
                    continue
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(3)
                    s.connect(path)
                    self.sock = s
                    self._send(self.OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
                    self._recv()  # respuesta READY, no necesitamos el contenido
                    self.connected = True
                    return True
                except Exception:
                    try:
                        s.close()
                    except Exception:
                        pass
            return False

    def _send(self, opcode, payload):
        data = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", opcode, len(data))
        self.sock.sendall(header + data)

    def _recv(self):
        header = self.sock.recv(8)
        if len(header) < 8:
            return None
        _, length = struct.unpack("<II", header)
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None

    def set_activity(self, game_name, start_ts=None):
        if not self.connected and not self.connect():
            return False
        try:
            payload = {
                "cmd": "SET_ACTIVITY",
                "args": {
                    "pid": os.getpid(),
                    "activity": {
                        "details": f"Jugando a {game_name}",
                        "state": "En Sakura Launcher",
                        "timestamps": {"start": int(start_ts or time.time())},
                        "assets": {"large_image": "sakura_logo", "large_text": "Sakura Launcher"},
                    },
                },
                "nonce": uuid.uuid4().hex,
            }
            with self.lock:
                self._send(self.OP_FRAME, payload)
                self._recv()
            return True
        except Exception:
            self.connected = False
            return False

    def clear_activity(self):
        if not self.connected:
            return
        try:
            payload = {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": None},
                "nonce": uuid.uuid4().hex,
            }
            with self.lock:
                self._send(self.OP_FRAME, payload)
                self._recv()
        except Exception:
            pass

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.connected = False


class _DiscordCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Servidor local mínimo que sólo existe para capturar el
    ?code=... que Discord manda al redirect_uri tras el login."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        self.server.oauth_code = (qs.get("code") or [None])[0]
        self.server.oauth_error = (qs.get("error") or [None])[0]
        ok = bool(self.server.oauth_code)
        title = "Conectado con Discord" if ok else "No se pudo conectar"
        body = (
            "<html><body style='background:#12080d;color:#fbeaf1;"
            "font-family:sans-serif;text-align:center;padding-top:80px'>"
            f"<h2>{title}</h2><p>Pod\u00e9s cerrar esta pesta\u00f1a y volver a Sakura Launcher.</p>"
            "</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):
        pass


NOISE_WORDS = {
    "setup", "install", "installer", "launcher", "final", "release",
    "win64", "win32", "x64", "x86", "portable", "standalone", "app",
}


def guess_name(exe_path, app_type):
    if not exe_path:
        return ""

    if app_type == "flatpak":
        # org.videolan.VLC -> VLC ; com.valvesoftware.Steam -> Steam
        last = exe_path.strip().split(".")[-1]
        if last.isupper():
            return last  # sigla tipo VLC, no separar letra por letra
        return re.sub(r"(?<!^)(?=[A-Z])", " ", last).strip() or exe_path

    base = os.path.basename(exe_path.strip().rstrip("/"))
    base = os.path.splitext(base)[0]

    # separadores comunes -> espacios
    base = re.sub(r"[._\-]+", " ", base)
    # cortar sufijos de arquitectura/plataforma pegados al final (x64, win64, vk, etc.)
    base = re.sub(r"(?i)\s*(x64|x86|win64|win32|64bit|32bit|dx11|dx12)\s*$", "", base).strip()
    # separar minúscula->Mayúscula (palabraPegada -> palabra Pegada), pero solo
    # cuando hay minúsculas de por medio para no destrozar siglas (VLC, RPG)
    base = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)

    words = [w for w in base.split() if w.lower() not in NOISE_WORDS]
    if not words:
        words = [w for w in base.split()]
    if not words:
        return ""

    def fmt(w):
        return w if w.isupper() and len(w) > 1 else w.capitalize()

    return " ".join(fmt(w) for w in words).strip()


# ---------- búsqueda de portadas en internet ----------
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
HTTP_TIMEOUT = 6


def _search_steam_covers(query, limit=3):
    """Busca en la tienda de Steam (API pública, sin key) y devuelve
    imágenes de cabecera (header/capsule) de los juegos que matchean."""
    results = []
    try:
        resp = requests.get(
            "https://store.steampowered.com/api/storesearch/",
            params={"term": query, "l": "spanish", "cc": "US"},
            headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", [])[:limit]:
            img = item.get("tiny_image") or ""
            if not img:
                continue
            # tiny_image es un capsule chico; el header art de mejor calidad
            # vive en una ruta predecible dentro de cdn.akamai.steamstatic.com
            appid = item.get("id")
            if appid:
                img = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"
            results.append({
                "url": img,
                "label": item.get("name", query),
                "source": "Steam",
            })
    except Exception as e:
        logger.warning(f"Búsqueda de portada en Steam falló para '{query}': {e}")
    return results


def _search_bing_covers(query, limit=3):
    """Respaldo genérico: scrapea resultados de Bing Images. Útil para apps
    que no están en Steam (flatpaks, herramientas, juegos viejos, etc.)."""
    results = []
    try:
        q = urllib.parse.quote(f"{query} banner cover art")
        resp = requests.get(
            "https://www.bing.com/images/search",
            params={"q": f"{query} banner cover art", "form": "HDRSC2"},
            headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        # Bing incrusta metadata de cada resultado en atributos m="{...}" con
        # una clave murl (media url) apuntando a la imagen original.
        murls = re.findall(r'murl&quot;:&quot;(.*?)&quot;', resp.text)
        if not murls:
            murls = re.findall(r'"murl":"(.*?)"', resp.text)
        for url in murls:
            url = url.encode().decode("unicode_escape")
            if url.startswith("http") and url not in [r["url"] for r in results]:
                results.append({"url": url, "label": query, "source": "Web"})
            if len(results) >= limit:
                break
    except Exception as e:
        logger.warning(f"Búsqueda de portada en Bing falló para '{query}': {e}")
    return results


def search_cover_images(name, limit=3):
    name = (name or "").strip()
    if not name:
        return []

    candidates = _search_steam_covers(name, limit=limit)
    if len(candidates) < limit:
        remaining = limit - len(candidates)
        candidates += _search_bing_covers(name, limit=remaining)

    # dedup por URL, preservando orden, y recortamos al límite pedido
    seen = set()
    unique = []
    for c in candidates:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        unique.append(c)
        if len(unique) >= limit:
            break
    return unique


# ---------- diagnóstico del entorno ----------
def _diag_status(ok, optional=False):
    if ok:
        return "ok"
    return "optional" if optional else "missing"


def _check_vulkan():
    """Vulkan: si existe vulkaninfo lo corremos (rápido, --summary); si no,
    buscamos los ICD json que son la señal real de que hay drivers Vulkan."""
    if shutil.which("vulkaninfo"):
        try:
            out = subprocess.run(["vulkaninfo", "--summary"], capture_output=True,
                                  text=True, timeout=6)
            if out.returncode == 0 and "deviceName" in (out.stdout or ""):
                m = re.search(r"deviceName\s*=\s*(.+)", out.stdout)
                return True, (m.group(1).strip() if m else "Detectado")
        except Exception:
            pass
    icd_dirs = ["/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d"]
    for d in icd_dirs:
        if os.path.isdir(d) and glob.glob(os.path.join(d, "*.json")):
            return True, "ICD detectado (sin vulkaninfo)"
    return False, ""


def _check_dxvk():
    """DXVK no es un binario del sistema: vive como DLLs dentro de cada
    prefix de Wine. Buscamos rastros en el prefix por defecto y en los
    prefixes conocidos por las apps."""
    prefixes = {default_wineprefix()}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                for a in json.load(f):
                    if a.get("wineprefix"):
                        prefixes.add(a["wineprefix"])
        except Exception:
            pass
    for prefix in prefixes:
        d3d11 = os.path.join(prefix, "drive_c", "windows", "system32", "d3d11.dll")
        if os.path.exists(d3d11):
            try:
                with open(d3d11, "rb") as f:
                    if b"dxvk" in f.read(4096).lower():
                        return True, os.path.basename(prefix) or prefix
            except Exception:
                pass
    return False, ""


def _check_vkd3d():
    if shutil.which("setup_vkd3d_proton") or shutil.which("vkd3d-compiler"):
        return True, "Detectado en el sistema"
    lib_paths = ["/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib"]
    for lp in lib_paths:
        if glob.glob(os.path.join(lp, "libvkd3d*")):
            return True, "Librería detectada"
    return False, ""


def _check_gpu_driver():
    """Heurística simple con lspci: busca la línea de VGA/3D y detecta
    fabricante para saber si conviene mesa (AMD/Intel) o nvidia."""
    if not shutil.which("lspci"):
        return None, "No se pudo detectar (falta 'lspci')"
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
        lines = [l for l in out.stdout.splitlines() if "VGA" in l or "3D controller" in l]
        if not lines:
            return None, "No se detectó GPU"
        line = lines[0]
        if "NVIDIA" in line.upper():
            ok = shutil.which("nvidia-smi") is not None
            return ok, line.split(":")[-1].strip()
        if "AMD" in line.upper() or "ATI" in line.upper() or "INTEL" in line.upper():
            return True, line.split(":")[-1].strip()  # mesa suele venir con el sistema
        return None, line.split(":")[-1].strip()
    except Exception:
        return None, "No se pudo detectar"


def check_diagnostics():
    """Chequea todo el entorno relevante para correr juegos de Windows/Linux
    y devuelve un listado con estado (ok/optional/missing) para la ventana
    de Diagnóstico, con acciones de reparación cuando aplica."""
    items = []

    wine_ok = _is_bin_installed("wine")
    items.append({
        "id": "wine", "label": "Wine", "detail": "Motor de compatibilidad",
        "status": _diag_status(wine_ok), "repairable": not wine_ok,
    })

    wt_ok = _is_bin_installed("winetricks")
    items.append({
        "id": "winetricks", "label": "Winetricks", "detail": "Instalador de componentes",
        "status": _diag_status(wt_ok), "repairable": not wt_ok,
    })

    vk_ok, vk_detail = _check_vulkan()
    items.append({
        "id": "vulkan", "label": "Vulkan", "detail": vk_detail or "Requerido para DXVK/VKD3D",
        "status": _diag_status(vk_ok), "repairable": not vk_ok,
    })

    dxvk_ok, dxvk_detail = _check_dxvk()
    items.append({
        "id": "dxvk", "label": "DXVK", "detail": dxvk_detail or "Traduce Direct3D 9/10/11 a Vulkan",
        "status": _diag_status(dxvk_ok, optional=True), "repairable": False,
    })

    vkd3d_ok, vkd3d_detail = _check_vkd3d()
    items.append({
        "id": "vkd3d", "label": "VKD3D", "detail": vkd3d_detail or "Traduce Direct3D 12 a Vulkan",
        "status": _diag_status(vkd3d_ok, optional=True), "repairable": False,
    })

    drv_ok, drv_detail = _check_gpu_driver()
    items.append({
        "id": "drivers", "label": "Drivers de GPU", "detail": drv_detail or "No detectado",
        "status": "ok" if drv_ok else ("missing" if drv_ok is False else "optional"),
        "repairable": False,
    })

    gs_ok = _is_bin_installed("gamescope")
    items.append({
        "id": "gamescope", "label": "Gamescope", "detail": "Compositor para juegos (opcional)",
        "status": _diag_status(gs_ok, optional=True), "repairable": not gs_ok,
    })

    gm_ok = _is_bin_installed("gamemoderun")
    items.append({
        "id": "gamemode", "label": "Gamemode", "detail": "Optimiza CPU/GPU al jugar (opcional)",
        "status": _diag_status(gm_ok, optional=True), "repairable": not gm_ok,
    })

    mh_ok = _is_bin_installed("mangohud")
    items.append({
        "id": "mangohud", "label": "MangoHud",
        "detail": "Necesario para que FPStation muestre FPS en el overlay (opcional)",
        "status": _diag_status(mh_ok, optional=True), "repairable": not mh_ok,
    })

    return {"items": items}


DIAGNOSTIC_PACKAGES = {
    "wine": {"apt": "wine", "dnf": "wine", "pacman": "wine", "zypper": "wine"},
    "winetricks": {"apt": "winetricks", "dnf": "winetricks", "pacman": "winetricks", "zypper": "winetricks"},
    "vulkan": {"apt": "mesa-vulkan-drivers", "dnf": "vulkan-loader", "pacman": "vulkan-icd-loader", "zypper": "libvulkan1"},
    "gamescope": {"apt": "gamescope", "dnf": "gamescope", "pacman": "gamescope", "zypper": "gamescope"},
    "gamemode": {"apt": "gamemode", "dnf": "gamemode", "pacman": "gamemode", "zypper": "gamemode"},
    "mangohud": {"apt": "mangohud", "dnf": "mangohud", "pacman": "mangohud", "zypper": "mangohud"},
}


# ---------- sistema de requisitos de Wine ----------
def detect_pkg_manager():
    """Detecta la familia de gestor de paquetes a partir de /etc/os-release
    y de qué binarios existen en el sistema. Devuelve un dict con la info
    necesaria para armar comandos de chequeo/instalación."""
    os_id = ""
    os_like = ""
    try:
        with open("/etc/os-release", "r") as f:
            content = f.read()
        m = re.search(r'^ID="?([^"\n]+)"?', content, re.MULTILINE)
        if m:
            os_id = m.group(1).lower()
        m = re.search(r'^ID_LIKE="?([^"\n]+)"?', content, re.MULTILINE)
        if m:
            os_like = m.group(1).lower()
    except Exception:
        pass

    family_by_id = {
        "ubuntu": "apt", "debian": "apt", "linuxmint": "apt", "pop": "apt",
        "fedora": "dnf", "rhel": "dnf", "centos": "dnf", "rocky": "dnf", "alma": "dnf",
        "arch": "pacman", "manjaro": "pacman", "endeavouros": "pacman",
        "opensuse": "zypper", "opensuse-leap": "zypper", "opensuse-tumbleweed": "zypper",
    }
    family = family_by_id.get(os_id)
    if not family:
        for token in os_like.split():
            if token in family_by_id:
                family = family_by_id[token]
                break

    if not family:
        # último recurso: mirar qué gestor existe en el PATH
        for fam, bin_name in (("apt", "apt-get"), ("dnf", "dnf"),
                               ("pacman", "pacman"), ("zypper", "zypper")):
            if shutil.which(bin_name):
                family = fam
                break

    return {"family": family, "os_id": os_id or "desconocida"}


# paquete de sistema por gestor, cuando el nombre difiere entre distros
WINE_REQUIREMENTS = [
    {
        "id": "wine",
        "label": "Wine",
        "desc": "Motor de compatibilidad para ejecutar apps de Windows",
        "check_bin": "wine",
        "packages": {"apt": "wine", "dnf": "wine", "pacman": "wine", "zypper": "wine"},
    },
    {
        "id": "winetricks",
        "label": "Winetricks",
        "desc": "Instalador de componentes extra (fuentes, runtimes, DLLs)",
        "check_bin": "winetricks",
        "packages": {"apt": "winetricks", "dnf": "winetricks", "pacman": "winetricks", "zypper": "winetricks"},
    },
    {
        "id": "cabextract",
        "label": "Cabextract",
        "desc": "Extrae archivos .cab, usado por winetricks para instalar fuentes",
        "check_bin": "cabextract",
        "packages": {"apt": "cabextract", "dnf": "cabextract", "pacman": "cabextract", "zypper": "cabextract"},
    },
    {
        "id": "unzip",
        "label": "Unzip",
        "desc": "Descomprime archivos .zip requeridos por varios componentes",
        "check_bin": "unzip",
        "packages": {"apt": "unzip", "dnf": "unzip", "pacman": "unzip", "zypper": "unzip"},
    },
    {
        "id": "p7zip",
        "label": "7-Zip",
        "desc": "Descomprime archivos .7z usados por instaladores de Windows",
        "check_bin": "7z",
        "packages": {"apt": "p7zip-full", "dnf": "p7zip", "pacman": "p7zip", "zypper": "7zip"},
    },
    {
        "id": "winbind",
        "label": "Winbind",
        "desc": "Resolución de nombres NTLM, requerida por algunos juegos online",
        "check_bin": "wbinfo",
        "packages": {"apt": "winbind", "dnf": "samba-winbind", "pacman": "samba", "zypper": "samba-winbind"},
    },
]


def _is_bin_installed(bin_name):
    return shutil.which(bin_name) is not None


def check_wine_requirements():
    """Chequea cada requisito y devuelve su estado, junto con la info de
    la distro detectada."""
    pkg_info = detect_pkg_manager()
    family = pkg_info["family"]

    items = []
    for req in WINE_REQUIREMENTS:
        installed = _is_bin_installed(req["check_bin"])
        package = req["packages"].get(family, "") if family else ""
        items.append({
            "id": req["id"],
            "label": req["label"],
            "desc": req["desc"],
            "installed": installed,
            "package": package,
            "installable": bool(package),
        })

    return {
        "os_id": pkg_info["os_id"],
        "family": family or "",
        "supported": family is not None,
        "requirements": items,
    }


DOSBOX_REQUIREMENT = {
    "id": "dosbox",
    "label": "DOSBox",
    "desc": "Emulador de MS-DOS necesario para correr este tipo de juego",
    "check_bin": "dosbox",
    "packages": {"apt": "dosbox", "dnf": "dosbox", "pacman": "dosbox", "zypper": "dosbox"},
}


def check_compat_preset(preset_id):
    """Chequea qué hace falta en el sistema para que el preset elegido
    funcione: binarios necesarios y, para presets basados en Wine, los
    componentes de winetricks que se instalarán en el prefix de la app."""
    preset = COMPAT_PRESETS.get(preset_id)
    if not preset:
        return {"error": "Preset no reconocido"}

    pkg_info = detect_pkg_manager()
    family = pkg_info["family"]
    items = []

    if preset["runner"] == "dosbox":
        installed = _is_bin_installed(DOSBOX_REQUIREMENT["check_bin"])
        package = DOSBOX_REQUIREMENT["packages"].get(family, "") if family else ""
        items.append({
            "id": "dosbox", "label": DOSBOX_REQUIREMENT["label"], "desc": DOSBOX_REQUIREMENT["desc"],
            "installed": installed, "package": package, "installable": bool(package), "status": "ok" if installed else "pending",
        })
    elif preset["runner"] == "custom_wine":
        items.append({
            "id": "custom_wine_path", "label": "Binario de Wine",
            "desc": "Elegí la ruta al ejecutable 'wine' que querés usar para esta app",
            "installed": None, "package": "", "installable": False, "status": "info",
        })
    else:
        base = check_wine_requirements()
        for r in base["requirements"]:
            if r["id"] in ("wine", "winetricks", "cabextract"):
                items.append({**r, "status": "ok" if r["installed"] else "pending"})
        if preset["winetricks"]:
            items.append({
                "id": "winetricks_verbs",
                "label": "Componentes: " + ", ".join(preset["winetricks"]),
                "desc": "Se instalan automáticamente en el prefix de esta app al agregarla",
                "installed": None, "package": "", "installable": False, "status": "info",
            })
        if preset.get("win_version"):
            items.append({
                "id": "win_version",
                "label": f"Versión de Windows: {WIN_VERSIONS.get(preset['win_version'], preset['win_version'])}",
                "desc": "Se configura automáticamente en el prefix de esta app al agregarla",
                "installed": None, "package": "", "installable": False, "status": "info",
            })

    return {
        "preset": preset_id,
        "runner": preset["runner"],
        "supported": family is not None or preset["runner"] != "system_wine",
        "items": items,
    }


# ---------- detección automática de apps instaladas ----------
def _vdf_parse_libraryfolders(path):
    """Parser mínimo de libraryfolders.vdf (formato Valve KeyValues) para
    sacar las rutas de bibliotecas de Steam sin depender de vdf/paquetes
    externos. Solo nos interesan las líneas '"path"  "..."'."""
    paths = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = re.search(r'"path"\s*"([^"]+)"', line)
                if m:
                    paths.append(m.group(1))
    except Exception:
        pass
    return paths


def _vdf_parse_appmanifest(path):
    """Extrae appid y nombre de un appmanifest_*.acf."""
    data = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        m = re.search(r'"appid"\s*"(\d+)"', content)
        if m:
            data["appid"] = m.group(1)
        m = re.search(r'"name"\s*"([^"]+)"', content)
        if m:
            data["name"] = m.group(1)
    except Exception:
        pass
    return data


def scan_steam_apps():
    """Escanea las bibliotecas de Steam (nativo y Flatpak) buscando
    appmanifest_*.acf y arma sugerencias lanzables vía 'steam://rungameid/'."""
    found = []
    steam_roots = [
        os.path.expanduser("~/.steam/steam"),
        os.path.expanduser("~/.local/share/Steam"),
        os.path.expanduser("~/.var/app/com.valvesoftware.Steam/data/Steam"),
    ]
    seen_appids = set()
    for root in steam_roots:
        base_lib = os.path.join(root, "steamapps")
        if not os.path.isdir(base_lib):
            continue
        libraries = {base_lib}
        vdf_path = os.path.join(base_lib, "libraryfolders.vdf")
        for extra in _vdf_parse_libraryfolders(vdf_path):
            libraries.add(os.path.join(extra, "steamapps"))

        for lib in libraries:
            if not os.path.isdir(lib):
                continue
            for manifest in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
                info = _vdf_parse_appmanifest(manifest)
                appid = info.get("appid")
                name = info.get("name")
                if not appid or not name or appid in seen_appids:
                    continue
                seen_appids.add(appid)
                found.append({
                    "source": "steam",
                    "name": name,
                    "exe": f"steam://rungameid/{appid}",
                    "type": "flatpak" if False else "native",
                    "launch_type": "steam",
                    "suggested_category": "Steam",
                })
    return found


def scan_lutris_apps():
    """Lee la base sqlite de Lutris (pga.db) si existe, para sugerir sus
    juegos ya configurados. Se ejecutan luego vía 'lutris:rungameid/<slug>'."""
    found = []
    db_path = os.path.expanduser("~/.local/share/lutris/pga.db")
    if not os.path.exists(db_path) or not shutil.which("lutris"):
        return found
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT slug, name FROM games WHERE installed = 1")
        for slug, name in cur.fetchall():
            if not slug or not name:
                continue
            found.append({
                "source": "lutris",
                "name": name,
                "exe": f"lutris:rungameid/{slug}",
                "type": "native",
                "launch_type": "lutris",
                "suggested_category": "Lutris",
            })
        conn.close()
    except Exception as e:
        logger.warning(f"No se pudo leer la base de Lutris: {e}")
    return found


def scan_wine_prefix_apps(prefix):
    """Busca .exe de nivel de usuario dentro de un prefix de Wine (Archivos
    de programa, Escritorio del usuario), filtrando instaladores/uninstall
    conocidos que no tiene sentido agregar como app."""
    found = []
    if not prefix or not os.path.isdir(prefix):
        return found

    ignore_patterns = ("unins", "setup", "vcredist", "dxsetup", "directx", "redist")
    search_dirs = [
        os.path.join(prefix, "drive_c", "Program Files"),
        os.path.join(prefix, "drive_c", "Program Files (x86)"),
        os.path.join(prefix, "drive_c", "users"),
    ]
    for base in search_dirs:
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            # no bajar más de 4 niveles para no tardar una eternidad en prefixes grandes
            depth = root[len(base):].count(os.sep)
            if depth >= 4:
                dirs[:] = []
                continue
            for fname in files:
                if not fname.lower().endswith(".exe"):
                    continue
                low = fname.lower()
                if any(p in low for p in ignore_patterns):
                    continue
                full = os.path.join(root, fname)
                found.append({
                    "source": "wine",
                    "name": guess_name(full, "exe"),
                    "exe": full,
                    "type": "exe",
                    "launch_type": "exe",
                    "suggested_category": "General",
                    "wineprefix": prefix,
                })
    return found


def run_auto_detection(extra_prefixes=None):
    """Corre todos los escaneos y devuelve sugerencias, excluyendo lo que
    ya está agregado (comparando por ruta/ID de lanzamiento)."""
    results = []
    results += scan_steam_apps()
    results += scan_lutris_apps()

    prefixes = set(extra_prefixes or [])
    prefixes.add(default_wineprefix())
    for prefix in prefixes:
        results += scan_wine_prefix_apps(prefix)

    # límite razonable por escaneo de Wine para no inundar la UI de instaladores
    return results


PKG_INSTALL_CMDS = {
    "apt": lambda pkgs: [["pkexec", "apt-get", "update", "-y"],
                          ["pkexec", "apt-get", "install", "-y", *pkgs]],
    "dnf": lambda pkgs: [["pkexec", "dnf", "install", "-y", *pkgs]],
    "pacman": lambda pkgs: [["pkexec", "pacman", "-Sy", "--noconfirm", *pkgs]],
    "zypper": lambda pkgs: [["pkexec", "zypper", "--non-interactive", "install", *pkgs]],
}


def load_apps():
    if not os.path.exists(CONFIG_FILE):
        return []
    with open(CONFIG_FILE, "r") as f:
        apps = json.load(f)
    changed = False
    for app in apps:
        if "id" not in app:
            app["id"] = uuid.uuid4().hex[:8]
            changed = True
        app.setdefault("type", "exe")
        app.setdefault("category", "General")
        app.setdefault("favorite", False)
        app.setdefault("playtime", 0)
        app.setdefault("missing_dlls", [])
        app.setdefault("wineprefix", "")
        app.setdefault("runner", "system_wine")
        app.setdefault("custom_wine_path", "")
        app.setdefault("compat_preset", "none")
        app.setdefault("last_played", None)
        if "added_at" not in app:
            # apps que ya existían antes de esta versión no tienen fecha real
            # de alta: les asignamos "ahora" una sola vez para no romper el
            # ordenamiento por fecha de agregado.
            app["added_at"] = datetime.datetime.now().isoformat()
            changed = True
    if changed:
        save_apps(apps)
    return apps


def save_apps(apps):
    with open(CONFIG_FILE, "w") as f:
        json.dump(apps, f, indent=4)


def build_launch_cmd(app, env):
    t = app.get("type", "exe")
    target = app["exe"]

    if target.startswith("steam://"):
        return ["xdg-open", target]
    if target.startswith("lutris:"):
        return ["xdg-open", target] if shutil.which("xdg-open") else ["lutris", target]

    if t == "exe":
        runner = app.get("runner", "system_wine")
        if runner == "dosbox":
            return ["dosbox", target, "-exit"]
        if runner == "custom_wine" and app.get("custom_wine_path"):
            return [app["custom_wine_path"], target]
        return ["wine", target]
    if t == "flatpak":
        return ["flatpak", "run", target]
    if t == "appimage":
        try:
            os.chmod(target, 0o755)
        except Exception:
            pass
        return [target]
    if t == "native":
        try:
            os.chmod(target, 0o755)
        except Exception:
            pass
        return [target]
    if t == "script":
        return ["bash", target]
    if t == "jar":
        return ["java", "-jar", target]
    return ["wine", target]


def target_exists(app):
    if app.get("type") == "flatpak":
        return True  # se valida por ID, no por path
    exe = app.get("exe", "")
    if exe.startswith("steam://") or exe.startswith("lutris:"):
        return True  # se validan al vuelo por Steam/Lutris, no por path local
    return os.path.exists(exe)


DLL_PATTERN = re.compile(r"([A-Za-z0-9_\-]+\.dll)", re.IGNORECASE)
SO_PATTERN = re.compile(r"([A-Za-z0-9_.\-]+\.so(?:\.\d+)*)")


class Api:
    # Client ID de una app de Discord propia del launcher, solo para poder
    # mostrar Rich Presence sin pedirle al usuario que configure nada
    # (a diferencia del login OAuth, que sí requiere client_id/secret propios).
    RPC_CLIENT_ID = "1234567890123456789"

    def __init__(self):
        self.apps = load_apps()
        self.window = None
        self.running_ids = set()  # apps corriendo ahora mismo (en memoria, no se persiste)
        self.running_procs = {}   # app_id -> {"proc": Popen, "prefix": str|None, "type": str}
        self.wine_setup_running = False
        self.discord_rpc = DiscordRPC(self.RPC_CLIENT_ID)
        self.tray = None
        # FPStation: ventana overlay propia, separada de la principal
        self.fpstation_window = None
        self.fpstation_lock = threading.Lock()
        self.fpstation_stop_event = None
        self.fpstation_current_app = None
        ensure_default_css_backup()

    # ---------- estado ----------
    def get_state(self):
        categories = sorted({a.get("category", "General") for a in self.apps})
        prefs = load_prefs()
        return {
            "apps": self.apps,
            "categories": categories,
            "app_types": APP_TYPES,
            "running": list(self.running_ids),
            "runners": {rid: {"label": r["label"], "desc": r["desc"]} for rid, r in RUNNERS.items()},
            "compat_presets": {
                pid: {"label": p["label"], "desc": p["desc"], "runner": p["runner"]}
                for pid, p in COMPAT_PRESETS.items()
            },
            "sort_by": prefs.get("sort_by", "name"),
            "app_version": APP_VERSION,
        }

    # ---------- ordenamiento / preferencias de biblioteca ----------
    SORT_OPTIONS = ("name", "favorite", "playtime", "last_played", "added")

    def set_sort(self, sort_by):
        if sort_by not in self.SORT_OPTIONS:
            return {"error": "Criterio de orden no reconocido"}
        prefs = load_prefs()
        prefs["sort_by"] = sort_by
        save_prefs(prefs)
        return {"status": "saved", "sort_by": sort_by}

    # ---------- crud ----------
    def add_app(self, data):
        if not data.get("name") or not data.get("exe"):
            return {"error": "Faltan datos (nombre y ruta/ID)"}
        preset_id = data.get("compat_preset") or "none"
        preset = COMPAT_PRESETS.get(preset_id, COMPAT_PRESETS["none"])
        runner = data.get("runner") or preset["runner"]
        app = {
            "id": uuid.uuid4().hex[:8],
            "name": data["name"],
            "exe": data["exe"],
            "image": data.get("image", ""),
            "type": data.get("type", "exe"),
            "category": data.get("category") or "General",
            "wineprefix": data.get("wineprefix", ""),
            "runner": runner,
            "custom_wine_path": data.get("custom_wine_path", ""),
            "compat_preset": preset_id,
            "favorite": False,
            "playtime": 0,
            "missing_dlls": [],
            "last_played": None,
            "added_at": datetime.datetime.now().isoformat(),
        }
        self.apps.append(app)
        save_apps(self.apps)
        if app["type"] == "exe" and preset_id != "none" and preset["runner"] == "system_wine":
            threading.Thread(target=self._apply_compat_preset, args=(app,), daemon=True).start()
        return self.get_state()

    def _apply_compat_preset(self, app):
        preset = COMPAT_PRESETS.get(app.get("compat_preset"))
        if not preset or preset["runner"] != "system_wine":
            return
        prefix = app.get("wineprefix") or default_wineprefix()
        env = os.environ.copy()
        env["WINEPREFIX"] = prefix
        try:
            os.makedirs(prefix, exist_ok=True)
            subprocess.run(["wineboot", "-u"], env=env, check=True)
            if preset.get("win_version"):
                subprocess.run(["winecfg", "/v", preset["win_version"]], env=env, check=True)
            if preset["winetricks"] and shutil.which("winetricks"):
                subprocess.run(["winetricks", "-q", *preset["winetricks"]], env=env, check=False)
            logger.info(f"[{app['name']}] Preset de compatibilidad '{preset['label']}' aplicado en {prefix}")
        except Exception as e:
            logger.error(f"[{app['name']}] Error aplicando preset de compatibilidad: {e}")

    # ---------- detección automática ----------
    def scan_installed_apps(self):
        try:
            known_paths = {a["exe"] for a in self.apps}
            known_names = {a["name"].strip().lower() for a in self.apps}
            extra_prefixes = {a["wineprefix"] for a in self.apps if a.get("wineprefix")}

            suggestions = run_auto_detection(extra_prefixes)
            filtered = [
                s for s in suggestions
                if s["exe"] not in known_paths and s["name"].strip().lower() not in known_names
            ]
            return {"suggestions": filtered}
        except Exception as e:
            logger.error(f"Error en detección automática: {e}")
            return {"suggestions": [], "error": str(e)}

    def add_detected_apps(self, items):
        """Agrega en lote una lista de sugerencias elegidas por el usuario
        en el diálogo de detección automática."""
        added = []
        for item in items or []:
            data = {
                "name": item.get("name"),
                "exe": item.get("exe"),
                "type": item.get("type", "native"),
                "category": item.get("suggested_category") or "General",
                "wineprefix": item.get("wineprefix", ""),
            }
            if not data["name"] or not data["exe"]:
                continue
            app = {
                "id": uuid.uuid4().hex[:8],
                "name": data["name"],
                "exe": data["exe"],
                "image": "",
                "type": data["type"],
                "category": data["category"],
                "wineprefix": data["wineprefix"],
                "runner": "system_wine",
                "custom_wine_path": "",
                "compat_preset": "none",
                "favorite": False,
                "playtime": 0,
                "missing_dlls": [],
                "last_played": None,
                "added_at": datetime.datetime.now().isoformat(),
            }
            self.apps.append(app)
            added.append(app["name"])
        if added:
            save_apps(self.apps)
            logger.info(f"Detección automática: agregadas {len(added)} apps ({', '.join(added)})")
        return self.get_state()

    def delete_app(self, app_id):
        self.apps = [a for a in self.apps if a["id"] != app_id]
        save_apps(self.apps)
        return self.get_state()

    def toggle_favorite(self, app_id):
        for a in self.apps:
            if a["id"] == app_id:
                a["favorite"] = not a.get("favorite", False)
        save_apps(self.apps)
        return self.get_state()

    def _find(self, app_id):
        for a in self.apps:
            if a["id"] == app_id:
                return a
        return None

    def _missing_runner(self, app):
        if app.get("type") == "exe":
            runner = app.get("runner", "system_wine")
            if runner == "dosbox":
                return None if shutil.which("dosbox") else "dosbox"
            if runner == "custom_wine":
                path = app.get("custom_wine_path")
                ok = bool(path) and os.path.exists(path) and os.access(path, os.X_OK)
                return None if ok else "wine personalizado"
            return None if shutil.which("wine") else "wine"
        runner = {"flatpak": "flatpak", "jar": "java"}.get(app.get("type", "exe"))
        if runner and not shutil.which(runner):
            return runner
        return None

    def _notify(self):
        """Avisa al frontend que hay un cambio de estado (no espera al próximo polling)."""
        try:
            if self.window:
                self.window.evaluate_js("window.dispatchEvent(new CustomEvent('backstage-update'))")
        except Exception:
            pass

    def _notify_error(self, message):
        try:
            if self.window:
                safe = json.dumps(message)
                self.window.evaluate_js(
                    f"window.dispatchEvent(new CustomEvent('backstage-error', {{detail:{safe}}}))"
                )
        except Exception:
            pass

    # ---------- notificaciones del sistema de requisitos Wine (sakura) ----------
    def _sakura_log(self, message, level="info"):
        logger.info(f"[Wine setup] {message}")
        try:
            if self.window:
                payload = json.dumps({"message": message, "level": level})
                self.window.evaluate_js(
                    f"window.dispatchEvent(new CustomEvent('sakura-log', {{detail:{payload}}}))"
                )
        except Exception:
            pass

    def _sakura_progress(self, percent, req_id=None, status=None):
        try:
            if self.window:
                payload = json.dumps({"percent": percent, "id": req_id, "status": status})
                self.window.evaluate_js(
                    f"window.dispatchEvent(new CustomEvent('sakura-progress', {{detail:{payload}}}))"
                )
        except Exception:
            pass

    def _sakura_done(self, ok, message=""):
        try:
            if self.window:
                payload = json.dumps({"ok": ok, "message": message})
                self.window.evaluate_js(
                    f"window.dispatchEvent(new CustomEvent('sakura-done', {{detail:{payload}}}))"
                )
        except Exception:
            pass

    # ---------- lanzar ----------
    def launch(self, app_id):
        app = self._find(app_id)
        if not app:
            return {"error": "App no encontrada"}
        if app_id in self.running_ids:
            return {"error": "Ya está corriendo"}
        if not target_exists(app):
            logger.error(f"[{app['name']}] Ruta/objetivo no encontrado: {app['exe']}")
            return {"error": "La ruta o el ID no existe"}

        missing_runner = self._missing_runner(app)
        if missing_runner:
            msg = f"No se encontró '{missing_runner}' instalado (no está en el PATH)."
            logger.error(f"[{app['name']}] {msg}")
            return {"error": msg}

        self.running_ids.add(app_id)
        threading.Thread(target=self._run_and_monitor, args=(app,), daemon=True).start()
        return {"status": "launching"}

    def _run_and_monitor(self, app):
        env = os.environ.copy()
        runner = app.get("runner", "system_wine")
        is_wine_runner = app.get("type") == "exe" and runner in ("system_wine", "custom_wine")
        if is_wine_runner:
            prefix = app.get("wineprefix") or default_wineprefix()
            env["WINEPREFIX"] = prefix
            wcfg = load_wine_config()
            if wcfg.get("esync"):
                env["WINEESYNC"] = "1"
            if wcfg.get("fsync"):
                env["WINEFSYNC"] = "1"
            if wcfg.get("debug_off"):
                env["WINEDEBUG"] = "-all"

        start = time.time()
        missing = set()
        cmd = build_launch_cmd(app, env)

        # ---------- FPStation ----------
        # Sistema activable/desactivable con un solo toggle (rail): si está
        # encendido, mientras el launcher tiene la app abierta se levanta una
        # ventanita propia, siempre-encima, decorada igual que el resto del
        # launcher, con métricas en tiempo real (FPS/CPU/RAM/batería/etc).
        # MangoHud corre en segundo plano (sin overlay propio, no_display)
        # solo para darle el dato de FPS a FPStation; el overlay visible es
        # siempre el nuestro.
        fp_cfg = load_fpstation_config()
        fp_active = (
            fp_cfg.get("enabled", False)
            and app.get("type") in ("exe", "native", "flatpak", "appimage")
            and cmd and cmd[0] != "xdg-open"
        )
        fp_log_dir = None

        if fp_active and fp_cfg.get("metrics", {}).get("fps") and shutil.which("mangohud"):
            fp_log_dir = os.path.join(FPSTATION_LOGS_DIR, app["id"])
            try:
                if os.path.isdir(fp_log_dir):
                    shutil.rmtree(fp_log_dir)
                os.makedirs(fp_log_dir, exist_ok=True)
                env["MANGOHUD"] = "1"
                env["MANGOHUD_CONFIG"] = f"autostart_log=1,output_folder={fp_log_dir},no_display"
                cmd = ["mangohud", *cmd]
            except Exception as e:
                logger.warning(f"[{app['name']}] FPStation: no se pudo preparar el log de MangoHud: {e}")
                fp_log_dir = None

        if fp_active:
            threading.Thread(target=self._fpstation_open, args=(app, fp_log_dir), daemon=True).start()

        try:
            threading.Thread(target=self.discord_rpc.set_activity, args=(app["name"], start), daemon=True).start()
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True,
                                     start_new_session=True)
            self.running_procs[app["id"]] = {
                "proc": proc,
                "prefix": env.get("WINEPREFIX") if is_wine_runner else None,
                "type": app.get("type", "exe"),
                "runner": runner,
            }
            for line in proc.stderr:
                if "err" in line.lower() or "error" in line.lower():
                    logger.error(f"[{app['name']}] {line.strip()}")
                    m = DLL_PATTERN.search(line) or SO_PATTERN.search(line)
                    if m and any(k in line.lower() for k in
                                  ("not found", "cannot find", "could not be loaded",
                                   "failed to load", "cannot open shared object")):
                        missing.add(m.group(1))
            proc.wait()
        except Exception as e:
            logger.error(f"[{app['name']}] Excepción al lanzar: {e}")
            self._notify_error(f"No se pudo abrir {app['name']}: {e}")
            return
        finally:
            self.running_ids.discard(app["id"])
            self.running_procs.pop(app["id"], None)
            self.discord_rpc.clear_activity()
            if fp_active:
                self._fpstation_close()
            self._notify()

        elapsed = time.time() - start
        app["playtime"] = app.get("playtime", 0) + elapsed
        app["last_played"] = datetime.datetime.now().isoformat()
        app["missing_dlls"] = sorted(missing) if missing else []
        if missing:
            logger.warning(f"[{app['name']}] Dependencias faltantes: {', '.join(missing)}")
        save_apps(self.apps)

    # ---------- FPStation: ventana overlay ----------
    def _fpstation_open(self, app, log_dir):
        """Abre (o reutiliza) la ventana de FPStation y arranca el hilo que
        la va alimentando con métricas en vivo mientras la app está abierta."""
        try:
            with self.fpstation_lock:
                if self.fpstation_window is None:
                    fp_cfg = load_fpstation_config()
                    self.fpstation_window = webview.create_window(
                        "FPStation",
                        os.path.join(UI_DIR, "fpstation.html"),
                        js_api=self,
                        width=230,
                        height=160,
                        x=None, y=None,
                        frameless=True,
                        easy_drag=True,
                        on_top=True,
                        transparent=True,
                        background_color="#000000",
                        resizable=True,
                        min_size=(140, 90),
                    )
                    self.fpstation_window.events.closed += self._on_fpstation_closed

                self.fpstation_stop_event = threading.Event()
                self.fpstation_current_app = app["id"]
                threading.Thread(
                    target=self._fpstation_feed_loop, args=(app, log_dir, self.fpstation_stop_event), daemon=True,
                ).start()
        except Exception as e:
            logger.warning(f"FPStation: no se pudo abrir la ventana de overlay: {e}")

    def _on_fpstation_closed(self):
        # si el usuario cierra la ventanita a mano, no rompemos nada: el
        # próximo lanzamiento simplemente la vuelve a crear.
        self.fpstation_window = None

    def _fpstation_feed_loop(self, app, log_dir, stop_event):
        cpu_sample = None
        net_sample = None
        # primera lectura de CPU sin bloquear demasiado el arranque de la app
        cpu_percent, cpu_sample = _read_cpu_percent(None, interval=0.2)

        while not stop_event.is_set():
            fp_cfg = load_fpstation_config()
            if not fp_cfg.get("enabled", False):
                break
            metrics = fp_cfg.get("metrics", {})
            refresh_ms = max(250, int(fp_cfg.get("refresh_ms", 1000)))

            payload = {"app_name": app["name"], "position": fp_cfg.get("position", "top-right")}

            if metrics.get("fps") and log_dir:
                payload["fps"] = _read_latest_mangohud_fps(log_dir)
            if metrics.get("cpu"):
                cpu_percent, cpu_sample = _read_cpu_percent(cpu_sample)
                payload["cpu_percent"] = cpu_percent
            if metrics.get("ram"):
                payload["ram"] = _read_ram_info()
            if metrics.get("battery"):
                payload["battery"] = _read_battery_info()
            if metrics.get("disk"):
                payload["disk"] = _read_disk_info()
            if metrics.get("net"):
                net_data, net_sample = _read_net_info(net_sample)
                payload["net"] = net_data

            try:
                if self.fpstation_window:
                    data_json = json.dumps(payload)
                    self.fpstation_window.evaluate_js(
                        f"window.dispatchEvent(new CustomEvent('fpstation-update', {{detail:{data_json}}}))"
                    )
            except Exception:
                pass  # la ventana pudo haberse cerrado justo ahora; el próximo tick lo maneja

            stop_event.wait(refresh_ms / 1000)

    def _fpstation_close(self):
        """Se llama al cerrarse la app: para el feed y oculta la ventanita
        (no la destruye, así el próximo lanzamiento es instantáneo)."""
        try:
            if self.fpstation_stop_event:
                self.fpstation_stop_event.set()
            self.fpstation_current_app = None
            if self.fpstation_window:
                self.fpstation_window.hide()
        except Exception:
            pass

    # ---------- FPStation: API ----------
    def fpstation_resize(self, width, height):
        """Llamado desde el JS del overlay (frameless: el resize nativo del
        SO no aplica, así que lo hacemos manual arrastrando un handle)."""
        try:
            if self.fpstation_window:
                self.fpstation_window.resize(max(140, int(width)), max(90, int(height)))
        except Exception:
            pass

    def get_fpstation_state(self):
        cfg = load_fpstation_config()
        return {
            "enabled": cfg.get("enabled", False),
            "mangohud_available": bool(shutil.which("mangohud")),
            "refresh_ms": cfg.get("refresh_ms", 1000),
            "position": cfg.get("position", "top-right"),
            "metrics": cfg.get("metrics", {}),
        }

    def toggle_fpstation(self):
        """El toggle simple pedido: un solo click prende/apaga todo el
        sistema. Si se apaga con la app corriendo, la ventana se oculta en
        el siguiente tick del feed (que revisa 'enabled' en cada vuelta)."""
        cfg = load_fpstation_config()
        cfg["enabled"] = not cfg.get("enabled", False)
        save_fpstation_config(cfg)
        logger.info(f"FPStation {'activado' if cfg['enabled'] else 'desactivado'}")
        if not cfg["enabled"]:
            self._fpstation_close()
        return {"enabled": cfg["enabled"]}

    def save_fpstation_settings(self, settings):
        """Guarda qué métricas se muestran, posición e intervalo. No toca
        'enabled': eso solo lo cambia el toggle simple del rail."""
        cfg = load_fpstation_config()
        if "position" in settings and settings["position"] in (
            "top-left", "top-right", "bottom-left", "bottom-right",
        ):
            cfg["position"] = settings["position"]
        if "refresh_ms" in settings:
            try:
                cfg["refresh_ms"] = max(250, min(5000, int(settings["refresh_ms"])))
            except (TypeError, ValueError):
                pass
        if isinstance(settings.get("metrics"), dict):
            for key in FPSTATION_DEFAULTS["metrics"]:
                if key in settings["metrics"]:
                    cfg["metrics"][key] = bool(settings["metrics"][key])
        save_fpstation_config(cfg)
        logger.info("FPStation: configuración de overlay actualizada")
        return {"status": "saved", "config": cfg}

    # ---------- cerrar ----------
    def close_app(self, app_id):
        app = self._find(app_id)
        if not app:
            return {"error": "App no encontrada"}
        if app_id not in self.running_ids:
            return {"error": "Esa app no está corriendo"}

        info = self.running_procs.get(app_id)

        try:
            if info and info.get("type") == "exe" and info.get("runner", "system_wine") in ("system_wine", "custom_wine"):
                # Wine lanza el .exe real bajo wineserver, no como hijo directo
                # del proceso "wine": matar solo el Popen no alcanza. wineserver -k
                # mata todos los procesos asociados a ese prefix específico.
                prefix = info.get("prefix") or default_wineprefix()
                env = os.environ.copy()
                env["WINEPREFIX"] = prefix
                subprocess.run(["wineserver", "-k"], env=env)
            elif info and info.get("proc"):
                proc = info["proc"]
                try:
                    os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM al grupo
                except Exception:
                    proc.terminate()
            logger.info(f"[{app['name']}] Cerrado manualmente por el usuario")
        except Exception as e:
            logger.error(f"[{app['name']}] Error al cerrar: {e}")
            return {"error": f"No se pudo cerrar: {e}"}

        return {"status": "closing"}

    # ---------- reparar entorno (solo wine) ----------
    def repair(self, app_id):
        app = self._find(app_id)
        if not app:
            return {"error": "App no encontrada"}
        if app.get("type") != "exe":
            return {"error": "Reparar entorno solo aplica a apps Windows/Wine"}

        env = os.environ.copy()
        prefix = app.get("wineprefix") or default_wineprefix()
        env["WINEPREFIX"] = prefix

        def _repair():
            try:
                subprocess.run(["wineboot", "-u"], env=env, check=True)
                if shutil.which("winetricks"):
                    subprocess.run(["winetricks", "-q", "corefonts", "vcrun2019"], env=env)
                logger.info(f"[{app['name']}] Entorno reparado en {prefix}")
                app["missing_dlls"] = []
                save_apps(self.apps)
            except Exception as e:
                logger.error(f"[{app['name']}] Error al reparar: {e}")

        threading.Thread(target=_repair, daemon=True).start()
        return {"status": "repairing"}

    # ---------- diagnóstico del entorno ----------
    def run_diagnostics(self):
        return check_diagnostics()

    def repair_diagnostic_item(self, item_id):
        """Repara un ítem faltante del diagnóstico instalando su paquete
        de sistema correspondiente, con el mismo feedback visual (log +
        progreso) que el resto de instalaciones del launcher."""
        if self.wine_setup_running:
            return {"error": "Ya hay una instalación en curso"}
        package_map = DIAGNOSTIC_PACKAGES.get(item_id)
        if not package_map:
            return {"error": "Este componente no se puede reparar automáticamente"}

        pkg_info = detect_pkg_manager()
        family = pkg_info["family"]
        if not family:
            return {"error": f"Distro '{pkg_info['os_id']}' no reconocida: instalá el paquete manualmente"}
        package = package_map.get(family)
        if not package:
            return {"error": f"Sin paquete conocido para '{item_id}' en esta distro"}

        self.wine_setup_running = True
        threading.Thread(
            target=self._run_diagnostic_repair, args=(family, item_id, package), daemon=True,
        ).start()
        return {"status": "repairing"}

    def _run_diagnostic_repair(self, family, item_id, package):
        cmd_builder = PKG_INSTALL_CMDS.get(family)
        try:
            if not cmd_builder:
                self._sakura_log(f"Gestor de paquetes '{family}' sin soporte de instalación", "error")
                self._sakura_done(False, "Gestor de paquetes no soportado")
                return
            self._sakura_log(f"Instalando '{package}' para reparar {item_id}…")
            self._sakura_progress(5)
            for cmd in cmd_builder([package]):
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        self._sakura_log(line)
                proc.wait()
                if proc.returncode != 0:
                    self._sakura_log(f"El comando terminó con código {proc.returncode}", "error")
                    self._sakura_done(False, "La reparación falló, revisá el log")
                    return
            self._sakura_progress(100)
            self._sakura_done(True, f"'{item_id}' reparado correctamente")
        except FileNotFoundError:
            self._sakura_log("No se encontró 'pkexec' en el sistema", "error")
            self._sakura_done(False, "Falta pkexec para pedir permisos de instalación")
        except Exception as e:
            self._sakura_log(f"Excepción durante la reparación: {e}", "error")
            self._sakura_done(False, "Ocurrió un error inesperado")
        finally:
            self.wine_setup_running = False

    # ---------- preparar aplicación ----------
    def analyze_app_requirements(self, app_id):
        """Analiza una app ya agregada: qué requisitos le faltan según su
        tipo/runner/preset, para poder instalarlos automáticamente."""
        app = self._find(app_id)
        if not app:
            return {"error": "App no encontrada"}

        items = []
        if app.get("type") == "exe":
            runner = app.get("runner", "system_wine")
            if runner == "dosbox":
                installed = _is_bin_installed("dosbox")
                items.append({
                    "id": "dosbox", "label": "DOSBox", "installed": installed,
                    "package": DOSBOX_REQUIREMENT["packages"].get(detect_pkg_manager()["family"], ""),
                })
            elif runner == "custom_wine":
                path = app.get("custom_wine_path", "")
                ok = bool(path) and os.path.exists(path) and os.access(path, os.X_OK)
                items.append({"id": "custom_wine", "label": "Binario de Wine personalizado",
                               "installed": ok, "package": ""})
            else:
                base = check_wine_requirements()
                for r in base["requirements"]:
                    if r["id"] in ("wine", "winetricks", "cabextract"):
                        items.append({"id": r["id"], "label": r["label"], "installed": r["installed"],
                                       "package": r["package"]})
                preset = COMPAT_PRESETS.get(app.get("compat_preset", "none"))
                if preset and preset["winetricks"]:
                    items.append({
                        "id": "winetricks_verbs",
                        "label": "Componentes: " + ", ".join(preset["winetricks"]),
                        "installed": None, "package": "",
                    })
            if app.get("missing_dlls"):
                items.append({
                    "id": "missing_dlls",
                    "label": "Dependencias detectadas en la última corrida: " + ", ".join(app["missing_dlls"]),
                    "installed": False, "package": "",
                })
        elif app.get("type") == "flatpak":
            items.append({"id": "flatpak", "label": "Flatpak", "installed": _is_bin_installed("flatpak"), "package": "flatpak"})
        elif app.get("type") == "jar":
            items.append({"id": "java", "label": "Java", "installed": _is_bin_installed("java"), "package": "default-jre"})

        pending = [i for i in items if i.get("installed") is False]
        return {"app": app["name"], "items": items, "pending_count": len(pending)}

    def prepare_app(self, app_id):
        """Instala automáticamente lo que le falte a una app para poder
        correr, mostrando progreso por los mismos eventos sakura-*."""
        if self.wine_setup_running:
            return {"error": "Ya hay una instalación en curso"}
        app = self._find(app_id)
        if not app:
            return {"error": "App no encontrada"}

        self.wine_setup_running = True
        threading.Thread(target=self._run_prepare_app, args=(app,), daemon=True).start()
        return {"status": "preparing"}

    def _run_prepare_app(self, app):
        try:
            analysis = self.analyze_app_requirements(app["id"])
            pending = [i for i in analysis["items"] if i.get("installed") is False and i.get("package")]

            if not pending:
                self._sakura_log(f"'{app['name']}' ya tiene todo lo necesario para correr")
                self._sakura_progress(100)
                self._sakura_done(True, "No hacía falta instalar nada")
                return

            pkg_info = detect_pkg_manager()
            family = pkg_info["family"]
            cmd_builder = PKG_INSTALL_CMDS.get(family)
            if not family or not cmd_builder:
                self._sakura_log(f"Distro '{pkg_info['os_id']}' no soportada para instalar automáticamente", "error")
                self._sakura_done(False, "Instalá los requisitos manualmente")
                return

            packages = [i["package"] for i in pending]
            self._sakura_log(f"Preparando '{app['name']}': instalando {', '.join(packages)}…")
            self._sakura_progress(5)

            for step_i, cmd in enumerate(cmd_builder(packages)):
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        self._sakura_log(line)
                proc.wait()
                if proc.returncode != 0:
                    self._sakura_log(f"El comando terminó con código {proc.returncode}", "error")
                    self._sakura_done(False, "La preparación falló, revisá el log")
                    return
                self._sakura_progress(min(90, int((step_i + 1) / 2 * 90)))

            # aplica también los winetricks del preset, si tenía
            preset = COMPAT_PRESETS.get(app.get("compat_preset", "none"))
            if preset and preset.get("winetricks") and shutil.which("winetricks"):
                self._sakura_log("Instalando componentes de compatibilidad…")
                prefix = app.get("wineprefix") or default_wineprefix()
                env = os.environ.copy()
                env["WINEPREFIX"] = prefix
                subprocess.run(["winetricks", "-q", *preset["winetricks"]], env=env, check=False)

            self._sakura_progress(100)
            self._sakura_done(True, f"'{app['name']}' está lista para jugarse")
        except Exception as e:
            self._sakura_log(f"Excepción preparando la app: {e}", "error")
            self._sakura_done(False, "Ocurrió un error inesperado")
        finally:
            self.wine_setup_running = False

    # ---------- sistema de requisitos Wine (petalo sakura) ----------
    def check_wine_setup(self):
        return check_wine_requirements()

    def install_wine_requirements(self, req_ids):
        if self.wine_setup_running:
            return {"error": "Ya hay una instalación en curso"}

        state = check_wine_requirements()
        if not state["supported"]:
            return {"error": f"Distro '{state['os_id']}' no reconocida: instalá los paquetes manualmente"}

        wanted = [r for r in state["requirements"] if r["id"] in req_ids and not r["installed"]]
        if not wanted:
            return {"error": "No hay nada pendiente para instalar"}

        missing_pkg = [r["label"] for r in wanted if not r["installable"]]
        if missing_pkg:
            return {"error": f"Sin paquete conocido para: {', '.join(missing_pkg)}"}

        self.wine_setup_running = True
        threading.Thread(
            target=self._run_wine_install,
            args=(state["family"], wanted),
            daemon=True,
        ).start()
        return {"status": "installing"}

    def _run_wine_install(self, family, wanted):
        pkgs = [r["package"] for r in wanted]
        cmd_builder = PKG_INSTALL_CMDS.get(family)

        try:
            if not cmd_builder:
                self._sakura_log(f"Gestor de paquetes '{family}' sin soporte de instalación", "error")
                self._sakura_done(False, "Gestor de paquetes no soportado")
                return

            self._sakura_log(f"Pétalo posándose sobre {len(wanted)} requisito(s)…")
            self._sakura_progress(2)

            commands = cmd_builder(pkgs)
            total_steps = len(commands)

            for step_i, cmd in enumerate(commands):
                readable = " ".join(cmd[1:])  # sin el pkexec, para el log
                self._sakura_log(f"Ejecutando: {readable}")

                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    lower = line.lower()
                    if any(k in lower for k in ("unable to locate", "no candidate", "not found",
                                                  "no such file", "failed", "error")):
                        self._sakura_log(line, "error")
                    elif any(k in lower for k in ("setting up", "unpacking", "installing",
                                                    "instalando", "descargando", "resolving")):
                        self._sakura_log(line)

                proc.wait()
                if proc.returncode != 0:
                    self._sakura_log(f"El comando terminó con código {proc.returncode}", "error")
                    self._sakura_done(False, "La instalación falló, revisá el log")
                    return

                base_pct = int((step_i + 1) / total_steps * 90)
                self._sakura_progress(max(2, base_pct))

            # chequeo final: confirmar que ahora sí están instalados
            self._sakura_log("Confirmando que los pétalos florecieron…")
            final_state = check_wine_requirements()
            still_missing = [r["label"] for r in final_state["requirements"]
                              if r["id"] in [w["id"] for w in wanted] and not r["installed"]]

            if still_missing:
                self._sakura_log(f"Sin confirmar instalación de: {', '.join(still_missing)}", "warn")
                self._sakura_progress(95)
                self._sakura_done(False, f"No se pudo confirmar: {', '.join(still_missing)}")
            else:
                self._sakura_log("Todos los requisitos están listos")
                self._sakura_progress(100)
                self._sakura_done(True, "Wine está listo para usarse")

        except FileNotFoundError:
            self._sakura_log("No se encontró 'pkexec' en el sistema", "error")
            self._sakura_done(False, "Falta pkexec para pedir permisos de instalación")
        except Exception as e:
            self._sakura_log(f"Excepción durante la instalación: {e}", "error")
            self._sakura_done(False, "Ocurrió un error inesperado")
        finally:
            self.wine_setup_running = False

    # ---------- configurar / optimizar wine ----------
    def get_wine_config(self):
        cfg = load_wine_config()
        cfg["default_prefix"] = default_wineprefix()
        cfg["win_versions"] = WIN_VERSIONS
        prefixes = sorted({a["wineprefix"] for a in self.apps if a.get("wineprefix")})
        cfg["known_prefixes"] = prefixes
        return cfg

    def save_wine_config_settings(self, data):
        cfg = load_wine_config()
        for key in ("esync", "fsync", "debug_off"):
            if key in data:
                cfg[key] = bool(data[key])
        if data.get("win_version") in WIN_VERSIONS:
            cfg["win_version"] = data["win_version"]
        save_wine_config(cfg)
        logger.info("Configuración de Wine actualizada")
        return {"status": "saved"}

    def _wine_env_for(self, prefix):
        env = os.environ.copy()
        env["WINEPREFIX"] = prefix or default_wineprefix()
        return env

    def open_winecfg(self, prefix):
        try:
            os.makedirs(prefix or default_wineprefix(), exist_ok=True)
            subprocess.Popen(["winecfg"], env=self._wine_env_for(prefix))
            return {"status": "opened"}
        except Exception as e:
            logger.error(f"No se pudo abrir winecfg: {e}")
            return {"error": str(e)}

    def open_winetricks_gui(self, prefix):
        if not shutil.which("winetricks"):
            return {"error": "Winetricks no está instalado"}
        try:
            os.makedirs(prefix or default_wineprefix(), exist_ok=True)
            subprocess.Popen(["winetricks"], env=self._wine_env_for(prefix))
            return {"status": "opened"}
        except Exception as e:
            logger.error(f"No se pudo abrir Winetricks: {e}")
            return {"error": str(e)}

    def open_prefix_folder(self, prefix):
        path = prefix or default_wineprefix()
        try:
            os.makedirs(path, exist_ok=True)
            subprocess.Popen(["xdg-open", path])
            return {"status": "opened"}
        except Exception as e:
            logger.error(f"No se pudo abrir la carpeta del prefix: {e}")
            return {"error": str(e)}

    def reset_wine_prefix(self, prefix):
        env = self._wine_env_for(prefix)

        def _reset():
            try:
                subprocess.run(["wineboot", "-u"], env=env, check=True)
                logger.info(f"Prefix reiniciado: {env['WINEPREFIX']}")
                self._notify()
            except Exception as e:
                logger.error(f"Error al reiniciar el prefix: {e}")

        threading.Thread(target=_reset, daemon=True).start()
        return {"status": "resetting"}

    def clean_wine_cache(self, prefix):
        env = self._wine_env_for(prefix)
        p = env["WINEPREFIX"]

        def _clean():
            try:
                users_dir = os.path.join(p, "drive_c", "users")
                if os.path.isdir(users_dir):
                    for user in os.listdir(users_dir):
                        temp_dir = os.path.join(users_dir, user, "Temp")
                        if os.path.isdir(temp_dir):
                            shutil.rmtree(temp_dir, ignore_errors=True)
                cache_dir = os.path.expanduser("~/.cache/wine")
                if os.path.isdir(cache_dir):
                    shutil.rmtree(cache_dir, ignore_errors=True)
                logger.info(f"Caché temporal de Wine limpiada para {p}")
            except Exception as e:
                logger.error(f"Error al limpiar caché de Wine: {e}")

        threading.Thread(target=_clean, daemon=True).start()
        return {"status": "cleaning"}

    def set_windows_version(self, prefix, version):
        if version not in WIN_VERSIONS:
            return {"error": "Versión no reconocida"}
        env = self._wine_env_for(prefix)

        def _apply():
            try:
                subprocess.run(["winecfg", "/v", version], env=env, check=True)
                logger.info(f"Versión de Windows del prefix {env['WINEPREFIX']} cambiada a {version}")
            except Exception as e:
                logger.error(f"Error al cambiar la versión de Windows: {e}")

        threading.Thread(target=_apply, daemon=True).start()
        return {"status": "applying"}

    # ---------- compatibilidad retro ----------
    def get_compat_presets(self):
        return {
            pid: {"label": p["label"], "desc": p["desc"], "runner": p["runner"]}
            for pid, p in COMPAT_PRESETS.items()
        }

    def check_compat_preset(self, preset_id):
        return check_compat_preset(preset_id)

    def validate_custom_wine(self, path):
        if not path or not os.path.exists(path):
            return {"error": "La ruta no existe"}
        if not os.access(path, os.X_OK):
            return {"error": "El archivo no es ejecutable"}
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            version = (out.stdout or out.stderr or "").strip()
            return {"status": "ok", "version": version or "Wine personalizado"}
        except Exception as e:
            return {"error": f"No se pudo ejecutar: {e}"}

    # ---------- temas / css ----------
    def get_theme_state(self):
        ensure_default_css_backup()
        css = ""
        try:
            with open(STYLE_CSS_PATH, "r", encoding="utf-8") as f:
                css = f.read()
        except Exception as e:
            logger.error(f"No se pudo leer style.css: {e}")
        return {
            "css": css,
            "themes": load_themes_registry(),
            "has_backup": os.path.exists(STYLE_LAST_BACKUP),
            "has_default": os.path.exists(STYLE_DEFAULT_BACKUP),
        }

    def _backup_current_css(self):
        try:
            if os.path.exists(STYLE_CSS_PATH):
                shutil.copyfile(STYLE_CSS_PATH, STYLE_LAST_BACKUP)
        except Exception as e:
            logger.error(f"No se pudo respaldar style.css: {e}")

    def save_css(self, css_text):
        try:
            self._backup_current_css()
            with open(STYLE_CSS_PATH, "w", encoding="utf-8") as f:
                f.write(css_text)
            logger.info("style.css actualizado desde el editor de temas")
            return {"status": "saved"}
        except Exception as e:
            logger.error(f"No se pudo guardar el CSS: {e}")
            return {"error": str(e)}

    def restore_default_css(self):
        ensure_default_css_backup()
        if not os.path.exists(STYLE_DEFAULT_BACKUP):
            return {"error": "No hay un CSS predeterminado guardado"}
        try:
            self._backup_current_css()
            shutil.copyfile(STYLE_DEFAULT_BACKUP, STYLE_CSS_PATH)
            with open(STYLE_CSS_PATH, "r", encoding="utf-8") as f:
                css = f.read()
            logger.info("CSS restaurado al predeterminado de fábrica")
            return {"status": "restored", "css": css}
        except Exception as e:
            logger.error(f"No se pudo restaurar el CSS predeterminado: {e}")
            return {"error": str(e)}

    def restore_backup_css(self):
        if not os.path.exists(STYLE_LAST_BACKUP):
            return {"error": "No hay un respaldo previo"}
        try:
            # antes de restaurar guardamos el estado actual, por si justo
            # el respaldo tampoco sirve y quiere volver para adelante
            broken_copy = STYLE_CSS_PATH + ".broken"
            if os.path.exists(STYLE_CSS_PATH):
                shutil.copyfile(STYLE_CSS_PATH, broken_copy)
            shutil.copyfile(STYLE_LAST_BACKUP, STYLE_CSS_PATH)
            with open(STYLE_CSS_PATH, "r", encoding="utf-8") as f:
                css = f.read()
            logger.info("CSS restaurado al último respaldo")
            return {"status": "restored", "css": css}
        except Exception as e:
            logger.error(f"No se pudo restaurar el respaldo: {e}")
            return {"error": str(e)}

    def save_theme(self, name, css_text):
        if not self.window:
            return {"error": "Ventana no disponible"}
        safe_name = re.sub(r"[^A-Za-z0-9_\- ]", "", name or "").strip() or "tema"
        default_filename = safe_name.replace(" ", "_") + ".css"
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_filename,
            )
            if not result:
                return {"status": "cancelled"}
            path = result if isinstance(result, str) else result[0]
            if not path.lower().endswith(".css"):
                path += ".css"
            with open(path, "w", encoding="utf-8") as f:
                f.write(css_text)
            themes = load_themes_registry()
            themes.append({"id": uuid.uuid4().hex[:8], "name": name or safe_name, "path": path})
            save_themes_registry(themes)
            logger.info(f"Tema guardado: {name} -> {path}")
            return {"status": "saved", "themes": themes}
        except Exception as e:
            logger.error(f"No se pudo guardar el tema: {e}")
            return {"error": str(e)}

    def apply_theme(self, theme_id):
        themes = load_themes_registry()
        theme = next((t for t in themes if t["id"] == theme_id), None)
        if not theme:
            return {"error": "Tema no encontrado"}
        if not os.path.exists(theme["path"]):
            return {"error": f"No se encontró el archivo: {theme['path']}"}
        try:
            with open(theme["path"], "r", encoding="utf-8") as f:
                css = f.read()
            self._backup_current_css()
            with open(STYLE_CSS_PATH, "w", encoding="utf-8") as f:
                f.write(css)
            logger.info(f"Tema aplicado: {theme['name']} ({theme['path']})")
            return {"status": "applied", "css": css}
        except Exception as e:
            logger.error(f"No se pudo aplicar el tema: {e}")
            return {"error": str(e)}

    def remove_theme(self, theme_id):
        themes = [t for t in load_themes_registry() if t["id"] != theme_id]
        save_themes_registry(themes)
        return {"themes": themes}

    def import_css_file(self):
        if not self.window:
            return {"error": "Ventana no disponible"}
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG, file_types=("Archivos CSS (*.css)", "Todos los archivos (*.*)")
            )
            if not result:
                return {"status": "cancelled"}
            path = result[0]
            with open(path, "r", encoding="utf-8") as f:
                css = f.read()
            return {"status": "ok", "path": path, "css": css}
        except Exception as e:
            logger.error(f"No se pudo importar el CSS: {e}")
            return {"error": str(e)}

    def export_css_file(self, css_text, suggested_name):
        if not self.window:
            return {"error": "Ventana no disponible"}
        safe_name = suggested_name or "style.css"
        if not safe_name.lower().endswith(".css"):
            safe_name += ".css"
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=safe_name,
            )
            if not result:
                return {"status": "cancelled"}
            path = result if isinstance(result, str) else result[0]
            if not path.lower().endswith(".css"):
                path += ".css"
            with open(path, "w", encoding="utf-8") as f:
                f.write(css_text)
            logger.info(f"CSS exportado a {path}")
            return {"status": "exported", "path": path}
        except Exception as e:
            logger.error(f"No se pudo exportar el CSS: {e}")
            return {"error": str(e)}

    # ---------- backup / restauración de biblioteca ----------
    def export_backup(self):
        """Empaqueta apps, favoritos, categorías, configuración, estadísticas
        y temas instalados (CSS + registro) en un único archivo .json que el
        usuario puede guardar donde quiera y volver a importar después."""
        if not self.window:
            return {"error": "Ventana no disponible"}
        try:
            css = ""
            if os.path.exists(STYLE_CSS_PATH):
                with open(STYLE_CSS_PATH, "r", encoding="utf-8") as f:
                    css = f.read()

            themes = load_themes_registry()
            themes_payload = []
            for t in themes:
                theme_css = ""
                if os.path.exists(t.get("path", "")):
                    try:
                        with open(t["path"], "r", encoding="utf-8") as f:
                            theme_css = f.read()
                    except Exception:
                        pass
                themes_payload.append({"id": t["id"], "name": t["name"], "css": theme_css})

            backup = {
                "backup_version": 1,
                "app_version": APP_VERSION,
                "exported_at": datetime.datetime.now().isoformat(),
                "apps": self.apps,
                "wine_config": load_wine_config(),
                "prefs": load_prefs(),
                "current_css": css,
                "themes": themes_payload,
            }

            default_name = f"sakura_backup_{datetime.date.today().isoformat()}.json"
            result = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=default_name)
            if not result:
                return {"status": "cancelled"}
            path = result if isinstance(result, str) else result[0]
            if not path.lower().endswith(".json"):
                path += ".json"

            with open(path, "w", encoding="utf-8") as f:
                json.dump(backup, f, indent=2, ensure_ascii=False)
            logger.info(f"Backup exportado a {path} ({len(self.apps)} apps, {len(themes_payload)} temas)")
            return {"status": "exported", "path": path}
        except Exception as e:
            logger.error(f"No se pudo exportar el backup: {e}")
            return {"error": str(e)}

    def import_backup(self):
        """Restaura un backup exportado previamente. Reemplaza la biblioteca,
        configuración de Wine, preferencias y temas actuales."""
        if not self.window:
            return {"error": "Ventana no disponible"}
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG, file_types=("Backups de Sakura (*.json)", "Todos los archivos (*.*)")
            )
            if not result:
                return {"status": "cancelled"}
            path = result[0]
            with open(path, "r", encoding="utf-8") as f:
                backup = json.load(f)

            if "apps" not in backup:
                return {"error": "El archivo no parece ser un backup válido de Sakura Launcher"}

            # apps + favoritos + categorías + estadísticas (todo vive junto en cada app)
            apps = backup.get("apps", [])
            for app in apps:
                app.setdefault("id", uuid.uuid4().hex[:8])
                app.setdefault("added_at", datetime.datetime.now().isoformat())
            self.apps = apps
            save_apps(self.apps)

            # configuración de Wine
            if backup.get("wine_config"):
                save_wine_config(backup["wine_config"])

            # preferencias generales (orden de biblioteca, etc)
            if backup.get("prefs"):
                save_prefs(backup["prefs"])

            # tema activo
            if backup.get("current_css"):
                self._backup_current_css()
                with open(STYLE_CSS_PATH, "w", encoding="utf-8") as f:
                    f.write(backup["current_css"])

            # temas guardados: se re-escriben como archivos .css nuevos y se
            # registran, sin pisar las rutas originales del sistema del usuario
            themes_dir = os.path.join(BASE_DIR, "imported_themes")
            os.makedirs(themes_dir, exist_ok=True)
            registry = []
            for theme in backup.get("themes", []):
                theme_id = theme.get("id") or uuid.uuid4().hex[:8]
                theme_path = os.path.join(themes_dir, f"{theme_id}.css")
                with open(theme_path, "w", encoding="utf-8") as f:
                    f.write(theme.get("css", ""))
                registry.append({"id": theme_id, "name": theme.get("name", "Tema importado"), "path": theme_path})
            if registry:
                save_themes_registry(registry)

            logger.info(f"Backup importado desde {path} ({len(apps)} apps)")
            self._notify()
            return {"status": "imported", "apps_count": len(apps)}
        except Exception as e:
            logger.error(f"No se pudo importar el backup: {e}")
            return {"error": f"No se pudo leer el backup: {e}"}

    # ---------- discord ----------
    def get_discord_state(self):
        cfg = load_discord_config()
        session = load_discord_session()
        return {
            "configured": bool(cfg.get("client_id") and cfg.get("client_secret")),
            "connected": bool(session.get("user")),
            "user": session.get("user"),
            "redirect_uri": DISCORD_REDIRECT_URI,
            "rpc_available": self.discord_rpc.connected or self.discord_rpc.connect(),
        }

    def set_discord_credentials(self, client_id, client_secret):
        save_discord_config({
            "client_id": (client_id or "").strip(),
            "client_secret": (client_secret or "").strip(),
        })
        return {"status": "saved"}

    def start_discord_login(self):
        cfg = load_discord_config()
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            return {"error": "Primero configurá el Client ID y el Client Secret"}
        threading.Thread(target=self._run_discord_login, args=(cfg,), daemon=True).start()
        return {"status": "opening"}

    def disconnect_discord(self):
        try:
            if os.path.exists(DISCORD_SESSION_FILE):
                os.remove(DISCORD_SESSION_FILE)
            logger.info("Discord desconectado")
            return {"status": "disconnected"}
        except Exception as e:
            logger.error(f"No se pudo desconectar Discord: {e}")
            return {"error": str(e)}

    def _run_discord_login(self, cfg):
        server = None
        try:
            server = http.server.HTTPServer(("localhost", DISCORD_REDIRECT_PORT), _DiscordCallbackHandler)
            server.timeout = 180
            server.oauth_code = None
            server.oauth_error = None

            authorize_url = (
                "https://discord.com/api/oauth2/authorize"
                f"?client_id={urllib.parse.quote(cfg['client_id'])}"
                f"&redirect_uri={urllib.parse.quote(DISCORD_REDIRECT_URI, safe='')}"
                "&response_type=code&scope=identify"
            )
            webbrowser.open(authorize_url)
            server.handle_request()

            code = server.oauth_code
            if not code:
                msg = "No se completó el login en Discord"
                logger.error(msg)
                self._notify_discord(False, msg)
                return

            token_resp = requests.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": DISCORD_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            if not token_resp.ok:
                msg = f"Discord rechazó el intercambio de token ({token_resp.status_code})"
                logger.error(msg)
                self._notify_discord(False, msg)
                return
            token_data = token_resp.json()

            user_resp = requests.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
                timeout=10,
            )
            if not user_resp.ok:
                msg = "No se pudo obtener el usuario de Discord"
                logger.error(msg)
                self._notify_discord(False, msg)
                return
            raw_user = user_resp.json()

            user = {
                "id": raw_user["id"],
                "username": raw_user.get("username"),
                "global_name": raw_user.get("global_name"),
                "avatar_url": discord_avatar_url(raw_user),
            }
            save_discord_session({
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token"),
                "expires_at": time.time() + token_data.get("expires_in", 604800),
                "user": user,
            })
            logger.info(f"Discord conectado: {user.get('username')}")
            self._notify_discord(True, "", user)
        except Exception as e:
            logger.error(f"Error en login de Discord: {e}")
            self._notify_discord(False, str(e))
        finally:
            if server:
                server.server_close()

    def _notify_discord(self, ok, message, user=None):
        try:
            if self.window:
                payload = json.dumps({"ok": ok, "message": message, "user": user})
                self.window.evaluate_js(
                    f"window.dispatchEvent(new CustomEvent('discord-auth', {{detail:{payload}}}))"
                )
        except Exception:
            pass

    # ---------- imagenes ----------
    def get_image_data(self, path):
        if not path or not os.path.exists(path):
            return ""
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(ext, "png")
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:image/{mime};base64,{b64}"
        except Exception as e:
            logger.error(f"No se pudo leer imagen {path}: {e}")
            return ""

    # ---------- logs ----------
    def get_logs(self):
        if not os.path.exists(LOG_FILE):
            return ""
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return f.read()

    def clear_logs(self):
        open(LOG_FILE, "w").close()
        return {"status": "cleared"}

    # ---------- dialogos de archivo ----------
    def browse_file(self, kind):
        if kind == "image":
            file_types = ("Imagenes (*.png;*.jpg;*.jpeg)",)
        elif kind == "exe":
            file_types = ("Ejecutables (*.exe;*.AppImage;*.sh;*.jar;*)",)
        else:
            file_types = ("Todos los archivos (*.*)",)
        result = self.window.create_file_dialog(webview.OPEN_DIALOG, file_types=file_types)
        return result[0] if result else ""

    def guess_name(self, exe_path, app_type):
        return guess_name(exe_path, app_type)

    def search_covers(self, name):
        try:
            return {"results": search_cover_images(name, limit=3)}
        except Exception as e:
            logger.error(f"search_covers falló para '{name}': {e}")
            return {"results": [], "error": "No se pudo buscar portadas ahora mismo"}

    def download_cover(self, url, name):
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, stream=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")

            safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name or "cover").strip("_") or "cover"
            filename = f"{safe_name}_{uuid.uuid4().hex[:6]}.{ext}"
            path = os.path.join(COVERS_DIR, filename)

            with open(path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return {"path": path}
        except Exception as e:
            logger.error(f"No se pudo descargar la portada {url}: {e}")
            return {"error": "No se pudo descargar esa imagen"}

    def browse_folder(self):
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else ""

    # ---------- actualizador ----------
    def check_for_updates(self):
        return UpdateChecker.check()

    def dismiss_update(self, version):
        prefs = load_prefs()
        prefs["dismissed_version"] = version or ""
        save_prefs(prefs)
        return {"status": "dismissed"}

    def open_update_page(self, url):
        try:
            if url and url.startswith("https://github.com/"):
                webbrowser.open(url)
                return {"status": "opened"}
            return {"error": "Enlace no permitido"}
        except Exception as e:
            return {"error": str(e)}

    # ---------- bandeja del sistema ----------
    def get_tray_support(self):
        return {"available": TRAY_AVAILABLE}

    def minimize_to_tray(self):
        """Se llama al apretar cerrar (la X). Si hay soporte de bandeja,
        oculta la ventana y deja el ícono corriendo; si no, minimiza."""
        prefs = load_prefs()
        if not prefs.get("close_to_tray", True):
            return {"status": "not_enabled"}
        try:
            if TRAY_AVAILABLE and self.window:
                self.window.hide()
                return {"status": "hidden_to_tray"}
            elif self.window:
                self.window.minimize()
                return {"status": "minimized"}
        except Exception as e:
            logger.error(f"No se pudo minimizar a bandeja: {e}")
        return {"status": "noop"}

    def set_close_to_tray(self, enabled):
        prefs = load_prefs()
        prefs["close_to_tray"] = bool(enabled)
        save_prefs(prefs)
        return {"status": "saved"}

    def quit_app(self):
        """Salida real desde el menú de bandeja o el atajo Ctrl+Q."""
        try:
            self.discord_rpc.close()
            if self.tray:
                self.tray.stop()
        finally:
            os._exit(0)

    # ---------- enlaces externos ----------
    def open_link(self, url):
        # Whitelist: solo abrimos los links del propio menú, para no exponer
        # esto como una forma genérica de abrir cualquier URL desde el JS.
        allowed = {
            "https://github.com/kivppy",
            "https://t.me/ashiganai",
            "https://x.com/Nau_webp",
            "https://discord.gg/JbGe6T8QFC",
        }
        if url not in allowed:
            return {"error": "Enlace no permitido"}
        try:
            webbrowser.open(url)
            return {"status": "opened"}
        except Exception as e:
            logger.error(f"No se pudo abrir el enlace {url}: {e}")
            return {"error": str(e)}


# ---------- actualizador (solo comprobar, nunca descargar solo) ----------
def _parse_version(v):
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) or (0,)


class UpdateChecker:
    @staticmethod
    def check():
        """Consulta la última release en GitHub y compara contra
        APP_VERSION. Nunca descarga nada automáticamente: solo informa."""
        try:
            req = urllib.request.Request(
                UPDATE_CHECK_URL,
                headers={"User-Agent": "SakuraLauncher", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            latest = (data.get("tag_name") or "").lstrip("v")
            if not latest:
                return {"error": "No se pudo leer la última versión publicada"}
            has_update = _parse_version(latest) > _parse_version(APP_VERSION)
            return {
                "current_version": APP_VERSION,
                "latest_version": latest,
                "has_update": has_update,
                "url": data.get("html_url", ""),
                "notes": (data.get("body") or "")[:600],
            }
        except Exception as e:
            logger.warning(f"No se pudo comprobar actualizaciones: {e}")
            return {"error": "No se pudo comprobar actualizaciones (¿sin conexión?)"}


class TrayManager:
    """Bandeja del sistema minimalista: cerrar la ventana la minimiza a la
    bandeja en vez de salir, con opción explícita de 'Salir'. Usa pystray
    si está disponible; si no, el botón de cerrar simplemente minimiza la
    ventana sin ícono en bandeja (fallback sin dependencias nuevas)."""

    def __init__(self, window, on_quit):
        self.window = window
        self.on_quit = on_quit
        self.icon = None

    def _build_image(self):
        # ícono simple: pétalo sakura en dos tonos, generado en memoria
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((14, 14, 50, 50), fill=(255, 111, 165, 255))
        draw.ellipse((24, 24, 40, 40), fill=(255, 228, 239, 255))
        return img

    def start(self):
        if not TRAY_AVAILABLE:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Mostrar Sakura Launcher", self._show, default=True),
            pystray.MenuItem("Salir", self._quit),
        )
        self.icon = pystray.Icon("sakura_launcher", self._build_image(), "Sakura Launcher", menu)
        # Con el backend xorg (ver import de pystray más arriba) el ícono
        # corre su propio loop de X11 en este hilo, sin pisar el de GTK que
        # usa pywebview, así que alcanza con el threading.Thread de siempre.
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _show(self, icon=None, item=None):
        try:
            self.window.restore()
            self.window.show()
        except Exception:
            pass

    def _quit(self, icon=None, item=None):
        if self.icon:
            self.icon.stop()
        self.on_quit()

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass


def main():
    api = Api()
    window = webview.create_window(
        "Sakura Launcher",
        UI_INDEX,
        js_api=api,
        width=1262,
        height=871,
        background_color="#12080d",
        min_size=(1262, 871),
    )
    api.window = window

    def _quit_from_tray():
        api.quit_app()

    tray = TrayManager(window, _quit_from_tray)
    api.tray = tray

    def _on_closing():
        """Cerrar (la X) minimiza a bandeja en vez de salir, salvo que el
        usuario haya desactivado esa preferencia. Devolver False acá evita
        que pywebview destruya la ventana."""
        prefs = load_prefs()
        if prefs.get("close_to_tray", True) and TRAY_AVAILABLE:
            api.minimize_to_tray()
            return False
        api.discord_rpc.close()
        tray.stop()
        return True

    window.events.closing += _on_closing

    if TRAY_AVAILABLE:
        tray.start()

    webview.start(debug=False)


if __name__ == "__main__":
    main()
