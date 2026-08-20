import os
import sys
import subprocess
import time
import re
import json
import threading
import queue
import io
import urllib.parse
import hashlib
import logging
import webbrowser
import traceback
from datetime import datetime, timedelta, date
from functools import wraps
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# SETUP FILE LOGGING EARLY
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "airgrabber.log")

if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
    try:
        os.remove(LOG_FILE)
    except:
        pass

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
logger.info("=== TorGrabber startup ===")

# ==========================================
# VERSION & UPDATE CONFIG
# ==========================================
CURRENT_VERSION = "1.0.2"
REPO_OWNER = "drunkgummyboy"
REPO_NAME = "AirGrabber"
SCRIPT_FILENAME = "airgrabber.py"

# ==========================================
# AUTO-INSTALL DEPENDENCIES
# ==========================================
def ensure_dependencies():
    required_packages = {
        "customtkinter": "customtkinter",
        "requests": "requests",
        "PIL": "Pillow",
        "cloudscraper": "cloudscraper"
    }
    for import_name, pip_name in required_packages.items():
        try:
            __import__(import_name)
            logger.debug(f"Dependency '{import_name}' already installed.")
        except ImportError:
            logger.info(f"Missing dependency '{pip_name}'. Installing now...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info(f"Successfully installed '{pip_name}'.")
            except Exception as e:
                logger.error(f"Failed to install '{pip_name}': {e}")

try:
    ensure_dependencies()
except Exception as e:
    logger.error(f"Dependency installation failed: {e}")

# ==========================================
# IMPORTS
# ==========================================
try:
    import customtkinter as ctk
    import requests
    import cloudscraper
    from PIL import Image, ImageOps, ImageTk
    import tkinter.filedialog as filedialog
    import tkinter.messagebox as messagebox
except ImportError as e:
    logger.critical(f"Critical import error: {e}")
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        tk.messagebox.showerror("Missing Dependencies",
                                f"Required module not found: {e}\n\n"
                                "Please run the script from a terminal to see the full error.")
        root.destroy()
    except:
        pass
    sys.exit(1)

# ==========================================
# GLOBAL HTTP SESSIONS
# ==========================================
http_session = requests.Session()
http_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*'
})

try:
    scraper_session = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
except Exception as e:
    logger.error(f"Cloudscraper init failed: {e}")
    scraper_session = requests.Session()

api_semaphore = threading.Semaphore(4)

# ==========================================
# CONFIGURATION & THEME
# ==========================================
DATA_FILE = os.path.join(SCRIPT_DIR, "followed_shows.json")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "history.json")
EPISODES_FILE = os.path.join(SCRIPT_DIR, "episodes_cache.json")
SIZE_CACHE_FILE = os.path.join(SCRIPT_DIR, "size_cache.json")
TORRENTS_DIR = os.path.join(SCRIPT_DIR, "torrents")
POSTERS_DIR = os.path.join(SCRIPT_DIR, "posters_cache")

os.makedirs(POSTERS_DIR, exist_ok=True)
os.makedirs(TORRENTS_DIR, exist_ok=True)

BG_BASE = "#0F0D14"
GLASS_CARD = "#1A1726"
GLASS_EDGE = "#5D4B8B"
ACCENT_COLOR = "#4d4180"
ACCENT_HOVER = "#3a3160"
TAB_BG = "#13111C"

ctk.set_appearance_mode("Dark")

def strip_html_tags(text):
    if not text: return "No summary available."
    return re.sub(re.compile('<.*?>'), '', text)

