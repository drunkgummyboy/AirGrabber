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
LOG_FILE = os.path.join(SCRIPT_DIR, "AirGrabber.log")

if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
    try:
        os.remove(LOG_FILE)
    except:
        pass

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)
logger.info("=== AirGrabber startup ===")

# ==========================================
# VERSION & UPDATE CONFIG
# ==========================================
CURRENT_VERSION = "1.0.9"
REPO_OWNER = "drunkgummyboy"
REPO_NAME = "AirGrabber"
SCRIPT_FILENAME = "AirGrabber.py"

# ==========================================
# AUTO-INSTALL DEPENDENCIES
# ==========================================
def ensure_dependencies():
    required_packages = {
        "customtkinter": "customtkinter",
        "requests": "requests",
        "PIL": "Pillow",
        "cloudscraper": "cloudscraper",
    }
    for import_name, pip_name in required_packages.items():
        try:
            __import__(import_name)
            logger.debug(f"Dependency '{import_name}' already installed.")
        except ImportError:
            logger.info(f"Missing dependency '{pip_name}'. Installing now...")
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", pip_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
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
        tk.messagebox.showerror(
            "Missing Dependencies",
            f"Required module not found: {e}\n\n"
            "Please run the script from a terminal to see the full error.",
        )
        root.destroy()
    except:
        pass
    sys.exit(1)

# ==========================================
# GLOBAL HTTP SESSIONS
# ==========================================
http_session = requests.Session()
http_session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
)

try:
    scraper_session = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
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
    if not text:
        return "No summary available."
    return re.sub(re.compile("<.*?>"), "", text)

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
class AirGrabber(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("AirGrabber - Track your shows. Grab your movies.")
        self.configure(fg_color=BG_BASE)

        window_width = 1650
        window_height = 900
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        self.geometry(
            f"{window_width}x{window_height}+{int((screen_width / 2) - (window_width / 2))}+{int((screen_height / 2) - (window_height / 2))}"
        )

        self.data_lock = threading.RLock()
        self.network_executor = ThreadPoolExecutor(max_workers=6)
        self.io_executor = ThreadPoolExecutor(max_workers=4)

        self.settings = self.load_settings()
        self.followed_shows = self.load_data()
        self.history = self.load_history()
        self.episodes_cache = self.load_json_dict(EPISODES_FILE)

        self.image_cache = LRUImageCache(maxsize=200)
        self.unfollowed_cache = {}
        self.calendar_day_frames = {}
        self.calendar_generation = 0
        self._cache_dirty = False
        self._sync_running = False

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

        self.logo_lbl = ctk.CTkLabel(
            self.top_bar,
            text="AirGrabber",
            font=ctk.CTkFont(size=24, weight="bold"),
            text_color=ACCENT_COLOR,
        )
        self.logo_lbl.grid(row=0, column=0, sticky="w", padx=(10, 0))

        self.global_media_var = ctk.StringVar(value="TV Shows")
        self.toggle_frame = ctk.CTkFrame(
            self.top_bar,
            fg_color=GLASS_CARD,
            corner_radius=15,
            border_width=1,
            border_color=GLASS_EDGE,
        )
        self.toggle_frame.grid(row=0, column=1)

        self.btn_tv = ctk.CTkButton(
            self.toggle_frame,
            text="",
            width=160,
            height=100,
            fg_color=ACCENT_COLOR,
            hover_color=ACCENT_HOVER,
            corner_radius=12,
            border_width=0,
            font=ctk.CTkFont(weight="bold", size=16),
            command=lambda: self.set_global_mode("TV Shows"),
        )
        self.btn_tv.grid(row=0, column=0, padx=4, pady=4)

        self.btn_movie = ctk.CTkButton(
            self.toggle_frame,
            text="",
            width=160,
            height=100,
            fg_color="transparent",
            hover_color="#2A2130",
            corner_radius=12,
            border_width=0,
            font=ctk.CTkFont(weight="bold", size=16),
            command=lambda: self.set_global_mode("Movies"),
        )
        self.btn_movie.grid(row=0, column=1, padx=4, pady=4)

        self.right_nav_frame = ctk.CTkFrame(self.top_bar, fg_color="transparent")
        self.right_nav_frame.grid(row=0, column=2, sticky="e")
        self.global_search_entry = ctk.CTkEntry(
            self.right_nav_frame,
            placeholder_text="Type to search...",
            placeholder_text_color="#A4B2C6",
            height=40,
            width=200,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
        )
        self.global_search_entry.pack(side="left", padx=(0, 5))
        self.global_search_entry.bind(
            "<Return>", lambda e: self.do_global_manual_search()
        )
        self.global_search_entry.bind("<KeyRelease>", self.on_global_search_key)
        self.global_search_entry.bind(
            "<FocusOut>", lambda e: self.after(200, self.hide_global_suggestions)
        )

        self._global_search_job = None
        self._global_latest_query = ""
        self.global_suggestion_window = None

        self.global_search_btn = ctk.CTkButton(
            self.right_nav_frame,
            text="🔍",
            width=40,
            height=40,
            fg_color=GLASS_CARD,
            hover_color=ACCENT_HOVER,
            border_width=1,
            border_color=GLASS_EDGE,
            corner_radius=10,
            font=ctk.CTkFont(size=18),
            command=self.do_global_manual_search,
        )
        self.global_search_btn.pack(side="left", padx=(0, 15))
        self.settings_btn = ctk.CTkButton(
            self.right_nav_frame,
            text="⚙",
            font=ctk.CTkFont(size=28),
            width=40,
            height=40,
            fg_color="transparent",
            hover_color=GLASS_CARD,
            border_width=0,
            corner_radius=10,
            command=self.open_settings_window,
        )
        self.settings_btn.pack(side="left")

        self.tabview = ctk.CTkTabview(
            self,
            corner_radius=15,
            fg_color=TAB_BG,
            border_width=1,
            border_color=GLASS_EDGE,
            segmented_button_fg_color=GLASS_CARD,
            segmented_button_selected_color=ACCENT_COLOR,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=GLASS_CARD,
            command=self.on_tab_change,
        )
        self.tabview._segmented_button.configure(
            font=ctk.CTkFont(size=16, weight="bold")
        )

        self.movie_frame = ctk.CTkFrame(self, fg_color="transparent")

        self.set_global_mode("TV Shows")

        self.status_label = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12), text_color="#A4B2C6"
        )
        self.status_label.place(relx=0.5, rely=0.99, anchor="s")

        self.after(2000, self.check_for_updates)

        self.io_executor.submit(self.load_app_icons)
        self.start_background_library_sync()

    # ==========================================
    # AUTO-UPDATE METHODS
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
                self.ui_queue.put(
                    lambda: self.status_label.configure(
                        text=f"⬆ Updating to version {remote_version}... Restarting soon."
                    )
                )

                script_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{SCRIPT_FILENAME}"
                script_resp = requests.get(script_url, timeout=10)

                if script_resp.status_code == 200:
                    new_content = script_resp.text

                    match = re.search(
                        r'CURRENT_VERSION\s*=\s*["\']([^"\']+)["\']', new_content
                    )
                    if match and match.group(1) == CURRENT_VERSION:
                        logger.warning(
                            "Downloaded script still contains the old version (CDN cache). Aborting."
                        )
                        self.ui_queue.put(lambda: self.status_label.configure(text=""))
                        return

                    self.apply_update(new_content, remote_version)
                else:
                    logger.error(
                        f"Script download failed: HTTP {script_resp.status_code}"
                    )
                    self.ui_queue.put(
                        lambda: self.status_label.configure(
                            text="❌ Update failed. Check log."
                        )
                    )

            except Exception as e:
                logger.error("Update check failed: %s", e)

        self.network_executor.submit(_check)

    def apply_update(self, new_content, new_version):
        try:
            script_path = os.path.join(SCRIPT_DIR, SCRIPT_FILENAME)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            self.ui_queue.put(
                lambda: self.status_label.configure(
                    text=f"✅ Updated to {new_version}. Restarting..."
                )
            )
            time.sleep(1.5)

            subprocess.Popen([sys.executable, script_path])
            self.quit()
            sys.exit(0)

        except Exception as e:
            logger.error("Failed to apply update: %s", e)
            self.ui_queue.put(
                lambda: self.status_label.configure(text="❌ Update overwrite failed.")
            )

    # ==========================================
    # UI HELPERS
    # ==========================================
    def do_global_manual_search(self):
        q = self.global_search_entry.get().strip()
        if q:
            self.hide_global_suggestions()
            self.open_manual_search(
                {
                    "show": q,
                    "episode": "",
                    "title": "Manual Action",
                    "show_id": None,
                    "qual_str": "",
                    "is_movie": self.global_media_var.get() == "Movies",
                }
            )
            self.global_search_entry.delete(0, "end")

    def on_global_search_key(self, event):
        if event.keysym in ["Return", "Up", "Down", "Left", "Right", "Tab", "Escape"]:
            return
        if self._global_search_job:
            self.after_cancel(self._global_search_job)
        self._global_search_job = self.after(300, self.do_global_suggestions)

    def do_global_suggestions(self):
        q = self.global_search_entry.get().strip()
        self._global_latest_query = q
        if len(q) < 3:
            self.hide_global_suggestions()
            return

        mode = self.global_media_var.get()

        def _fetch():
            try:
                if mode == "TV Shows":
                    res = self.api_get(
                        f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(q)}",
                        timeout=3,
                    )
                    if res.status_code == 200:
                        data = res.json()[:6]
                        if self._global_latest_query == q:
                            self.ui_queue.put(
                                lambda: self.show_global_suggestions(data, q, mode)
                            )
                else:
                    api_key = self.settings.get("tmdb_api_key", "").strip()
                    if not api_key:
                        if self._global_latest_query == q:
                            self.ui_queue.put(
                                lambda: self.show_global_suggestions(
                                    [
                                        {
                                            "error": "TMDB API Key missing. Add in Settings."
                                        }
                                    ],
                                    q,
                                    mode,
                                )
                            )
                        return

                    res = self.api_get(
                        f"https://api.themoviedb.org/3/search/movie?api_key={api_key}&query={urllib.parse.quote(q)}",
                        timeout=3,
                    )
                    if res.status_code == 200:
                        results = res.json().get("results", [])[:6]
                        if self._global_latest_query == q:
                            self.ui_queue.put(
                                lambda: self.show_global_suggestions(results, q, mode)
                            )
            except Exception as e:
                logger.warning("Global suggestion fetch error: %s", e)

        self.network_executor.submit(_fetch)

    def hide_global_suggestions(self):
        if (
            hasattr(self, "global_suggestion_window")
            and self.global_suggestion_window
            and self.global_suggestion_window.winfo_exists()
        ):
            self.global_suggestion_window.destroy()
        self.global_suggestion_window = None

    def show_global_suggestions(self, data, query, mode):
        if self._global_latest_query != query:
            return
        self.hide_global_suggestions()
        if not data:
            return

        x = self.global_search_entry.winfo_rootx()
        y = (
            self.global_search_entry.winfo_rooty()
            + self.global_search_entry.winfo_height()
            + 2
        )
        w = self.global_search_entry.winfo_width()

        self.global_suggestion_window = ctk.CTkToplevel(self)
        self.global_suggestion_window.wm_overrideredirect(True)
        self.global_suggestion_window.geometry(f"{w}x{len(data) * 35 + 2}+{x}+{y}")
        self.global_suggestion_window.configure(fg_color=GLASS_CARD)
        self.global_suggestion_window.attributes("-topmost", True)

        container = ctk.CTkFrame(
            self.global_suggestion_window,
            fg_color=GLASS_CARD,
            border_width=1,
            border_color=GLASS_EDGE,
            corner_radius=4,
        )
        container.pack(fill="both", expand=True)

        if mode == "Movies" and "error" in data[0]:
            lbl = ctk.CTkLabel(
                container,
                text=data[0]["error"],
                text_color="#C0392B",
                font=ctk.CTkFont(size=11, weight="bold"),
            )
            lbl.pack(fill="x", pady=8)
            return

        for item in data:
            if mode == "TV Shows":
                show = item.get("show", {})
                name = show.get("name", "Unknown")
                year = show.get("premiered", "")[:4] if show.get("premiered") else ""
                label_text = f"{name} ({year})" if year else name

                btn = ctk.CTkButton(
                    container,
                    text=label_text,
                    fg_color="transparent",
                    hover_color=ACCENT_HOVER,
                    anchor="w",
                    corner_radius=0,
                    height=35,
                )
                btn.pack(fill="x")

                def on_click(s=show):
                    self.hide_global_suggestions()
                    self.global_search_entry.delete(0, "end")
                    self.open_manual_search(
                        {
                            "show": s.get("name"),
                            "episode": "S01E01",
                            "media_id": str(s.get("id")),
                            "show_id": str(s.get("id")),
                            "title": "Manual Action",
                        }
                    )

                btn.configure(command=on_click)
            else:
                name = item.get("title", "Unknown")
                year = (
                    item.get("release_date", "")[:4] if item.get("release_date") else ""
                )
                label_text = f"{name} ({year})" if year else name

                btn = ctk.CTkButton(
                    container,
                    text=label_text,
                    fg_color="transparent",
                    hover_color=ACCENT_HOVER,
                    anchor="w",
                    corner_radius=0,
                    height=35,
                )
                btn.pack(fill="x")

                def on_click(m=item):
                    self.hide_global_suggestions()
                    self.global_search_entry.delete(0, "end")
                    search_str = (
                        f"{m.get('title')} {m.get('release_date', '')[:4]}".strip()
                    )
                    self.open_manual_search(
                        {
                            "show": search_str,
                            "episode": "",
                            "title": "Manual Action",
                            "show_id": None,
                            "qual_str": "",
                            "is_movie": True,
                        }
                    )

                btn.configure(command=on_click)

    def set_global_mode(self, mode):
        self.global_media_var.set(mode)
        if mode == "TV Shows":
            self.btn_tv.configure(fg_color=ACCENT_COLOR)
            self.btn_movie.configure(fg_color="transparent")
            
            self.movie_frame.grid_remove()
            self.tabview.grid(row=1, column=0, padx=15, pady=5, sticky="nsew")

            try:
                self.tabview.tab("Calendar")
                tabs_exist = True
            except ValueError:
                tabs_exist = False

            if not tabs_exist:
                self.tab_calendar = self.tabview.add("Calendar")
                self.tab_discover = self.tabview.add("Discover")
                self.tab_library = self.tabview.add("Tracked")
                
                self.setup_calendar_tab()
                self.setup_tv_discover_tab()
                self.setup_library_tab()
            
            self.tabview.set("Calendar")
            self.refresh_calendar_data()
        else:
            self.btn_tv.configure(fg_color="transparent")
            self.btn_movie.configure(fg_color=ACCENT_COLOR)
            
            self.tabview.grid_remove()
            self.movie_frame.grid(row=1, column=0, padx=15, pady=5, sticky="nsew")
            
            if not self.movie_frame.winfo_children():
                self.setup_releases_tab()
                
            self.build_movie_releases_ui()

    def load_app_icons(self):
        logo = self.fetch_pil_image(
            "https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/refs/heads/main/logo.png"
        )
        tv_ico = self.fetch_pil_image(
            "https://github.com/drunkgummyboy/AirGrabber/blob/main/tv.png?raw=true"
        )
        mov_ico = self.fetch_pil_image(
            "https://github.com/drunkgummyboy/AirGrabber/blob/main/movie.png?raw=true"
        )
        if not mov_ico:
            mov_ico = self.fetch_pil_image(
                "https://github.com/drunkgummyboy/AirGrabber/blob/main/movies.png?raw=true"
            )
        settings_ico = self.fetch_pil_image(
            "https://raw.githubusercontent.com/google/material-design-icons/master/png/action/settings/materialicons/48dp/2x/baseline_settings_white_48dp.png"
        )

        def apply():
            if logo and hasattr(self, "logo_lbl") and self.logo_lbl.winfo_exists():
                try:
                    self.iconphoto(False, ImageTk.PhotoImage(logo))
                except:
                    pass
                w, h = logo.size
                new_h = 100
                new_w = int(new_h * (w / h))
                self.logo_lbl.configure(
                    image=ctk.CTkImage(
                        light_image=logo, dark_image=logo, size=(new_w, new_h)
                    ),
                    text="",
                )
            if hasattr(self, "btn_tv") and self.btn_tv.winfo_exists():
                if tv_ico:
                    self.tv_ico_img = ctk.CTkImage(
                        light_image=tv_ico, dark_image=tv_ico, size=(80, 80)
                    )
                    self.btn_tv.configure(image=self.tv_ico_img, text="")
                else:
                    self.btn_tv.configure(text="TV Shows")
            if hasattr(self, "btn_movie") and self.btn_movie.winfo_exists():
                if mov_ico:
                    self.mov_ico_img = ctk.CTkImage(
                        light_image=mov_ico, dark_image=mov_ico, size=(80, 80)
                    )
                    self.btn_movie.configure(image=self.mov_ico_img, text="")
                else:
                    self.btn_movie.configure(text="Movies")
            if (
                settings_ico
                and hasattr(self, "settings_btn")
                and self.settings_btn.winfo_exists()
            ):
                self.settings_img = ctk.CTkImage(
                    light_image=settings_ico, dark_image=settings_ico, size=(32, 32)
                )
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
            temp_file = DATA_FILE + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(self.followed_shows, f)
            os.replace(temp_file, DATA_FILE)

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
            temp_file = HISTORY_FILE + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(self.history, f)
            os.replace(temp_file, HISTORY_FILE)

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
            temp_file = EPISODES_FILE + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(self.episodes_cache, f)
            os.replace(temp_file, EPISODES_FILE)
            self._cache_dirty = False

    def mark_caches_dirty(self):
        self._cache_dirty = True

    def maybe_save_caches(self):
        if self._cache_dirty:
            self.save_caches()

    def load_settings(self):
        default_settings = {
            "first_day": "Monday",
            "quality": "1080p",
            "download_dir": TORRENTS_DIR,
            "weeks_to_show": 3,
            "prev_weeks_to_show": 0,
            "tmdb_api_key": "",
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
            temp_file = SETTINGS_FILE + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(self.settings, f)
            os.replace(temp_file, SETTINGS_FILE)

    def format_size(self, size_bytes):
        if not size_bytes:
            return ""
        try:
            size = float(size_bytes)
            for unit in ["B", "KB", "MB", "GB", "TB"]:
                if size < 1024.0:
                    return f"{size:.2f} {unit}"
                size /= 1024.0
            return f"{size:.2f} PB"
        except ValueError:
            return ""

    def fetch_pil_image(self, url):
        if not url:
            return None
        hsh = hashlib.md5(url.encode("utf-8")).hexdigest()
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
        loader = ctk.CTkProgressBar(
            parent_frame, mode="indeterminate", height=4, progress_color=ACCENT_COLOR
        )
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
        elif tab == "Discover":
            self.build_tv_discover_ui()
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

    def api_get(self, url, **kwargs):
        with api_semaphore:
            return http_session.get(url, **kwargs)

    def _get_or_fetch_imdb_id(self, show_id):
        if not show_id:
            return None

        imdb_id = None
        with self.data_lock:
            show_data = self.followed_shows.get(str(show_id), {})
            meta = show_data.get("metadata")
            if meta and meta.get("externals"):
                imdb_id = meta["externals"].get("imdb")

        if not imdb_id:
            try:
                res_meta = self.api_get(
                    f"https://api.tvmaze.com/shows/{show_id}?embed[]=externals",
                    timeout=5,
                )
                if res_meta.status_code == 200:
                    imdb_id = res_meta.json().get("externals", {}).get("imdb")
                    if imdb_id:
                        with self.data_lock:
                            if str(show_id) in self.followed_shows:
                                if (
                                    "metadata" not in self.followed_shows[str(show_id)]
                                    or not self.followed_shows[str(show_id)]["metadata"]
                                ):
                                    self.followed_shows[str(show_id)]["metadata"] = {}
                                self.followed_shows[str(show_id)]["metadata"][
                                    "externals"
                                ] = {"imdb": imdb_id}
                                self.mark_caches_dirty()
                                self.save_data()  # Ensure persistence
            except Exception as e:
                logger.warning(f"TVMaze metadata fetch error for show {show_id}: {e}")

        return imdb_id

    def download_torrent_file(self, data, best, f_size=None, callback=None):
        dl_dir = self.settings.get("download_dir", TORRENTS_DIR)
        os.makedirs(dl_dir, exist_ok=True)
        raw_name = best.get("name", "torrent")
        safe = re.sub(r'[<>:"/\\|?*\[\]()]+', "_", raw_name)
        safe = "".join(c for c in safe if c.isalnum() or c in " ._-").strip()
        if not safe:
            safe = "torrent"
        safe = safe[:120]

        def dl():
            success = False
            info_hash = best.get("info_hash")
            magnet = best.get("magnet")
            torrent_url = best.get("torrent_url")

            t_path = os.path.join(dl_dir, f"{safe}.torrent")

            if torrent_url:
                try:
                    r = scraper_session.get(torrent_url, timeout=10)
                    if r.status_code == 200 and (
                        b"d8:announce" in r.content or b"d4:info" in r.content
                    ):
                        with open(t_path, "wb") as f:
                            f.write(r.content)
                        success = True
                        logger.info(f"Downloaded .torrent file directly from {torrent_url}")
                    else:
                        logger.debug(f"Direct torrent download failed: {r.status_code}")
                except Exception as e:
                    logger.warning(f"Error downloading direct torrent: {e}")

            if not success and info_hash:
                part = t_path + ".part"
                for base in [
                    f"https://itorrents.org/torrent/{info_hash}.torrent",
                    f"https://btcache.me/torrent/{info_hash}",
                    f"https://torrage.info/torrent.php?h={info_hash}"
                ]:
                    try:
                        r = scraper_session.get(base, timeout=10)
                        if r.status_code == 200 and (
                            b"d8:announce" in r.content or b"d4:info" in r.content
                        ):
                            with open(part, "wb") as f:
                                f.write(r.content)
                            os.replace(part, t_path)
                            success = True
                            logger.info(f"Downloaded .torrent file to {t_path}")
                            break
                        else:
                            logger.debug(
                                f"Torrent download failed from {base}: status {r.status_code}"
                            )
                    except Exception as e:
                        logger.warning(f"Error downloading torrent from {base}: {e}")

            if not success and magnet:
                try:
                    magnet_path = os.path.join(dl_dir, f"{safe}.magnet")
                    with open(magnet_path, "w", encoding="utf-8") as f:
                        f.write(magnet)
                    success = True
                    logger.info(f"Saved magnet link to {magnet_path}")
                except Exception as e:
                    logger.error(f"Failed to save magnet file: {e}")

            if success:
                if data.get("media_id") and data.get("episode"):
                    hk = f"{data['media_id']}_{data['episode']}"
                    with self.data_lock:
                        if hk not in self.history:
                            self.history.append(hk)
                            self.save_history()
                            logger.debug(f"Added to history: {hk}")
            else:
                logger.error(
                    f"Download failed for {best.get('name', 'Unknown')}: no URL, cache, or magnet available"
                )

            if callback:
                try:
                    callback(success)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

        self.io_executor.submit(dl)

    def start_background_library_sync(self):
        def safe_schedule():
            if self.winfo_exists():
                self.after(5000, self._run_library_sync)
        self.after(0, safe_schedule)

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
                            res = self.api_get(
                                f"https://api.tvmaze.com/shows/{sid}?embed[]=episodes&embed[]=seasons",
                                timeout=5,
                            )
                            res.raise_for_status()
                        d = res.json()
                        with self.data_lock:
                            if sid in self.followed_shows:
                                self.followed_shows[sid]["metadata"] = d
                        self.episodes_cache[sid] = d.get("_embedded", {}).get(
                            "episodes", []
                        )
                        if d.get("image", {}).get("medium"):
                            self.io_executor.submit(
                                self.fetch_pil_image, d["image"]["medium"]
                            )
                        time.sleep(0.4)
                    except (
                        requests.exceptions.RequestException,
                        json.JSONDecodeError,
                    ) as e:
                        logger.warning(
                            "Failed to sync library data for show %s: %s", sid, e
                        )
                self.save_data()
                self.mark_caches_dirty()
                self.maybe_save_caches()
            finally:
                self._sync_running = False
                self.ui_queue.put(
                    lambda: self.after(6 * 60 * 60 * 1000, self._run_library_sync)
                )

        self.network_executor.submit(sync)

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

        ctk.CTkLabel(
            weeks_frame,
            text="Prev Weeks:",
            text_color="gray70",
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 5))
        self.prev_weeks_var = ctk.StringVar(
            value=str(self.settings.get("prev_weeks_to_show", 0))
        )
        ctk.CTkOptionMenu(
            weeks_frame,
            values=["0", "1", "2", "3", "4"],
            height=28,
            width=60,
            fg_color=GLASS_CARD,
            button_color=GLASS_EDGE,
            variable=self.prev_weeks_var,
            command=lambda e: self.refresh_calendar_data(),
        ).pack(side="left", padx=(0, 15))

        ctk.CTkLabel(
            weeks_frame,
            text="Total Weeks:",
            text_color="gray70",
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 5))
        self.weeks_var = ctk.StringVar(value=str(self.settings.get("weeks_to_show", 3)))
        ctk.CTkOptionMenu(
            weeks_frame,
            values=["1", "2", "3", "4", "5"],
            height=28,
            width=60,
            fg_color=GLASS_CARD,
            button_color=GLASS_EDGE,
            variable=self.weeks_var,
            command=lambda e: self.refresh_calendar_data(),
        ).pack(side="left")

        self.tv_header_frame = ctk.CTkFrame(self.tab_calendar, fg_color="transparent")
        self.tv_header_frame.grid(row=1, column=0, sticky="ew", padx=15)

        self.tv_scroll = ctk.CTkScrollableFrame(
            self.tab_calendar, fg_color="transparent"
        )
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

        days_of_week = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        for i, d in enumerate(days_of_week):
            ctk.CTkLabel(
                self.tv_header_frame,
                text=d,
                text_color="gray60",
                font=ctk.CTkFont(weight="bold"),
            ).grid(row=0, column=i)

        with self.data_lock:
            shows_snapshot = {
                sid: dict(d_dict) for sid, d_dict in self.followed_shows.items()
            }
            episodes_snapshot = {
                sid: list(eps) for sid, eps in self.episodes_cache.items()
            }

        schedule = {}
        for sid, d_dict in shows_snapshot.items():
            for ep in episodes_snapshot.get(sid, []):
                ad = ep.get("airdate")
                if ad:
                    if ad not in schedule:
                        schedule[ad] = []
                    schedule[ad].append(
                        {
                            "media_id": sid,
                            "show": d_dict["name"],
                            "episode": f"S{ep.get('season', 1):02d}E{ep.get('number', 1):02d}",
                            "title": ep.get("name", ""),
                        }
                    )

        max_daily = 0
        for week in range(tw):
            self.tv_scroll.grid_rowconfigure(week, weight=0)
            for day in range(7):
                curr = start + timedelta(days=(week * 7) + day)
                d_str = curr.strftime("%Y-%m-%d")
                max_daily = max(max_daily, len(schedule.get(d_str, [])))

                cell = ctk.CTkFrame(
                    self.tv_scroll,
                    corner_radius=6,
                    fg_color="#182133" if curr == today else "#121620",
                    border_width=1,
                    border_color="#1F3B60" if curr == today else "#1C222E",
                )
                cell.grid(row=week, column=day, sticky="nsew", padx=3, pady=3)
                cell.grid_columnconfigure(0, weight=1)
                self.calendar_day_frames[d_str] = cell

                hdr = ctk.CTkFrame(cell, fg_color="transparent")
                hdr.pack(fill="x", padx=4, pady=2)
                ctk.CTkLabel(
                    hdr,
                    text=curr.strftime("%b %d"),
                    text_color=ACCENT_COLOR if curr == today else "gray60",
                    font=ctk.CTkFont(size=11, weight="bold"),
                ).pack(side="right")

                start_of_prev_week = start_of_current_week - timedelta(days=7)
                end_of_prev_week = start_of_current_week - timedelta(days=1)
                if curr < start_of_prev_week:
                    is_prev_week = True
                elif start_of_prev_week <= curr <= end_of_prev_week:
                    is_prev_week = today.weekday() >= 2
                else:
                    is_prev_week = False

                for data in schedule.get(d_str, []):
                    self.create_calendar_card(
                        cell, data, curr, show_poster=not is_prev_week
                    )

        self.network_executor.submit(
            self._fetch_and_render_unfollowed,
            start,
            tw * 7,
            schedule,
            max_daily,
            current_gen,
        )

    def _fetch_and_render_unfollowed(
        self, start_date, total_days, schedule, max_daily_tracked, generation
    ):
        target = max(3, max_daily_tracked)
        days_to_fetch = []
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
                days_to_fetch.append((d_str, needed))

        if not days_to_fetch:
            return

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = []
            for d_str, needed in days_to_fetch:
                future = pool.submit(self._fetch_unfollowed_for_day, d_str, generation)
                futures.append((future, d_str, needed))

            for future, d_str, needed in futures:
                try:
                    items = future.result(timeout=15)
                    if items and generation == self.calendar_generation:
                        self.ui_queue.put(
                            lambda d=d_str, it=items, n=needed, g=generation: (
                                self._render_unfollowed_cells(d, it, n, g)
                            )
                        )
                except Exception as e:
                    logger.warning(f"Failed to fetch unfollowed for {d_str}: {e}")

    def _fetch_unfollowed_for_day(self, d_str, generation):
        if d_str in self.unfollowed_cache:
            return self.unfollowed_cache[d_str]
        try:
            r = self.api_get(
                f"https://api.tvmaze.com/schedule?date={d_str}", timeout=5
            )
            if r.status_code == 200:
                valid = [
                    item
                    for item in r.json()
                    if item.get("show", {}).get("type")
                    in ["Scripted", "Animation"]
                    and item.get("show", {}).get("language") == "English"
                    and item.get("show", {}).get("weight", 0) > 40
                ]
                valid.sort(
                    key=lambda x: x["show"].get("weight", 0), reverse=True
                )
                self.unfollowed_cache[d_str] = valid[:15]
                return self.unfollowed_cache[d_str]
        except Exception as e:
            logger.warning(f"Error fetching unfollowed schedule for {d_str}: {e}")
        return []

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
            show = item.get("show")
            if not show or str(show["id"]) in followed:
                continue
            if count >= needed:
                break
            count += 1

            card = ctk.CTkFrame(
                cell,
                fg_color="#14121A",
                border_color="#2A2438",
                border_width=1,
                corner_radius=8,
                height=55,
            )
            card.pack(fill="x", padx=6, pady=4)
            card.pack_propagate(False)

            inf = ctk.CTkFrame(card, fg_color="transparent")
            inf.pack(side="left", fill="both", expand=True, padx=8, pady=4)
            ctk.CTkLabel(
                inf,
                text=show.get("name", ""),
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color="gray50",
                anchor="w",
            ).pack(anchor="w")

            btm = ctk.CTkFrame(inf, fg_color="transparent")
            btm.pack(side="bottom", fill="x")
            ctk.CTkLabel(
                btm,
                text=f"S{item.get('season', 1):02d}E{item.get('number', 1):02d}",
                font=ctk.CTkFont(size=9),
                text_color="gray40",
            ).pack(side="left")

            btn = ctk.CTkButton(
                btm,
                text="+ Track",
                height=16,
                width=45,
                font=ctk.CTkFont(size=9),
                fg_color="transparent",
                border_width=1,
                border_color="gray30",
                text_color="gray60",
                hover_color="#2A2438",
            )
            btn.configure(
                command=lambda sid=str(show["id"]), name=show.get("name", ""): (
                    self.toggle_follow(sid, name, True)
                )
            )
            btn.pack(side="right")

    def create_calendar_card(self, parent, data, release_date, show_poster=True):
        card = ctk.CTkFrame(
            parent,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
            border_width=1,
            corner_radius=8,
            height=120 if show_poster else 46,
        )
        card.pack(fill="x", padx=6, pady=4)
        card.pack_propagate(False)

        future = release_date > datetime.now().date()
        btn_text = "Not Aired" if future else "Search"
        btn_color = "gray25" if future else ACCENT_COLOR

        qual = self.settings.get("quality", "1080p")
        data["qual_str"] = qual

        with self.data_lock:
            meta = self.followed_shows.get(str(data["media_id"]), {}).get(
                "metadata", {}
            )
        imdb_id = meta.get("externals", {}).get("imdb")

        if show_poster:
            pf = ctk.CTkFrame(
                card, width=68, height=100, fg_color="gray20", corner_radius=5
            )
            pf.pack(side="left", padx=10, pady=10)
            pf.pack_propagate(False)
            lbl = ctk.CTkLabel(pf, text="")
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            data["poster_lbl"] = lbl

            inf = ctk.CTkFrame(card, fg_color="transparent")
            inf.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)

            btn = ctk.CTkButton(
                inf,
                text=btn_text,
                height=22,
                font=ctk.CTkFont(size=10, weight="bold"),
                fg_color=btn_color,
                hover_color=ACCENT_HOVER,
                corner_radius=4,
                border_width=0,
            )
            btn.configure(command=lambda d=data: self.open_manual_search(d))
            if future:
                btn.configure(state="disabled", hover_color="gray25")
            btn.pack(side="bottom", fill="x")
            data["button_ref"] = btn

            text_f = ctk.CTkFrame(inf, fg_color="transparent")
            text_f.pack(side="top", fill="both", expand=True, padx=(0, 25))
            ctk.CTkLabel(
                text_f,
                text=data["show"],
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="white",
                wraplength=100,
                justify="left",
            ).pack(anchor="nw")
            ctk.CTkLabel(
                text_f,
                text=data["episode"],
                font=ctk.CTkFont(size=9),
                text_color="#A4B2C6",
            ).pack(anchor="nw", pady=(2, 0))

            def load():
                url = meta.get("image", {}).get("medium") if meta else None
                if url:
                    img = self.fetch_pil_image(url)
                    if img:
                        self.ui_queue.put(
                            lambda: (
                                lbl.winfo_exists()
                                and lbl.configure(
                                    image=ctk.CTkImage(
                                        light_image=ImageOps.fit(img, (68, 100)),
                                        dark_image=ImageOps.fit(img, (68, 100)),
                                        size=(68, 100),
                                    ),
                                    text="",
                                )
                            )
                        )

            self.io_executor.submit(load)
        else:
            inf = ctk.CTkFrame(card, fg_color="transparent")
            inf.pack(fill="both", expand=True, padx=(10, 35), pady=8)

            title_str = f"{data['show']} - {data['episode']}"
            if len(title_str) > 22:
                title_str = title_str[:19] + "..."

            btn = ctk.CTkButton(
                inf,
                text="Search" if not future else "Not Aired",
                height=22,
                width=40,
                font=ctk.CTkFont(size=10, weight="bold"),
                fg_color=btn_color,
                hover_color=ACCENT_HOVER,
                corner_radius=4,
                border_width=0,
            )
            btn.configure(command=lambda d=data: self.open_manual_search(d))
            if future:
                btn.configure(state="disabled", hover_color="gray25")
            btn.pack(side="right", padx=(5, 0))
            data["button_ref"] = btn

            ctk.CTkLabel(
                inf,
                text=title_str,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="white",
                justify="left",
            ).pack(side="left", anchor="w")

        if imdb_id:
            safe_imdb = (
                f"tt{imdb_id}" if not str(imdb_id).startswith("tt") else str(imdb_id)
            )
            info_icon = ctk.CTkLabel(
                card,
                text="ⓘ",
                width=24,
                height=24,
                font=ctk.CTkFont(size=16),
                text_color="#A4B2C6",
                cursor="hand2",
            )
            info_icon.place(relx=1.0, x=-8, y=8, anchor="ne")
            info_icon.bind(
                "<Button-1>",
                lambda e, i=safe_imdb: webbrowser.open(
                    f"https://www.imdb.com/title/{i}/"
                ),
            )
            info_icon.bind(
                "<Enter>", lambda e, w=info_icon: w.configure(text_color="white")
            )
            card.bind(
                "<Leave>", lambda e, w=info_icon: w.configure(text_color="#A4B2C6")
            )
            info_icon.bind(
                "<Leave>", lambda e, w=info_icon: w.configure(text_color="#A4B2C6")
            )

    # ==========================================
    # TV DISCOVER UI
    # ==========================================
    def setup_tv_discover_tab(self):
        self.tab_discover.grid_columnconfigure(0, weight=1)
        self.tab_discover.grid_rowconfigure(0, weight=1)

        self.tv_discover_scroll = ctk.CTkScrollableFrame(
            self.tab_discover, fg_color="transparent"
        )
        self.tv_discover_scroll.grid(row=0, column=0, sticky="nsew", padx=15, pady=10)

    def build_tv_discover_ui(self):
        for w in self.tv_discover_scroll.winfo_children():
            w.destroy()

        loader = self.show_loading(self.tv_discover_scroll)

        def fetch_data():
            api_key = self.settings.get("tmdb_api_key", "").strip()
            if not api_key:
                self.ui_queue.put(
                    lambda: self._render_tv_dashboard(
                        None,
                        loader,
                        error_msg="TMDB API key is required for Discovery.\nPlease add it in Settings.",
                    )
                )
                return

            dashboard_data = {}
            try:
                # Trending
                trend_params = {"api_key": api_key, "page": 1}
                res_trend = self.api_get(
                    "https://api.themoviedb.org/3/trending/tv/week",
                    params=trend_params,
                    timeout=10,
                )
                if res_trend.status_code == 200:
                    dashboard_data["trending"] = {
                        "title": "🔥 Trending This Week",
                        "shows": self._parse_tmdb_tv(res_trend.json().get("results", [])[:12]),
                        "url": "https://api.themoviedb.org/3/trending/tv/week",
                        "params": trend_params,
                    }

                # On the Air (New Episodes)
                air_params = {"api_key": api_key, "language": "en-US", "page": 1}
                res_air = self.api_get(
                    "https://api.themoviedb.org/3/tv/on_the_air",
                    params=air_params,
                    timeout=10,
                )
                if res_air.status_code == 200:
                    dashboard_data["on_air"] = {
                        "title": "📺 Currently Airing",
                        "shows": self._parse_tmdb_tv(res_air.json().get("results", [])[:12]),
                        "url": "https://api.themoviedb.org/3/tv/on_the_air",
                        "params": air_params,
                    }

                self.ui_queue.put(
                    lambda: self._render_tv_dashboard(dashboard_data, loader)
                )

            except Exception as e:
                logger.error(f"TV Discover API error: {e}")
                self.ui_queue.put(
                    lambda: self._render_tv_dashboard(
                        None, loader, error_msg=f"Loading failed: {str(e)}"
                    )
                )

        self.network_executor.submit(fetch_data)

    def _parse_tmdb_tv(self, raw_results):
        parsed = []
        for show in raw_results:
            first_air_date = show.get("first_air_date", "")
            date_obj = None
            if first_air_date:
                try:
                    date_obj = datetime.strptime(first_air_date, "%Y-%m-%d").date()
                except:
                    pass
            poster_url = None
            if show.get("poster_path"):
                poster_url = f"https://image.tmdb.org/t/p/w185{show.get('poster_path')}"

            parsed.append(
                {
                    "tmdb_id": show.get("id"),
                    "title": show.get("name", "Unknown"),
                    "date": date_obj,
                    "desc": show.get("overview", "")[:160],
                    "score": show.get("vote_average", "N/A"),
                    "poster_url": poster_url,
                }
            )
        return parsed

    def _render_tv_dashboard(self, dashboard_data, loader=None, error_msg=""):
        if loader:
            self.hide_loading(loader)
        if error_msg:
            ctk.CTkLabel(
                self.tv_discover_scroll,
                text=f"❌ Oops:\n{error_msg}",
                text_color="#C0392B",
                font=ctk.CTkFont(size=14, weight="bold"),
            ).pack(pady=80)
            return

        for section_key, section_data in dashboard_data.items():
            shows = section_data.get("shows", [])
            if not shows:
                continue

            section_frame = ctk.CTkFrame(self.tv_discover_scroll, fg_color="transparent")
            section_frame.pack(fill="x", pady=(0, 25))

            title_color = "#F39C12" if section_key == "trending" else "#A4B2C6"
            
            title_frame = ctk.CTkFrame(section_frame, fg_color="transparent")
            title_frame.pack(fill="x", padx=10, pady=(0, 10))
            
            ctk.CTkLabel(
                title_frame,
                text=section_data["title"],
                font=ctk.CTkFont(size=18, weight="bold"),
                text_color=title_color,
            ).pack(side="left")

            btn_see_all = ctk.CTkButton(
                title_frame, 
                text="See All ➔", 
                width=70, 
                height=24, 
                fg_color="transparent", 
                hover_color="gray20", 
                text_color="#A4B2C6", 
                font=ctk.CTkFont(size=11, weight="bold")
            )
            btn_see_all.configure(command=lambda sd=section_data: self.open_expanded_category(
                scroll_widget=self.tv_discover_scroll, 
                title_text=sd["title"], 
                url=sd["url"], 
                base_params=sd["params"], 
                parser_func=self._parse_tmdb_tv, 
                card_func=self.create_tv_discover_card, 
                page=1, 
                back_command=self.build_tv_discover_ui
            ))
            btn_see_all.pack(side="right", padx=10)

            grid = ctk.CTkFrame(section_frame, fg_color="transparent")
            grid.pack(anchor="w", fill="x")

            for i in range(6):
                grid.grid_columnconfigure(i, weight=1, uniform="tv_col")

            for idx, show in enumerate(shows):
                row = idx // 6
                col = idx % 6
                self.create_tv_discover_card(grid, show, row, col)

    def create_tv_discover_card(self, parent, data, row, col):
        card = ctk.CTkFrame(
            parent,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
            border_width=1,
            corner_radius=8,
            height=135,
        )
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

        title = data["title"]
        if len(title) > 23:
            title = title[:20] + "..."
        ctk.CTkLabel(
            inf,
            text=title,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="white",
            wraplength=120,
            justify="left",
        ).pack(anchor="nw")

        date_str = data["date"].strftime("%Y") if data.get("date") else "Unknown"
        score_text = f"{date_str} | ★ {data.get('score', 'N/A')}"
        ctk.CTkLabel(
            inf, text=score_text, font=ctk.CTkFont(size=9), text_color="#A4B2C6"
        ).pack(anchor="w", pady=(2, 0))

        details_lbl = ctk.CTkLabel(
            inf, text="Loading details...", font=ctk.CTkFont(size=9), text_color="gray50"
        )
        details_lbl.pack(anchor="w", pady=(0, 2))

        tmdb_id = data.get("tmdb_id")
        api_key = self.settings.get("tmdb_api_key", "").strip()
        
        if tmdb_id and api_key:
            def load_details():
                try:
                    res = self.api_get(f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}", timeout=5)
                    if res.status_code == 200:
                        det = res.json()
                        status = det.get("status", "Unknown")
                        seasons = det.get("number_of_seasons", "?")
                        self.ui_queue.put(
                            lambda: details_lbl.winfo_exists() and details_lbl.configure(text=f"{status} • {seasons} Seasons")
                        )
                    else:
                        self.ui_queue.put(lambda: details_lbl.winfo_exists() and details_lbl.configure(text=""))
                except:
                    pass
            self.network_executor.submit(load_details)
        else:
            details_lbl.configure(text="")

        with self.data_lock:
            is_tracked = any(sdata.get("name", "").lower() == data["title"].lower() for sdata in self.followed_shows.values())

        btn = ctk.CTkButton(
            inf,
            text="Tracking" if is_tracked else "+ Track",
            height=22,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="transparent" if is_tracked else ACCENT_COLOR,
            state="disabled" if is_tracked else "normal",
            hover_color=ACCENT_HOVER,
            border_width=0,
            corner_radius=4,
        )
        if not is_tracked:
            btn.configure(
                command=lambda t=data["title"], b=btn: self._track_from_discover(t, b)
            )
        btn.pack(side="bottom", fill="x")

        if data.get("poster_url"):
            def load_img():
                pil_img = self.fetch_pil_image(data["poster_url"])
                if pil_img:
                    img = ImageOps.fit(pil_img, (68, 100), Image.Resampling.LANCZOS)
                    self.ui_queue.put(
                        lambda: (
                            poster_lbl.winfo_exists()
                            and poster_lbl.configure(
                                image=ctk.CTkImage(
                                    light_image=img, dark_image=img, size=(68, 100)
                                ),
                                text="",
                            )
                        )
                    )
            self.io_executor.submit(load_img)

    def _track_from_discover(self, title, btn):
        btn.configure(state="disabled", text="Searching...")
        def _task():
            try:
                res = self.api_get(
                    f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(title)}",
                    timeout=5,
                )
                if res.status_code == 200 and res.json():
                    data = res.json()[0]["show"]
                    sid = str(data["id"])
                    name = data["name"]
                    
                    with self.data_lock:
                        already_tracked = sid in self.followed_shows
                        
                    if already_tracked:
                        self.ui_queue.put(lambda: btn.configure(text="Tracking", fg_color="transparent", state="disabled"))
                    else:
                        self.toggle_follow(sid, name, True)
                        self.ui_queue.put(lambda: btn.configure(text="Tracking", fg_color="transparent", state="disabled"))
                else:
                    self.ui_queue.put(lambda: btn.configure(text="Not Found", fg_color="#C0392B"))
            except Exception as e:
                logger.error(f"Failed to track from discover: {e}")
                self.ui_queue.put(lambda: btn.configure(text="Error", fg_color="#C0392B"))

        self.network_executor.submit(_task)

    # ==========================================
    # MOVIE RELEASES TAB
    # ==========================================
    def setup_releases_tab(self):
        self.movie_frame.grid_columnconfigure(0, weight=1)
        self.movie_frame.grid_rowconfigure(1, weight=1)

        search_frame = ctk.CTkFrame(
            self.movie_frame,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
            border_width=1,
            corner_radius=10,
        )
        search_frame.grid(row=0, column=0, padx=15, pady=(15, 10), sticky="ew")
        search_frame.grid_columnconfigure(0, weight=1)

        title_lbl = ctk.CTkLabel(
            search_frame,
            text="Find & Download Any Movie",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="white",
        )
        title_lbl.grid(row=0, column=0, pady=(15, 5), padx=20, sticky="w")

        sub_lbl = ctk.CTkLabel(
            search_frame,
            text="Search or browse the latest high-quality digital releases below.",
            font=ctk.CTkFont(size=12),
            text_color="#A4B2C6",
        )
        sub_lbl.grid(row=1, column=0, pady=(0, 15), padx=20, sticky="w")

        input_container = ctk.CTkFrame(search_frame, fg_color="transparent")
        input_container.grid(row=2, column=0, padx=20, pady=(0, 20), sticky="ew")
        input_container.grid_columnconfigure(0, weight=1)

        self.movie_search_entry = ctk.CTkEntry(
            input_container,
            placeholder_text="e.g., Deadpool, Inception, The Matrix...",
            font=ctk.CTkFont(size=16),
            height=45,
            fg_color=BG_BASE,
            border_color=GLASS_EDGE,
        )
        self.movie_search_entry.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        self.movie_search_entry.bind("<Return>", lambda e: self.execute_movie_search())

        self.movie_search_btn = ctk.CTkButton(
            input_container,
            text="Search",
            width=120,
            height=45,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT_COLOR,
            hover_color=ACCENT_HOVER,
            command=self.execute_movie_search,
        )
        self.movie_search_btn.grid(row=0, column=1)

        self.btn_clear_search = ctk.CTkButton(
            input_container,
            text="✖ Clear",
            width=60,
            height=45,
            fg_color="transparent",
            hover_color="#2A2438",
            border_width=1,
            border_color="gray30",
            command=self.clear_movie_search,
        )
        self.btn_clear_search.grid(row=0, column=2, padx=(10, 0))
        self.btn_clear_search.grid_remove() 

        self.releases_scroll = ctk.CTkScrollableFrame(
            self.movie_frame, fg_color="transparent"
        )
        self.releases_scroll.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 10))

    def clear_movie_search(self):
        self.movie_search_entry.delete(0, "end")
        self.btn_clear_search.grid_remove()
        self.build_movie_releases_ui()

    def execute_movie_search(self):
        query = self.movie_search_entry.get().strip()
        if not query:
            return

        self.btn_clear_search.grid()
        for w in self.releases_scroll.winfo_children():
            w.destroy()

        loader = self.show_loading(self.releases_scroll)

        def fetch_search():
            api_key = self.settings.get("tmdb_api_key", "").strip()
            if not api_key:
                self.ui_queue.put(
                    lambda: self._render_movie_dashboard(
                        None,
                        loader,
                        error_msg="TMDB API key is not set. Please add it in Settings.",
                    )
                )
                return

            try:
                search_params = {"api_key": api_key, "query": query, "language": "en-US", "page": 1}
                res = self.api_get(
                    "https://api.themoviedb.org/3/search/movie",
                    params=search_params,
                    timeout=10,
                )
                res.raise_for_status()
                results = res.json().get("results", [])

                parsed_results = self._parse_tmdb_movies(results)

                dashboard_data = {
                    "search": {
                        "title": f"Search Results for '{query}'",
                        "movies": parsed_results[:12],
                        "url": "https://api.themoviedb.org/3/search/movie",
                        "params": search_params,
                    }
                }
                self.ui_queue.put(
                    lambda: self._render_movie_dashboard(dashboard_data, loader)
                )

            except Exception as e:
                logger.error(f"TMDB Search error: {e}")
                self.ui_queue.put(
                    lambda: self._render_movie_dashboard(
                        None, loader, error_msg=f"Search failed: {str(e)}"
                    )
                )

        self.network_executor.submit(fetch_search)

    def _parse_tmdb_movies(self, raw_results):
        parsed = []
        for movie in raw_results:
            release_date_str = movie.get("release_date", "")
            release_date = None
            if release_date_str:
                try:
                    release_date = datetime.strptime(
                        release_date_str, "%Y-%m-%d"
                    ).date()
                except:
                    pass

            poster_url = None
            if movie.get("poster_path"):
                poster_url = (
                    f"https://image.tmdb.org/t/p/w185{movie.get('poster_path')}"
                )

            parsed.append(
                {
                    "title": movie.get("title", "Unknown"),
                    "date": release_date,
                    "desc": movie.get("overview", "")[:160],
                    "score": movie.get("vote_average", "N/A"),
                    "rating": "NR",
                    "poster_url": poster_url,
                    "popularity": movie.get("popularity", 0),
                }
            )
        return parsed

    def build_movie_releases_ui(self):
        self.btn_clear_search.grid_remove()
        for w in self.releases_scroll.winfo_children():
            w.destroy()

        loader = self.show_loading(self.releases_scroll)

        def fetch_dashboard_data():
            api_key = self.settings.get("tmdb_api_key", "").strip()
            if not api_key:
                self.ui_queue.put(
                    lambda: self._render_movie_dashboard(
                        None,
                        loader,
                        error_msg="TMDB API key is not set. Please add it in Settings.",
                    )
                )
                return

            dashboard_data = {}
            today_str = date.today().strftime("%Y-%m-%d")
            forty_five_days_ago = (date.today() - timedelta(days=45)).strftime(
                "%Y-%m-%d"
            )

            try:
                # 1. Recent Digital Drops (VOD)
                digital_params = {
                    "api_key": api_key,
                    "language": "en-US",
                    "sort_by": "popularity.desc",
                    "with_release_type": "4|5",  # Digital or Physical
                    "release_date.gte": forty_five_days_ago,
                    "release_date.lte": today_str,
                    "page": 1,
                }
                res_dig = self.api_get(
                    "https://api.themoviedb.org/3/discover/movie",
                    params=digital_params,
                    timeout=10,
                )
                if res_dig.status_code == 200:
                    dashboard_data["digital"] = {
                        "title": "🔥 Just Dropped",
                        "movies": self._parse_tmdb_movies(
                            res_dig.json().get("results", [])[:12]
                        ),
                        "url": "https://api.themoviedb.org/3/discover/movie",
                        "params": digital_params,
                    }

                # 2. Trending in Theaters
                theater_params = {
                    "api_key": api_key,
                    "language": "en-US",
                    "sort_by": "popularity.desc",
                    "with_release_type": "3",  # Theatrical
                    "primary_release_date.gte": (
                        date.today() - timedelta(days=60)
                    ).strftime("%Y-%m-%d"),
                    "primary_release_date.lte": (
                        date.today() + timedelta(days=7)
                    ).strftime("%Y-%m-%d"),
                    "page": 1,
                }
                res_theaters = self.api_get(
                    "https://api.themoviedb.org/3/discover/movie",
                    params=theater_params,
                    timeout=10,
                )
                if res_theaters.status_code == 200:
                    dashboard_data["theaters"] = {
                        "title": "🎥 Trending in Theaters",
                        "movies": self._parse_tmdb_movies(
                            res_theaters.json().get("results", [])[:12]
                        ),
                        "url": "https://api.themoviedb.org/3/discover/movie",
                        "params": theater_params,
                    }

                self.ui_queue.put(
                    lambda: self._render_movie_dashboard(dashboard_data, loader)
                )

            except Exception as e:
                logger.error(f"TMDB Dashboard API error: {e}")
                self.ui_queue.put(
                    lambda: self._render_movie_dashboard(
                        None, loader, error_msg=f"Dashboard loading failed: {str(e)}"
                    )
                )

        self.network_executor.submit(fetch_dashboard_data)

    def _render_movie_dashboard(self, dashboard_data, loader=None, error_msg=""):
        if loader:
            self.hide_loading(loader)

        if error_msg:
            ctk.CTkLabel(
                self.releases_scroll,
                text=f"❌ Oops:\n{error_msg}\n\nCheck your internet connection or TMDB API key.",
                text_color="#C0392B",
                font=ctk.CTkFont(size=14, weight="bold"),
            ).pack(pady=80)
            return

        if not dashboard_data:
            ctk.CTkLabel(
                self.releases_scroll,
                text="No movies found.",
                text_color="gray50",
                font=ctk.CTkFont(size=13),
            ).pack(pady=80)
            return

        for w in self.releases_scroll.winfo_children():
            w.destroy()

        for section_key, section_data in dashboard_data.items():
            movies = section_data.get("movies", [])
            if not movies:
                continue

            section_frame = ctk.CTkFrame(self.releases_scroll, fg_color="transparent")
            section_frame.pack(fill="x", pady=(0, 25))

            title_color = (
                "#2FA572"
                if section_key == "search"
                else ("#F39C12" if section_key == "digital" else "#A4B2C6")
            )
            
            title_frame = ctk.CTkFrame(section_frame, fg_color="transparent")
            title_frame.pack(fill="x", padx=10, pady=(0, 10))
            
            ctk.CTkLabel(
                title_frame,
                text=section_data["title"],
                font=ctk.CTkFont(size=18, weight="bold"),
                text_color=title_color,
            ).pack(side="left")

            btn_see_all = ctk.CTkButton(
                title_frame, 
                text="See All ➔", 
                width=70, 
                height=24, 
                fg_color="transparent", 
                hover_color="gray20", 
                text_color="#A4B2C6", 
                font=ctk.CTkFont(size=11, weight="bold")
            )
            btn_see_all.configure(command=lambda sd=section_data, sk=section_key: self.open_expanded_category(
                scroll_widget=self.releases_scroll, 
                title_text=sd["title"], 
                url=sd["url"], 
                base_params=sd["params"], 
                parser_func=self._parse_tmdb_movies, 
                card_func=self.create_movie_horizontal_card, 
                page=1, 
                back_command=self.execute_movie_search if sk == "search" else self.build_movie_releases_ui
            ))
            btn_see_all.pack(side="right", padx=10)

            grid = ctk.CTkFrame(section_frame, fg_color="transparent")
            grid.pack(anchor="w", fill="x")

            for i in range(6):
                grid.grid_columnconfigure(i, weight=1, uniform="movie_col")

            for idx, movie in enumerate(movies):
                row = idx // 6
                col = idx % 6
                self.create_movie_horizontal_card(grid, movie, row, col)

    def open_expanded_category(
        self,
        scroll_widget,
        title_text,
        url,
        base_params,
        parser_func,
        card_func,
        page=1,
        back_command=None,
    ):
        """Generic method to render a full paginated grid for a specific TMDB category or search result."""
        for w in scroll_widget.winfo_children():
            w.destroy()

        hdr = ctk.CTkFrame(scroll_widget, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 15))

        if back_command:
            btn_back = ctk.CTkButton(
                hdr,
                text="← Back",
                width=60,
                fg_color="gray25",
                hover_color="gray35",
                command=back_command,
            )
            btn_back.pack(side="left", padx=(10, 15))

        ctk.CTkLabel(
            hdr,
            text=f"{title_text} - Page {page}",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="white",
        ).pack(side="left")

        loader = self.show_loading(scroll_widget)

        def fetch():
            params = dict(base_params)
            params["page"] = page
            try:
                res = self.api_get(url, params=params, timeout=10)
                res.raise_for_status()
                data = res.json()
                results = data.get("results", [])
                total_pages = data.get("total_pages", 1)
                parsed = parser_func(results)
                self.ui_queue.put(lambda: render(parsed, total_pages))
            except Exception as e:
                logger.error(f"Expanded category error: {e}")
                self.ui_queue.put(
                    lambda: self._render_category_error(scroll_widget, str(e), loader)
                )

        def render(items, total_pages):
            self.hide_loading(loader)
            if not items:
                ctk.CTkLabel(
                    scroll_widget, text="No more items found.", text_color="gray50"
                ).pack(pady=40)
                return

            grid = ctk.CTkFrame(scroll_widget, fg_color="transparent")
            grid.pack(fill="x", anchor="w")
            for i in range(6):
                grid.grid_columnconfigure(i, weight=1, uniform="cat_col")

            for idx, item in enumerate(items):
                row = idx // 6
                col = idx % 6
                card_func(grid, item, row, col)

            pg_frame = ctk.CTkFrame(scroll_widget, fg_color="transparent")
            pg_frame.pack(fill="x", pady=25)

            if page > 1:
                ctk.CTkButton(
                    pg_frame,
                    text="← Previous Page",
                    width=120,
                    fg_color=ACCENT_COLOR,
                    hover_color=ACCENT_HOVER,
                    command=lambda: self.open_expanded_category(
                        scroll_widget, title_text, url, base_params, parser_func, card_func, page - 1, back_command
                    ),
                ).pack(side="left", padx=10)

            ctk.CTkLabel(
                pg_frame,
                text=f"Page {page} of {total_pages}",
                text_color="gray60",
                font=ctk.CTkFont(weight="bold"),
            ).pack(side="left", expand=True)

            if page < total_pages and page < 500: # TMDB typically limits to 500 pages
                ctk.CTkButton(
                    pg_frame,
                    text="Next Page →",
                    width=120,
                    fg_color=ACCENT_COLOR,
                    hover_color=ACCENT_HOVER,
                    command=lambda: self.open_expanded_category(
                        scroll_widget, title_text, url, base_params, parser_func, card_func, page + 1, back_command
                    ),
                ).pack(side="right", padx=10)

        self.network_executor.submit(fetch)

    def _render_category_error(self, scroll_widget, error_msg, loader):
        self.hide_loading(loader)
        ctk.CTkLabel(
            scroll_widget,
            text=f"❌ Failed to load category:\n{error_msg}",
            text_color="#C0392B",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(pady=40)

    def create_movie_horizontal_card(self, parent, data, row, col):
        card = ctk.CTkFrame(
            parent,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
            border_width=1,
            corner_radius=8,
            height=135,
        )
        card.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
        card.grid_propagate(False)
        card.pack_propagate(False)

        pf = ctk.CTkFrame(
            card, width=68, height=100, fg_color="gray20", corner_radius=5
        )
        pf.pack(side="left", padx=10, pady=10)
        pf.pack_propagate(False)
        poster_lbl = ctk.CTkLabel(pf, text="")
        poster_lbl.place(relx=0.5, rely=0.5, anchor="center")

        inf = ctk.CTkFrame(card, fg_color="transparent")
        inf.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)

        title = data["title"]
        if len(title) > 23:
            title = title[:20] + "..."
        ctk.CTkLabel(
            inf,
            text=title,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="white",
            wraplength=120,
            justify="left",
        ).pack(anchor="nw")

        date_str = data["date"].strftime("%Y") if data.get("date") else "Unknown"
        score_text = f"{date_str} | ★ {data.get('score', 'N/A')}"
        ctk.CTkLabel(
            inf, text=score_text, font=ctk.CTkFont(size=9), text_color="#A4B2C6"
        ).pack(anchor="w", pady=(2, 0))

        imdb_search_url = (
            f"https://www.imdb.com/find?q={urllib.parse.quote(data['title'])}"
        )
        imdb_lbl = ctk.CTkLabel(
            inf,
            text="IMDb",
            text_color="#5D8AA8",
            font=ctk.CTkFont(size=9, underline=True),
            cursor="hand2",
        )
        imdb_lbl.pack(anchor="w", pady=(0, 2))
        imdb_lbl.bind("<Button-1>", lambda e, url=imdb_search_url: webbrowser.open(url))

        release_year = data["date"].year if data.get("date") else ""
        search_query = f"{data['title']} {release_year}".strip()

        btn = ctk.CTkButton(
            inf,
            text="Search Film",
            height=22,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=ACCENT_COLOR,
            hover_color=ACCENT_HOVER,
            border_width=0,
            corner_radius=4,
        )
        btn.configure(
            command=lambda q=search_query: self.open_manual_search(
                {
                    "show": q,
                    "episode": "",
                    "title": "Manual Action",
                    "show_id": None,
                    "qual_str": "",
                    "is_movie": True,
                }
            )
        )
        btn.pack(side="bottom", fill="x")

        if data.get("poster_url"):

            def load_img():
                pil_img = self.fetch_pil_image(data["poster_url"])
                if pil_img:
                    img = ImageOps.fit(pil_img, (68, 100), Image.Resampling.LANCZOS)
                    self.ui_queue.put(
                        lambda: (
                            poster_lbl.winfo_exists()
                            and poster_lbl.configure(
                                image=ctk.CTkImage(
                                    light_image=img, dark_image=img, size=(68, 100)
                                ),
                                text="",
                            )
                        )
                    )

            self.io_executor.submit(load_img)

    # ==========================================
    # LIBRARY TAB
    # ==========================================
    def setup_library_tab(self):
        self.tab_library.grid_columnconfigure(0, weight=1)
        self.tab_library.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=15, pady=5, sticky="ew")

        self.lbl_lib_count = ctk.CTkLabel(
            hdr,
            text="Tracked Library",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="white",
        )
        self.lbl_lib_count.pack(side="left")

        self.btn_search_shows = ctk.CTkButton(
            hdr,
            text="🔍 Search Shows",
            width=120,
            height=30,
            fg_color=ACCENT_COLOR,
            hover_color=ACCENT_HOVER,
            command=self.open_show_search_dialog,
        )
        self.btn_search_shows.pack(side="left", padx=(10, 5))

        self.btn_import = ctk.CTkButton(
            hdr,
            text="Import Shows",
            width=120,
            height=30,
            fg_color=ACCENT_COLOR,
            hover_color=ACCENT_HOVER,
            command=self.import_shows_dialog,
        )
        self.btn_import.pack(side="left", padx=(5, 5))

        self.btn_cleanup = ctk.CTkButton(
            hdr,
            text="Cleanup Ended",
            width=120,
            height=30,
            fg_color="#C0392B",
            hover_color="#922B21",
            command=self.cleanup_ended_shows,
        )
        self.btn_cleanup.pack(side="left")

        self.library_scroll = ctk.CTkScrollableFrame(
            self.tab_library, fg_color="transparent"
        )
        self.library_scroll.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 5))

    def open_show_search_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Search & Add Shows")
        dialog.geometry("600x500")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(fg_color=BG_BASE)

        search_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        search_frame.pack(fill="x", padx=20, pady=20)

        entry = ctk.CTkEntry(
            search_frame,
            placeholder_text="Start typing to search shows...",
            width=400,
            height=40,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
        )
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        loader = ctk.CTkProgressBar(
            search_frame,
            mode="indeterminate",
            width=80,
            height=4,
            progress_color=ACCENT_COLOR,
            fg_color="transparent",
        )

        results_frame = ctk.CTkScrollableFrame(dialog, fg_color="transparent")
        results_frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        dialog._search_job = None
        dialog._latest_query = ""

        def render_results(data, query):
            if dialog._latest_query != query:
                return
            loader.pack_forget()
            loader.stop()

            for w in results_frame.winfo_children():
                w.destroy()

            if not data:
                ctk.CTkLabel(
                    results_frame, text="No shows found.", text_color="gray50"
                ).pack(pady=20)
                return

            for item in data:
                show = item.get("show", {})
                sid = str(show.get("id"))
                name = show.get("name", "Unknown")
                status = show.get("status", "Unknown")
                prem = show.get("premiered", "")[:4] if show.get("premiered") else "?"

                with self.data_lock:
                    tracked = sid in self.followed_shows

                card = ctk.CTkFrame(
                    results_frame,
                    fg_color=GLASS_CARD,
                    border_color=GLASS_EDGE,
                    border_width=1,
                    corner_radius=8,
                )
                card.pack(fill="x", pady=5)
                card.grid_columnconfigure(0, weight=1)

                info = ctk.CTkFrame(card, fg_color="transparent")
                info.grid(row=0, column=0, padx=10, pady=8, sticky="w")
                ctk.CTkLabel(
                    info,
                    text=f"{name} ({prem})",
                    font=ctk.CTkFont(size=14, weight="bold"),
                    text_color="white",
                ).pack(anchor="w")
                ctk.CTkLabel(
                    info,
                    text=f"Status: {status}",
                    font=ctk.CTkFont(size=11),
                    text_color="gray60",
                ).pack(anchor="w")

                btn = ctk.CTkButton(
                    card,
                    text="Tracking" if tracked else "+ Track",
                    width=80,
                    height=28,
                    fg_color="transparent" if tracked else ACCENT_COLOR,
                    state="disabled" if tracked else "normal",
                )
                btn.grid(row=0, column=1, padx=10, pady=8, sticky="e")

                def _track(s=sid, n=name, btn_ref=btn):
                    self.toggle_follow(s, n, True)
                    btn_ref.configure(
                        text="Tracking", fg_color="transparent", state="disabled"
                    )

                btn.configure(command=_track)

        def do_search():
            q = entry.get().strip()
            dialog._latest_query = q
            if not q:
                loader.pack_forget()
                loader.stop()
                for w in results_frame.winfo_children():
                    w.destroy()
                return

            loader.pack(side="right", padx=10)
            loader.start()

            def _fetch():
                try:
                    resp = self.api_get(
                        f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(q)}",
                        timeout=5,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    self.ui_queue.put(lambda: render_results(data, q))
                except Exception as e:
                    logger.warning(f"Show search failed: {e}")
                    self.ui_queue.put(lambda: render_results([], q))

            self.network_executor.submit(_fetch)

        def on_key_release(event):
            if event.keysym in [
                "Return",
                "Up",
                "Down",
                "Left",
                "Right",
                "Tab",
                "Shift_L",
                "Shift_R",
                "Control_L",
                "Control_R",
                "Alt_L",
                "Alt_R",
                "Caps_Lock",
                "Escape",
            ]:
                return
            if dialog._search_job:
                dialog.after_cancel(dialog._search_job)
            dialog._search_job = dialog.after(400, do_search)

        entry.bind("<KeyRelease>", on_key_release)
        entry.focus_set()

    def import_shows_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Import Shows")
        dialog.geometry("500x400")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(fg_color=BG_BASE)

        lbl = ctk.CTkLabel(
            dialog, text="Paste show names (one per line):", font=ctk.CTkFont(size=12)
        )
        lbl.pack(pady=(20, 5))

        textbox = ctk.CTkTextbox(
            dialog,
            width=460,
            height=250,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
            border_width=1,
        )
        textbox.pack(pady=10)

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", pady=10)

        def do_import():
            content = textbox.get("1.0", "end-1c").strip()
            if not content:
                self._show_message("Import", "No show names entered.")
                dialog.destroy()
                return
            shows = [line.strip() for line in content.split("\n") if line.strip()]
            dialog.destroy()
            self._import_show_list(shows)

        ctk.CTkButton(
            btn_frame,
            text="Import",
            width=100,
            height=30,
            fg_color=ACCENT_COLOR,
            hover_color=ACCENT_HOVER,
            command=do_import,
        ).pack(side="right", padx=10)
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            width=100,
            height=30,
            fg_color="transparent",
            hover_color=GLASS_CARD,
            command=dialog.destroy,
        ).pack(side="right")

    def _import_show_list(self, shows):
        def import_task():
            added = 0
            failed = 0
            for show in shows:
                if show.isdigit():
                    sid = show
                    name = None
                    try:
                        res = self.api_get(
                            f"https://api.tvmaze.com/shows/{sid}", timeout=5
                        )
                        if res.status_code == 200:
                            data = res.json()
                            name = data.get("name")
                    except Exception as e:
                        logger.warning(f"Import by ID failed for {show}: {e}")
                    if not name:
                        failed += 1
                        continue
                else:
                    try:
                        res = self.api_get(
                            f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(show)}",
                            timeout=5,
                        )
                        if res.status_code == 200 and res.json():
                            data = res.json()[0]
                            sid = str(data["show"]["id"])
                            name = data["show"]["name"]
                        else:
                            failed += 1
                            continue
                    except Exception as e:
                        logger.warning(f"Import search failed for {show}: {e}")
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
            self.ui_queue.put(
                lambda: self._show_message(
                    "Import Complete", f"Added {added} shows. Failed: {failed}"
                )
            )

        self.network_executor.submit(import_task)

    def cleanup_ended_shows(self):
        def cleanup_task():
            with self.data_lock:
                to_remove = []
                for sid, data in self.followed_shows.items():
                    meta = data.get("metadata")
                    if meta and meta.get("status") == "Ended":
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
            self.ui_queue.put(
                lambda: self._show_message(
                    "Cleanup Complete", f"Removed {removed} ended shows."
                )
            )

        self.network_executor.submit(cleanup_task)

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

        items = [
            s.get("metadata") if s.get("metadata") else {"id": k, "name": s["name"]}
            for k, s in shows.items()
        ]
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
            card = ctk.CTkFrame(
                parent,
                fg_color=GLASS_CARD,
                border_color=GLASS_EDGE,
                border_width=1,
                corner_radius=8,
                height=120,
            )

            title = item.get("name") or item.get("title", "Unknown")

            if horizontal_rail:
                card.configure(width=260)
                card.pack(side="left", padx=6, pady=4)
                card.pack_propagate(False)
            else:
                card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
                card.grid_propagate(False)
                card.pack_propagate(False)

            pf = ctk.CTkFrame(
                card, width=68, height=100, fg_color="gray20", corner_radius=5
            )
            pf.pack(side="left", padx=8, pady=10)
            pf.pack_propagate(False)
            lbl = ctk.CTkLabel(pf, text="")
            lbl.place(relx=0.5, rely=0.5, anchor="center")

            inf = ctk.CTkFrame(card, fg_color="transparent")
            inf.pack(side="left", fill="both", expand=True, padx=5, pady=10)

            disp_title = title
            if len(disp_title) > 20:
                disp_title = disp_title[:17] + "..."
            ctk.CTkLabel(
                inf,
                text=disp_title,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="white",
                anchor="w",
            ).pack(anchor="w")

            if horizontal_rail and "date" in item:
                date_val = item.get("date")
                date_str = (
                    date_val.strftime("%b %d, %Y")
                    if hasattr(date_val, "strftime")
                    else ""
                )
                if date_str:
                    ctk.CTkLabel(
                        inf,
                        text=f"Date: {date_str}",
                        font=ctk.CTkFont(size=10, weight="bold"),
                        text_color="#F39C12",
                    ).pack(anchor="w", pady=(2, 0))

                score_text = (
                    f"★ {item.get('score', 'N/A')} | {item.get('rating', 'NR')}"
                )
                ctk.CTkLabel(
                    inf, text=score_text, font=ctk.CTkFont(size=9), text_color="#A4B2C6"
                ).pack(anchor="w", pady=(0, 2))

                btm = ctk.CTkFrame(inf, fg_color="transparent")
                btm.pack(side="bottom", fill="x")

                release_year = item["date"].year if item.get("date") else ""
                search_query = f"{title} {release_year}" if release_year else title

                btn = ctk.CTkButton(
                    btm,
                    text="Search Film",
                    height=20,
                    font=ctk.CTkFont(size=10, weight="bold"),
                    fg_color=ACCENT_COLOR,
                    hover_color=ACCENT_HOVER,
                    border_width=0,
                    corner_radius=4,
                )
                btn.configure(
                    command=lambda q=search_query: self.open_manual_search(
                        {
                            "show": q,
                            "episode": "",
                            "title": "Manual Action",
                            "show_id": None,
                            "qual_str": "",
                            "is_movie": True,
                        }
                    )
                )
                btn.pack(fill="x")

            else:
                ctk.CTkLabel(
                    inf,
                    text=f"Status: {item.get('status', 'Unknown')}",
                    font=ctk.CTkFont(size=9),
                    text_color="gray50",
                ).pack(anchor="w", pady=2)

                btm = ctk.CTkFrame(inf, fg_color="transparent")
                btm.pack(side="bottom", fill="x")

                sid = str(item.get("id", ""))
                imdb_id = item.get("externals", {}).get("imdb")

                if is_library:
                    ubtn = ctk.CTkButton(
                        btm,
                        text="Drop",
                        width=40,
                        height=20,
                        font=ctk.CTkFont(size=10, weight="bold"),
                        fg_color="#C0392B",
                        border_width=0,
                        command=lambda s=sid, n=title: self.toggle_follow(
                            s, n, False
                        ),
                    )
                    ubtn.pack(side="right")

                    if imdb_id:
                        safe_imdb = (
                            f"tt{imdb_id}"
                            if not str(imdb_id).startswith("tt")
                            else str(imdb_id)
                        )
                        ibtn = ctk.CTkButton(
                            btm,
                            text="IMDb",
                            width=40,
                            height=20,
                            font=ctk.CTkFont(size=10, weight="bold"),
                            fg_color="#F5C518",
                            text_color="black",
                            hover_color="#D4A710",
                            command=lambda i=safe_imdb: webbrowser.open(
                                f"https://www.imdb.com/title/{i}/"
                            ),
                        )
                        ibtn.pack(side="right", padx=(0, 5))
                else:
                    with self.data_lock:
                        tracked = sid in self.followed_shows
                    tbtn = ctk.CTkButton(
                        btm,
                        text="Tracking" if tracked else "+ Track",
                        height=20,
                        font=ctk.CTkFont(size=10, weight="bold"),
                        fg_color="transparent" if tracked else ACCENT_COLOR,
                        state="disabled" if tracked else "normal",
                        border_width=0,
                        command=lambda s=sid, n=title: self.toggle_follow(
                            s, n, True
                        ),
                    )
                    tbtn.pack(fill="x")

            def load_grid_poster(url, target_lbl=lbl):
                if url:
                    img = self.fetch_pil_image(url)
                    if img:
                        self.ui_queue.put(
                            lambda: (
                                target_lbl.winfo_exists()
                                and target_lbl.configure(
                                    image=ctk.CTkImage(
                                        light_image=ImageOps.fit(img, (68, 100)),
                                        dark_image=ImageOps.fit(img, (68, 100)),
                                        size=(68, 100),
                                    ),
                                    text="",
                                )
                            )
                        )

            img_url = item.get("poster_url") or (
                item.get("image", {}).get("medium") if item.get("image") else None
            )
            self.io_executor.submit(load_grid_poster, img_url)

            if not horizontal_rail:
                col += 1
                if col >= 6:
                    col = 0
                    row += 1

    def toggle_follow(self, sid, name, follow):
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

        self.network_executor.submit(_task)

    # ==========================================
    # ADVANCED MANUAL DIALOGUE – stays open
    # ==========================================
    def open_manual_search(self, ep_data):
        popup = ctk.CTkToplevel(self)
        popup.title("Advanced Indexer Interrogation")
        w, h = 1100, 750
        sw, sh = popup.winfo_screenwidth(), popup.winfo_screenheight()
        popup.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        popup.transient(self)
        popup.configure(fg_color="#0D0D0D")
        popup.grab_set()

        popup.results_pool = []
        popup.results_lock = threading.Lock()
        popup.searching = False
        popup.sort_col = "size"
        popup.sort_desc = True

        match = re.search(r"S(\d+)E(\d+)", ep_data.get("episode", ""), re.IGNORECASE)
        popup.current_s = int(match.group(1)) if match else 1
        popup.current_e = int(match.group(2)) if match else 1
        popup.show_id = str(ep_data.get("show_id") or ep_data.get("media_id", ""))
        popup.show_name = ep_data.get("show", "Unknown")
        
        with self.data_lock:
            popup.episodes_data = list(self.episodes_cache.get(popup.show_id, []))
        popup.is_movie = (
            ep_data.get("is_movie", False) or self.global_media_var.get() == "Movies"
        )

        popup.imdb_id_cache = None
        popup.tmdb_cached_id = None

        # ==========================================
        # HEADER DASHBOARD
        # ==========================================
        dash_frame = ctk.CTkFrame(
            popup,
            fg_color=GLASS_CARD,
            border_width=1,
            border_color=GLASS_EDGE,
            corner_radius=8,
        )
        dash_frame.pack(fill="x", padx=15, pady=15)

        poster_frame = ctk.CTkFrame(
            dash_frame, width=150, height=225, fg_color="gray20", corner_radius=8
        )
        poster_frame.pack(side="left", padx=15, pady=15)
        poster_frame.pack_propagate(False)
        poster_lbl = ctk.CTkLabel(poster_frame, text="")
        poster_lbl.place(relx=0.5, rely=0.5, anchor="center")

        info_frame = ctk.CTkFrame(dash_frame, fg_color="transparent")
        info_frame.pack(side="left", fill="both", expand=True, pady=15, padx=(0, 15))

        title_row = ctk.CTkFrame(info_frame, fg_color="transparent")
        title_row.pack(fill="x")

        if popup.is_movie:
            popup.lbl_title = ctk.CTkLabel(
                title_row,
                text=popup.show_name,
                font=ctk.CTkFont(size=24, weight="bold"),
                text_color="white",
            )
        else:
            popup.lbl_title = ctk.CTkLabel(
                title_row,
                text=f"{popup.show_name}: S{popup.current_s}E{popup.current_e}",
                font=ctk.CTkFont(size=24, weight="bold"),
                text_color="white",
            )
        popup.lbl_title.pack(side="left")

        def fix_match():
            dialog = ctk.CTkInputDialog(
                text="Enter the correct TVMaze ID for this show:", title="Fix Mismatch"
            )
            new_id = dialog.get_input()
            if new_id and new_id.isdigit():
                popup.show_id = new_id

                def fetch_and_refresh():
                    try:
                        res = self.api_get(
                            f"https://api.tvmaze.com/shows/{new_id}?embed[]=episodes",
                            timeout=5,
                        )
                        if res.status_code == 200:
                            data = res.json()
                            with self.data_lock:
                                self.followed_shows[new_id] = {
                                    "name": data.get("name"),
                                    "metadata": data,
                                }
                                self.episodes_cache[new_id] = data.get(
                                    "_embedded", {}
                                ).get("episodes", [])
                            self.save_data()
                            self.save_caches()
                            self.ui_queue.put(lambda: popup.destroy())
                            self.ui_queue.put(
                                lambda: self.open_manual_search(
                                    {
                                        "show": data.get("name"),
                                        "episode": f"S{popup.current_s:02d}E{popup.current_e:02d}",
                                        "show_id": new_id,
                                    }
                                )
                            )
                    except Exception as e:
                        logger.error(f"Failed to fix match: {e}")

                self.network_executor.submit(fetch_and_refresh)

        if not popup.is_movie:
            ctk.CTkButton(
                title_row,
                text="✎ Fix Match",
                width=60,
                height=20,
                fg_color="transparent",
                hover_color="#2A2438",
                font=ctk.CTkFont(size=10),
                command=fix_match,
            ).pack(side="left", padx=10)

        popup.lbl_meta = ctk.CTkLabel(
            info_frame,
            text="Loading metadata...",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
        )
        popup.lbl_meta.pack(anchor="w", pady=(2, 10))

        def create_scroll_selector(parent, label_text):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=5)
            ctk.CTkLabel(
                row,
                text=label_text,
                width=70,
                anchor="w",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="gray60",
            ).pack(side="left")
            scroll = ctk.CTkScrollableFrame(
                row, orientation="horizontal", height=35, fg_color="transparent"
            )
            scroll.pack(side="left", fill="x", expand=True)
            return scroll

        if not popup.is_movie:
            popup.season_scroll = create_scroll_selector(info_frame, "SEASON")
            popup.episode_scroll = create_scroll_selector(info_frame, "EPISODE")

        qual_scroll = create_scroll_selector(info_frame, "VERSION")

        popup.qual_var = ctk.StringVar(value=self.settings.get("quality", "1080p"))

        def render_quality_buttons():
            for w in qual_scroll.winfo_children():
                w.destroy()
            qualities = [
                ("4K", "2160p"),
                ("1080P", "1080p"),
                ("720P", "720p"),
                ("480P", "480p"),
                ("ANY", "any"),
            ]
            current_qual = popup.qual_var.get().lower()
            if current_qual == "2160p (4k)":
                current_qual = "2160p"

            for text_lbl, val in qualities:
                is_sel = val == current_qual
                if current_qual == "any" and text_lbl == "ANY":
                    is_sel = True

                btn = ctk.CTkButton(
                    qual_scroll,
                    text=text_lbl,
                    width=50,
                    height=25,
                    fg_color=ACCENT_COLOR if is_sel else "gray15",
                    hover_color=ACCENT_HOVER if is_sel else "gray25",
                    command=lambda v=val: on_quality_change(v),
                )
                btn.pack(side="left", padx=3)

        def on_quality_change(val):
            popup.qual_var.set(val)
            render_quality_buttons()
            execute_manual_search()

        render_quality_buttons()

        def render_selectors():
            if popup.is_movie:
                return
            for w in popup.season_scroll.winfo_children():
                w.destroy()
            for w in popup.episode_scroll.winfo_children():
                w.destroy()

            seasons = sorted(
                list(set(ep.get("season", 1) for ep in popup.episodes_data))
            )
            if not seasons:
                seasons = [1]
            if popup.current_s not in seasons:
                popup.current_s = seasons[0]

            for s in seasons:
                is_sel = s == popup.current_s
                btn = ctk.CTkButton(
                    popup.season_scroll,
                    text=str(s),
                    width=35,
                    height=25,
                    fg_color=ACCENT_COLOR if is_sel else "gray15",
                    hover_color=ACCENT_HOVER if is_sel else "gray25",
                    command=lambda season=s: on_season_change(season),
                )
                btn.pack(side="left", padx=3)

            episodes = [
                ep for ep in popup.episodes_data if ep.get("season") == popup.current_s
            ]
            max_ep = (
                max([ep.get("number", 1) for ep in episodes])
                if episodes
                else popup.current_e
            )

            for e in range(1, max_ep + 1):
                is_sel = e == popup.current_e
                btn = ctk.CTkButton(
                    popup.episode_scroll,
                    text=str(e),
                    width=35,
                    height=25,
                    fg_color=ACCENT_COLOR if is_sel else "gray15",
                    hover_color=ACCENT_HOVER if is_sel else "gray25",
                    command=lambda ep=e: on_episode_change(ep),
                )
                btn.pack(side="left", padx=3)

        def on_season_change(s):
            popup.current_s = s
            popup.current_e = 1
            render_selectors()
            execute_manual_search()

        def on_episode_change(e):
            popup.current_e = e
            render_selectors()
            execute_manual_search()

        def load_meta_and_poster():
            meta = {}
            if not popup.is_movie:
                with self.data_lock:
                    if popup.show_id in self.followed_shows:
                        meta = self.followed_shows[popup.show_id].get("metadata", {})

                if (not meta or not popup.episodes_data) and popup.show_id:
                    try:
                        res = self.api_get(
                            f"https://api.tvmaze.com/shows/{popup.show_id}?embed[]=episodes",
                            timeout=5,
                        )
                        if res.status_code == 200:
                            data = res.json()
                            meta = data
                            popup.episodes_data = data.get("_embedded", {}).get(
                                "episodes", []
                            )
                            self.ui_queue.put(render_selectors)
                    except Exception as e:
                        logger.warning(f"Failed to fetch missing metadata: {e}")

                genres = ", ".join(meta.get("genres", [])) if meta else "Unknown Genre"
                status = meta.get("status", "Unknown").upper() if meta else "UNKNOWN"
                prem = (
                    meta.get("premiered", "")[:4]
                    if meta and meta.get("premiered")
                    else "?"
                )

                self.ui_queue.put(
                    lambda: popup.lbl_meta.configure(
                        text=f"{prem} • {genres} • {status}"
                    )
                )
                self.ui_queue.put(
                    lambda: popup.lbl_title.configure(
                        text=f"{popup.show_name}: S{popup.current_s:02d}E{popup.current_e:02d}"
                    )
                )
                url = (
                    meta.get("image", {}).get("original")
                    or meta.get("image", {}).get("medium")
                    if meta
                    else None
                )

            else:
                api_key = self.settings.get("tmdb_api_key", "").strip()
                url = None
                prem = "?"
                genres = "Movie"

                if api_key:
                    try:
                        match = re.search(r"(.+?)\s+(\d{4})$", popup.show_name)
                        if match:
                            query_title, year = match.group(1), match.group(2)
                            search_url = f"https://api.themoviedb.org/3/search/movie?api_key={api_key}&query={urllib.parse.quote(query_title)}&year={year}"
                        else:
                            search_url = f"https://api.themoviedb.org/3/search/movie?api_key={api_key}&query={urllib.parse.quote(popup.show_name)}"

                        res = self.api_get(search_url, timeout=5)
                        if res.status_code == 200 and res.json().get("results"):
                            first = res.json()["results"][0]
                            prem = first.get("release_date", "")[:4]
                            genres = f"Score: {first.get('vote_average', 'N/A')}"
                            if first.get("poster_path"):
                                url = f"https://image.tmdb.org/t/p/w342{first['poster_path']}"
                            popup.tmdb_cached_id = first.get("id")
                    except Exception as e:
                        logger.warning(f"Movie meta fetch failed: {e}")

                self.ui_queue.put(
                    lambda: popup.lbl_meta.configure(text=f"{prem} • {genres}")
                )
                title_text = (
                    f"{popup.show_name} ({prem})" if prem != "?" else popup.show_name
                )
                self.ui_queue.put(lambda: popup.lbl_title.configure(text=title_text))

            if url:
                img = self.fetch_pil_image(url)
                if img:
                    self.ui_queue.put(
                        lambda: (
                            poster_lbl.winfo_exists()
                            and poster_lbl.configure(
                                image=ctk.CTkImage(
                                    light_image=ImageOps.fit(img, (150, 225)),
                                    dark_image=ImageOps.fit(img, (150, 225)),
                                    size=(150, 225),
                                ),
                                text="",
                            )
                        )
                    )

        self.network_executor.submit(load_meta_and_poster)
        render_selectors()

        # ==========================================
        # RESULTS SECTION
        # ==========================================
        res_box = ctk.CTkFrame(
            popup,
            fg_color=GLASS_CARD,
            border_width=1,
            border_color=GLASS_EDGE,
            corner_radius=8,
        )
        res_box.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        top_bar = ctk.CTkFrame(res_box, fg_color="transparent")
        top_bar.pack(fill="x", padx=10, pady=10)

        res_title = ctk.CTkLabel(
            top_bar,
            text="Torrents",
            text_color="white",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        res_title.pack(side="left")

        status_frame = ctk.CTkFrame(top_bar, fg_color="transparent")
        status_frame.pack(side="right")
        apibay_lbl = ctk.CTkLabel(
            status_frame,
            text="⏳ APIBay",
            text_color="yellow",
            font=("Consolas", 11, "bold"),
        )
        apibay_lbl.pack(side="left", padx=(0, 10))
        eztv_lbl = ctk.CTkLabel(
            status_frame,
            text="⏳ EZTV",
            text_color="yellow",
            font=("Consolas", 11, "bold"),
        )
        eztv_lbl.pack(side="left", padx=(0, 10))
        sol_lbl = ctk.CTkLabel(
            status_frame,
            text="⏳ Solid",
            text_color="yellow",
            font=("Consolas", 11, "bold"),
        )
        sol_lbl.pack(side="left", padx=(0, 10))
        yts_lbl = ctk.CTkLabel(
            status_frame,
            text="⏳ YTS",
            text_color="yellow",
            font=("Consolas", 11, "bold"),
        )
        yts_lbl.pack(side="left")

        def set_grid_cols(frame):
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_columnconfigure(1, weight=0, minsize=100)
            frame.grid_columnconfigure(2, weight=0, minsize=110)
            frame.grid_columnconfigure(3, weight=0, minsize=60)
            frame.grid_columnconfigure(4, weight=0, minsize=80)

        header_frame = ctk.CTkFrame(res_box, fg_color="transparent", height=28)
        header_frame.pack(fill="x", padx=(10, 26), pady=(5, 0))
        set_grid_cols(header_frame)

        def set_sort(col):
            if popup.sort_col == col:
                popup.sort_desc = not popup.sort_desc
            else:
                popup.sort_col = col
                popup.sort_desc = True
            render_results()

        def make_hdr(parent, text, col_key, col_idx, anchor="w", width=None):
            btn = ctk.CTkButton(
                parent,
                text=text,
                height=24,
                width=width if width else 0,
                fg_color="transparent",
                hover_color="#202531",
                text_color=ACCENT_COLOR,
                font=("Consolas", 12, "bold"),
                anchor=anchor,
                command=lambda c=col_key: set_sort(c),
            )
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
                        fg_color=anim_colors[step % len(anim_colors)],
                    )
                    popup.after(100, animate, step + 1)
                else:
                    ep_data_patched = dict(ep_data)
                    if not popup.is_movie:
                        ep_data_patched["episode"] = (
                            f"S{popup.current_s:02d}E{popup.current_e:02d}"
                        )
                        if not ep_data_patched.get("media_id"):
                            ep_data_patched["media_id"] = popup.show_id

                    def download_callback(success):
                        if success:
                            btn_ref.configure(
                                text="✅ Done! (keep searching)", fg_color="#2FA572"
                            )
                        else:
                            btn_ref.configure(
                                text="❌ Failed", fg_color="#C0392B"
                            )

                        cal_btn = ep_data.get("button_ref")
                        if cal_btn and cal_btn.winfo_exists():
                            if success:
                                cal_btn.configure(
                                    text="✅ Downloaded",
                                    fg_color="#2FA572",
                                    hover_color="#2FA572",
                                )
                            else:
                                cal_btn.configure(
                                    text="Failed",
                                    fg_color="#C0392B",
                                    hover_color="#C0392B",
                                )

                    self.download_torrent_file(
                        ep_data_patched,
                        {
                            "info_hash": row_r.get("info_hash", ""),
                            "magnet": row_r.get("magnet", ""),
                            "torrent_url": row_r.get("torrent_url", ""),
                            "name": row_r.get("name", "Unknown"),
                        },
                        row_s,
                        callback=download_callback,
                    )

            animate()

        def render_results():
            for w in scroll.winfo_children():
                w.destroy()

            def update_hdr_text(btn, base_text, col_key):
                indicator = (
                    " ▼"
                    if popup.sort_col == col_key and popup.sort_desc
                    else " ▲"
                    if popup.sort_col == col_key
                    else ""
                )
                btn.configure(text=f"{base_text}{indicator}")

            update_hdr_text(hdr_name, "Name", "name")
            update_hdr_text(hdr_size, "Size", "size")
            update_hdr_text(hdr_seed, "Seed:Lech", "seeders")
            update_hdr_text(hdr_src, "Src", "source")

            filtered = []
            q_val = popup.qual_var.get().lower()

            with popup.results_lock:
                results_snapshot = list(popup.results_pool)

            for r in results_snapshot:
                name_lower = r["name"].lower()
                if q_val != "any" and q_val not in name_lower:
                    if q_val == "4k" and "2160p" not in name_lower:
                        continue
                    elif q_val != "4k":
                        continue
                filtered.append(r)

            filtered.sort(key=lambda x: x[popup.sort_col], reverse=popup.sort_desc)

            if popup.searching:
                res_title.configure(text=f"Searching... Found {len(filtered)}")
            else:
                res_title.configure(text=f"Torrents ({len(filtered)})")

            if not filtered:
                ctk.CTkLabel(
                    scroll,
                    text="No matching torrents found yet.",
                    text_color="gray50",
                    font=("Consolas", 12),
                ).pack(anchor="w", pady=10)
                return

            for idx, r in enumerate(filtered):
                size_str = self.format_size(r.get("size", 0))
                name = r.get("name", "Unknown")
                seed = str(r.get("seeders", "0"))
                leech = str(r.get("leechers", "0"))
                src_label = r.get("source", "unk")[:3].upper()

                row_frame = ctk.CTkFrame(scroll, fg_color="transparent")
                row_frame.pack(fill="x", pady=2)
                set_grid_cols(row_frame)

                ctk.CTkLabel(
                    row_frame,
                    text=name,
                    font=("Consolas", 12),
                    text_color="#A4B2C6",
                    anchor="w",
                ).grid(row=0, column=0, sticky="w", padx=2)
                ctk.CTkLabel(
                    row_frame,
                    text=size_str,
                    width=100,
                    font=("Consolas", 12),
                    text_color="#A4B2C6",
                    anchor="e",
                ).grid(row=0, column=1, sticky="e", padx=2)
                ctk.CTkLabel(
                    row_frame,
                    text=f"{seed}:{leech}",
                    width=110,
                    font=("Consolas", 12),
                    text_color="#A4B2C6",
                    anchor="center",
                ).grid(row=0, column=2, sticky="ew", padx=2)
                ctk.CTkLabel(
                    row_frame,
                    text=src_label,
                    width=60,
                    font=("Consolas", 12),
                    text_color="#A4B2C6",
                    anchor="center",
                ).grid(row=0, column=3, sticky="ew", padx=2)
                btn_frame = ctk.CTkFrame(row_frame, fg_color="transparent", width=80)
                btn_frame.grid(row=0, column=4, sticky="e", padx=2)
                dl_btn = ctk.CTkButton(
                    btn_frame,
                    text="Grab",
                    width=65,
                    height=24,
                    fg_color=ACCENT_COLOR,
                    hover_color=ACCENT_HOVER,
                    font=ctk.CTkFont(size=10, weight="bold"),
                    corner_radius=4,
                )
                dl_btn.configure(
                    command=lambda br=dl_btn, rr=r, rs=size_str: on_dl(br, rr, rs)
                )
                dl_btn.pack(side="right")

        def execute_manual_search():
            if popup.searching:
                return
            popup.searching = True
            popup.results_pool.clear()
            render_results()

            if popup.is_movie:
                query = f"{popup.show_name}"
            else:
                query = (
                    f"{popup.show_name} S{popup.current_s:02d}E{popup.current_e:02d}"
                )

            apibay_lbl.configure(text="⏳ APIBay", text_color="yellow")
            sol_lbl.configure(text="⏳ Solid", text_color="yellow")

            if popup.is_movie:
                eztv_lbl.configure(text="➖ EZTV (TV)", text_color="gray50")
                yts_lbl.configure(text="⏳ YTS", text_color="yellow")
            else:
                eztv_lbl.configure(text="⏳ EZTV", text_color="yellow")
                yts_lbl.configure(text="➖ YTS (Movie)", text_color="gray50")

            popup.active_threads = 0
            popup.thread_lock = threading.Lock()

            def thread_wrapper(target_func):
                def wrapper():
                    try:
                        target_func()
                    except Exception as e:
                        logger.error("Search thread error: %s", e, exc_info=True)
                    finally:
                        with popup.thread_lock:
                            popup.active_threads -= 1
                            if popup.active_threads <= 0:
                                popup.searching = False
                        self.ui_queue.put(render_results)

                return wrapper

            def run_searches_async():
                if popup.imdb_id_cache is None:
                    if not popup.is_movie:
                        popup.imdb_id_cache = self._get_or_fetch_imdb_id(popup.show_id)
                    else:
                        api_key = self.settings.get("tmdb_api_key", "").strip()
                        tmdb_id = getattr(popup, "tmdb_cached_id", None)
                        if api_key and tmdb_id:
                            try:
                                res_id = self.api_get(
                                    f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={api_key}",
                                    timeout=5,
                                )
                                if res_id.status_code == 200:
                                    popup.imdb_id_cache = res_id.json().get("imdb_id")
                            except Exception as e:
                                logger.warning("TMDB external ID fetch failed: %s", e)
                        elif api_key:
                            try:
                                match = re.search(r"(.+?)\s+(\d{4})$", popup.show_name)
                                if match:
                                    q_title, year = match.group(1), match.group(2)
                                    s_url = f"https://api.themoviedb.org/3/search/movie?api_key={api_key}&query={urllib.parse.quote(q_title)}&year={year}"
                                else:
                                    s_url = f"https://api.themoviedb.org/3/search/movie?api_key={api_key}&query={urllib.parse.quote(popup.show_name)}"
                                res = self.api_get(s_url, timeout=5)
                                if res.status_code == 200 and res.json().get("results"):
                                    tmdb_id = res.json()["results"][0].get("id")
                                    res_id = self.api_get(
                                        f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={api_key}",
                                        timeout=5,
                                    )
                                    if res_id.status_code == 200:
                                        popup.imdb_id_cache = res_id.json().get(
                                            "imdb_id"
                                        )
                            except Exception as e:
                                logger.warning("IMDb ID resolution failed: %s", e)

                def fetch_apibay():
                    try:
                        url = f"https://apibay.org/q.php?q={urllib.parse.quote(query)}"
                        res = self.api_get(url, timeout=10)
                        if res.status_code == 200:
                            data = res.json()
                            count = 0
                            if (
                                isinstance(data, list)
                                and len(data) > 0
                                and data[0].get("id") != "0"
                            ):
                                for r in data:
                                    if self._safe_int(r.get("seeders", 0)) > 0:
                                        with popup.results_lock:
                                            popup.results_pool.append(
                                                {
                                                    "source": "apibay",
                                                    "name": r.get("name", "Unknown"),
                                                    "info_hash": r.get("info_hash", ""),
                                                    "magnet": "",
                                                    "size": self._safe_int(
                                                        r.get("size", 0)
                                                    ),
                                                    "seeders": self._safe_int(
                                                        r.get("seeders", 0)
                                                    ),
                                                    "leechers": self._safe_int(
                                                        r.get("leechers", 0)
                                                    ),
                                                }
                                            )
                                        count += 1
                                self.ui_queue.put(
                                    lambda: apibay_lbl.configure(
                                        text=f"✅ APIBay ({count})",
                                        text_color="#2FA572",
                                    )
                                )
                            else:
                                self.ui_queue.put(
                                    lambda: apibay_lbl.configure(
                                        text="❌ APIBay", text_color="#C0392B"
                                    )
                                )
                    except Exception as e:
                        logger.warning(f"APIBay search failed: {e}")
                        self.ui_queue.put(
                            lambda: apibay_lbl.configure(
                                text="❌ APIBay", text_color="#C0392B"
                            )
                        )

                def fetch_eztv():
                    if popup.is_movie:
                        return
                    try:
                        imdb_id = popup.imdb_id_cache
                        if imdb_id:
                            eztv_imdb = imdb_id.replace("tt", "")
                            url = (
                                f"https://eztv.re/api/get-torrents?imdb_id={eztv_imdb}"
                            )
                            res = self.api_get(url, timeout=10)
                            if res.status_code == 200:
                                count = 0
                                for t in res.json().get("torrents", []):
                                    if str(t.get("season")) == str(
                                        popup.current_s
                                    ) and str(t.get("episode")) == str(popup.current_e):
                                        seeders = self._safe_int(t.get("seeds", 0))
                                        if seeders > 0:
                                            with popup.results_lock:
                                                popup.results_pool.append(
                                                    {
                                                        "source": "eztv",
                                                        "name": t.get("title", ""),
                                                        "info_hash": t.get("hash", ""),
                                                        "magnet": t.get(
                                                            "magnet_url", ""
                                                        ),
                                                        "torrent_url": t.get("torrent_url", ""),
                                                        "size": self._safe_int(
                                                            t.get("size_bytes", 0)
                                                        ),
                                                        "seeders": seeders,
                                                        "leechers": self._safe_int(
                                                            t.get("peers", 0)
                                                        )
                                                        - seeders
                                                        if t.get("peers")
                                                        else 0,
                                                    }
                                                )
                                            count += 1
                                self.ui_queue.put(
                                    lambda: eztv_lbl.configure(
                                        text=f"✅ EZTV ({count})", text_color="#2FA572"
                                    )
                                )
                            else:
                                self.ui_queue.put(
                                    lambda: eztv_lbl.configure(
                                        text="❌ EZTV", text_color="#C0392B"
                                    )
                                )
                        else:
                            self.ui_queue.put(
                                lambda: eztv_lbl.configure(
                                    text="❌ No IMDB", text_color="#C0392B"
                                )
                            )
                    except Exception as e:
                        logger.warning(f"EZTV search failed: {e}")
                        self.ui_queue.put(
                            lambda: eztv_lbl.configure(
                                text="❌ EZTV", text_color="#C0392B"
                            )
                        )

                def fetch_solidtorrents():
                    try:
                        url = f"https://solidtorrents.to/api/v1/search?q={urllib.parse.quote(query)}&category=Video"
                        res = self.api_get(url, timeout=10)
                        if res.status_code == 200:
                            data = res.json()
                            count = 0
                            for r in data.get("results", []):
                                seeders = self._safe_int(
                                    r.get("swarm", {}).get("seeders", 0)
                                )
                                if seeders > 0:
                                    magnet = r.get("magnet", "")
                                    info_hash_match = re.search(
                                        r"xt=urn:btih:([a-zA-Z0-9]+)",
                                        magnet,
                                        re.IGNORECASE,
                                    )
                                    info_hash = (
                                        info_hash_match.group(1)
                                        if info_hash_match
                                        else ""
                                    )

                                    with popup.results_lock:
                                        popup.results_pool.append(
                                            {
                                                "source": "solidtorrents",
                                                "name": r.get("title", "Unknown"),
                                                "info_hash": info_hash,
                                                "magnet": magnet,
                                                "size": self._safe_int(
                                                    r.get("size", 0)
                                                ),
                                                "seeders": seeders,
                                                "leechers": self._safe_int(
                                                    r.get("swarm", {}).get(
                                                        "leechers", 0
                                                    )
                                                ),
                                            }
                                        )
                                    count += 1
                            self.ui_queue.put(
                                lambda: sol_lbl.configure(
                                    text=f"✅ Solid ({count})", text_color="#2FA572"
                                )
                            )
                        else:
                            self.ui_queue.put(
                                lambda: sol_lbl.configure(
                                    text="❌ Solid", text_color="#C0392B"
                                )
                            )
                    except Exception as e:
                        logger.warning(f"SolidTorrents search failed: {e}")
                        self.ui_queue.put(
                            lambda: sol_lbl.configure(
                                text="❌ Solid", text_color="#C0392B"
                            )
                        )

                def fetch_yts():
                    if not popup.is_movie:
                        return
                    try:
                        imdb_id = popup.imdb_id_cache
                        if imdb_id:
                            yts_imdb = (
                                imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
                            )
                            url = f"https://yts.mx/api/v2/list_movies.json?query_term={yts_imdb}"
                            res = self.api_get(url, timeout=10)
                            if res.status_code == 200:
                                data = res.json()
                                count = 0
                                if data.get("status") == "ok" and data.get(
                                    "data", {}
                                ).get("movies"):
                                    movie = data["data"]["movies"][0]
                                    title = movie.get(
                                        "title_long", movie.get("title", "Unknown")
                                    )
                                    for t in movie.get("torrents", []):
                                        seeders = t.get("seeds", 0)
                                        if seeders > 0:
                                            hash_str = t.get("hash", "")
                                            magnet = f"magnet:?xt=urn:btih:{hash_str}&dn={urllib.parse.quote(title)}"
                                            torrent_url = t.get("url", "")
                                            qual = t.get("quality", "")
                                            rtype = t.get("type", "")
                                            full_name = (
                                                f"YTS {title} [{qual}] [{rtype}]"
                                            )

                                            with popup.results_lock:
                                                popup.results_pool.append(
                                                    {
                                                        "source": "yts",
                                                        "name": full_name,
                                                        "info_hash": hash_str,
                                                        "magnet": magnet,
                                                        "torrent_url": torrent_url,
                                                        "size": t.get("size_bytes", 0),
                                                        "seeders": seeders,
                                                        "leechers": t.get("peers", 0),
                                                    }
                                                )
                                            count += 1
                                self.ui_queue.put(
                                    lambda: yts_lbl.configure(
                                        text=f"✅ YTS ({count})", text_color="#2FA572"
                                    )
                                )
                            else:
                                self.ui_queue.put(
                                    lambda: yts_lbl.configure(
                                        text="❌ YTS", text_color="#C0392B"
                                    )
                                )
                        else:
                            self.ui_queue.put(
                                lambda: yts_lbl.configure(
                                    text="❌ No IMDB", text_color="#C0392B"
                                )
                            )
                    except Exception as e:
                        logger.warning(f"YTS search failed: {e}")
                        self.ui_queue.put(
                            lambda: yts_lbl.configure(
                                text="❌ YTS", text_color="#C0392B"
                            )
                        )

                threads = [
                    threading.Thread(target=thread_wrapper(fetch_apibay)),
                    threading.Thread(target=thread_wrapper(fetch_eztv)),
                    threading.Thread(target=thread_wrapper(fetch_solidtorrents)),
                    threading.Thread(target=thread_wrapper(fetch_yts)),
                ]

                with popup.thread_lock:
                    popup.active_threads = len(threads)

                for t in threads:
                    t.start()

            threading.Thread(target=run_searches_async).start()

        execute_manual_search()

    # ==========================================
    # SETTINGS MODAL
    # ==========================================
    def open_settings_window(self):
        win = ctk.CTkToplevel(self)
        win.title("System Parameters")
        w, h = 600, 500
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        win.transient(self)
        win.grab_set()
        win.configure(fg_color=BG_BASE)

        c = ctk.CTkFrame(win, fg_color="transparent")
        c.pack(fill="both", expand=True, padx=25, pady=25)

        ctk.CTkLabel(
            c, text="System Configurations", font=ctk.CTkFont(size=20, weight="bold")
        ).pack(anchor="w", pady=(0, 20))

        f1 = ctk.CTkFrame(c, fg_color="transparent")
        f1.pack(fill="x", pady=8)
        ctk.CTkLabel(f1, text="Preferred Quality Profile:", text_color="#A4B2C6").pack(
            side="left"
        )
        self.quality_var = ctk.StringVar(value=self.settings.get("quality", "1080p"))
        ctk.CTkOptionMenu(
            f1,
            values=["Any", "720p", "1080p", "2160p", "x265/HEVC"],
            variable=self.quality_var,
            fg_color=GLASS_CARD,
            button_color=GLASS_EDGE,
        ).pack(side="right")

        f2 = ctk.CTkFrame(c, fg_color="transparent")
        f2.pack(fill="x", pady=8)
        ctk.CTkLabel(f2, text="Storage Output Directory:", text_color="#A4B2C6").pack(
            side="left"
        )
        self.dl_dir_var = ctk.StringVar(
            value=self.settings.get("download_dir", TORRENTS_DIR)
        )
        ctk.CTkEntry(
            f2,
            textvariable=self.dl_dir_var,
            width=220,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
        ).pack(side="left", padx=10, expand=True, fill="x")
        ctk.CTkButton(
            f2,
            text="Browse",
            width=60,
            fg_color=ACCENT_COLOR,
            command=self.browse_directory,
        ).pack(side="right")

        f4 = ctk.CTkFrame(c, fg_color="transparent")
        f4.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(
            f4, text="TMDB API Key (for Movie Releases & TV Discovery):", text_color="#A4B2C6"
        ).pack(side="left")
        self.tmdb_key_var = ctk.StringVar(value=self.settings.get("tmdb_api_key", ""))
        ctk.CTkEntry(
            f4,
            textvariable=self.tmdb_key_var,
            width=220,
            fg_color=GLASS_CARD,
            border_color=GLASS_EDGE,
        ).pack(side="left", padx=10, expand=True, fill="x")

        link_f = ctk.CTkFrame(c, fg_color="transparent")
        link_f.pack(fill="x", pady=(0, 8))
        link_lbl = ctk.CTkLabel(
            link_f,
            text="Get an API key here: https://www.themoviedb.org/settings/api",
            text_color="#5D8AA8",
            cursor="hand2",
            font=ctk.CTkFont(size=10, underline=True),
        )
        link_lbl.pack(side="right", padx=10)
        link_lbl.bind(
            "<Button-1>",
            lambda e: webbrowser.open("https://www.themoviedb.org/settings/api"),
        )

        msg = ctk.CTkLabel(c, text="", text_color="#2FA572", font=ctk.CTkFont(size=12))
        msg.pack(side="bottom", pady=5)

        def save():
            self.settings["quality"] = self.quality_var.get()
            self.settings["download_dir"] = self.dl_dir_var.get()
            self.settings["tmdb_api_key"] = self.tmdb_key_var.get().strip()
            self.save_settings()
            msg.configure(text="Local preferences synced to disk successfully.")
            self.after(2000, win.destroy)

        ctk.CTkButton(
            c,
            text="Commit Parameters",
            height=32,
            font=ctk.CTkFont(weight="bold"),
            fg_color=ACCENT_COLOR,
            hover_color=ACCENT_HOVER,
            border_width=0,
            command=save,
        ).pack(side="bottom", fill="x", pady=15)

    def browse_directory(self):
        d = filedialog.askdirectory()
        if d:
            self.dl_dir_var.set(d)


# ==========================================
# MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    try:
        app = AirGrabber()
        app.mainloop()
    except Exception as e:
        error_msg = traceback.format_exc()
        logger.critical(f"Unhandled exception:\n{error_msg}")
        try:
            import tkinter.messagebox as msg

            msg.showerror(
                "AirGrabber Fatal Error",
                f"The application crashed.\n\nPlease check the log file:\n{LOG_FILE}\n\nError:\n{str(e)}",
            )
        except:
            pass
        print(f"Fatal error. See log: {LOG_FILE}")
        time.sleep(3)
        sys.exit(1)