# ==========================================
# RETRY DECORATOR
# ==========================================
def retry(max_attempts=3, delay=1, backoff=2, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            _delay = delay
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 429:
                        try:
                            retry_after = int(e.response.headers.get('Retry-After', _delay * 2))
                        except ValueError:
                            retry_after = _delay * 2
                        time.sleep(retry_after)
                        _delay = retry_after
                    else:
                        if attempt == max_attempts - 1: raise
                        time.sleep(_delay)
                        _delay *= backoff
                except exceptions:
                    if attempt == max_attempts - 1: raise
                    time.sleep(_delay)
                    _delay *= backoff
            return None
        return wrapper
    return decorator

# ==========================================
# LRU IMAGE CACHE
# ==========================================
class LRUImageCache:
    def __init__(self, maxsize=128):
        self._cache = {}
        self._maxsize = maxsize
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            if key in self._cache:
                value = self._cache.pop(key)
                self._cache[key] = value
                return value
            return None

    def put(self, key, value):
        with self._lock:
            if key in self._cache:
                self._cache.pop(key)
            elif len(self._cache) >= self._maxsize:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = value

# ==========================================
# MAIN APPLICATION
# ==========================================
class TorGrabberApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("TorGrabber - Automated Media Desktop Frontend")
        self.configure(fg_color=BG_BASE)

        window_width = 1650
        window_height = 900
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        self.geometry(f"{window_width}x{window_height}+{int((screen_width/2)-(window_width/2))}+{int((screen_height/2)-(window_height/2))}")

        self.data_lock = threading.RLock()
        self.background_executor = ThreadPoolExecutor(max_workers=4)

        self.settings = self.load_settings()
        self.followed_shows = self.load_data()
        self.history = self.load_history()
        self.episodes_cache = self.load_json_dict(EPISODES_FILE)

        self.image_cache = LRUImageCache(maxsize=200)
        self.unfollowed_cache = {}
        self.calendar_day_frames = {}
        self.calendar_generation = 0
        self._cache_dirty = False

        self.ui_queue = queue.Queue()
        self.poll_ui_queue()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # TOP NAVIGATION BAR
        self.top_bar = ctk.CTkFrame(self, fg_color="transparent")
        self.top_bar.grid(row=0, column=0, padx=15, pady=(15, 5), sticky="ew")
        self.top_bar.grid_columnconfigure(0, weight=1, uniform="nav")
        self.top_bar.grid_columnconfigure(1, weight=0)
        self.top_bar.grid_columnconfigure(2, weight=1, uniform="nav")

        self.logo_lbl = ctk.CTkLabel(self.top_bar, text="TorGrabber", font=ctk.CTkFont(size=24, weight="bold"), text_color=ACCENT_COLOR)
        self.logo_lbl.grid(row=0, column=0, sticky="w", padx=(10, 0))

        self.global_media_var = ctk.StringVar(value="TV Shows")
        self.toggle_frame = ctk.CTkFrame(self.top_bar, fg_color=GLASS_CARD, corner_radius=15, border_width=1, border_color=GLASS_EDGE)
        self.toggle_frame.grid(row=0, column=1)

        self.btn_tv = ctk.CTkButton(self.toggle_frame, text="", width=160, height=100, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, corner_radius=12, border_width=0, font=ctk.CTkFont(weight="bold", size=16), command=lambda: self.set_global_mode("TV Shows"))
        self.btn_tv.grid(row=0, column=0, padx=4, pady=4)

        self.btn_movie = ctk.CTkButton(self.toggle_frame, text="", width=160, height=100, fg_color="transparent", hover_color="#2A2130", corner_radius=12, border_width=0, font=ctk.CTkFont(weight="bold", size=16), command=lambda: self.set_global_mode("Movies"))
        self.btn_movie.grid(row=0, column=1, padx=4, pady=4)

        self.right_nav_frame = ctk.CTkFrame(self.top_bar, fg_color="transparent")
        self.right_nav_frame.grid(row=0, column=2, sticky="e")
        self.global_search_entry = ctk.CTkEntry(self.right_nav_frame, placeholder_text="Type to search...", placeholder_text_color="#A4B2C6", height=40, width=200, fg_color=GLASS_CARD, border_color=GLASS_EDGE)
        self.global_search_entry.pack(side="left", padx=(0, 5))
        self.global_search_entry.bind("<Return>", lambda e: self.do_global_manual_search())
        self.global_search_btn = ctk.CTkButton(self.right_nav_frame, text="🔍", width=40, height=40, fg_color=GLASS_CARD, hover_color=ACCENT_HOVER, border_width=1, border_color=GLASS_EDGE, corner_radius=10, font=ctk.CTkFont(size=18), command=self.do_global_manual_search)
        self.global_search_btn.pack(side="left", padx=(0, 15))
        self.settings_btn = ctk.CTkButton(self.right_nav_frame, text="⚙", font=ctk.CTkFont(size=28), width=40, height=40, fg_color="transparent", hover_color=GLASS_CARD, border_width=0, corner_radius=10, command=self.open_settings_window)
        self.settings_btn.pack(side="left")

        self.tabview = ctk.CTkTabview(
            self, corner_radius=15, fg_color=TAB_BG, border_width=1, border_color=GLASS_EDGE,
            segmented_button_fg_color=GLASS_CARD, segmented_button_selected_color=ACCENT_COLOR,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=GLASS_CARD, command=self.on_tab_change
        )
        self.tabview.grid(row=1, column=0, padx=15, pady=5, sticky="nsew")
        self.tabview._segmented_button.configure(font=ctk.CTkFont(size=16, weight="bold"))

        self.current_movie_month = date.today().replace(day=1)
        self._sync_timer = None
        self._sync_running = False
        self.current_movie_buckets = None

        self.set_global_mode("TV Shows")

        # Status label for update notifications
        self.status_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=12), text_color="#A4B2C6")
        self.status_label.place(relx=0.5, rely=0.99, anchor="s")

        # Delayed update check
        self.after(2000, self.check_for_updates)

        self.background_executor.submit(self.load_app_icons)
        self.start_background_library_sync()

    # ==========================================
    # AUTO-UPDATE METHODS (improved with UI feedback)
    # ==========================================
    def check_for_updates(self):
        def _check():
            try:
                version_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/version.txt"
                resp = requests.get(version_url, timeout=5)
                if resp.status_code != 200:
                    logger.debug(f"Version check failed: HTTP {resp.status_code}")
                    return
                remote_version = resp.text.strip()
                if remote_version == CURRENT_VERSION:
                    return
                logger.info(f"New version {remote_version} available. Updating...")
                self.ui_queue.put(lambda: self.status_label.configure(
                    text=f"⬆ Updating to version {remote_version}... Restarting soon."
                ))

                for attempt in range(3):
                    try:
                        script_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{SCRIPT_FILENAME}"
                        script_resp = requests.get(script_url, timeout=10)
                        if script_resp.status_code == 200:
                            self.apply_update(script_resp.text, remote_version)
                            return
                        else:
                            logger.error(f"Script download failed (attempt {attempt+1}): HTTP {script_resp.status_code}")
                            time.sleep(2 ** attempt)
                    except Exception as e:
                        logger.error(f"Script download error (attempt {attempt+1}): {e}")
                        time.sleep(2 ** attempt)
                logger.error("Failed to download new script after 3 attempts.")
                self.ui_queue.put(lambda: self.status_label.configure(text="❌ Update failed. Check log."))
            except Exception as e:
                logger.error("Update check failed: %s", e)

        self.background_executor.submit(_check)

    def apply_update(self, new_content, new_version):
        """Spawn a robust updater that overwrites the script and restarts."""
        script_path = os.path.join(SCRIPT_DIR, SCRIPT_FILENAME)
        import json, tempfile

        escaped_content = json.dumps(new_content)

        # Create a more robust updater that retries and logs errors
        updater_code = f'''import os, sys, time, subprocess, json, shutil

def log_error(msg):
    try:
        with open(r"{script_path}.update_error.log", "a") as f:
            f.write(f"{{time.ctime()}}: {{msg}}\\n")
    except:
        pass

time.sleep(2)  # extra delay

script_path = r"{script_path}"
new_content = json.loads(r"""{escaped_content}""")

# Try to write the new file, retry up to 5 times
for attempt in range(5):
    try:
        # Write to a temporary file first
        temp_path = script_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        # Replace the original
        if os.path.exists(script_path):
            # On Windows, file may be locked; try renaming first
            try:
                os.replace(temp_path, script_path)
            except PermissionError:
                # If replace fails, try to remove first
                os.remove(script_path)
                os.rename(temp_path, script_path)
        else:
            os.rename(temp_path, script_path)
        # Success
        break
    except Exception as e:
        log_error(f"Write attempt {{attempt+1}} failed: {{e}}")
        time.sleep(0.5)
else:
    log_error("All write attempts failed. Update aborted.")
    sys.exit(1)

# Launch the new version
try:
    subprocess.Popen([sys.executable, script_path], creationflags=0 if sys.platform != "win32" else subprocess.CREATE_NO_WINDOW)
except Exception as e:
    log_error(f"Failed to restart: {{e}}")
'''
        temp_dir = tempfile.gettempdir()
        updater_path = os.path.join(temp_dir, f"airgrabber_updater_{int(time.time())}.py")
        with open(updater_path, "w", encoding="utf-8") as f:
            f.write(updater_code)

        # Launch the updater and exit after a short delay to show status
        try:
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW
            else:
                creationflags = 0
            subprocess.Popen([sys.executable, updater_path],
                             creationflags=creationflags,
                             close_fds=True)
        except Exception as e:
            logger.error("Failed to launch updater: %s", e)
            self.status_label.configure(text="❌ Update error. See log.")
            return

        # Show a final message and wait a bit before quitting
        self.status_label.configure(text=f"✅ Updated to {new_version}. Restarting...")
        self.update()  # force UI redraw
        time.sleep(1.5)  # let user see the message

        self.quit()
        sys.exit(0)

    # ==========================================
    # UI HELPERS
    # ==========================================
    def do_global_manual_search(self):
        q = self.global_search_entry.get().strip()
        if q:
            self.open_generic_manual_search_with_query(q)
            self.global_search_entry.delete(0, 'end')

    def set_global_mode(self, mode):
        self.global_media_var.set(mode)
        if mode == "TV Shows":
            self.btn_tv.configure(fg_color=ACCENT_COLOR)
            self.btn_movie.configure(fg_color="transparent")
        else:
            self.btn_tv.configure(fg_color="transparent")
            self.btn_movie.configure(fg_color=ACCENT_COLOR)

        for t in ["Calendar", "Releases", "Tracked"]:
            try:
                self.tabview.delete(t)
            except (ValueError, AttributeError):
                pass

        if mode == "TV Shows":
            self.tab_calendar = self.tabview.add("Calendar")
            self.tab_library = self.tabview.add("Tracked")
            self.setup_calendar_tab()
            self.setup_library_tab()
            self.tabview.set("Calendar")
            self.refresh_calendar_data()
        else:
            self.tab_releases = self.tabview.add("Releases")
            self.setup_releases_tab()
            self.tabview.set("Releases")
            self.build_movie_releases_ui()

    def load_app_icons(self):
        logo = self.fetch_pil_image("https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/refs/heads/main/logo.png")
        tv_ico = self.fetch_pil_image("https://github.com/drunkgummyboy/AirGrabber/blob/main/tv.png?raw=true")
        mov_ico = self.fetch_pil_image("https://github.com/drunkgummyboy/AirGrabber/blob/main/movie.png?raw=true")
        if not mov_ico:
            mov_ico = self.fetch_pil_image("https://github.com/drunkgummyboy/AirGrabber/blob/main/movies.png?raw=true")
        settings_ico = self.fetch_pil_image("https://raw.githubusercontent.com/google/material-design-icons/master/png/action/settings/materialicons/48dp/2x/baseline_settings_white_48dp.png")

        def apply():
            if logo and hasattr(self, 'logo_lbl') and self.logo_lbl.winfo_exists():
                try:
                    self.iconphoto(False, ImageTk.PhotoImage(logo))
                except:
                    pass
                w, h = logo.size
                new_h = 100
                new_w = int(new_h * (w / h))
                self.logo_lbl.configure(image=ctk.CTkImage(light_image=logo, dark_image=logo, size=(new_w, new_h)), text="")
            if hasattr(self, 'btn_tv') and self.btn_tv.winfo_exists():
                if tv_ico:
                    self.tv_ico_img = ctk.CTkImage(light_image=tv_ico, dark_image=tv_ico, size=(80, 80))
                    self.btn_tv.configure(image=self.tv_ico_img, text="")
                else:
                    self.btn_tv.configure(text="TV Shows")
            if hasattr(self, 'btn_movie') and self.btn_movie.winfo_exists():
                if mov_ico:
                    self.mov_ico_img = ctk.CTkImage(light_image=mov_ico, dark_image=mov_ico, size=(80, 80))
                    self.btn_movie.configure(image=self.mov_ico_img, text="")
                else:
                    self.btn_movie.configure(text="Movies")
            if settings_ico and hasattr(self, 'settings_btn') and self.settings_btn.winfo_exists():
                self.settings_img = ctk.CTkImage(light_image=settings_ico, dark_image=settings_ico, size=(32, 32))
                self.settings_btn.configure(image=self.settings_img, text="")

        self.ui_queue.put(apply)

    def poll_ui_queue(self):
        try:
            for _ in range(20):
                func = self.ui_queue.get_nowait()
                try:
                    func()
                except Exception as e:
                    logger.error("UI callback failed: %s", e, exc_info=True)
        except queue.Empty:
            pass
        self.after(50, self.poll_ui_queue)

    def load_data(self):
        with self.data_lock:
            if not os.path.exists(DATA_FILE):
                return {}
            try:
                with open(DATA_FILE, "r") as f:
                    return json.load(f)
            except:
                return {}

    def save_data(self):
        with self.data_lock:
            with open(DATA_FILE, "w") as f:
                json.dump(self.followed_shows, f)

    def load_history(self):
        with self.data_lock:
            if os.path.exists(HISTORY_FILE):
                try:
                    with open(HISTORY_FILE, "r") as f:
                        return json.load(f)
                except:
                    return []
            return []

    def save_history(self):
        with self.data_lock:
            with open(HISTORY_FILE, "w") as f:
                json.dump(self.history, f)

    def load_json_dict(self, filepath):
        with self.data_lock:
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r") as f:
                        return json.load(f)
                except:
                    return {}
            return {}

    def save_caches(self):
        with self.data_lock:
            with open(EPISODES_FILE, "w") as f:
                json.dump(self.episodes_cache, f)
            self._cache_dirty = False

    def mark_caches_dirty(self):
        self._cache_dirty = True

    def maybe_save_caches(self):
        if self._cache_dirty:
            self.save_caches()

    def load_settings(self):
        default_settings = {
            "first_day": "Monday", "quality": "1080p",
            "download_dir": TORRENTS_DIR, "weeks_to_show": 3,
            "prev_weeks_to_show": 0, "create_torgrabber_json": True, "tmdb_api_key": ""
        }
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    default_settings.update(json.load(f))
            except:
                pass
        return default_settings

    def save_settings(self):
        with self.data_lock:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(self.settings, f)

    def format_size(self, size_bytes):
        if not size_bytes:
            return ""
        try:
            size = float(size_bytes)
            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if size < 1024.0:
                    return f"{size:.2f} {unit}"
                size /= 1024.0
            return f"{size:.2f} PB"
        except ValueError:
            return ""

    @retry(max_attempts=3, delay=1)
    def fetch_pil_image(self, url):
        if not url:
            return None
        hsh = hashlib.md5(url.encode('utf-8')).hexdigest()
        ext = ".png" if ".png" in url.lower() else ".jpg"
        local_path = os.path.join(POSTERS_DIR, f"{hsh}{ext}")

        cached = self.image_cache.get(url)
        if cached:
            return cached
        if os.path.exists(local_path):
            try:
                pil_img = Image.open(local_path)
                pil_img.load()
                self.image_cache.put(url, pil_img)
                return pil_img
            except Exception as e:
                logger.warning(f"Corrupted cache file detected, deleting... ({e})")
                try:
                    os.remove(local_path)
                except:
                    pass
        try:
            resp = http_session.get(url, timeout=5)
            resp.raise_for_status()
            pil_img = Image.open(io.BytesIO(resp.content))
            pil_img.load()
            pil_img.save(local_path)
            self.image_cache.put(url, pil_img)
            return pil_img
        except Exception as e:
            logger.debug(f"Failed to fetch image {url}: {e}")
            return None

    def show_loading(self, parent_frame):
        loader = ctk.CTkProgressBar(parent_frame, mode="indeterminate", height=4, progress_color=ACCENT_COLOR)
        loader.pack(fill="x", pady=2)
        loader.start()
        return loader

    def hide_loading(self, loader_widget):
        if loader_widget and loader_widget.winfo_exists():
            loader_widget.stop()
            loader_widget.destroy()

    def on_tab_change(self):
        tab = self.tabview.get()
        if tab == "Calendar":
            self.refresh_calendar_data()
        elif tab == "Releases":
            self.build_movie_releases_ui()
        elif tab == "Tracked":
            self.refresh_library_list()

    # ==========================================
    # SEARCH ENGINE
    # ==========================================
    def _safe_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def search_best_torrent(self, show_name, episode_code, show_id=None):
        quality_pref = self.settings.get("quality", "1080p")
        if quality_pref == "2160p (4K)":
            q_str = "2160p"
            quality_token = "2160p"
        elif quality_pref == "x265/HEVC":
            q_str = "x265"
            quality_token = "x265"
        else:
            q_str = "" if quality_pref == "Any" else quality_pref
            quality_token = q_str

        clean = re.sub(r"[^\w\s]", " ", show_name)
        clean = " ".join(clean.split())

        query_variants = [
            f"{clean} {episode_code} {q_str}".strip(),
            f"{clean} {episode_code}".strip(),
            f"{show_name} {episode_code} {q_str}".strip(),
            f"{show_name} {episode_code}".strip()
        ]

        for q in query_variants:
            try:
                with api_semaphore:
                    res = http_session.get(f"https://apibay.org/q.php?q={urllib.parse.quote(q)}", timeout=5)
                    res.raise_for_status()
                data = res.json()
                if isinstance(data, list) and len(data) > 0 and data[0].get('id') != '0':
                    valid = [r for r in data if self._safe_int(r.get('seeders', 0)) > 0]
                    filtered = self.apply_quality_filter(valid)
                    if filtered:
                        filtered.sort(key=lambda x: self._safe_int(x['seeders']), reverse=True)
                        return {
                            "source": "apibay",
                            "name": filtered[0]['name'],
                            "info_hash": filtered[0]['info_hash'],
                            "magnet": "",
                            "size": self._safe_int(filtered[0].get('size', 0)),
                            "seeders": self._safe_int(filtered[0].get('seeders', 0))
                        }
            except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError) as e:
                logger.debug("APIBay search error for '%s': %s", q, e)

        match = re.search(r'S(\d+)E(\d+)', episode_code, re.IGNORECASE)
        s_num, e_num = (int(match.group(1)), int(match.group(2))) if match else (1, 1)
        imdb_id = None
        if show_id:
            with self.data_lock:
                show_data = self.followed_shows.get(str(show_id), {})
                meta = show_data.get('metadata')
                if meta and meta.get('externals'):
                    imdb_id = meta['externals'].get('imdb')
            if not imdb_id:
                try:
                    with api_semaphore:
                        res = http_session.get(f"https://api.tvmaze.com/shows/{show_id}?embed[]=externals", timeout=5)
                        res.raise_for_status()
                    data = res.json()
                    imdb_id = data.get('externals', {}).get('imdb')
                    if imdb_id:
                        with self.data_lock:
                            if str(show_id) in self.followed_shows:
                                if 'metadata' not in self.followed_shows[str(show_id)] or not self.followed_shows[str(show_id)]['metadata']:
                                    self.followed_shows[str(show_id)]['metadata'] = {}
                                self.followed_shows[str(show_id)]['metadata']['externals'] = {'imdb': imdb_id}
                                self.mark_caches_dirty()
                except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
                    logger.debug("TVMaze metadata fetch error for show %s: %s", show_id, e)

        if imdb_id:
            if not imdb_id.startswith('tt'):
                imdb_id = f"tt{imdb_id}"
            try:
                with api_semaphore:
                    res = http_session.get(f"https://torrentio.strem.fun/stream/series/{imdb_id}:{s_num}:{e_num}.json", timeout=6)
                    res.raise_for_status()
                streams = self._parse_torrentio_streams(res.json().get('streams', []), quality_pref, q_str)
                if streams:
                    return streams[0]
            except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
                logger.debug("Torrentio search error for %s: %s", imdb_id, e)

        try:
            with api_semaphore:
                res = http_session.get(f"https://solidtorrents.to/api/v1/search?q={urllib.parse.quote(query_variants[0])}&category=Video", timeout=6)
                res.raise_for_status()
            valid_solid = []
            for r in res.json().get('results', []):
                seeders = self._safe_int(r.get('swarm', {}).get('seeders', 0))
                if seeders > 0:
                    magnet = r.get('magnet', '')
                    hash_match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', magnet, re.IGNORECASE)
                    valid_solid.append({
                        'source': 'solidtorrents',
                        'name': r.get('title', ''),
                        'magnet': magnet,
                        'info_hash': hash_match.group(1) if hash_match else "",
                        'size': self._safe_int(r.get('size', 0)),
                        'seeders': seeders
                    })
            if valid_solid:
                valid_solid.sort(key=lambda x: x['seeders'], reverse=True)
                return valid_solid[0]
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            logger.debug("SolidTorrents search error for '%s': %s", query_variants[0], e)
        return None

    def _parse_torrentio_streams(self, streams, quality_pref, q_str):
        valid = []
        if quality_pref == "2160p (4K)":
            quality_token = "2160p"
        elif quality_pref == "x265/HEVC":
            quality_token = "x265"
        else:
            quality_token = q_str

        for s in streams:
            title = s.get('title', '').lower()
            seed_match = re.search(r'👤\s*(\d+)', s.get('title', ''))
            seeders = int(seed_match.group(1)) if seed_match else 0
            if seeders > 0:
                if quality_pref == "Any" or (quality_token.lower() in title):
                    magnet = s.get('url', '')
                    hash_match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', magnet, re.IGNORECASE)
                    sz_match = re.search(r'💾\s*([\d.]+)\s*([A-Za-z]+)', s.get('title', ''))
                    sz_bytes = 0
                    if sz_match:
                        val, unit = float(sz_match.group(1)), sz_match.group(2).upper()
                        sz_bytes = val * (1024**3 if unit == 'GB' else 1024**2 if unit == 'MB' else 1024)
                    valid.append({
                        'source': 'torrentio',
                        'name': s.get('title', '').split('\n')[0],
                        'magnet': magnet,
                        'info_hash': hash_match.group(1) if hash_match else "",
                        'size': sz_bytes,
                        'seeders': seeders
                    })
        valid.sort(key=lambda x: x['seeders'], reverse=True)
        return valid

    def apply_quality_filter(self, results):
        pref = self.settings.get("quality", "1080p")
        if pref == "Any":
            return results
        if pref == "2160p (4K)":
            q_str = "2160p"
        elif pref == "x265/HEVC":
            q_str = "x265"
        else:
            q_str = pref
        valid = []
        for r in results:
            n = r.get('name', '').lower()
            if pref == "x265/HEVC" and ("x265" in n or "hevc" in n):
                valid.append(r)
            elif q_str.lower() in n:
                valid.append(r)
        return valid

    def download_torrent_file(self, data, best, f_size=None):
        dl_dir = self.settings.get("download_dir", TORRENTS_DIR)
        os.makedirs(dl_dir, exist_ok=True)
        raw_name = best.get('name', 'torrent')
        safe = re.sub(r'[<>:"/\\|?*\[\]()]+', '_', raw_name)
        safe = "".join(c for c in safe if c.isalnum() or c in " ._-").strip()
        if not safe:
            safe = "torrent"
        safe = safe[:120]

        def dl():
            success = False
            info_hash = best.get('info_hash')
            magnet = best.get('magnet')

            if magnet:
                try:
                    magnet_path = os.path.join(dl_dir, f"{safe}.magnet")
                    with open(magnet_path, "w", encoding="utf-8") as f:
                        f.write(magnet)
                    success = True
                    logger.info(f"Saved magnet link to {magnet_path}")
                except Exception as e:
                    logger.error(f"Failed to save magnet file: {e}")
            elif info_hash:
                t_path = os.path.join(dl_dir, f"{safe}.torrent")
                part = t_path + ".part"
                for base in [f"https://itorrents.org/torrent/{info_hash}.torrent",
                             f"https://btcache.me/torrent/{info_hash}"]:
                    try:
                        r = scraper_session.get(base, timeout=10)
                        if r.status_code == 200 and (b'd8:announce' in r.content or b'd4:info' in r.content):
                            with open(part, 'wb') as f:
                                f.write(r.content)
                            os.replace(part, t_path)
                            success = True
                            logger.info(f"Downloaded .torrent file to {t_path}")
                            break
                        else:
                            logger.debug(f"Torrent download failed from {base}: status {r.status_code}")
                    except Exception as e:
                        logger.warning(f"Error downloading torrent from {base}: {e}")

            if success:
                if self.settings.get("create_torgrabber_json", True):
                    try:
                        media_type = "movie" if self.global_media_var.get() == "Movies" else "tv"
                        json_path = os.path.join(dl_dir, f"{safe}_torgrabber.json")
                        with open(json_path, 'w') as mf:
                            json.dump({
                                "media_type": media_type,
                                "show_name": data.get('show', 'Unknown'),
                                "episode_code": data.get('episode', ''),
                                "title": data.get('title', ''),
                                "tvmaze_id": data.get('media_id', '0')
                            }, mf, indent=4)
                        logger.info(f"Saved metadata to {json_path}")
                    except Exception as e:
                        logger.error(f"Failed to save metadata JSON: {e}")
                if data.get('media_id') and data.get('episode'):
                    hk = f"{data['media_id']}_{data['episode']}"
                    with self.data_lock:
                        if hk not in self.history:
                            self.history.append(hk)
                            self.save_history()
                            logger.debug(f"Added to history: {hk}")
            else:
                logger.error(f"Download failed for {best.get('name', 'Unknown')}: no info_hash or magnet available")

        self.background_executor.submit(dl)

    def start_background_library_sync(self):
        self.after(5000, self._run_library_sync)

    def _run_library_sync(self):
        if self._sync_running:
            return
        self._sync_running = True
        def sync():
            try:
                with self.data_lock:
                    ids = list(self.followed_shows.keys())
                for sid in ids:
                    try:
                        with api_semaphore:
                            res = http_session.get(f"https://api.tvmaze.com/shows/{sid}?embed[]=episodes&embed[]=seasons", timeout=5)
                            res.raise_for_status()
                        d = res.json()
                        with self.data_lock:
                            if sid in self.followed_shows:
                                self.followed_shows[sid]["metadata"] = d
                        self.episodes_cache[sid] = d.get('_embedded', {}).get('episodes', [])
                        if d.get('image', {}).get('medium'):
                            self.background_executor.submit(self.fetch_pil_image, d['image']['medium'])
                        time.sleep(0.4)
                    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
                        logger.warning("Failed to sync library data for show %s: %s", sid, e)
                self.save_data()
                self.mark_caches_dirty()
                self.maybe_save_caches()
            finally:
                self._sync_running = False
                self.ui_queue.put(lambda: self.after(6*60*60*1000, self._run_library_sync))
        self.background_executor.submit(sync)

    # ==========================================
    # TV CALENDAR UI
    # ==========================================
    def setup_calendar_tab(self):
        self.tab_calendar.grid_columnconfigure(0, weight=1)
        self.tab_calendar.grid_rowconfigure(2, weight=1)

        controls = ctk.CTkFrame(self.tab_calendar, fg_color="transparent")
        controls.grid(row=0, column=0, padx=15, pady=5, sticky="ew")
        controls.grid_columnconfigure(0, weight=1)

        weeks_frame = ctk.CTkFrame(controls, fg_color="transparent")
        weeks_frame.grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(weeks_frame, text="Prev Weeks:", text_color="gray70", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 5))
        self.prev_weeks_var = ctk.StringVar(value=str(self.settings.get("prev_weeks_to_show", 0)))
        ctk.CTkOptionMenu(weeks_frame, values=["0", "1", "2", "3", "4"], height=28, width=60, fg_color=GLASS_CARD, button_color=GLASS_EDGE, variable=self.prev_weeks_var, command=lambda e: self.refresh_calendar_data()).pack(side="left", padx=(0, 15))

        ctk.CTkLabel(weeks_frame, text="Total Weeks:", text_color="gray70", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 5))
        self.weeks_var = ctk.StringVar(value=str(self.settings.get("weeks_to_show", 3)))
        ctk.CTkOptionMenu(weeks_frame, values=["1", "2", "3", "4", "5"], height=28, width=60, fg_color=GLASS_CARD, button_color=GLASS_EDGE, variable=self.weeks_var, command=lambda e: self.refresh_calendar_data()).pack(side="left")

        self.tv_header_frame = ctk.CTkFrame(self.tab_calendar, fg_color="transparent")
        self.tv_header_frame.grid(row=1, column=0, sticky="ew", padx=15)

        self.tv_scroll = ctk.CTkScrollableFrame(self.tab_calendar, fg_color="transparent")
        self.tv_scroll.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 5))

    def refresh_calendar_data(self):
        self.ui_queue.put(self.build_calendar_ui)

    def build_calendar_ui(self):
        self.calendar_generation += 1
        current_gen = self.calendar_generation

        for w in self.tv_header_frame.winfo_children():
            w.destroy()
        for w in self.tv_scroll.winfo_children():
            w.destroy()
        self.calendar_day_frames.clear()

        pw = int(self.prev_weeks_var.get())
        tw = int(self.weeks_var.get())
        self.settings["prev_weeks_to_show"] = pw
        self.settings["weeks_to_show"] = tw
        self.save_settings()

        for i in range(7):
            self.tv_header_frame.grid_columnconfigure(i, weight=1, uniform="day")
            self.tv_scroll.grid_columnconfigure(i, weight=1, uniform="day")

        today = datetime.now().date()
        start_of_current_week = today - timedelta(days=today.weekday())
        start = start_of_current_week - timedelta(days=7 * pw)

        days_of_week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for i, d in enumerate(days_of_week):
            ctk.CTkLabel(self.tv_header_frame, text=d, text_color="gray60", font=ctk.CTkFont(weight="bold")).grid(row=0, column=i)

        with self.data_lock:
            shows_snapshot = {sid: dict(d_dict) for sid, d_dict in self.followed_shows.items()}
            episodes_snapshot = {sid: list(eps) for sid, eps in self.episodes_cache.items()}

        schedule = {}
        for sid, d_dict in shows_snapshot.items():
            for ep in episodes_snapshot.get(sid, []):
                ad = ep.get('airdate')
                if ad:
                    if ad not in schedule:
                        schedule[ad] = []
                    schedule[ad].append({
                        "media_id": sid,
                        "show": d_dict['name'],
                        "episode": f"S{ep.get('season',1):02d}E{ep.get('number',1):02d}",
                        "title": ep.get('name', '')
                    })

        max_daily = 0
        for week in range(tw):
            self.tv_scroll.grid_rowconfigure(week, weight=0)
            for day in range(7):
                curr = start + timedelta(days=(week * 7) + day)
                d_str = curr.strftime("%Y-%m-%d")
                max_daily = max(max_daily, len(schedule.get(d_str, [])))

                cell = ctk.CTkFrame(self.tv_scroll, corner_radius=6, fg_color="#182133" if curr==today else "#121620", border_width=1, border_color="#1F3B60" if curr==today else "#1C222E")
                cell.grid(row=week, column=day, sticky="nsew", padx=3, pady=3)
                cell.grid_columnconfigure(0, weight=1)
                self.calendar_day_frames[d_str] = cell

                hdr = ctk.CTkFrame(cell, fg_color="transparent")
                hdr.pack(fill="x", padx=4, pady=2)
                ctk.CTkLabel(hdr, text=curr.strftime("%b %d"), text_color=ACCENT_COLOR if curr==today else "gray60", font=ctk.CTkFont(size=11, weight="bold")).pack(side="right")

                start_of_prev_week = start_of_current_week - timedelta(days=7)
                end_of_prev_week = start_of_current_week - timedelta(days=1)
                if curr < start_of_prev_week:
                    is_prev_week = True
                elif start_of_prev_week <= curr <= end_of_prev_week:
                    is_prev_week = today.weekday() >= 2
                else:
                    is_prev_week = False

                for data in schedule.get(d_str, []):
                    self.create_calendar_card(cell, data, curr, show_poster=not is_prev_week)

        self.background_executor.submit(
            self._fetch_and_render_unfollowed, start, tw * 7, schedule, max_daily, current_gen
        )

    def _fetch_and_render_unfollowed(self, start_date, total_days, schedule, max_daily_tracked, generation):
        target = max(3, max_daily_tracked)
        for i in range(total_days):
            if generation != self.calendar_generation:
                return
            curr = start_date + timedelta(days=i)
            d_str = curr.strftime("%Y-%m-%d")
            tracked = len(schedule.get(d_str, []))
            needed = target - tracked
            if needed <= 0:
                continue

            if d_str not in self.unfollowed_cache:
                try:
                    r = http_session.get(f"https://api.tvmaze.com/schedule?date={d_str}", timeout=5)
                    if r.status_code == 200:
                        valid = [item for item in r.json() if item.get('show',{}).get('type') in ['Scripted', 'Animation'] and item.get('show',{}).get('language') == 'English' and item.get('show',{}).get('weight', 0) > 40]
                        valid.sort(key=lambda x: x['show'].get('weight', 0), reverse=True)
                        self.unfollowed_cache[d_str] = valid[:15]
                except:
                    pass

            items = self.unfollowed_cache.get(d_str, [])
            if items:
                self.ui_queue.put(lambda d=d_str, it=items, n=needed, g=generation: self._render_unfollowed_cells(d, it, n, g))

    def _render_unfollowed_cells(self, date_str, items, needed, generation):
        if generation != self.calendar_generation:
            return
        cell = self.calendar_day_frames.get(date_str)
        if not cell or not cell.winfo_exists():
            return
        with self.data_lock:
            followed = set(self.followed_shows.keys())
        count = 0
        for item in items:
            show = item.get('show')
            if not show or str(show['id']) in followed:
                continue
            if count >= needed:
                break
            count += 1

            card = ctk.CTkFrame(cell, fg_color="#14121A", border_color="#2A2438", border_width=1, corner_radius=8, height=55)
            card.pack(fill="x", padx=6, pady=4)
            card.pack_propagate(False)

            inf = ctk.CTkFrame(card, fg_color="transparent")
            inf.pack(side="left", fill="both", expand=True, padx=8, pady=4)
            ctk.CTkLabel(inf, text=show.get('name',''), font=ctk.CTkFont(size=11, weight="bold"), text_color="gray50", anchor="w").pack(anchor="w")

            btm = ctk.CTkFrame(inf, fg_color="transparent")
            btm.pack(side="bottom", fill="x")
            ctk.CTkLabel(btm, text=f"S{item.get('season',1):02d}E{item.get('number',1):02d}", font=ctk.CTkFont(size=9), text_color="gray40").pack(side="left")

            btn = ctk.CTkButton(btm, text="+ Track", height=16, width=45, font=ctk.CTkFont(size=9), fg_color="transparent", border_width=1, border_color="gray30", text_color="gray60", hover_color="#2A2438")
            btn.configure(command=lambda sid=str(show['id']), name=show.get('name',''), b=btn: self.toggle_follow(sid, name, True, b))
            btn.pack(side="right")

    def create_calendar_card(self, parent, data, release_date, show_poster=True):
        card = ctk.CTkFrame(parent, fg_color=GLASS_CARD, border_color=GLASS_EDGE, border_width=1, corner_radius=8, height=120 if show_poster else 46)
        card.pack(fill="x", padx=6, pady=4)
        card.pack_propagate(False)

        future = release_date > datetime.now().date()
        btn_text = "Not Aired" if future else "Search"
        btn_color = "gray25" if future else ACCENT_COLOR

        qual = self.settings.get("quality", "1080p")
        data['qual_str'] = qual

        if show_poster:
            pf = ctk.CTkFrame(card, width=68, height=100, fg_color="gray20", corner_radius=5)
            pf.pack(side="left", padx=10, pady=10)
            pf.pack_propagate(False)
            lbl = ctk.CTkLabel(pf, text="")
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            data['poster_lbl'] = lbl

            inf = ctk.CTkFrame(card, fg_color="transparent")
            inf.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)
            ctk.CTkLabel(inf, text=data['show'], font=ctk.CTkFont(size=12, weight="bold"), text_color="white", wraplength=120, justify="left").pack(anchor="w")
            ctk.CTkLabel(inf, text=data['episode'], font=ctk.CTkFont(size=9), text_color="#A4B2C6").pack(anchor="w")

            btn = ctk.CTkButton(inf, text=btn_text, height=22, font=ctk.CTkFont(size=10, weight="bold"), fg_color=btn_color, hover_color=ACCENT_HOVER, corner_radius=4, border_width=0)
            btn.configure(command=lambda d=data: self.open_manual_search(d))
            if future:
                btn.configure(state="disabled", hover_color="gray25")
            btn.pack(side="bottom", fill="x")
            data['button_ref'] = btn

            dots = ctk.CTkLabel(card, text="⋮", width=20, height=30, font=ctk.CTkFont(size=18, weight="bold"), text_color="#A4B2C6", cursor="hand2")
            dots.place(relx=1.0, x=-5, y=5, anchor="ne")
            dots.bind("<Button-1>", lambda e, d=data: self.open_manual_search(d))
            dots.bind("<Enter>", lambda e, w=dots: w.configure(text_color="white"))
            dots.bind("<Leave>", lambda e, w=dots: w.configure(text_color="#A4B2C6"))
            dots.lift()

            def load():
                with self.data_lock:
                    meta = self.followed_shows.get(data.get('media_id'), {}).get('metadata', {})
                url = meta.get('image', {}).get('medium') if meta else None
                if url:
                    img = self.fetch_pil_image(url)
                    if img:
                        self.ui_queue.put(lambda: lbl.winfo_exists() and lbl.configure(
                            image=ctk.CTkImage(light_image=ImageOps.fit(img, (68,100)), dark_image=ImageOps.fit(img, (68,100)), size=(68,100)),
                            text=""
                        ))
            self.background_executor.submit(load)
        else:
            inf = ctk.CTkFrame(card, fg_color="transparent")
            inf.pack(fill="both", expand=True, padx=(10, 30), pady=8)

            title_str = f"{data['show']} - {data['episode']}"
            if len(title_str) > 35:
                title_str = title_str[:32] + "..."
            ctk.CTkLabel(inf, text=title_str, font=ctk.CTkFont(size=12, weight="bold"), text_color="white", justify="left").pack(side="left", anchor="w")

            dots = ctk.CTkLabel(card, text="⋮", width=20, height=30, font=ctk.CTkFont(size=18, weight="bold"), text_color="#A4B2C6", cursor="hand2")
            dots.place(relx=1.0, x=-10, rely=0.5, anchor="e")
            dots.bind("<Button-1>", lambda e, d=data: self.open_manual_search(d))
            dots.bind("<Enter>", lambda e, w=dots: w.configure(text_color="white"))
            dots.bind("<Leave>", lambda e, w=dots: w.configure(text_color="#A4B2C6"))
            dots.lift()

            data['button_ref'] = None

    # ==========================================
    # MOVIE RELEASES TAB
    # ==========================================
    def get_relative_time_text(self, target_date):
        today = datetime.now().date()
        start_of_current_week = today - timedelta(days=today.weekday())
        start_of_target_week = target_date - timedelta(days=target_date.weekday())
        weeks_diff = (start_of_target_week - start_of_current_week).days // 7
        if weeks_diff == 0:
            return "(this week)", True
        elif weeks_diff == 1:
            return "(next week)", True
        elif weeks_diff == -1:
            return "(last week)", False
        elif weeks_diff > 1:
            return f"(in {weeks_diff} weeks)", True
        else:
            return f"({abs(weeks_diff)} weeks ago)", False

    def setup_releases_tab(self):
        self.tab_releases.grid_columnconfigure(0, weight=1)
        self.tab_releases.grid_rowconfigure(2, weight=1)

        nav = ctk.CTkFrame(self.tab_releases, fg_color="transparent")
        nav.grid(row=0, column=0, sticky="ew", pady=(10, 5))
        nav.grid_columnconfigure(0, weight=1)
        nav.grid_columnconfigure(1, weight=1)
        nav.grid_columnconfigure(2, weight=1)

        self.btn_prev_m = ctk.CTkButton(nav, text="< Previous Month", fg_color="transparent", text_color="#5D8AA8", hover_color=GLASS_CARD, font=ctk.CTkFont(weight="bold"), command=lambda: self.change_movie_month(-1))
        self.btn_prev_m.grid(row=0, column=0, sticky="w", padx=20)

        mid_c = ctk.CTkFrame(nav, fg_color="transparent")
        mid_c.grid(row=0, column=1)
        self.lbl_m_title = ctk.CTkLabel(mid_c, text="", font=ctk.CTkFont(size=18, weight="bold"), text_color="white")
        self.lbl_m_title.pack()

        self.movie_filter_var = ctk.StringVar(value="Digital releases")
        self.movie_filter_menu = ctk.CTkOptionMenu(mid_c, values=["Digital releases", "Theatrical release", "All"], variable=self.movie_filter_var, command=lambda e: self.build_movie_releases_ui(), fg_color=GLASS_CARD, button_color=GLASS_EDGE, height=24)
        self.movie_filter_menu.pack(pady=(5, 0))

        self.btn_next_m = ctk.CTkButton(nav, text="Next Month >", fg_color="transparent", text_color="#5D8AA8", hover_color=GLASS_CARD, font=ctk.CTkFont(weight="bold"), command=lambda: self.change_movie_month(1))
        self.btn_next_m.grid(row=0, column=2, sticky="e", padx=20)

        search_frame = ctk.CTkFrame(self.tab_releases, fg_color="transparent")
        search_frame.grid(row=1, column=0, padx=15, pady=(0, 5), sticky="ew")
        search_frame.grid_columnconfigure(0, weight=1)

        self.movie_search_entry = ctk.CTkEntry(search_frame, placeholder_text="Search releases...", placeholder_text_color="#A4B2C6", height=28, fg_color=BG_BASE, border_color=GLASS_EDGE)
        self.movie_search_entry.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        self.movie_search_entry.bind("<Return>", lambda e: self.filter_movie_releases())
        self.movie_search_btn = ctk.CTkButton(search_frame, text="Search", width=80, height=28, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, command=self.filter_movie_releases)
        self.movie_search_btn.grid(row=0, column=1)

        self.releases_scroll = ctk.CTkScrollableFrame(self.tab_releases, fg_color="transparent")
        self.releases_scroll.grid(row=2, column=0, sticky="nsew", padx=15, pady=5)

    def change_movie_month(self, delta):
        m = self.current_movie_month.month - 1 + delta
        y = self.current_movie_month.year + m // 12
        self.current_movie_month = date(y, (m % 12) + 1, 1)
        if hasattr(self, 'movie_search_entry') and self.movie_search_entry.winfo_exists():
            self.movie_search_entry.delete(0, 'end')
        self.build_movie_releases_ui()

    def _get_tmdb_release_type(self, filter_str):
        if filter_str == "Digital releases":
            return 4
        elif filter_str == "Theatrical release":
            return 3
        else:
            return None

    def build_movie_releases_ui(self):
        for w in self.releases_scroll.winfo_children():
            w.destroy()
        self.lbl_m_title.configure(text=f"{self.current_movie_month.strftime('%B %Y')}")

        loader = self.show_loading(self.releases_scroll)

        def fetch_tmdb_releases():
            api_key = self.settings.get("tmdb_api_key", "").strip()
            if not api_key:
                self.ui_queue.put(lambda: self._render_movie_weeks(
                    {}, loader, failed=True, error_msg="TMDB API key is not set. Please add it in Settings."
                ))
                return

            month_start = self.current_movie_month.replace(day=1)
            next_month = month_start + timedelta(days=32)
            month_end = next_month.replace(day=1) - timedelta(days=1)

            release_type = self._get_tmdb_release_type(self.movie_filter_var.get())

            base_url = "https://api.themoviedb.org/3/discover/movie"
            params = {
                "api_key": api_key,
                "language": "en-US",
                "sort_by": "popularity.desc",
                "primary_release_date.gte": month_start.strftime("%Y-%m-%d"),
                "primary_release_date.lte": month_end.strftime("%Y-%m-%d"),
                "page": 1,
            }
            if release_type is not None:
                params["with_release_type"] = release_type

            all_results = []
            try:
                for page in range(1, 6):
                    params["page"] = page
                    resp = http_session.get(base_url, params=params, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()
                    results = data.get("results", [])
                    all_results.extend(results)
                    if page >= data.get("total_pages", 1):
                        break
                    if len(all_results) >= 100:
                        break
            except Exception as e:
                logger.error(f"TMDB API error: {e}")
                self.ui_queue.put(lambda: self._render_movie_weeks(
                    {}, loader, failed=True, error_msg=f"TMDB API request failed: {str(e)}"
                ))
                return

            if not all_results:
                if self.movie_filter_var.get() != "All":
                    self.movie_filter_var.set("All")
                    self.build_movie_releases_ui()
                    return
                self.ui_queue.put(lambda: self._render_movie_weeks(
                    {}, loader, failed=False, error_msg="No releases found for this month."
                ))
                return

            week_buckets = {}
            for movie in all_results:
                release_date_str = movie.get("release_date", "")
                if not release_date_str:
                    continue
                try:
                    release_date = datetime.strptime(release_date_str, "%Y-%m-%d").date()
                except:
                    continue
                start_w = release_date - timedelta(days=release_date.weekday())
                if start_w not in week_buckets:
                    week_buckets[start_w] = []
                poster_url = None
                poster_path = movie.get("poster_path")
                if poster_path:
                    poster_url = f"https://image.tmdb.org/t/p/w185{poster_path}"
                week_buckets[start_w].append({
                    "title": movie.get("title", "Unknown"),
                    "date": release_date,
                    "desc": movie.get("overview", "")[:160],
                    "score": movie.get("vote_average", "N/A"),
                    "rating": "NR",
                    "poster_url": poster_url,
                    "popularity": movie.get("popularity", 0)
                })

            self.current_movie_buckets = week_buckets
            self.ui_queue.put(lambda: self.lbl_m_title.configure(
                text=f"{self.current_movie_month.strftime('%B %Y')}"
            ))
            self.ui_queue.put(lambda: self._render_movie_weeks(week_buckets, loader))

        self.background_executor.submit(fetch_tmdb_releases)

    def filter_movie_releases(self):
        if self.current_movie_buckets is not None:
            self._render_movie_weeks(self.current_movie_buckets, None)

    def _render_movie_weeks(self, buckets, loader=None, failed=False, error_msg=""):
        if loader:
            self.hide_loading(loader)

        if failed:
            ctk.CTkLabel(self.releases_scroll, text=f"❌ Failed to load movie releases:\n{error_msg}\n\nCheck your internet connection or API key.", text_color="#C0392B", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=80)
            return

        if not buckets:
            ctk.CTkLabel(self.releases_scroll, text="No movie releases found for this month.", text_color="gray50", font=ctk.CTkFont(size=13)).pack(pady=80)
            return

        for w in self.releases_scroll.winfo_children():
            w.destroy()

        search_query = self.movie_search_entry.get().strip().lower()
        any_week_rendered = False

        for start_w in sorted(buckets.keys()):
            week_movies = buckets[start_w]
            if search_query:
                week_movies = [m for m in week_movies if search_query in m['title'].lower()]

            if not week_movies:
                continue

            week_movies.sort(key=lambda x: x.get('popularity', 0), reverse=True)

            max_movies = 12
            n = min(max_movies, len(week_movies))
            if n % 2 != 0:
                n -= 1
            if n <= 0:
                continue
            movies_limited = week_movies[:n]

            any_week_rendered = True

            end_w = start_w + timedelta(days=6)
            y_num, w_num, _ = start_w.isocalendar()

            if start_w.month == end_w.month:
                date_range = f"{start_w.strftime('%B %d')} - {end_w.strftime('%d')}"
            else:
                date_range = f"{start_w.strftime('%B %d')} - {end_w.strftime('%B %d')}"

            rel_text, is_highlighted = self.get_relative_time_text(start_w)
            text_color = "#D32F2F" if is_highlighted else "#A4B2C6"

            f = ctk.CTkFrame(self.releases_scroll, fg_color="transparent")
            f.pack(fill="x", pady=15)

            hdr_text = f"■ Week {w_num}: {date_range} {rel_text}"
            ctk.CTkLabel(f, text=hdr_text, font=ctk.CTkFont(size=16, weight="bold"), text_color=text_color).pack(anchor="w", padx=10, pady=(0, 10))

            grid = ctk.CTkFrame(f, fg_color="transparent")
            grid.pack(anchor="center")

            for idx, movie in enumerate(movies_limited):
                row = idx // 6
                col = idx % 6
                self.create_movie_horizontal_card(grid, movie, row, col)

        if not any_week_rendered:
            ctk.CTkLabel(self.releases_scroll, text="No releases match your search.", text_color="gray50", font=ctk.CTkFont(size=13)).pack(pady=80)

    def create_movie_horizontal_card(self, parent, data, row, col):
        card = ctk.CTkFrame(parent, fg_color=GLASS_CARD, border_color=GLASS_EDGE, border_width=1, corner_radius=8, height=120)
        card.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
        card.grid_propagate(False)
        card.pack_propagate(False)

        pf = ctk.CTkFrame(card, width=68, height=100, fg_color="gray20", corner_radius=5)
        pf.pack(side="left", padx=10, pady=10)
        pf.pack_propagate(False)
        poster_lbl = ctk.CTkLabel(pf, text="")
        poster_lbl.place(relx=0.5, rely=0.5, anchor="center")

        inf = ctk.CTkFrame(card, fg_color="transparent")
        inf.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)

        title = data['title']
        if len(title) > 25:
            title = title[:22] + "..."
        ctk.CTkLabel(inf, text=title, font=ctk.CTkFont(size=12, weight="bold"), text_color="white", wraplength=120, justify="left").pack(anchor="w")

        score_text = f"★ {data.get('score', 'N/A')} | {data.get('rating', 'NR')}"
        ctk.CTkLabel(inf, text=score_text, font=ctk.CTkFont(size=9), text_color="#A4B2C6").pack(anchor="w", pady=2)

        imdb_search_url = f"https://www.imdb.com/find?q={urllib.parse.quote(data['title'])}"
        imdb_lbl = ctk.CTkLabel(inf, text="IMDb", text_color="#5D8AA8", font=ctk.CTkFont(size=9, underline=True), cursor="hand2")
        imdb_lbl.pack(anchor="w", pady=(0, 2))
        imdb_lbl.bind("<Button-1>", lambda e, url=imdb_search_url: webbrowser.open(url))

        release_year = data['date'].year if data.get('date') else ""
        if release_year:
            search_query = f"{data['title']} {release_year}"
        else:
            search_query = data['title']

        btn = ctk.CTkButton(inf, text="Search Film", height=22, font=ctk.CTkFont(size=10, weight="bold"), fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, border_width=0, corner_radius=4)
        btn.configure(command=lambda q=search_query: self.open_generic_manual_search_with_query(q))
        btn.pack(side="bottom", fill="x")

        if data.get('poster_url'):
            def load_img():
                pil_img = self.fetch_pil_image(data['poster_url'])
                if pil_img:
                    img = ImageOps.fit(pil_img, (68, 100), Image.Resampling.LANCZOS)
                    self.ui_queue.put(lambda: poster_lbl.winfo_exists() and poster_lbl.configure(
                        image=ctk.CTkImage(light_image=img, dark_image=img, size=(68, 100)),
                        text=""
                    ))
            self.background_executor.submit(load_img)

    def open_generic_manual_search_with_query(self, query):
        self.open_manual_search({'show': query, 'episode': '', 'title': 'Manual Action', 'show_id': None, 'qual_str': ''})

    # ==========================================
    # LIBRARY TAB
    # ==========================================
    def setup_library_tab(self):
        self.tab_library.grid_columnconfigure(0, weight=1)
        self.tab_library.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=15, pady=5, sticky="ew")

        self.lbl_lib_count = ctk.CTkLabel(hdr, text="Tracked Library", font=ctk.CTkFont(size=18, weight="bold"), text_color="white")
        self.lbl_lib_count.pack(side="left")

        self.btn_import = ctk.CTkButton(hdr, text="Import Shows", width=120, height=30, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, command=self.import_shows_dialog)
        self.btn_import.pack(side="left", padx=(20, 10))

        self.btn_cleanup = ctk.CTkButton(hdr, text="Cleanup Ended", width=120, height=30, fg_color="#C0392B", hover_color="#922B21", command=self.cleanup_ended_shows)
        self.btn_cleanup.pack(side="left")

        self.library_scroll = ctk.CTkScrollableFrame(self.tab_library, fg_color="transparent")
        self.library_scroll.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 5))

    def import_shows_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Import Shows")
        dialog.geometry("500x400")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(fg_color=BG_BASE)

        lbl = ctk.CTkLabel(dialog, text="Paste show names (one per line):", font=ctk.CTkFont(size=12))
        lbl.pack(pady=(20, 5))

        textbox = ctk.CTkTextbox(dialog, width=460, height=250, fg_color=GLASS_CARD, border_color=GLASS_EDGE, border_width=1)
        textbox.pack(pady=10)

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", pady=10)

        def do_import():
            content = textbox.get("1.0", "end-1c").strip()
            if not content:
                self._show_message("Import", "No show names entered.")
                dialog.destroy()
                return
            shows = [line.strip() for line in content.split('\n') if line.strip()]
            dialog.destroy()
            self._import_show_list(shows)

        ctk.CTkButton(btn_frame, text="Import", width=100, height=30, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, command=do_import).pack(side="right", padx=10)
        ctk.CTkButton(btn_frame, text="Cancel", width=100, height=30, fg_color="transparent", hover_color=GLASS_CARD, command=dialog.destroy).pack(side="right")

    def _import_show_list(self, shows):
        def import_task():
            added = 0
            failed = 0
            for show in shows:
                if show.isdigit():
                    sid = show
                    name = None
                    try:
                        res = http_session.get(f"https://api.tvmaze.com/shows/{sid}", timeout=5)
                        if res.status_code == 200:
                            data = res.json()
                            name = data.get('name')
                    except:
                        pass
                    if not name:
                        failed += 1
                        continue
                else:
                    try:
                        res = http_session.get(f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(show)}", timeout=5)
                        if res.status_code == 200 and res.json():
                            data = res.json()[0]
                            sid = str(data['show']['id'])
                            name = data['show']['name']
                        else:
                            failed += 1
                            continue
                    except:
                        failed += 1
                        continue

                with self.data_lock:
                    if sid not in self.followed_shows:
                        self.followed_shows[sid] = {"name": name, "metadata": None}
                        added += 1
                time.sleep(0.2)

            self.save_data()
            self.mark_caches_dirty()
            self.maybe_save_caches()
            self.start_background_library_sync()
            self.ui_queue.put(self.refresh_library_list)
            self.ui_queue.put(self.refresh_calendar_data)
            self.ui_queue.put(lambda: self._show_message("Import Complete", f"Added {added} shows. Failed: {failed}"))

        self.background_executor.submit(import_task)

    def cleanup_ended_shows(self):
        def cleanup_task():
            with self.data_lock:
                to_remove = []
                for sid, data in self.followed_shows.items():
                    meta = data.get('metadata')
                    if meta and meta.get('status') == 'Ended':
                        to_remove.append(sid)
                for sid in to_remove:
                    del self.followed_shows[sid]
                    if sid in self.episodes_cache:
                        del self.episodes_cache[sid]
                removed = len(to_remove)
            self.save_data()
            self.mark_caches_dirty()
            self.maybe_save_caches()
            self.ui_queue.put(self.refresh_library_list)
            self.ui_queue.put(self.refresh_calendar_data)
            self.ui_queue.put(lambda: self._show_message("Cleanup Complete", f"Removed {removed} ended shows."))

        self.background_executor.submit(cleanup_task)

    def _show_message(self, title, message):
        popup = ctk.CTkToplevel(self)
        popup.title(title)
        popup.geometry("400x150")
        popup.transient(self)
        popup.grab_set()
        popup.configure(fg_color=BG_BASE)
        ctk.CTkLabel(popup, text=message, wraplength=350).pack(pady=30)
        ctk.CTkButton(popup, text="OK", command=popup.destroy).pack()

    def refresh_library_list(self):
        with self.data_lock:
            shows = dict(self.followed_shows)
        self.lbl_lib_count.configure(text=f"Tracked TV Shows ({len(shows)})")
        for w in self.library_scroll.winfo_children():
            w.destroy()

        items = [s.get("metadata") if s.get("metadata") else {"id": k, "name": s["name"]} for k, s in shows.items()]
        self.render_show_grid(self.library_scroll, items, is_library=True)

    # ==========================================
    # SHARED GRID BUILDER
    # ==========================================
    def render_show_grid(self, parent, data, is_library=False, horizontal_rail=False):
        if not horizontal_rail:
            for i in range(6):
                parent.grid_columnconfigure(i, weight=1, uniform="g_col")

        row, col = 0, 0
        for item in data:
            card = ctk.CTkFrame(parent, fg_color=GLASS_CARD, border_color=GLASS_EDGE, border_width=1, corner_radius=8, height=120)
            if horizontal_rail:
                card.configure(width=260)
                card.pack(side="left", padx=6, pady=4)
            else:
                card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
            card.grid_propagate(False)
            card.pack_propagate(False)

            pf = ctk.CTkFrame(card, width=68, height=100, fg_color="gray20", corner_radius=5)
            pf.pack(side="left", padx=8, pady=10)
            pf.pack_propagate(False)
            lbl = ctk.CTkLabel(pf, text="")
            lbl.place(relx=0.5, rely=0.5, anchor="center")

            inf = ctk.CTkFrame(card, fg_color="transparent")
            inf.pack(side="left", fill="both", expand=True, padx=5, pady=10)

            title = item.get('name', 'Unknown')
            if len(title) > 20:
                title = title[:17] + "..."
            ctk.CTkLabel(inf, text=title, font=ctk.CTkFont(size=12, weight="bold"), text_color="white", anchor="w").pack(anchor="w")
            ctk.CTkLabel(inf, text=f"Status: {item.get('status','Unknown')}", font=ctk.CTkFont(size=9), text_color="gray50").pack(anchor="w", pady=2)

            btm = ctk.CTkFrame(inf, fg_color="transparent")
            btm.pack(side="bottom", fill="x")

            sid = str(item.get('id', ''))
            if is_library:
                ubtn = ctk.CTkButton(btm, text="Drop", width=50, height=20, font=ctk.CTkFont(size=10, weight="bold"), fg_color="#C0392B", border_width=0, command=lambda s=sid, n=item.get('name'): self.toggle_follow(s, n, False, None))
                ubtn.pack(side="right")
            else:
                with self.data_lock:
                    tracked = sid in self.followed_shows
                tbtn = ctk.CTkButton(btm, text="Tracking" if tracked else "+ Track", height=20, font=ctk.CTkFont(size=10, weight="bold"), fg_color="transparent" if tracked else ACCENT_COLOR, state="disabled" if tracked else "normal", border_width=0, command=lambda s=sid, n=item.get('name'): self.toggle_follow(s, n, True, None))
                tbtn.pack(fill="x")

            def load_grid_poster(url, target_lbl=lbl):
                if url:
                    img = self.fetch_pil_image(url)
                    if img:
                        self.ui_queue.put(lambda: target_lbl.winfo_exists() and target_lbl.configure(
                            image=ctk.CTkImage(light_image=ImageOps.fit(img, (68,100)), dark_image=ImageOps.fit(img, (68,100)), size=(68,100)),
                            text=""
                        ))
            self.background_executor.submit(load_grid_poster, item.get('image', {}).get('medium') if item.get('image') else None)

            if not horizontal_rail:
                col += 1
                if col >= 6:
                    col = 0
                    row += 1

    def toggle_follow(self, sid, name, follow, btn):
        def _task():
            sid_str = str(sid)
            with self.data_lock:
                if follow:
                    self.followed_shows[sid_str] = {"name": name, "metadata": None}
                else:
                    if sid_str in self.followed_shows:
                        del self.followed_shows[sid_str]
                    if sid_str in self.episodes_cache:
                        del self.episodes_cache[sid_str]
            self.save_data()
            self.mark_caches_dirty()
            self.maybe_save_caches()

            self.ui_queue.put(self.refresh_library_list)
            self.ui_queue.put(self.refresh_calendar_data)
            self.start_background_library_sync()

        self.background_executor.submit(_task)

    # ==========================================
    # ADVANCED MANUAL DIALOGUE
    # ==========================================
    def open_manual_search(self, ep_data):
        popup = ctk.CTkToplevel(self)
        popup.title("Advanced Indexer Interrogation")
        w, h = 1000, 650
        sw, sh = popup.winfo_screenwidth(), popup.winfo_screenheight()
        popup.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        popup.transient(self)
        popup.configure(fg_color="#0D0D0D")
        popup.grab_set()

        popup.results_pool = []
        popup.results_lock = threading.Lock()
        popup.searching = False
        popup.sort_col = 'size'
        popup.sort_desc = True

        sf = ctk.CTkFrame(popup, fg_color=GLASS_CARD, border_width=1, border_color=GLASS_EDGE, corner_radius=8)
        sf.pack(fill="x", padx=15, pady=15)

        controls_frame = ctk.CTkFrame(sf, fg_color="transparent")
        controls_frame.pack(fill="x", padx=15, pady=(10, 0))

        status_frame = ctk.CTkFrame(controls_frame, fg_color="transparent")
        status_frame.pack(side="left")

        apibay_lbl = ctk.CTkLabel(status_frame, text="⏳ APIBay", text_color="yellow", font=("Consolas", 12, "bold"))
        apibay_lbl.pack(side="left", padx=(0, 15))
        tor_lbl = ctk.CTkLabel(status_frame, text="⏳ Torrentio", text_color="yellow", font=("Consolas", 12, "bold"))
        tor_lbl.pack(side="left", padx=(0, 15))
        eztv_lbl = ctk.CTkLabel(status_frame, text="⏳ EZTV", text_color="yellow", font=("Consolas", 12, "bold"))
        eztv_lbl.pack(side="left", padx=(0, 15))
        sol_lbl = ctk.CTkLabel(status_frame, text="⏳ Solid", text_color="yellow", font=("Consolas", 12, "bold"))
        sol_lbl.pack(side="left", padx=(0, 15))

        filter_frame = ctk.CTkFrame(controls_frame, fg_color="transparent")
        filter_frame.pack(side="right")

        quality_setting = self.settings.get("quality", "1080p")
        if quality_setting == "Any":
            init_qual = "Any Quality"
        elif quality_setting == "2160p (4K)":
            init_qual = "2160p"
        elif quality_setting == "x265/HEVC":
            init_qual = "x265"
        else:
            init_qual = quality_setting

        qual_var = ctk.StringVar(value=init_qual)
        size_var = ctk.StringVar(value="Any Size")
        src_var = ctk.StringVar(value="All Sources")

        def on_filter_change(*args):
            render_results()

        def clear_filters():
            qual_var.set("Any Quality")
            size_var.set("Any Size")
            src_var.set("All Sources")
            render_results()

        ctk.CTkOptionMenu(filter_frame, values=["Any Quality", "720p", "1080p", "2160p", "x265", "HEVC"], variable=qual_var, command=on_filter_change, width=110, height=28, fg_color=BG_BASE, button_color=GLASS_EDGE).pack(side="left", padx=(0, 10))
        ctk.CTkOptionMenu(filter_frame, values=["Any Size", "Under 1GB", "1GB - 3GB", "Over 3GB"], variable=size_var, command=on_filter_change, width=110, height=28, fg_color=BG_BASE, button_color=GLASS_EDGE).pack(side="left", padx=(0, 10))
        ctk.CTkOptionMenu(filter_frame, values=["All Sources", "APIBay", "Torrentio", "EZTV", "SolidTorrents"], variable=src_var, command=on_filter_change, width=110, height=28, fg_color=BG_BASE, button_color=GLASS_EDGE).pack(side="left")
        ctk.CTkButton(filter_frame, text="Clear", width=60, height=28, fg_color="#C0392B", hover_color="#922B21", font=ctk.CTkFont(weight="bold"), command=clear_filters).pack(side="left", padx=(10, 0))

        entry_f = ctk.CTkFrame(sf, fg_color="transparent")
        entry_f.pack(fill="x", padx=15, pady=15)

        ctk.CTkLabel(entry_f, text="> ", font=("Consolas", 14, "bold")).pack(side="left")
        search_v = ctk.StringVar(value=f"{ep_data['show']} {ep_data['episode']}".strip())
        inp = ctk.CTkEntry(entry_f, textvariable=search_v, font=("Consolas", 14), fg_color=BG_BASE, border_width=1, border_color=GLASS_EDGE)
        inp.pack(side="left", fill="x", expand=True)

        res_box = ctk.CTkFrame(popup, fg_color=GLASS_CARD, border_width=1, border_color=GLASS_EDGE, corner_radius=8)
        res_box.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        res_title = ctk.CTkLabel(res_box, text="Results", text_color=ACCENT_COLOR, font=("Consolas", 11, "bold"))
        res_title.place(x=10, y=-10)

        def set_grid_cols(frame):
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_columnconfigure(1, weight=0, minsize=100)
            frame.grid_columnconfigure(2, weight=0, minsize=110)
            frame.grid_columnconfigure(3, weight=0, minsize=60)
            frame.grid_columnconfigure(4, weight=0, minsize=80)

        header_frame = ctk.CTkFrame(res_box, fg_color="transparent", height=28)
        header_frame.pack(fill="x", padx=(10, 26), pady=(15, 0))
        set_grid_cols(header_frame)

        def set_sort(col):
            if popup.sort_col == col:
                popup.sort_desc = not popup.sort_desc
            else:
                popup.sort_col = col
                popup.sort_desc = True
            render_results()

        def make_hdr(parent, text, col_key, col_idx, anchor="w", width=None):
            btn = ctk.CTkButton(parent, text=text, height=24, width=width if width else 0, fg_color="transparent", hover_color="#202531", text_color=ACCENT_COLOR, font=("Consolas", 12, "bold"), anchor=anchor, command=lambda c=col_key: set_sort(c))
            btn.grid(row=0, column=col_idx, sticky="ew", padx=2)
            return btn

        hdr_name = make_hdr(header_frame, "Name", "name", 0, "w")
        hdr_size = make_hdr(header_frame, "Size", "size", 1, "e", 100)
        hdr_seed = make_hdr(header_frame, "Seed:Lech", "seeders", 2, "center", 110)
        hdr_src = make_hdr(header_frame, "Src", "source", 3, "center", 60)
        ctk.CTkLabel(header_frame, text="", width=80).grid(row=0, column=4)

        scroll = ctk.CTkScrollableFrame(res_box, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        def on_dl(btn_ref, row_r, row_s):
            btn_ref.configure(state="disabled")
            anim_colors = ["#2ECC71", "#27AE60", "#1E8449", "#145A32"]
            anim_texts = ["Grabbing", "Grabbing.", "Grabbing..", "Grabbing..."]
            def animate(step=0):
                if step < 10:
                    btn_ref.configure(
                        text=anim_texts[step % len(anim_texts)],
                        fg_color=anim_colors[step % len(anim_colors)]
                    )
                    popup.after(100, animate, step+1)
                else:
                    btn_ref.configure(text="✅ Done!", fg_color="#2FA572")
                    self.download_torrent_file(ep_data, {
                        'info_hash': row_r.get('info_hash', ''),
                        'magnet': row_r.get('magnet', ''),
                        'name': row_r.get('name', 'Unknown')
                    }, row_s)
                    cal_btn = ep_data.get('button_ref')
                    if cal_btn and cal_btn.winfo_exists():
                        cal_btn.configure(text="✅ Downloaded", fg_color="#2FA572", hover_color="#2FA572")
                    popup.after(400, popup.destroy)
            animate()

        def render_results():
            for w in scroll.winfo_children():
                w.destroy()

            def update_hdr_text(btn, base_text, col_key):
                indicator = " ▼" if popup.sort_col == col_key and popup.sort_desc else " ▲" if popup.sort_col == col_key else ""
                btn.configure(text=f"{base_text}{indicator}")

            update_hdr_text(hdr_name, "Name", "name")
            update_hdr_text(hdr_size, "Size", "size")
            update_hdr_text(hdr_seed, "Seed:Lech", "seeders")
            update_hdr_text(hdr_src, "Src", "source")

            filtered = []
            q_val = qual_var.get().lower()
            s_val = size_var.get()
            src_val = src_var.get().lower()

            with popup.results_lock:
                results_snapshot = list(popup.results_pool)

            for r in results_snapshot:
                if src_val != "all sources" and src_val not in r['source']:
                    continue
                name_lower = r['name'].lower()
                if q_val != "any quality" and q_val not in name_lower:
                    continue
                gb_size = r['size'] / (1024**3) if r['size'] else 0
                if s_val == "Under 1GB" and gb_size > 1.0:
                    continue
                if s_val == "1GB - 3GB" and (gb_size < 1.0 or gb_size > 3.0):
                    continue
                if s_val == "Over 3GB" and gb_size < 3.0:
                    continue
                filtered.append(r)

            filtered.sort(key=lambda x: x[popup.sort_col], reverse=popup.sort_desc)
            res_title.configure(text=f"Results ({len(filtered)})")

            if not filtered:
                ctk.CTkLabel(scroll, text="No matching torrents found.", text_color="gray50", font=("Consolas", 12)).pack(anchor="w", pady=10)
                return

            for idx, r in enumerate(filtered):
                size_str = self.format_size(r.get('size', 0))
                name = r.get('name', 'Unknown')
                seed = str(r.get('seeders', '0'))
                leech = str(r.get('leechers', '0'))
                src_label = r.get('source', 'unk')[:3].upper()

                row_frame = ctk.CTkFrame(scroll, fg_color="transparent")
                row_frame.pack(fill="x", pady=2)
                set_grid_cols(row_frame)

                ctk.CTkLabel(row_frame, text=name, font=("Consolas", 12), text_color="#A4B2C6", anchor="w").grid(row=0, column=0, sticky="w", padx=2)
                ctk.CTkLabel(row_frame, text=size_str, width=100, font=("Consolas", 12), text_color="#A4B2C6", anchor="e").grid(row=0, column=1, sticky="e", padx=2)
                ctk.CTkLabel(row_frame, text=f"{seed}:{leech}", width=110, font=("Consolas", 12), text_color="#A4B2C6", anchor="center").grid(row=0, column=2, sticky="ew", padx=2)
                ctk.CTkLabel(row_frame, text=src_label, width=60, font=("Consolas", 12), text_color="#A4B2C6", anchor="center").grid(row=0, column=3, sticky="ew", padx=2)
                btn_frame = ctk.CTkFrame(row_frame, fg_color="transparent", width=80)
                btn_frame.grid(row=0, column=4, sticky="e", padx=2)
                dl_btn = ctk.CTkButton(btn_frame, text="Grab", width=65, height=24, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, font=ctk.CTkFont(size=10, weight="bold"), corner_radius=4)
                dl_btn.configure(command=lambda br=dl_btn, rr=r, rs=size_str: on_dl(br, rr, rs))
                dl_btn.pack(side="right")

        def execute_manual_search():
            if popup.searching:
                return
            popup.searching = True
            inp.configure(state="disabled")
            popup.results_pool.clear()
            render_results()
            query = search_v.get().strip()

            res_title.configure(text=f"Searching APIs concurrently...")
            apibay_lbl.configure(text="⏳ APIBay", text_color="yellow")
            tor_lbl.configure(text="⏳ Torrentio", text_color="yellow")
            eztv_lbl.configure(text="⏳ EZTV", text_color="yellow")
            sol_lbl.configure(text="⏳ Solid", text_color="yellow")

            def fetch_apibay():
                try:
                    url = f"https://apibay.org/q.php?q={urllib.parse.quote(query)}"
                    res = http_session.get(url, timeout=10)
                    if res.status_code == 200:
                        data = res.json()
                        count = 0
                        if isinstance(data, list) and len(data) > 0 and data[0].get('id') != '0':
                            for r in data:
                                if self._safe_int(r.get('seeders', 0)) > 0:
                                    with popup.results_lock:
                                        popup.results_pool.append({
                                            'source': 'apibay', 'name': r.get('name', 'Unknown'), 'info_hash': r.get('info_hash', ''),
                                            'magnet': '', 'size': self._safe_int(r.get('size', 0)), 'seeders': self._safe_int(r.get('seeders', 0)), 'leechers': self._safe_int(r.get('leechers', 0))
                                        })
                                    count += 1
                        self.ui_queue.put(lambda: apibay_lbl.configure(text=f"✅ APIBay ({count})", text_color="#2FA572"))
                    else:
                        self.ui_queue.put(lambda: apibay_lbl.configure(text="❌ APIBay", text_color="#C0392B"))
                except Exception:
                    self.ui_queue.put(lambda: apibay_lbl.configure(text="❌ APIBay", text_color="#C0392B"))

            def fetch_torrentio():
                try:
                    match = re.search(r'S(\d+)E(\d+)', query, re.IGNORECASE)
                    if match:
                        season_num = int(match.group(1))
                        episode_num = int(match.group(2))
                    else:
                        match_ep = re.search(r'S(\d+)E(\d+)', ep_data.get('episode', ''), re.IGNORECASE)
                        season_num = int(match_ep.group(1)) if match_ep else 1
                        episode_num = int(match_ep.group(2)) if match_ep else 1

                    show_id = ep_data.get('show_id') or ep_data.get('media_id')
                    imdb_id = None
                    if show_id:
                        with self.data_lock:
                            show_data = self.followed_shows.get(str(show_id), {})
                            meta = show_data.get('metadata')
                            if meta and meta.get('externals'):
                                imdb_id = meta['externals'].get('imdb')
                        if not imdb_id:
                            try:
                                res_meta = http_session.get(f"https://api.tvmaze.com/shows/{show_id}?embed[]=externals", timeout=5)
                                if res_meta.status_code == 200:
                                    imdb_id = res_meta.json().get('externals', {}).get('imdb')
                                    if imdb_id:
                                        with self.data_lock:
                                            if str(show_id) in self.followed_shows:
                                                if 'metadata' not in self.followed_shows[str(show_id)] or not self.followed_shows[str(show_id)]['metadata']:
                                                    self.followed_shows[str(show_id)]['metadata'] = {}
                                                self.followed_shows[str(show_id)]['metadata']['externals'] = {'imdb': imdb_id}
                                                self.mark_caches_dirty()
                            except:
                                pass

                    if imdb_id:
                        if not imdb_id.startswith('tt'):
                            imdb_id = f"tt{imdb_id}"
                        url = f"https://torrentio.strem.fun/stream/series/{imdb_id}:{season_num}:{episode_num}.json"
                        res = http_session.get(url, timeout=10)
                        if res.status_code == 200:
                            count = 0
                            for s in res.json().get('streams', []):
                                title = s.get('title', '')
                                seed_match = re.search(r'👤\s*(\d+)', title)
                                seeders = int(seed_match.group(1)) if seed_match else 0
                                if seeders > 0:
                                    magnet = s.get('url', '')
                                    hash_match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', magnet, re.IGNORECASE)
                                    size_match = re.search(r'💾\s*([\d.]+)\s*([A-Za-z]+)', title)
                                    size_bytes = 0
                                    if size_match:
                                        val = float(size_match.group(1))
                                        unit = size_match.group(2).upper()
                                        if unit == 'GB':
                                            size_bytes = val * 1024**3
                                        elif unit == 'MB':
                                            size_bytes = val * 1024**2
                                        elif unit == 'KB':
                                            size_bytes = val * 1024

                                    with popup.results_lock:
                                        popup.results_pool.append({
                                            'source': 'torrentio', 'name': s.get('title', '').split('\n')[0],
                                            'info_hash': hash_match.group(1) if hash_match else "", 'magnet': magnet,
                                            'size': size_bytes, 'seeders': seeders, 'leechers': 0
                                        })
                                    count += 1
                            self.ui_queue.put(lambda: tor_lbl.configure(text=f"✅ Torrentio ({count})", text_color="#2FA572"))
                        else:
                            self.ui_queue.put(lambda: tor_lbl.configure(text="❌ Torrentio", text_color="#C0392B"))
                    else:
                        self.ui_queue.put(lambda: tor_lbl.configure(text="❌ No IMDB", text_color="#C0392B"))
                except Exception:
                    self.ui_queue.put(lambda: tor_lbl.configure(text="❌ Torrentio", text_color="#C0392B"))

            def fetch_eztv():
                try:
                    match = re.search(r'S(\d+)E(\d+)', query, re.IGNORECASE)
                    if match:
                        season_num = int(match.group(1))
                        episode_num = int(match.group(2))
                    else:
                        match_ep = re.search(r'S(\d+)E(\d+)', ep_data.get('episode', ''), re.IGNORECASE)
                        season_num = int(match_ep.group(1)) if match_ep else 1
                        episode_num = int(match_ep.group(2)) if match_ep else 1

                    show_id = ep_data.get('show_id') or ep_data.get('media_id')
                    imdb_id = None
                    if show_id:
                        with self.data_lock:
                            show_data = self.followed_shows.get(str(show_id), {})
                            meta = show_data.get('metadata')
                            if meta and meta.get('externals'):
                                imdb_id = meta['externals'].get('imdb')
                        if not imdb_id:
                            try:
                                res_meta = http_session.get(f"https://api.tvmaze.com/shows/{show_id}?embed[]=externals", timeout=5)
                                if res_meta.status_code == 200:
                                    imdb_id = res_meta.json().get('externals', {}).get('imdb')
                                    if imdb_id:
                                        with self.data_lock:
                                            if str(show_id) in self.followed_shows:
                                                if 'metadata' not in self.followed_shows[str(show_id)] or not self.followed_shows[str(show_id)]['metadata']:
                                                    self.followed_shows[str(show_id)]['metadata'] = {}
                                                self.followed_shows[str(show_id)]['metadata']['externals'] = {'imdb': imdb_id}
                                                self.mark_caches_dirty()
                            except:
                                pass

                    if imdb_id:
                        eztv_imdb = imdb_id.replace('tt', '')
                        url = f"https://eztv.re/api/get-torrents?imdb_id={eztv_imdb}"
                        res = http_session.get(url, timeout=10)
                        if res.status_code == 200:
                            count = 0
                            for t in res.json().get('torrents', []):
                                if str(t.get('season')) == str(season_num) and str(t.get('episode')) == str(episode_num):
                                    seeders = self._safe_int(t.get('seeds', 0))
                                    if seeders > 0:
                                        with popup.results_lock:
                                            popup.results_pool.append({
                                                'source': 'eztv', 'name': t.get('title', ''), 'info_hash': t.get('hash', ''),
                                                'magnet': t.get('magnet_url', ''), 'size': self._safe_int(t.get('size_bytes', 0)),
                                                'seeders': seeders, 'leechers': self._safe_int(t.get('peers', 0)) - seeders if t.get('peers') else 0
                                            })
                                        count += 1
                            self.ui_queue.put(lambda: eztv_lbl.configure(text=f"✅ EZTV ({count})", text_color="#2FA572"))
                        else:
                            self.ui_queue.put(lambda: eztv_lbl.configure(text="❌ EZTV", text_color="#C0392B"))
                    else:
                        self.ui_queue.put(lambda: eztv_lbl.configure(text="❌ No IMDB", text_color="#C0392B"))
                except Exception:
                    self.ui_queue.put(lambda: eztv_lbl.configure(text="❌ EZTV", text_color="#C0392B"))

            def fetch_solidtorrents():
                try:
                    url = f"https://solidtorrents.to/api/v1/search?q={urllib.parse.quote(query)}&category=Video"
                    res = http_session.get(url, timeout=10)
                    if res.status_code == 200:
                        data = res.json()
                        count = 0
                        for r in data.get('results', []):
                            seeders = self._safe_int(r.get('swarm', {}).get('seeders', 0))
                            if seeders > 0:
                                magnet = r.get('magnet', '')
                                info_hash_match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', magnet, re.IGNORECASE)
                                info_hash = info_hash_match.group(1) if info_hash_match else ""

                                with popup.results_lock:
                                    popup.results_pool.append({
                                        'source': 'solidtorrents', 'name': r.get('title', 'Unknown'), 'info_hash': info_hash,
                                        'magnet': magnet, 'size': self._safe_int(r.get('size', 0)), 'seeders': seeders,
                                        'leechers': self._safe_int(r.get('swarm', {}).get('leechers', 0))
                                    })
                                count += 1
                        self.ui_queue.put(lambda: sol_lbl.configure(text=f"✅ Solid ({count})", text_color="#2FA572"))
                    else:
                        self.ui_queue.put(lambda: sol_lbl.configure(text="❌ Solid", text_color="#C0392B"))
                except Exception:
                    self.ui_queue.put(lambda: sol_lbl.configure(text="❌ Solid", text_color="#C0392B"))

            def run_all_searches():
                threads = [
                    threading.Thread(target=fetch_apibay),
                    threading.Thread(target=fetch_torrentio),
                    threading.Thread(target=fetch_eztv),
                    threading.Thread(target=fetch_solidtorrents)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                popup.searching = False
                self.ui_queue.put(lambda: inp.configure(state="normal"))
                self.ui_queue.put(render_results)

            self.background_executor.submit(run_all_searches)

        inp.bind("<Return>", lambda e: execute_manual_search())
        execute_manual_search()

    # ==========================================
    # SETTINGS MODAL
    # ==========================================
    def open_settings_window(self):
        win = ctk.CTkToplevel(self)
        win.title("System Parameters")
        w, h = 600, 500
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        win.transient(self)
        win.grab_set()
        win.configure(fg_color=BG_BASE)

        c = ctk.CTkFrame(win, fg_color="transparent")
        c.pack(fill="both", expand=True, padx=25, pady=25)

        ctk.CTkLabel(c, text="System Configurations", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", pady=(0, 20))

        f1 = ctk.CTkFrame(c, fg_color="transparent")
        f1.pack(fill="x", pady=8)
        ctk.CTkLabel(f1, text="Preferred Quality Profile:", text_color="#A4B2C6").pack(side="left")
        self.quality_var = ctk.StringVar(value=self.settings.get("quality", "1080p"))
        ctk.CTkOptionMenu(f1, values=["Any", "720p", "1080p", "2160p", "x265/HEVC"], variable=self.quality_var, fg_color=GLASS_CARD, button_color=GLASS_EDGE).pack(side="right")

        f2 = ctk.CTkFrame(c, fg_color="transparent")
        f2.pack(fill="x", pady=8)
        ctk.CTkLabel(f2, text="Storage Output Directory:", text_color="#A4B2C6").pack(side="left")
        self.dl_dir_var = ctk.StringVar(value=self.settings.get("download_dir", TORRENTS_DIR))
        ctk.CTkEntry(f2, textvariable=self.dl_dir_var, width=220, fg_color=GLASS_CARD, border_color=GLASS_EDGE).pack(side="left", padx=10, expand=True, fill="x")
        ctk.CTkButton(f2, text="Browse", width=60, fg_color=ACCENT_COLOR, command=self.browse_directory).pack(side="right")

        f4 = ctk.CTkFrame(c, fg_color="transparent")
        f4.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(f4, text="TMDB API Key (for Movie Releases):", text_color="#A4B2C6").pack(side="left")
        self.tmdb_key_var = ctk.StringVar(value=self.settings.get("tmdb_api_key", ""))
        ctk.CTkEntry(f4, textvariable=self.tmdb_key_var, width=220, fg_color=GLASS_CARD, border_color=GLASS_EDGE).pack(side="left", padx=10, expand=True, fill="x")

        link_f = ctk.CTkFrame(c, fg_color="transparent")
        link_f.pack(fill="x", pady=(0, 8))
        link_lbl = ctk.CTkLabel(link_f, text="Get an API key here: https://www.themoviedb.org/settings/api", text_color="#5D8AA8", cursor="hand2", font=ctk.CTkFont(size=10, underline=True))
        link_lbl.pack(side="right", padx=10)
        link_lbl.bind("<Button-1>", lambda e: webbrowser.open("https://www.themoviedb.org/settings/api"))

        f5 = ctk.CTkFrame(c, fg_color="transparent")
        f5.pack(fill="x", pady=12)
        self.tg_json_var = ctk.BooleanVar(value=self.settings.get("create_torgrabber_json", True))
        ctk.CTkSwitch(f5, text="Compile TorGrabber context schema (.json descriptor meta)", variable=self.tg_json_var, progress_color=ACCENT_COLOR).pack(side="left")

        msg = ctk.CTkLabel(c, text="", text_color="#2FA572", font=ctk.CTkFont(size=12))
        msg.pack(side="bottom", pady=5)

        def save():
            self.settings["quality"] = self.quality_var.get()
            self.settings["download_dir"] = self.dl_dir_var.get()
            self.settings["tmdb_api_key"] = self.tmdb_key_var.get().strip()
            self.settings["create_torgrabber_json"] = self.tg_json_var.get()
            self.save_settings()
            msg.configure(text="Local preferences synced to disk successfully.")
            self.after(2000, win.destroy)

        ctk.CTkButton(c, text="Commit Parameters", height=32, font=ctk.CTkFont(weight="bold"), fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, border_width=0, command=save).pack(side="bottom", fill="x", pady=15)

    def browse_directory(self):
        d = filedialog.askdirectory()
        if d:
            self.dl_dir_var.set(d)

# ==========================================
# MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    try:
        app = TorGrabberApp()
        app.mainloop()
    except Exception as e:
        error_msg = traceback.format_exc()
        logger.critical(f"Unhandled exception:\n{error_msg}")
        try:
            import tkinter.messagebox as msg
            msg.showerror("TorGrabber Fatal Error",
                          f"The application crashed.\n\nPlease check the log file:\n{LOG_FILE}\n\nError:\n{str(e)}")
        except:
            pass
        print(f"Fatal error. See log: {LOG_FILE}")
        time.sleep(3)
        sys.exit(1)
