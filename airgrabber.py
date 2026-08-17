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
from datetime import datetime, timedelta
from functools import wraps
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

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
        except ImportError:
            print(f"Missing dependency '{pip_name}'. Installing now...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])

ensure_dependencies()
import customtkinter as ctk
import requests
import cloudscraper
from PIL import Image, ImageOps, ImageTk
import tkinter.filedialog as filedialog

# ==========================================
# GLOBAL HTTP SESSIONS
# ==========================================
http_session = requests.Session()
http_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*'
})

scraper_session = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})

# ==========================================
# CONFIGURATION & THEME
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "followed_shows.json")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "history.json")
EPISODES_FILE = os.path.join(SCRIPT_DIR, "episodes_cache.json")
SIZE_CACHE_FILE = os.path.join(SCRIPT_DIR, "size_cache.json")
TORRENTS_DIR = os.path.join(SCRIPT_DIR, "torrents")
POSTERS_DIR = os.path.join(SCRIPT_DIR, "posters_cache")

os.makedirs(POSTERS_DIR, exist_ok=True)
os.makedirs(TORRENTS_DIR, exist_ok=True)

# Setup enhanced logging
LOG_FILE = os.path.join(SCRIPT_DIR, "airgrabber.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# AIRGRABBER COLOR PALETTE (Muted Purple Theme)
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
# RETRY DECORATOR WITH 429 HANDLING
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
                        retry_after = int(e.response.headers.get('Retry-After', _delay * 2))
                        logger.warning(f"Rate limited on {func.__name__}. Retrying after {retry_after}s")
                        time.sleep(retry_after)
                        _delay = retry_after
                    else:
                        if attempt == max_attempts - 1:
                            logger.error(f"HTTPError in {func.__name__} after {max_attempts} attempts: {e}")
                            raise
                        time.sleep(_delay)
                        _delay *= backoff
                except exceptions as ex:
                    if attempt == max_attempts - 1:
                        logger.error(f"Exception in {func.__name__} after {max_attempts} attempts: {ex}")
                        raise
                    time.sleep(_delay)
                    _delay *= backoff
            return None
        return wrapper
    return decorator

# ==========================================
# THREAD-SAFE LRU IMAGE CACHE
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

    def clear(self):
        with self._lock:
            self._cache.clear()

# ==========================================
# MAIN APPLICATION
# ==========================================
class TorlinkCalendarApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("AirGrabber - Your TV radar, automated.")
        logger.info("Initializing AirGrabber application...")
        self.configure(fg_color=BG_BASE)
        
        window_width = 1650
        window_height = 900
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x_cordinate = int((screen_width / 2) - (window_width / 2))
        y_cordinate = int((screen_height / 2) - (window_height / 2))
        self.geometry(f"{window_width}x{window_height}+{x_cordinate}+{y_cordinate}")
        
        self.data_lock = threading.RLock()
        self.prefetch_executor = ThreadPoolExecutor(max_workers=2)
        
        self.settings = self.load_settings()
        self.followed_shows = self.load_data()
        self.history = self.load_history()
        
        self.episodes_cache = self.load_episodes_cache() 
        self.size_cache = self.load_json_dict(SIZE_CACHE_FILE)
        
        self.image_cache = LRUImageCache(maxsize=200)
        self.unfollowed_cache = {}
        self.calendar_day_frames = {}
        
        self.size_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        
        self.poll_ui_queue()
        
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

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
            segmented_button_unselected_hover_color="#2A2130",
            command=self.on_tab_change
        )
        self.tabview.grid(row=0, column=0, padx=15, pady=5, sticky="nsew")
        self.tabview._segmented_button.configure(font=ctk.CTkFont(size=18, weight="bold"))
        
        self.tab_calendar = self.tabview.add("Calendar")
        self.tab_discover = self.tabview.add("Discover")
        self.tab_library = self.tabview.add("Tracked")
        self.tab_settings = self.tabview.add("Settings")
        
        self.setup_calendar_tab()
        self.setup_discover_tab()
        self.setup_library_tab()
        self.setup_settings_tab()
        
        self._sync_timer = None
        self._sync_running = False
        
        threading.Thread(target=self.load_app_logo, daemon=True).start()
        
        self.refresh_calendar_data()
        self.refresh_library_list()
        self.after(500, self.fetch_discover_categories)
        
        self.start_auto_fetch_daemon()
        self.start_background_library_sync()
        self.start_size_prefetch_worker()

    def load_app_logo(self):
        url = "https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/refs/heads/main/logo.png"
        logger.info(f"Fetching remote logo from: {url}")
        pil_img = self.fetch_pil_image(url)
        if pil_img:
            try:
                def apply_icon():
                    try:
                        icon_img = ImageTk.PhotoImage(pil_img)
                        self.iconphoto(False, icon_img)
                    except Exception as e:
                        logger.error(f"Could not apply window icon: {e}")
                self.ui_queue.put(apply_icon)
            except Exception as e:
                logger.error(f"Could not load icon thread: {e}")
            
            w, h = pil_img.size
            aspect = w / h
            new_h = 75 
            new_w = int(new_h * aspect)
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(new_w, new_h))
            self.ui_queue.put(lambda: hasattr(self, 'calendar_logo_lbl') and self.calendar_logo_lbl.winfo_exists() and self.calendar_logo_lbl.configure(image=ctk_img, text=""))

    def poll_ui_queue(self):
        try:
            for _ in range(15):  
                task = self.ui_queue.get_nowait()
                task()
        except queue.Empty:
            pass
        self.after(50, self.poll_ui_queue)

    def refresh_calendar_data(self):
        self.ui_queue.put(self.build_calendar_ui)

    def load_data(self):
        with self.data_lock:
            data = {}
            if os.path.exists(DATA_FILE):
                try:
                    with open(DATA_FILE, "r") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict) and raw:
                        for k, v in raw.items():
                            if isinstance(v, str):
                                data[str(k)] = {"name": v, "auto": False, "metadata": None}
                            else:
                                if "metadata" not in v:
                                    v["metadata"] = None
                                data[str(k)] = v
                except Exception as e:
                    logger.error(f"Error loading followed shows data: {e}")
            return data
        
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
                except Exception as e:
                    logger.error(f"Error loading history: {e}")
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
                except Exception as e:
                    logger.error(f"Error loading JSON dictionary {filepath}: {e}")
            return {}

    def load_episodes_cache(self):
        return self.load_json_dict(EPISODES_FILE)
        
    def save_episodes_cache(self):
        with self.data_lock:
            with open(EPISODES_FILE, "w") as f:
                json.dump(self.episodes_cache, f)

    def save_size_cache(self):
        with self.data_lock:
            with open(SIZE_CACHE_FILE, "w") as f:
                json.dump(self.size_cache, f)
            
    def load_settings(self):
        default_settings = {
            "first_day": "Monday", 
            "calendar_view": "Vertical", 
            "quality": "1080p", 
            "download_dir": TORRENTS_DIR,
            "auto_fetch_days": 5,
            "weeks_to_show": 3,
            "create_mediaforge_json": True
        }
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    loaded = json.load(f)
                    default_settings.update(loaded)
            except Exception as e:
                logger.error(f"Error loading settings: {e}")
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

    @retry(max_attempts=3, delay=1, exceptions=(requests.RequestException,))
    def fetch_pil_image(self, url):
        if not url:
            return None
        url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()
        ext = os.path.splitext(url.split('/')[-1])[1] or '.jpg'
        local_path = os.path.join(POSTERS_DIR, f"{url_hash}{ext}")
        
        pil_img = self.image_cache.get(url)
        if pil_img:
            return pil_img
            
        try:
            if os.path.exists(local_path):
                pil_img = Image.open(local_path)
            else:
                resp = http_session.get(url, timeout=5)
                resp.raise_for_status()
                pil_img = Image.open(io.BytesIO(resp.content))
                pil_img.save(local_path)
            self.image_cache.put(url, pil_img)
            return pil_img
        except Exception as e:
            logger.error(f"Error fetching image from URL {url}: {e}")
            return None

    def show_loading(self, parent_frame):
        loader = ctk.CTkProgressBar(parent_frame, mode="indeterminate", height=4, progress_color=ACCENT_COLOR)
        loader.grid(row=0, column=0, columnspan=10, sticky="ew", pady=(0, 2))
        loader.start()
        return loader

    def hide_loading(self, loader_widget):
        if loader_widget:
            loader_widget.stop()
            loader_widget.destroy()

    def on_tab_change(self):
        tab = self.tabview.get()
        if tab == "Calendar":
            self.refresh_calendar_data()
        elif tab == "Tracked":
            self.refresh_library_list()

    def search_best_torrent(self, show_name, episode_code, show_id=None):
        quality_pref = self.settings.get("quality", "1080p")
        q_str = "" if quality_pref == "Any" else ("x265" if quality_pref == "x265/HEVC" else quality_pref)

        clean_show = re.sub(r"[^\w\s]", " ", show_name)
        clean_show = " ".join(clean_show.split())
        query_variants = [
            f"{clean_show} {episode_code} {q_str}".strip(),
            f"{clean_show} {episode_code}".strip(),
            f"{show_name} {episode_code} {q_str}".strip(),
            f"{show_name} {episode_code}".strip()
        ]
        
        logger.info(f"Initiating search chain for show: '{show_name}' ({episode_code}) with preference '{quality_pref}'")

        # 1. APIBAY
        for query in query_variants:
            url = f"https://apibay.org/q.php?q={urllib.parse.quote(query)}"
            try:
                logger.debug(f"Querying APIBay: {url}")
                res = http_session.get(url, timeout=5)
                res.raise_for_status()
                data = res.json()
                if isinstance(data, list) and len(data) > 0 and data[0].get('id') != '0':
                    valid = [r for r in data if int(r.get('seeders', 0)) > 0]
                    if valid:
                        filtered = self.apply_quality_filter(valid)
                        if filtered:
                            filtered.sort(key=lambda x: int(x['seeders']), reverse=True)
                            best = filtered[0]
                            logger.info(f"Found match on APIBay: {best['name']}")
                            return {
                                "source": "apibay",
                                "name": best['name'],
                                "info_hash": best['info_hash'],
                                "magnet": "",
                                "size": int(best.get('size', 0)),
                                "seeders": int(best.get('seeders', 0))
                            }
                        if quality_pref == "Any":
                            valid.sort(key=lambda x: int(x['seeders']), reverse=True)
                            best = valid[0]
                            return {
                                "source": "apibay",
                                "name": best['name'],
                                "info_hash": best['info_hash'],
                                "size": best.get('size', 0),
                                "seeders": int(best.get('seeders', 0))
                            }
            except Exception as e:
                logger.debug(f"APIBay query failed for '{query}': {e}")

        match = re.search(r'S(\d+)E(\d+)', episode_code, re.IGNORECASE)
        season_num = int(match.group(1)) if match else 1
        episode_num = int(match.group(2)) if match else 1
        
        imdb_id = None
        if show_id:
            with self.data_lock:
                show_data = self.followed_shows.get(str(show_id), {})
                meta = show_data.get('metadata')
                if meta and meta.get('externals'):
                    imdb_id = meta['externals'].get('imdb')

        if not imdb_id:
            return None
        
        if not imdb_id.startswith('tt'):
            imdb_id = f"tt{imdb_id}"

        # 2. TORRENTIO
        try:
            url_torrentio = f"https://torrentio.strem.fun/stream/series/{imdb_id}:{season_num}:{episode_num}.json"
            logger.debug(f"Querying Torrentio: {url_torrentio}")
            res = http_session.get(url_torrentio, timeout=6)
            if res.status_code == 200:
                streams = res.json().get('streams', [])
                valid_streams = []
                for s in streams:
                    title = s.get('title', '')
                    title_lower = title.lower()
                    
                    seed_match = re.search(r'👤\s*(\d+)', title)
                    seeders = int(seed_match.group(1)) if seed_match else 0
                    
                    name = s.get('title', '').split('\n')[0]
                    
                    if seeders > 0:
                        if quality_pref == "Any" or (quality_pref == "x265/HEVC" and ("x265" in title_lower or "hevc" in title_lower)) or (q_str.lower() in title_lower):
                            magnet = s.get('url', '')
                            info_hash_match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', magnet, re.IGNORECASE)
                            info_hash = info_hash_match.group(1) if info_hash_match else ""

                            size_match = re.search(r'💾\s*([\d.]+)\s*([A-Za-z]+)', title)
                            size_bytes = 0
                            if size_match:
                                val = float(size_match.group(1))
                                unit = size_match.group(2).upper()
                                if unit == 'GB': size_bytes = val * 1024**3
                                elif unit == 'MB': size_bytes = val * 1024**2
                                elif unit == 'KB': size_bytes = val * 1024
                            
                            valid_streams.append({
                                'source': 'torrentio',
                                'name': name,
                                'magnet': magnet,
                                'info_hash': info_hash,
                                'size': size_bytes,
                                'seeders': seeders
                            })
                
                if valid_streams:
                    valid_streams.sort(key=lambda x: x['seeders'], reverse=True)
                    logger.info(f"Found match on Torrentio: {valid_streams[0]['name']}")
                    return valid_streams[0]
        except Exception as e:
            logger.debug(f"Torrentio fallback failed: {e}")

        # 3. EZTV
        try:
            eztv_imdb = imdb_id.replace('tt', '')
            url_eztv = f"https://eztv.re/api/get-torrents?imdb_id={eztv_imdb}"
            logger.debug(f"Querying EZTV: {url_eztv}")
            res = http_session.get(url_eztv, timeout=6)
            if res.status_code == 200:
                for t in res.json().get('torrents', []):
                    if str(t.get('season')) == str(season_num) and str(t.get('episode')) == str(episode_num):
                        seeders = int(t.get('seeds', 0))
                        if seeders > 0:
                            logger.info(f"Found match on EZTV: {t.get('title', '')}")
                            return {
                                'source': 'eztv',
                                'name': t.get('title', ''),
                                'magnet': t.get('magnet_url', ''),
                                'info_hash': t.get('hash', ''),
                                'size': int(t.get('size_bytes', 0)),
                                'seeders': seeders
                            }
        except Exception as e:
            logger.debug(f"EZTV fallback failed: {e}")

        # 4. SOLIDTORRENTS
        try:
            url_solid = f"https://solidtorrents.to/api/v1/search?q={urllib.parse.quote(query_variants[0])}&category=Video"
            logger.debug(f"Querying SolidTorrents: {url_solid}")
            res = http_session.get(url_solid, timeout=6)
            if res.status_code == 200:
                data = res.json()
                valid_solid = []
                for r in data.get('results', []):
                    seeders = int(r.get('swarm', {}).get('seeders', 0))
                    if seeders > 0:
                        magnet = r.get('magnet', '')
                        hash_match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', magnet, re.IGNORECASE)
                        valid_solid.append({
                            'source': 'solidtorrents',
                            'name': r.get('title', ''),
                            'magnet': magnet,
                            'info_hash': hash_match.group(1) if hash_match else "",
                            'size': int(r.get('size', 0)),
                            'seeders': seeders
                        })
                if valid_solid:
                    valid_solid.sort(key=lambda x: x['seeders'], reverse=True)
                    logger.info(f"Found match on SolidTorrents: {valid_solid[0]['name']}")
                    return valid_solid[0]
        except Exception as e:
            logger.debug(f"SolidTorrents fallback failed: {e}")

        logger.warning(f"No valid torrent found across all providers for {show_name} {episode_code}")
        return None

    def apply_quality_filter(self, results):
        quality_pref = self.settings.get("quality", "1080p")
        valid_results = []
        q_str = "" if quality_pref == "Any" else ("x265" if quality_pref == "x265/HEVC" else quality_pref)
        
        if isinstance(results, list) and len(results) > 0:
            for res in results:
                s = int(res.get('seeders', 0))
                if s > 0:
                    name_lower = res.get('name', '').lower()
                    if quality_pref == "Any":
                        valid_results.append(res)
                    elif quality_pref == "x265/HEVC" and ("x265" in name_lower or "hevc" in name_lower):
                        valid_results.append(res)
                    elif q_str.lower() in name_lower:
                        valid_results.append(res)
        return valid_results

    def start_size_prefetch_worker(self):
        def worker():
            while True:
                ep_data = self.size_queue.get()
                self.prefetch_executor.submit(self._do_prefetch, ep_data)
                self.size_queue.task_done()
        threading.Thread(target=worker, daemon=True).start()

    def _do_prefetch(self, ep_data):
        time.sleep(1.5) 
        try:
            cache_key = f"{ep_data['show']}_{ep_data['episode']}_{self.settings.get('quality', '1080p')}"
            with self.data_lock:
                if cache_key in self.size_cache:
                    f_size = self.size_cache[cache_key]
                    self.update_prefetched_size(ep_data, f_size)
                    return
            best = self.search_best_torrent(ep_data['show'], ep_data['episode'], ep_data.get('show_id'))
            if best:
                f_size = self.format_size(best.get('size', 0))
            else:
                f_size = "N/A"
            with self.data_lock:
                self.size_cache[cache_key] = f_size
                self.save_size_cache()
            self.update_prefetched_size(ep_data, f_size)
        except Exception as e:
            logger.error(f"Prefetch error for {ep_data['show']} {ep_data['episode']}: {e}")

    def update_prefetched_size(self, ep_data, size_str):
        btn = ep_data.get('button_ref')
        qual = ep_data.get('qual_str', '')
        if btn is not None:
            if size_str == "N/A":
                self.ui_queue.put(lambda: btn.winfo_exists() and btn.configure(text="No Torrents", fg_color="#C0392B"))
            else:
                self.ui_queue.put(lambda: btn.winfo_exists() and btn.configure(text=f"🧲 {size_str} - {qual}"))

    def start_auto_fetch_daemon(self):
        def daemon():
            time.sleep(10) 
            while True:
                self.run_auto_fetch_cycle()
                time.sleep(43200) 
        threading.Thread(target=daemon, daemon=True).start()

    def run_auto_fetch_cycle(self):
        today = datetime.now().date()
        max_days = int(self.settings.get("auto_fetch_days", 5))
        logger.info(f"Starting auto-fetch cycle (Window: past {max_days} days)")
        
        with self.data_lock:
            show_items = list(self.followed_shows.items())
            history_set = set(self.history)
        
        for show_id, data_dict in show_items:
            if not data_dict.get('auto'):
                continue
            
            if show_id not in self.episodes_cache:
                try:
                    self.episodes_cache[show_id] = self._fetch_episodes(show_id)
                except Exception as e:
                    logger.error(f"Auto-fetch: Could not fetch episodes for show_id {show_id}: {e}")
                    continue
            episodes = self.episodes_cache.get(show_id, [])
            
            for ep in episodes:
                airdate_str = ep.get('airdate')
                if not airdate_str:
                    continue
                try:
                    ep_date = datetime.strptime(airdate_str, "%Y-%m-%d").date()
                    days_since = (today - ep_date).days
                except:
                    continue
                if 0 <= days_since <= max_days:
                    ep_code = f"S{ep.get('season',1):02d}E{ep.get('number',1):02d}"
                    hist_key = f"{show_id}_{ep_code}"
                    if hist_key not in history_set:
                        logger.info(f"Auto-fetching missing episode: {data_dict['name']} {ep_code}")
                        best = self.search_best_torrent(data_dict['name'], ep_code, show_id)
                        if best:
                            episode_payload = {"show_id": show_id, "show": data_dict['name'], "episode": ep_code, "title": ep.get('name', 'Unknown')}
                            self.download_torrent_file(episode_payload, best, self.format_size(best.get('size', 0)))
                        time.sleep(3)

    @retry(max_attempts=3, delay=1)
    def _fetch_episodes(self, show_id):
        resp = http_session.get(f"https://api.tvmaze.com/shows/{show_id}/episodes", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def handle_fetch(self, episode_data):
        btn = episode_data.get('button_ref')
        if btn is not None:
            btn.configure(text="🔄 Scanning...", state="disabled", fg_color="gray40")
        
        def run_search():
            logger.info(f"Manual fetch triggered for: {episode_data['show']} {episode_data['episode']}")
            best = self.search_best_torrent(episode_data['show'], episode_data['episode'], episode_data.get('show_id'))
            if best:
                self.ui_queue.put(lambda: self.download_torrent_file(episode_data, best, self.format_size(best.get('size', 0))))
            else:
                logger.warning(f"Manual fetch failed to find torrent for {episode_data['show']} {episode_data['episode']}")
                self.ui_queue.put(lambda: self.on_fetch_failed(episode_data, "Not Found"))
                
        threading.Thread(target=run_search, daemon=True).start()

    def download_torrent_file(self, episode_data, best_torrent, formatted_size):
        btn = episode_data.get('button_ref')
        qual = episode_data.get('qual_str', '')
        if btn is not None:
            btn.configure(text=f"✅ {formatted_size} - {qual}", text_color="white", fg_color="#3A7D44", hover_color="#2B5E33")
        
        dl_dir = self.settings.get("download_dir", TORRENTS_DIR)
        try:
            os.makedirs(dl_dir, exist_ok=True)
        except Exception as ex:
            logger.error(f"Failed to create download directory '{dl_dir}': {ex}")
            if btn is not None:
                self.ui_queue.put(lambda: self.on_fetch_failed(episode_data, "Dir Error"))
            return
        
        torrent_name = best_torrent.get('name', 'Unknown')
        info_hash = best_torrent.get('info_hash', '').upper()
        magnet_url = best_torrent.get('magnet', '')
        
        safe_filename = re.sub(r'[<>:"/\\|?*\[\]()]+', '_', torrent_name)
        safe_filename = "".join(c for c in safe_filename if c.isalnum() or c in " ._-").strip()
        if not safe_filename:
            safe_filename = "media_download"
        safe_filename = safe_filename[:120] 
        
        def download_task():
            success = False
            
            if info_hash:
                file_path = os.path.join(dl_dir, f"{safe_filename}.torrent")
                cache_urls = [
                    f"https://itorrents.org/torrent/{info_hash}.torrent",
                    f"https://btcache.me/torrent/{info_hash}",
                    f"http://torrage.info/torrent.php?h={info_hash}"
                ]
                
                temp_torrent_path = os.path.join(dl_dir, f"{safe_filename}.torrent.part")
                final_torrent_path = os.path.join(dl_dir, f"{safe_filename}.torrent")
                
                for url in cache_urls:
                    try:
                        headers = {'Referer': 'https://itorrents.org/'} if 'itorrents' in url else {}
                        logger.debug(f"Attempting cache download from: {url}")
                        response = scraper_session.get(url, timeout=10)
                        
                        if response.status_code == 200:
                            content = response.content
                            if b'd8:announce' in content or b'd4:info' in content:
                                with open(temp_torrent_path, 'wb') as f:
                                    f.write(content)
                                os.replace(temp_torrent_path, final_torrent_path)
                                success = True
                                logger.info(f"Successfully downloaded .torrent file to: {final_torrent_path}")
                                break 
                            else:
                                logger.debug(f"Cache mirror {url} returned invalid Bencode data (likely Cloudflare CAPTCHA block).")
                    except Exception as e:
                        logger.debug(f"Cache mirror {url} failed: {e}")
                        continue
            
            if not success:
                try:
                    if not magnet_url and info_hash:
                        magnet_uri = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(torrent_name)}"
                        trackers = [
                            "udp://tracker.opentrackr.org:1337/announce",
                            "udp://9.rarbg.com:2810/announce",
                            "udp://tracker.openbittorrent.com:80/announce"
                        ]
                        for tr in trackers:
                            magnet_uri += f"&tr={urllib.parse.quote(tr)}"
                    else:
                        magnet_uri = magnet_url
                        
                    if magnet_uri:
                        temp_magnet_path = os.path.join(dl_dir, f"{safe_filename}.magnet.part")
                        final_magnet_path = os.path.join(dl_dir, f"{safe_filename}.magnet")
                        
                        with open(temp_magnet_path, "w", encoding="utf-8") as f:
                            f.write(magnet_uri)
                        os.replace(temp_magnet_path, final_magnet_path)
                            
                        success = True
                        logger.info(f"Successfully generated fallback .magnet file at: {final_magnet_path}")
                except Exception as e:
                    logger.error(f"Failed to write magnet file to disk: {str(e)}")
                    
            if success:
                if self.settings.get("create_mediaforge_json", True) and episode_data.get('show'):
                    try:
                        mf_metadata = {
                            "media_type": "tv", 
                            "show_name": episode_data.get('show', 'Unknown'), 
                            "episode_code": episode_data.get('episode', ''), 
                            "title": episode_data.get('title', 'Manual Download'), 
                            "tvmaze_id": episode_data.get('show_id', '0')
                        }
                        temp_meta_path = os.path.join(dl_dir, f"{safe_filename}_mediaforge.json.part")
                        final_meta_path = os.path.join(dl_dir, f"{safe_filename}_mediaforge.json")
                        
                        with open(temp_meta_path, 'w', encoding="utf-8") as mf:
                            json.dump(mf_metadata, mf, indent=4)
                        os.replace(temp_meta_path, final_meta_path)
                        
                        logger.info(f"Successfully created MediaForge metadata file: {final_meta_path}")
                    except Exception as e:
                        logger.error(f"Failed to save metadata JSON: {str(e)}")
                try:
                    if episode_data.get('show_id'):
                        hist_key = f"{episode_data['show_id']}_{episode_data.get('episode', '')}"
                        with self.data_lock:
                            if hist_key not in self.history:
                                self.history.append(hist_key)
                                self.save_history()
                except Exception as e:
                    logger.error(f"Failed to update history: {str(e)}")
            else:
                logger.error(f"Critical Error: Failed to save torrent or magnet file for '{torrent_name}'")
                if btn is not None:
                    self.ui_queue.put(lambda: self.on_fetch_failed(episode_data, "DL Failed"))

        threading.Thread(target=download_task, daemon=True).start()

    def on_fetch_failed(self, episode_data, reason):
        btn = episode_data.get('button_ref')
        qual = episode_data.get('qual_str', '')
        if btn is not None:
            btn.configure(text=f"❌ {reason}", text_color="white", fg_color="#C0392B") 
            self.after(3000, lambda: btn.configure(text=f"🧲 No Torrents" if reason == "Not Found" else f"🧲 -- MB - {qual}", state="normal", fg_color=ACCENT_COLOR))
        
    def start_background_library_sync(self):
        if hasattr(self, '_sync_timer') and self._sync_timer:
            try:
                self.after_cancel(self._sync_timer)
            except Exception:
                pass
        self._sync_timer = self.after(5000, self._run_library_sync)

    def _run_library_sync(self):
        if self._sync_running:
            return
        self._sync_running = True
        def sync():
            try:
                updated_shows = False
                updated_eps = False
                with self.data_lock:
                    show_ids = list(self.followed_shows.keys())
                for show_id in show_ids:
                    try:
                        data = self._fetch_show_metadata(show_id)
                        with self.data_lock:
                            if show_id in self.followed_shows:
                                self.followed_shows[show_id]["metadata"] = data
                                updated_shows = True
                        self.episodes_cache[show_id] = data.get('_embedded', {}).get('episodes', [])
                        updated_eps = True
                        img_data = data.get('image')
                        if img_data and img_data.get('medium'):
                            threading.Thread(target=self.fetch_pil_image, args=(img_data['medium'],), daemon=True).start()
                        time.sleep(0.5)
                    except Exception as e:
                        logger.error(f"Error syncing show {show_id}: {e}")
                if updated_shows:
                    self.save_data()
                if updated_eps:
                    self.save_episodes_cache()
            finally:
                self._sync_running = False
        threading.Thread(target=sync, daemon=True).start()

    @retry(max_attempts=3, delay=1, exceptions=(requests.RequestException,))
    def _fetch_show_metadata(self, show_id):
        url = f"https://api.tvmaze.com/shows/{show_id}?embed[]=episodes&embed[]=seasons"
        resp = http_session.get(url, timeout=5)
        resp.raise_for_status()
        return resp.json()

    # ==========================================
    # TAB 1: CALENDAR 
    # ==========================================
    def setup_calendar_tab(self):
        self.tab_calendar.grid_columnconfigure(0, weight=1)
        self.tab_calendar.grid_rowconfigure(3, weight=1)

        self.cal_loader_frame = ctk.CTkFrame(self.tab_calendar, fg_color="transparent", height=4)
        self.cal_loader_frame.grid(row=0, column=0, sticky="ew")

        controls_frame = ctk.CTkFrame(self.tab_calendar, fg_color="transparent")
        controls_frame.grid(row=1, column=0, padx=15, pady=5, sticky="ew")
        
        controls_frame.grid_columnconfigure(0, weight=1, uniform="hdr")
        controls_frame.grid_columnconfigure(1, weight=1, uniform="hdr")
        controls_frame.grid_columnconfigure(2, weight=1, uniform="hdr")

        # Global Search Button added to column 0
        manual_search_btn = ctk.CTkButton(
            controls_frame, text="🔍 Manual Search", width=120, height=28, 
            font=ctk.CTkFont(weight="bold"), fg_color=GLASS_CARD, hover_color=GLASS_EDGE, 
            border_width=1, border_color=GLASS_EDGE, command=self.open_generic_manual_search
        )
        manual_search_btn.grid(row=0, column=0, sticky="w")

        self.calendar_logo_lbl = ctk.CTkLabel(controls_frame, text="AirGrabber", font=ctk.CTkFont(size=28, weight="bold"), text_color=ACCENT_COLOR)
        self.calendar_logo_lbl.grid(row=0, column=1, sticky="")

        self.weeks_var = ctk.StringVar(value=str(self.settings.get("weeks_to_show", 3)))
        weeks_frame = ctk.CTkFrame(controls_frame, fg_color="transparent")
        weeks_frame.grid(row=0, column=2, sticky="e")
        
        ctk.CTkLabel(weeks_frame, text="Weeks: ", text_color="gray70", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 5))
        weeks_menu = ctk.CTkOptionMenu(weeks_frame, values=["1", "2", "3", "4", "5"], height=28, fg_color=GLASS_CARD, button_color=GLASS_EDGE, variable=self.weeks_var, command=lambda e: self.build_calendar_ui(), width=70)
        weeks_menu.pack(side="left")

        self.cal_header_frame = ctk.CTkFrame(self.tab_calendar, fg_color="transparent")
        self.calendar_scroll = None

    def open_generic_manual_search(self):
        dummy_ep = {
            'show': '',
            'episode': '',
            'title': 'Manual Download',
            'show_id': None,
            'qual_str': '',
            'button_ref': None
        }
        self.open_manual_search(dummy_ep)

    def build_calendar_ui(self):
        if self.calendar_scroll:
            self.calendar_scroll.destroy()
        for widget in self.cal_header_frame.winfo_children():
            widget.destroy()

        self.calendar_day_frames = {}
        view_mode = self.settings.get("calendar_view", "Vertical")

        if view_mode == "Horizontal":
            self.cal_header_frame.grid_remove()
            self.calendar_scroll = ctk.CTkScrollableFrame(
                self.tab_calendar, fg_color="transparent",
                orientation="horizontal",
                scrollbar_button_color=TAB_BG,
                scrollbar_button_hover_color=TAB_BG,
                scrollbar_fg_color="transparent"
            )
            self.calendar_scroll.grid(row=3, column=0, sticky="nsew", padx=15, pady=(0, 5))
        else:
            self.cal_header_frame.grid(row=2, column=0, sticky="ew", padx=15, pady=(5, 0))
            self.calendar_scroll = ctk.CTkScrollableFrame(
                self.tab_calendar, fg_color="transparent",
                orientation="vertical",
                scrollbar_button_color=TAB_BG,
                scrollbar_button_hover_color=TAB_BG,
                scrollbar_fg_color="transparent"
            )
            self.calendar_scroll.grid(row=3, column=0, sticky="nsew", padx=15, pady=(0, 5))
            for i in range(7):
                self.cal_header_frame.grid_columnconfigure(i, weight=1, uniform="day")
                self.calendar_scroll.grid_columnconfigure(i, weight=1, uniform="day")

        if not self.followed_shows:
            ctk.CTkLabel(self.calendar_scroll, text="Your calendar is empty. Head to Discover to track some shows!",
                         text_color="gray60").grid(row=0, column=0, columnspan=7, pady=100)
            return

        weeks_to_show = int(self.weeks_var.get())
        self.settings["weeks_to_show"] = weeks_to_show
        self.save_settings()

        today = datetime.now().date()

        if self.settings.get("first_day") == "Sunday":
            start_date = today - timedelta(days=(today.weekday() + 1) % 7)
            days_of_week = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        else:
            start_date = today - timedelta(days=today.weekday())
            days_of_week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        schedule = {}
        with self.data_lock:
            shows_copy = dict(self.followed_shows)
            eps_copy = dict(self.episodes_cache)
        for show_id, data_dict in shows_copy.items():
            for ep in eps_copy.get(show_id, []):
                airdate = ep.get('airdate')
                if airdate:
                    if airdate not in schedule:
                        schedule[airdate] = []
                    schedule[airdate].append({
                        "show_id": show_id,
                        "show": data_dict['name'],
                        "episode": f"S{ep.get('season',1):02d}E{ep.get('number',1):02d}",
                        "title": ep.get('name', 'Unknown'),
                        "runtime": ep.get('runtime', '')
                    })

        days_to_show = weeks_to_show * 7
        max_daily_tracked = 0
        for i in range(days_to_show):
            d_str = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
            max_daily_tracked = max(max_daily_tracked, len(schedule.get(d_str, [])))

        if view_mode == "Horizontal":
            for day_offset in range(days_to_show):
                current_date = start_date + timedelta(days=day_offset)
                date_str = current_date.strftime("%Y-%m-%d")
                day_episodes = schedule.get(date_str, [])

                col_frame = ctk.CTkScrollableFrame(
                    self.calendar_scroll, fg_color="transparent", width=240,
                    scrollbar_button_color=TAB_BG,
                    scrollbar_button_hover_color=TAB_BG,
                    scrollbar_fg_color="transparent"
                )
                col_frame.pack(side="left", fill="y", expand=True, padx=2, pady=2)
                self.calendar_day_frames[date_str] = col_frame

                header_color = ACCENT_COLOR if current_date == today else "white"
                ctk.CTkLabel(col_frame, text=f"{current_date.strftime('%a').upper()}, {current_date.strftime('%b %d').upper()}",
                             text_color=header_color, font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")

                ep_count = len(day_episodes)
                ctk.CTkLabel(col_frame, text=f"{ep_count} Tracked Episode{'s' if ep_count != 1 else ''}",
                             text_color="gray60", font=ctk.CTkFont(size=10)).pack(anchor="w", pady=(0, 4))

                for ep_data in day_episodes:
                    self.create_vertical_calendar_episode(col_frame, ep_data, current_date)
        else:
            for i, day in enumerate(days_of_week):
                ctk.CTkLabel(self.cal_header_frame, text=day, text_color="gray60",
                             font=ctk.CTkFont(weight="bold", size=13)).grid(row=0, column=i, pady=(0, 2))

            for week in range(weeks_to_show):
                self.calendar_scroll.grid_rowconfigure(week, weight=0)
                for day in range(7):
                    current_date = start_date + timedelta(days=(week * 7) + day)
                    date_str = current_date.strftime("%Y-%m-%d")
                    day_episodes = schedule.get(date_str, [])

                    is_today = current_date == today
                    bg_color = "#182133" if is_today else "#121620"
                    border = "#1F3B60" if is_today else "#1C222E"

                    cell_frame = ctk.CTkFrame(self.calendar_scroll, corner_radius=6,
                                              fg_color=bg_color, border_width=1, border_color=border)
                    cell_frame.grid(row=week, column=day, sticky="nsew", padx=3, pady=3)
                    cell_frame.grid_columnconfigure(0, weight=1)
                    self.calendar_day_frames[date_str] = cell_frame

                    header_frame = ctk.CTkFrame(cell_frame, fg_color="transparent")
                    header_frame.pack(fill="x", padx=4, pady=2)

                    ctk.CTkLabel(header_frame, text=current_date.strftime("%b %d"),
                                 text_color=ACCENT_COLOR if is_today else "gray60",
                                 font=ctk.CTkFont(size=11, weight="bold")).pack(side="right")

                    ep_count = len(day_episodes)
                    if ep_count > 0:
                        ctk.CTkLabel(header_frame, text=f"{ep_count} Ep{'s' if ep_count > 1 else ''}",
                                     text_color="gray50", font=ctk.CTkFont(size=10, weight="bold")).pack(side="left")

                    for ep_data in day_episodes:
                        self.create_vertical_calendar_episode(cell_frame, ep_data, current_date)
                        
        threading.Thread(target=self._fetch_and_render_unfollowed, args=(start_date, days_to_show, schedule, max_daily_tracked), daemon=True).start()

    def _fetch_and_render_unfollowed(self, start_date, days_to_show, schedule, max_daily_tracked):
        target_count = max(3, max_daily_tracked)
        
        for i in range(days_to_show):
            current = start_date + timedelta(days=i)
            date_str = current.strftime("%Y-%m-%d")
            tracked_count = len(schedule.get(date_str, []))
            needed = target_count - tracked_count
            
            if needed <= 0:
                continue
                
            if date_str not in self.unfollowed_cache:
                try:
                    res = http_session.get(f"https://api.tvmaze.com/schedule?date={date_str}", timeout=5)
                    if res.status_code == 200:
                        raw_data = res.json()
                        valid = []
                        for item in raw_data:
                            show = item.get('show', {})
                            if show.get('type') in ['Scripted', 'Animation'] and show.get('language') == 'English' and show.get('weight', 0) > 40:
                                valid.append(item)
                        valid.sort(key=lambda x: x['show'].get('weight', 0), reverse=True)
                        self.unfollowed_cache[date_str] = valid[:15]
                except Exception:
                    pass
            
            items = self.unfollowed_cache.get(date_str, [])
            if items:
                self.ui_queue.put(lambda d=date_str, it=items, n=needed: self._render_unfollowed_for_day(d, it, n))

    def _render_unfollowed_for_day(self, date_str, items, needed):
        parent_frame = self.calendar_day_frames.get(date_str)
        if not parent_frame or not parent_frame.winfo_exists(): return
        
        with self.data_lock:
            followed_ids = set(self.followed_shows.keys())
            
        count = 0
        for item in items:
            show = item.get('show')
            if not show or str(show['id']) in followed_ids: continue
            if count >= needed: break 
            count += 1
            
            card = ctk.CTkFrame(parent_frame, fg_color="#14121A", border_color="#2A2438", border_width=1, corner_radius=8, height=65)
            card.pack(fill="x", padx=6, pady=6)
            card.pack_propagate(False)

            info_frame = ctk.CTkFrame(card, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, padx=10, pady=8)
            
            title = show.get('name', 'Unknown')
            ep_code = f"S{item.get('season',1):02d}E{item.get('number',1):02d}"
            
            ctk.CTkLabel(info_frame, text=title, font=ctk.CTkFont(size=11, weight="bold"), text_color="gray50", wraplength=140, justify="left", height=14).pack(anchor="w")
            
            bottom_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
            bottom_frame.pack(side="bottom", fill="x", pady=(2, 0))
            
            ctk.CTkLabel(bottom_frame, text=ep_code, font=ctk.CTkFont(size=9), text_color="gray40", height=12).pack(side="left")
            
            btn = ctk.CTkButton(bottom_frame, text="+ Track", height=18, width=50, font=ctk.CTkFont(size=9, weight="bold"), fg_color="transparent", border_width=1, border_color="gray30", text_color="gray50", hover_color="#2A2438")
            btn.configure(command=lambda s_id=str(show['id']), s_name=title, b=btn: self.toggle_follow(s_id, s_name, True, b))
            btn.pack(side="right")

    def create_vertical_calendar_episode(self, parent, ep_data, current_date):
        card = ctk.CTkFrame(parent, fg_color=GLASS_CARD, border_color=GLASS_EDGE, border_width=1, corner_radius=8, height=120)
        card.pack(fill="x", padx=6, pady=6)
        card.pack_propagate(False)

        poster_frame = ctk.CTkFrame(card, width=68, height=100, fg_color="gray20", corner_radius=5)
        poster_frame.pack(side="left", padx=10, pady=10)
        poster_frame.pack_propagate(False)
        poster_lbl = ctk.CTkLabel(poster_frame, text="", image=None)
        poster_lbl.place(relx=0.5, rely=0.5, anchor="center")
        ep_data['poster_lbl'] = poster_lbl

        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)

        title_lbl = ctk.CTkLabel(info_frame, text=ep_data['show'], font=ctk.CTkFont(size=12, weight="bold"), text_color="white", wraplength=120, justify="left", height=14)
        title_lbl.pack(anchor="w")

        ep_title = ctk.CTkLabel(info_frame, text=ep_data['title'], font=ctk.CTkFont(size=9), text_color="#A4B2C6", wraplength=120, justify="left", height=12)
        ep_title.pack(anchor="w")

        ep_code = ctk.CTkLabel(info_frame, text=ep_data['episode'], font=ctk.CTkFont(size=9), text_color="#A4B2C6", height=12)
        ep_code.pack(anchor="w")

        q_pref = self.settings.get("quality", "1080p")
        q_map = {"Any": "HD", "2160p (4K)": "UHD", "x265/HEVC": "HEVC"}
        qual_str = q_map.get(q_pref, q_pref)
        ep_data['qual_str'] = qual_str

        is_future = current_date > datetime.now().date()
        btn_text = "⏳  Not Aired Yet" if is_future else "🧲  Scanning..."

        action_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
        action_frame.pack(side="bottom", fill="x")
        action_frame.grid_columnconfigure(0, weight=1)

        btn = ctk.CTkButton(
            action_frame, text=btn_text, height=22, font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, corner_radius=4,
            command=lambda data=ep_data: self.handle_fetch(data)
        )
        if is_future:
            btn.configure(state="disabled", fg_color="gray25", text_color="gray50")
        btn.grid(row=0, column=0, sticky="ew")
        
        ep_data['button_ref'] = btn

        dots_lbl = ctk.CTkLabel(
            action_frame, text="⋮", width=8, height=22,
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#A4B2C6", cursor="hand2"
        )
        dots_lbl.grid(row=0, column=1, padx=(5, 2))
        dots_lbl.bind("<Button-1>", lambda e, data=ep_data: self.open_manual_search(data))
        dots_lbl.bind("<Enter>", lambda e, w=dots_lbl: w.configure(text_color="white"))
        dots_lbl.bind("<Leave>", lambda e, w=dots_lbl: w.configure(text_color="#A4B2C6"))

        if not is_future:
            self.size_queue.put(ep_data)

        def load_poster():
            img_url = None
            with self.data_lock:
                show_meta = self.followed_shows.get(ep_data['show_id'], {}).get('metadata')
            if show_meta:
                img_data = show_meta.get('image')
                if img_data:
                    img_url = img_data.get('medium')
            if img_url:
                pil_img = self.fetch_pil_image(img_url)
                if pil_img:
                    self.ui_queue.put(lambda: self._update_poster(ep_data, pil_img, 68, 100))
        threading.Thread(target=load_poster, daemon=True).start()

    def _update_poster(self, ep_data, pil_img, w=68, h=100):
        lbl = ep_data.get('poster_lbl')
        if lbl and lbl.winfo_exists():
            img = ImageOps.fit(pil_img, (w, h), Image.Resampling.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(w, h))
            lbl.configure(image=ctk_img, text="")

    # ==========================================
    # CLI MANUAL SEARCH WINDOW (UNIFIED & FILTERED)
    # ==========================================
    def open_manual_search(self, ep_data):
        popup = ctk.CTkToplevel(self)
        popup.title("Advanced Manual Search")
        popup.geometry("1000x650")
        popup.transient(self)
        popup.configure(fg_color="#0D0D0D")
        
        popup.update_idletasks()
        p_width = 1000
        p_height = 650
        s_width = popup.winfo_screenwidth()
        s_height = popup.winfo_screenheight()
        p_x = int((s_width / 2) - (p_width / 2))
        p_y = int((s_height / 2) - (p_height / 2))
        popup.geometry(f"{p_width}x{p_height}+{p_x}+{p_y}")
        
        popup.results_pool = []
        popup.sort_col = 'seeders'
        popup.sort_desc = True
        
        search_frame = ctk.CTkFrame(popup, fg_color="transparent", border_width=1, border_color=GLASS_EDGE, corner_radius=6)
        search_frame.pack(fill="x", padx=15, pady=(15, 5))
        
        ctk.CTkLabel(search_frame, text="Search Query", text_color="gray50", font=("Consolas", 11)).place(x=10, y=-10)
        
        entry_frame = ctk.CTkFrame(search_frame, fg_color="transparent")
        entry_frame.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(entry_frame, text="> ", text_color="white", font=("Consolas", 14, "bold")).pack(side="left")
        
        default_query = f"{ep_data.get('show', '')} {ep_data.get('episode', '')}".strip()
        search_query = ctk.StringVar(value=default_query)
        search_input = ctk.CTkEntry(entry_frame, textvariable=search_query, font=("Consolas", 14), fg_color="transparent", border_width=0, text_color="white")
        search_input.pack(side="left", fill="x", expand=True)

        controls_frame = ctk.CTkFrame(popup, fg_color="transparent")
        controls_frame.pack(fill="x", padx=15, pady=(0, 10))
        
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
        
        qual_var = ctk.StringVar(value="Any Quality")
        size_var = ctk.StringVar(value="Any Size")
        src_var = ctk.StringVar(value="All Sources")
        
        def on_filter_change(*args):
            render_results()

        def clear_filters():
            qual_var.set("Any Quality")
            size_var.set("Any Size")
            src_var.set("All Sources")
            render_results()

        ctk.CTkOptionMenu(filter_frame, values=["Any Quality", "720p", "1080p", "2160p", "x265", "HEVC"], variable=qual_var, command=on_filter_change, width=110, height=28, fg_color=GLASS_CARD, button_color=GLASS_EDGE).pack(side="left", padx=(0, 10))
        ctk.CTkOptionMenu(filter_frame, values=["Any Size", "Under 1GB", "1GB - 3GB", "Over 3GB"], variable=size_var, command=on_filter_change, width=110, height=28, fg_color=GLASS_CARD, button_color=GLASS_EDGE).pack(side="left", padx=(0, 10))
        ctk.CTkOptionMenu(filter_frame, values=["All Sources", "APIBay", "Torrentio", "EZTV", "SolidTorrents"], variable=src_var, command=on_filter_change, width=110, height=28, fg_color=GLASS_CARD, button_color=GLASS_EDGE).pack(side="left")
        ctk.CTkButton(filter_frame, text="Clear", width=60, height=28, fg_color="#C0392B", hover_color="#922B21", font=ctk.CTkFont(weight="bold"), command=clear_filters).pack(side="left", padx=(10, 0))

        res_container = ctk.CTkFrame(popup, fg_color="transparent", border_width=1, border_color="#5D4B8B", corner_radius=6)
        res_container.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        
        res_title = ctk.CTkLabel(res_container, text="Results", text_color=ACCENT_COLOR, font=("Consolas", 11, "bold"))
        res_title.place(x=10, y=-10)
        
        def set_sort(col):
            if popup.sort_col == col:
                popup.sort_desc = not popup.sort_desc
            else:
                popup.sort_col = col
                popup.sort_desc = True
            render_results()

        header_frame = ctk.CTkFrame(res_container, fg_color="transparent", height=28)
        header_frame.pack(fill="x", padx=10, pady=(15, 0))
        
        header_frame.grid_columnconfigure(0, weight=1, minsize=300)
        header_frame.grid_columnconfigure(1, minsize=80)
        header_frame.grid_columnconfigure(2, minsize=80)
        header_frame.grid_columnconfigure(3, minsize=60)
        header_frame.grid_columnconfigure(4, minsize=100)
        
        def make_hdr(parent, text, col_key, col_idx, anchor="w"):
            btn = ctk.CTkButton(parent, text=text, height=24, fg_color="transparent", hover_color="#202531", text_color=ACCENT_COLOR, font=("Consolas", 12, "bold"), anchor=anchor, command=lambda c=col_key: set_sort(c))
            btn.grid(row=0, column=col_idx, sticky="ew", padx=2)
            return btn
            
        hdr_name = make_hdr(header_frame, "Name", "name", 0, "w")
        hdr_size = make_hdr(header_frame, "Size", "size", 1, "e")
        hdr_seed = make_hdr(header_frame, "Seed:Lech", "seeders", 2, "center")
        hdr_src = make_hdr(header_frame, "Src", "source", 3, "center")
        ctk.CTkLabel(header_frame, text="", width=100).grid(row=0, column=4)

        results_scroll = ctk.CTkScrollableFrame(res_container, fg_color="transparent", scrollbar_button_color="#202020")
        results_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        def on_dl(btn_ref, row_r, row_s):
            btn_ref.configure(text="✅ Started", fg_color="#3A7D44", text_color="white", state="disabled")
            self.download_torrent_file(ep_data, {'info_hash': row_r['info_hash'], 'magnet': row_r['magnet'], 'name': row_r['name']}, row_s)
            popup.after(2000, popup.destroy)

        def render_results():
            for w in results_scroll.winfo_children():
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
            
            for r in popup.results_pool:
                if src_val != "all sources" and src_val not in r['source']:
                    continue
                    
                name_lower = r['name'].lower()
                if q_val != "any quality" and q_val not in name_lower:
                    continue
                    
                gb_size = r['size'] / (1024**3) if r['size'] else 0
                if s_val == "Under 1GB" and gb_size > 1.0: continue
                if s_val == "1GB - 3GB" and (gb_size < 1.0 or gb_size > 3.0): continue
                if s_val == "Over 3GB" and gb_size < 3.0: continue
                
                filtered.append(r)
                
            filtered.sort(key=lambda x: x[popup.sort_col], reverse=popup.sort_desc)
            res_title.configure(text=f"Results ({len(filtered)})")
            
            if not filtered:
                ctk.CTkLabel(results_scroll, text="No matching torrents found.", text_color="gray50", font=("Consolas", 12)).pack(anchor="w", pady=10)
                return
            
            for idx, r in enumerate(filtered):
                size_str = self.format_size(r.get('size', 0))
                name = r.get('name', 'Unknown')
                
                seed = str(r.get('seeders', '0'))
                leech = str(r.get('leechers', '0'))
                src_label = r.get('source', 'unk')[:3].upper()
                
                row_frame = ctk.CTkFrame(results_scroll, fg_color="transparent")
                row_frame.pack(fill="x", pady=2)
                row_frame.grid_columnconfigure(0, weight=1, minsize=300)
                row_frame.grid_columnconfigure(1, minsize=80)
                row_frame.grid_columnconfigure(2, minsize=80)
                row_frame.grid_columnconfigure(3, minsize=60)
                row_frame.grid_columnconfigure(4, minsize=100)
                
                ctk.CTkLabel(row_frame, text=name, font=("Consolas", 12), text_color="#A4B2C6", anchor="w").grid(row=0, column=0, sticky="w", padx=2)
                ctk.CTkLabel(row_frame, text=size_str, font=("Consolas", 12), text_color="#A4B2C6", anchor="e").grid(row=0, column=1, sticky="e", padx=2)
                ctk.CTkLabel(row_frame, text=f"{seed}:{leech}", font=("Consolas", 12), text_color="#A4B2C6", anchor="center").grid(row=0, column=2, sticky="ew", padx=2)
                ctk.CTkLabel(row_frame, text=src_label, font=("Consolas", 12), text_color="#A4B2C6", anchor="center").grid(row=0, column=3, sticky="ew", padx=2)
                
                dl_btn = ctk.CTkButton(row_frame, text="Download", width=80, height=24, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, font=ctk.CTkFont(size=10, weight="bold"))
                dl_btn.configure(command=lambda br=dl_btn, rr=r, rs=size_str: on_dl(br, rr, rs))
                dl_btn.grid(row=0, column=4, sticky="e", padx=2)

        def execute_manual_search():
            popup.results_pool.clear()
            render_results()
            
            query = search_input.get().strip()
            self.ui_queue.put(lambda: res_title.configure(text=f"Searching APIs concurrently..."))
            self.ui_queue.put(lambda: apibay_lbl.configure(text="⏳ APIBay", text_color="yellow"))
            self.ui_queue.put(lambda: tor_lbl.configure(text="⏳ Torrentio", text_color="yellow"))
            self.ui_queue.put(lambda: eztv_lbl.configure(text="⏳ EZTV", text_color="yellow"))
            self.ui_queue.put(lambda: sol_lbl.configure(text="⏳ Solid", text_color="yellow"))
            
            def fetch_apibay():
                try:
                    url = f"https://apibay.org/q.php?q={urllib.parse.quote(query)}"
                    res = http_session.get(url, timeout=10)
                    if res.status_code == 200:
                        data = res.json()
                        count = 0
                        if isinstance(data, list) and len(data) > 0 and data[0].get('id') != '0':
                            for r in data:
                                if int(r.get('seeders', 0)) > 0:
                                    popup.results_pool.append({
                                        'source': 'apibay',
                                        'name': r.get('name', 'Unknown'),
                                        'info_hash': r.get('info_hash', ''),
                                        'magnet': '',
                                        'size': int(r.get('size', 0)),
                                        'seeders': int(r.get('seeders', 0)),
                                        'leechers': int(r.get('leechers', 0))
                                    })
                                    count += 1
                        self.ui_queue.put(lambda: apibay_lbl.configure(text=f"✅ APIBay ({count})", text_color="#2FA572"))
                    else:
                        self.ui_queue.put(lambda: apibay_lbl.configure(text="❌ APIBay", text_color="#C0392B"))
                except Exception as e:
                    logger.debug(f"APIBay failed: {e}")
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
                    
                    show_id = ep_data.get('show_id')
                    imdb_id = None
                    if show_id:
                        with self.data_lock:
                            show_data = self.followed_shows.get(str(show_id), {})
                            meta = show_data.get('metadata')
                            if meta and meta.get('externals'):
                                imdb_id = meta['externals'].get('imdb')

                    if imdb_id:
                        if not imdb_id.startswith('tt'): imdb_id = f"tt{imdb_id}"
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
                                        if unit == 'GB': size_bytes = val * 1024**3
                                        elif unit == 'MB': size_bytes = val * 1024**2
                                        elif unit == 'KB': size_bytes = val * 1024
                                        
                                    popup.results_pool.append({
                                        'source': 'torrentio',
                                        'name': s.get('title', '').split('\n')[0],
                                        'info_hash': hash_match.group(1) if hash_match else "",
                                        'magnet': magnet,
                                        'size': size_bytes,
                                        'seeders': seeders,
                                        'leechers': 0
                                    })
                                    count += 1
                            self.ui_queue.put(lambda: tor_lbl.configure(text=f"✅ Torrentio ({count})", text_color="#2FA572"))
                        else:
                            self.ui_queue.put(lambda: tor_lbl.configure(text="❌ Torrentio", text_color="#C0392B"))
                    else:
                        self.ui_queue.put(lambda: tor_lbl.configure(text="❌ No IMDB ID", text_color="#C0392B"))
                except Exception as e:
                    logger.debug(f"Torrentio failed: {e}")
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
                    
                    show_id = ep_data.get('show_id')
                    imdb_id = None
                    if show_id:
                        with self.data_lock:
                            show_data = self.followed_shows.get(str(show_id), {})
                            meta = show_data.get('metadata')
                            if meta and meta.get('externals'):
                                imdb_id = meta['externals'].get('imdb')

                    if imdb_id:
                        eztv_imdb = imdb_id.replace('tt', '')
                        url = f"https://eztv.re/api/get-torrents?imdb_id={eztv_imdb}"
                        res = http_session.get(url, timeout=10)
                        if res.status_code == 200:
                            count = 0
                            for t in res.json().get('torrents', []):
                                if str(t.get('season')) == str(season_num) and str(t.get('episode')) == str(episode_num):
                                    seeders = int(t.get('seeds', 0))
                                    if seeders > 0:
                                        popup.results_pool.append({
                                            'source': 'eztv',
                                            'name': t.get('title', ''),
                                            'info_hash': t.get('hash', ''),
                                            'magnet': t.get('magnet_url', ''),
                                            'size': int(t.get('size_bytes', 0)),
                                            'seeders': seeders,
                                            'leechers': int(t.get('peers', 0)) - seeders if t.get('peers') else 0
                                        })
                                        count += 1
                            self.ui_queue.put(lambda: eztv_lbl.configure(text=f"✅ EZTV ({count})", text_color="#2FA572"))
                        else:
                            self.ui_queue.put(lambda: eztv_lbl.configure(text="❌ EZTV", text_color="#C0392B"))
                    else:
                        self.ui_queue.put(lambda: eztv_lbl.configure(text="❌ No IMDB ID", text_color="#C0392B"))
                except Exception as e:
                    logger.debug(f"EZTV failed: {e}")
                    self.ui_queue.put(lambda: eztv_lbl.configure(text="❌ EZTV", text_color="#C0392B"))

            def fetch_solidtorrents():
                try:
                    url = f"https://solidtorrents.to/api/v1/search?q={urllib.parse.quote(query)}&category=Video"
                    res = http_session.get(url, timeout=10)
                    if res.status_code == 200:
                        data = res.json()
                        count = 0
                        for r in data.get('results', []):
                            seeders = int(r.get('swarm', {}).get('seeders', 0))
                            if seeders > 0:
                                magnet = r.get('magnet', '')
                                info_hash_match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', magnet, re.IGNORECASE)
                                info_hash = info_hash_match.group(1) if info_hash_match else ""
                                
                                popup.results_pool.append({
                                    'source': 'solidtorrents',
                                    'name': r.get('title', 'Unknown'),
                                    'info_hash': info_hash,
                                    'magnet': magnet,
                                    'size': int(r.get('size', 0)),
                                    'seeders': seeders,
                                    'leechers': int(r.get('swarm', {}).get('leechers', 0))
                                })
                                count += 1
                        self.ui_queue.put(lambda: sol_lbl.configure(text=f"✅ Solid ({count})", text_color="#2FA572"))
                    else:
                        self.ui_queue.put(lambda: sol_lbl.configure(text="❌ Solid", text_color="#C0392B"))
                except Exception as e:
                    logger.debug(f"SolidTorrents failed: {e}")
                    self.ui_queue.put(lambda: sol_lbl.configure(text="❌ Solid", text_color="#C0392B"))

            def run_all_searches():
                threads = [
                    threading.Thread(target=fetch_apibay),
                    threading.Thread(target=fetch_torrentio),
                    threading.Thread(target=fetch_eztv),
                    threading.Thread(target=fetch_solidtorrents)
                ]
                for t in threads: t.start()
                for t in threads: t.join()
                
                self.ui_queue.put(render_results)
                
            threading.Thread(target=run_all_searches, daemon=True).start()

        search_input.bind("<Return>", lambda e: execute_manual_search())
        execute_manual_search()

    # ==========================================
    # TAB 2: DISCOVER & SEARCH
    # ==========================================
    def setup_discover_tab(self):
        self.tab_discover.grid_columnconfigure(0, weight=1)
        self.tab_discover.grid_rowconfigure(2, weight=1)

        self.disc_loader_frame = ctk.CTkFrame(self.tab_discover, fg_color="transparent", height=4)
        self.disc_loader_frame.grid(row=0, column=0, sticky="ew")

        controls_frame = ctk.CTkFrame(self.tab_discover, fg_color="transparent")
        controls_frame.grid(row=1, column=0, padx=15, pady=(5, 5), sticky="ew")
        controls_frame.grid_columnconfigure(0, weight=1)

        self.search_entry = ctk.CTkEntry(controls_frame, placeholder_text="Give AirGrabber a new obsession to track...", height=32, fg_color=BG_BASE, border_color=GLASS_EDGE)
        self.search_entry.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        self.search_entry.bind("<Return>", lambda e: self.execute_discover_search())
        
        self.search_btn = ctk.CTkButton(controls_frame, text="Search", width=80, height=32, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, command=self.execute_discover_search)
        self.search_btn.grid(row=0, column=1, padx=(0, 15))

        self.discover_scroll = ctk.CTkScrollableFrame(self.tab_discover, fg_color="transparent", scrollbar_button_color=TAB_BG, scrollbar_fg_color="transparent")
        self.discover_scroll.grid(row=2, column=0, padx=5, pady=(0, 5), sticky="nsew")
        self.discover_scroll.grid_columnconfigure(0, weight=1)
        
        def _on_mousewheel_h(event, rs=self.discover_scroll):
            rs._parent_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self.discover_scroll.bind_all("<MouseWheel>", _on_mousewheel_h)

    def fetch_discover_categories(self):
        for widget in self.discover_scroll.winfo_children():
            widget.destroy()
        loader = self.show_loading(self.disc_loader_frame)
        
        def fetch():
            try:
                res1 = http_session.get("https://api.tvmaze.com/shows?page=0", timeout=5)
                res1.raise_for_status()
                trending_data = res1.json()
                trending_data.sort(key=lambda x: x.get('weight', 0), reverse=True)
                trending_shows = [s for s in trending_data if s.get('type') in ['Scripted', 'Animation']][:40]

                this_week_shows = []
                seen_this_week = set()
                for i in range(7):
                    if len(this_week_shows) >= 20: break
                    date_str = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
                    try:
                        sched_res = http_session.get(f"https://api.tvmaze.com/schedule?date={date_str}", timeout=5)
                        sched_res.raise_for_status()
                        items = sched_res.json()
                        items.sort(key=lambda x: x.get('show', {}).get('weight', 0), reverse=True)
                        for item in items:
                            show = item['show']
                            if show.get('type') not in ['Scripted', 'Animation']: continue
                            if show.get('language') != 'English': continue 
                            sid = str(show['id'])
                            if sid not in seen_this_week:
                                this_week_shows.append(show)
                                seen_this_week.add(sid)
                            if len(this_week_shows) >= 20: break
                    except Exception as e:
                        logger.debug(f"Schedule this week error on {date_str}: {e}")
                    time.sleep(0.2)

                premieres_shows = []
                seen_premieres = set()
                for i in range(1, 14): 
                    if len(premieres_shows) >= 20: break
                    date_str = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
                    try:
                        sched_res = http_session.get(f"https://api.tvmaze.com/schedule?date={date_str}", timeout=5)
                        sched_res.raise_for_status()
                        for item in sched_res.json():
                            if item.get('number') == 1: 
                                show = item['show']
                                if show.get('type') not in ['Scripted', 'Animation']: continue
                                sid = str(show['id'])
                                if sid not in seen_premieres:
                                    premieres_shows.append(show)
                                    seen_premieres.add(sid)
                    except Exception as e:
                        logger.debug(f"Premieres error on {date_str}: {e}")
                    time.sleep(0.2)

                self.ui_queue.put(lambda: self.render_horizontal_rail("Trending", trending_shows, rows=2))
                self.ui_queue.put(lambda: self.render_horizontal_rail("Popular Shows Airing This Week", this_week_shows, rows=1))
                self.ui_queue.put(lambda: self.render_horizontal_rail("Upcoming Season Premieres", premieres_shows, rows=1))

            except Exception as e:
                logger.error(f"Error fetching discover categories: {e}")
            finally:
                self.ui_queue.put(lambda: self.hide_loading(loader))
                
        threading.Thread(target=fetch, daemon=True).start()

    def render_horizontal_rail(self, title, shows_data, rows=1):
        if not shows_data: return

        rail_container = ctk.CTkFrame(self.discover_scroll, fg_color="transparent")
        rail_container.pack(fill="x", pady=(10, 20))

        header = ctk.CTkLabel(rail_container, text=f"⊙ {title} >", font=ctk.CTkFont(size=18, weight="bold"), text_color="white")
        header.pack(anchor="w", padx=10, pady=(0, 10))

        rail_height = (120 + 16) * rows + 20 
        rail_scroll = ctk.CTkScrollableFrame(rail_container, fg_color="transparent", orientation="horizontal", height=rail_height, scrollbar_button_color=TAB_BG, scrollbar_fg_color="transparent")
        rail_scroll.pack(fill="x", expand=True)
        
        def _on_mousewheel_h(event, rs=rail_scroll):
            rs._parent_canvas.xview_scroll(int(-1*(event.delta/120)), "units")
        rail_scroll.bind_all("<MouseWheel>", _on_mousewheel_h)
        
        self.render_show_grid(rail_scroll, shows_data, is_library=False, horizontal_rail=True, horizontal_rows=rows)

    def execute_discover_search(self):
        query = self.search_entry.get()
        if not query:
            self.fetch_discover_categories()
            return
            
        self.search_btn.configure(state="disabled")
        for widget in self.discover_scroll.winfo_children():
            widget.destroy()
        loader = self.show_loading(self.disc_loader_frame)
        
        def fetch():
            try:
                res = http_session.get(f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(query)}", timeout=5)
                res.raise_for_status()
                data = res.json()
                shows = [item['show'] for item in data if item['show'].get('type') in ['Scripted', 'Animation']]
                self.ui_queue.put(lambda: self.render_horizontal_rail(f"Search Results for '{query}'", shows, rows=1))
            except Exception as e:
                logger.error(f"Search error: {e}")
            finally:
                self.ui_queue.put(lambda: self.search_btn.configure(state="normal"))
                self.ui_queue.put(lambda: self.hide_loading(loader))
                
        threading.Thread(target=fetch, daemon=True).start()

    # ==========================================
    # TAB 3: TRACKED & BATCH IMPORT
    # ==========================================
    def setup_library_tab(self):
        self.tab_library.grid_columnconfigure(0, weight=1)
        self.tab_library.grid_rowconfigure(2, weight=1) 
        
        self.lib_loader_frame = ctk.CTkFrame(self.tab_library, fg_color="transparent", height=4)
        self.lib_loader_frame.grid(row=0, column=0, sticky="ew")

        header_frame = ctk.CTkFrame(self.tab_library, fg_color="transparent")
        header_frame.grid(row=1, column=0, padx=15, pady=5, sticky="ew")
        header_frame.grid_columnconfigure(1, weight=1)
        
        self.library_header_lbl = ctk.CTkLabel(header_frame, text=f"Tracked Shows ({len(self.followed_shows)})", font=ctk.CTkFont(size=20, weight="bold"), text_color="white")
        self.library_header_lbl.grid(row=0, column=0, sticky="w")
        
        self.library_filter = ctk.CTkEntry(header_frame, placeholder_text="Filter library...", width=180, height=30, fg_color=BG_BASE, border_color=GLASS_EDGE)
        self.library_filter.grid(row=0, column=2, sticky="e", padx=(0, 10))
        self.library_filter.bind("<KeyRelease>", self.filter_library_view)
        
        cleanup_btn = ctk.CTkButton(header_frame, text="Clean Up Ended Shows", width=120, height=30, fg_color="#C0392B", hover_color="#922B21", font=ctk.CTkFont(size=12), command=self.cleanup_ended_shows)
        cleanup_btn.grid(row=0, column=3, sticky="e", padx=(0, 10))

        batch_import_btn = ctk.CTkButton(header_frame, text="Batch Import", width=100, height=30, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, font=ctk.CTkFont(size=12), command=self.open_batch_import_window)
        batch_import_btn.grid(row=0, column=4, sticky="e")

        self.library_scroll = ctk.CTkScrollableFrame(self.tab_library, fg_color="transparent", scrollbar_button_color=TAB_BG, scrollbar_fg_color="transparent")
        self.library_scroll.grid(row=2, column=0, padx=5, pady=(0, 5), sticky="nsew")
        self.library_scroll.grid_columnconfigure(0, weight=1)

    def filter_library_view(self, event=None):
        query = self.library_filter.get().lower()
        with self.data_lock:
            filtered = {sid: data for sid, data in self.followed_shows.items() if query in data['name'].lower()}
        self._trigger_library_render(filtered)

    def refresh_library_list(self):
        with self.data_lock:
            shows = dict(self.followed_shows)
        self._trigger_library_render(shows)
        
    def _trigger_library_render(self, shows_dict):
        if hasattr(self, 'library_header_lbl') and self.library_header_lbl.winfo_exists():
            self.library_header_lbl.configure(text=f"Tracked Shows ({len(self.followed_shows)})")
            
        for widget in self.library_scroll.winfo_children():
            widget.destroy()
            
        if not shows_dict:
            ctk.CTkLabel(self.library_scroll, text="No shows match your library.", text_color="gray60").grid(row=0, column=0, columnspan=7, pady=50)
            return

        shows = []
        for show_id, data in shows_dict.items():
            if data.get("metadata"):
                shows.append(data["metadata"])
            else:
                shows.append({"id": show_id, "name": data["name"]})
            
        self.render_show_grid(self.library_scroll, shows, is_library=True)

    def cleanup_ended_shows(self):
        loader = self.show_loading(self.lib_loader_frame)
        def scan_and_remove():
            removed_count = 0
            with self.data_lock:
                show_ids = list(self.followed_shows.keys())
            for show_id in show_ids:
                try:
                    res = http_session.get(f"https://api.tvmaze.com/shows/{show_id}", timeout=5)
                    res.raise_for_status()
                    data = res.json()
                    if data.get("status") == "Ended":
                        with self.data_lock:
                            if show_id in self.followed_shows:
                                del self.followed_shows[show_id]
                            if show_id in self.episodes_cache:
                                del self.episodes_cache[show_id]
                        removed_count += 1
                except Exception as e:
                    logger.error(f"Error checking show {show_id}: {e}")
            
            if removed_count > 0:
                self.save_data()
                self.save_episodes_cache()
                self.ui_queue.put(self.refresh_library_list)
                self.ui_queue.put(self.refresh_calendar_data)
            self.ui_queue.put(lambda: self.hide_loading(loader))
            
        threading.Thread(target=scan_and_remove, daemon=True).start()

    def open_batch_import_window(self):
        popup = ctk.CTkToplevel(self)
        popup.title("Batch Import Shows")
        popup.geometry("520x620")
        popup.transient(self)
        
        popup.update_idletasks()
        p_width = 520
        p_height = 620
        s_width = popup.winfo_screenwidth()
        s_height = popup.winfo_screenheight()
        p_x = int((s_width / 2) - (p_width / 2))
        p_y = int((s_height / 2) - (p_height / 2))
        popup.geometry(f"{p_width}x{p_height}+{p_x}+{p_y}")

        shadow_frame = ctk.CTkFrame(popup, fg_color="#080A0E", corner_radius=15)
        shadow_frame.place(relx=0.51, rely=0.52, relwidth=0.96, relheight=0.94, anchor="center")

        main_frame = ctk.CTkFrame(popup, fg_color=GLASS_CARD, border_color=GLASS_EDGE, border_width=1, corner_radius=12)
        main_frame.place(relx=0.5, rely=0.5, relwidth=0.96, relheight=0.94, anchor="center")

        ctk.CTkLabel(main_frame, text="Batch Import to AirGrabber", font=ctk.CTkFont(size=20, weight="bold"), text_color="white").pack(pady=(20, 5))
        ctk.CTkLabel(main_frame, text="Paste a list of shows (one per line):", font=ctk.CTkFont(size=13), text_color="#A4B2C6").pack(pady=(0, 10))
        
        textbox = ctk.CTkTextbox(main_frame, width=450, height=380, fg_color=BG_BASE, border_color=GLASS_EDGE, border_width=1)
        textbox.pack(pady=10)
        
        progress = ctk.CTkProgressBar(main_frame, width=400, mode="determinate", progress_color=ACCENT_COLOR)
        progress.pack(pady=5)
        progress.set(0)
        status_lbl = ctk.CTkLabel(main_frame, text="", text_color="gray60")
        status_lbl.pack(pady=5)

        def run_import():
            lines = textbox.get("1.0", "end-1c").split("\n")
            shows_to_search = [line.strip() for line in lines if line.strip()]
            if not shows_to_search:
                return
            
            import_btn.configure(state="disabled")
            textbox.configure(state="disabled")
            
            def process():
                total = len(shows_to_search)
                success_count = 0
                for idx, query in enumerate(shows_to_search):
                    self.ui_queue.put(lambda q=query, i=idx+1, t=total: status_lbl.configure(text=f"Searching ({i}/{t}): {q}..."))
                    self.ui_queue.put(lambda val=(idx+1)/total: progress.set(val))
                    try:
                        res = http_session.get(f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(query)}", timeout=5)
                        res.raise_for_status()
                        data = res.json()
                        if data:
                            show = data[0]['show']
                            with self.data_lock:
                                self.followed_shows[str(show['id'])] = {"name": show['name'], "auto": False, "metadata": None}
                            success_count += 1
                        time.sleep(0.6)
                    except Exception as e:
                        logger.error(f"Import error for {query}: {e}")
                
                self.save_data()
                self.ui_queue.put(self.start_background_library_sync)
                self.ui_queue.put(lambda: status_lbl.configure(text=f"Import complete! Added {success_count} shows.", text_color="#2FA572"))
                self.ui_queue.put(self.refresh_library_list)
                self.ui_queue.put(self.refresh_calendar_data)
                self.ui_queue.put(lambda: progress.set(1.0))
                self.ui_queue.put(lambda: import_btn.configure(state="normal"))
                
            threading.Thread(target=process, daemon=True).start()

        import_btn = ctk.CTkButton(main_frame, text="Import Shows", font=ctk.CTkFont(weight="bold"), fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, command=run_import)
        import_btn.pack(pady=10)

    # ==========================================
    # SHARED GRID RENDERER
    # ==========================================
    def render_show_grid(self, parent_frame, shows_data, is_library=False, horizontal_rail=False, horizontal_rows=1):
        for widget in parent_frame.winfo_children():
            widget.destroy()

        if not shows_data:
            ctk.CTkLabel(parent_frame, text="No shows found.", text_color="gray60").grid(row=0, column=0, pady=50)
            return

        if not horizontal_rail:
            max_cols = 6
            for i in range(max_cols):
                parent_frame.grid_columnconfigure(i, weight=1)

        def load_images_and_render():
            processed_cards = []
            for show in shows_data:
                img_url = show.get('image', {}).get('medium') if show.get('image') else None
                poster = None
                if img_url:
                    poster = self.fetch_pil_image(img_url)
                
                seasons = len(show.get('_embedded', {}).get('seasons', [])) if '_embedded' in show else "?"
                genres = ", ".join(show.get('genres', [])) if show.get('genres') else "Unknown"
                if len(genres) > 20:
                    genres = genres[:17] + "..."
                
                processed_cards.append({
                    "id": str(show.get('id', '')),
                    "name": show.get('name', 'Unknown'),
                    "year": f" ({show['premiered'][:4]})" if show.get('premiered') else "",
                    "status": show.get('status', 'Unknown'),
                    "genres": genres,
                    "seasons": seasons,
                    "poster": poster
                })
                
            self.ui_queue.put(lambda: self._build_grid_widgets(parent_frame, processed_cards, is_library, horizontal_rail, horizontal_rows))
            
        threading.Thread(target=load_images_and_render, daemon=True).start()

    def _build_grid_widgets(self, parent_frame, cards_data, is_library, horizontal_rail, horizontal_rows=1):
        row, col = 0, 0
            
        for card in cards_data:
            if not horizontal_rail:
                parent_frame.grid_rowconfigure(row, weight=0) 
            
            card_frame = ctk.CTkFrame(parent_frame, fg_color=GLASS_CARD, border_color=GLASS_EDGE, border_width=1, corner_radius=8)
            
            if horizontal_rail:
                card_frame.configure(width=260, height=120)
                card_frame.grid(row=row, column=col, padx=8, pady=8)
            else:
                card_frame.configure(height=120)
                card_frame.grid(row=row, column=col, padx=10, pady=10, sticky="nsew") 
                
            card_frame.grid_propagate(False)
            card_frame.pack_propagate(False)
            
            poster_frame = ctk.CTkFrame(card_frame, width=68, height=100, fg_color="gray20", corner_radius=5)
            poster_frame.pack(side="left", padx=5, pady=10)
            poster_frame.pack_propagate(False)
            
            if card['poster']:
                img = ImageOps.fit(card['poster'], (68, 100), Image.Resampling.LANCZOS)
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(68, 100))
                img_lbl = ctk.CTkLabel(poster_frame, image=ctk_img, text="")
                img_lbl.place(relx=0.5, rely=0.5, anchor="center")
            else:
                ep_data = {}
                ep_data['poster_lbl'] = ctk.CTkLabel(poster_frame, text="")
                ep_data['poster_lbl'].place(relx=0.5, rely=0.5, anchor="center")
            
            info_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)
            
            title_text = f"{card['name']}{card['year']}"
            ctk.CTkLabel(info_frame, text=title_text, font=ctk.CTkFont(size=12, weight="bold"), text_color="white", wraplength=140, justify="left", height=14).pack(anchor="w")
            
            ctk.CTkLabel(info_frame, text=f"status: {card['status']}", text_color="#A4B2C6", font=ctk.CTkFont(size=9), height=12).pack(anchor="w", pady=(2, 0))
            ctk.CTkLabel(info_frame, text=f"genre: {card['genres']}", text_color="#A4B2C6", font=ctk.CTkFont(size=9), height=12).pack(anchor="w", pady=0)
            
            if card['seasons'] != "?":
                ctk.CTkLabel(info_frame, text=f"seasons: {card['seasons']}", text_color="#A4B2C6", font=ctk.CTkFont(size=9), height=12).pack(anchor="w", pady=0)
            
            bottom_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
            bottom_frame.pack(side="bottom", fill="x")
            
            if is_library:
                with self.data_lock:
                    auto_val = self.followed_shows.get(card['id'], {}).get('auto', False)
                auto_var = ctk.BooleanVar(value=auto_val)
                auto_switch = ctk.CTkSwitch(
                    bottom_frame, text="Auto", variable=auto_var, font=ctk.CTkFont(size=10), width=35,
                    progress_color=ACCENT_COLOR,
                    command=lambda sid=card['id'], var=auto_var: self.toggle_auto_fetch(sid, var.get())
                )
                auto_switch.pack(side="left")
                
                btn = ctk.CTkButton(bottom_frame, width=50, height=20, text="Unfollow", fg_color="#C0392B", hover_color="#922B21", font=ctk.CTkFont(size=10, weight="bold"))
                btn.configure(command=lambda s_id=str(card['id']), s_name=card['name'], b=btn: self.toggle_follow(s_id, s_name, False, b))
                btn.pack(side="right")
            else:
                is_followed = card['id'] in self.followed_shows
                btn = ctk.CTkButton(bottom_frame, height=20, text="Following" if is_followed else "+ track show", font=ctk.CTkFont(size=10, weight="bold"),
                              state="disabled" if is_followed else "normal",
                              fg_color="transparent" if is_followed else ACCENT_COLOR, hover_color=ACCENT_HOVER)
                btn.configure(command=lambda s_id=str(card['id']), s_name=card['name'], b=btn: self.toggle_follow(s_id, s_name, True, b))
                btn.pack(fill="x")

            if horizontal_rail:
                row += 1
                if row >= horizontal_rows:
                    row = 0
                    col += 1
            else:
                col += 1
                if col >= 6:
                    col = 0
                    row += 1

    def toggle_follow(self, show_id, show_name, is_following, btn_ref=None):
        show_id = str(show_id)
        with self.data_lock:
            if is_following:
                self.followed_shows[show_id] = {"name": show_name, "auto": False, "metadata": None}
            else:
                if show_id in self.followed_shows:
                    del self.followed_shows[show_id]
                if show_id in self.episodes_cache:
                    del self.episodes_cache[show_id]
                keys_to_remove = [k for k in self.size_cache if k.startswith(show_id + "_")]
                for k in keys_to_remove:
                    del self.size_cache[k]
                self.save_size_cache()
        self.save_data()
        self.save_episodes_cache()
        self.start_background_library_sync() 
        
        if btn_ref is not None:
            if is_following:
                if self.tabview.get() == "Tracked":
                    btn_ref.configure(text="Unfollow", state="normal", fg_color="#C0392B", hover_color="#922B21")
                    btn_ref.configure(command=lambda: self.toggle_follow(show_id, show_name, False, btn_ref))
                else:
                    btn_ref.configure(text="Following", state="disabled", fg_color="transparent", border_width=0)
            else:
                btn_ref.configure(text="Follow" if self.tabview.get() == "Tracked" else "+ Track Show", 
                                  state="normal", fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER)
                btn_ref.configure(command=lambda: self.toggle_follow(show_id, show_name, True, btn_ref))
        
        if hasattr(self, 'library_header_lbl') and self.library_header_lbl.winfo_exists():
            self.library_header_lbl.configure(text=f"Tracked Shows ({len(self.followed_shows)})")

    def toggle_auto_fetch(self, show_id, enable):
        with self.data_lock:
            if str(show_id) in self.followed_shows:
                self.followed_shows[str(show_id)]['auto'] = enable
                self.save_data()

    # ==========================================
    # TAB 4: SETTINGS
    # ==========================================
    def setup_settings_tab(self):
        self.tab_settings.grid_columnconfigure(0, weight=1)
        self.tab_settings.grid_rowconfigure(0, weight=1)

        container = ctk.CTkFrame(self.tab_settings, fg_color="transparent")
        container.grid(row=0, column=0, padx=20, pady=20, sticky="nsw")
        container.grid_columnconfigure(1, weight=1)
        
        ctk.CTkLabel(container, text="Application Settings", font=ctk.CTkFont(size=20, weight="bold"), text_color="white").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ctk.CTkLabel(container, text="First Day of Week:", font=ctk.CTkFont(size=13), text_color="#A4B2C6").grid(row=2, column=0, sticky="w", pady=5, padx=(0, 15))
        self.first_day_var = ctk.StringVar(value=self.settings.get("first_day", "Monday"))
        day_menu = ctk.CTkOptionMenu(container, values=["Monday", "Sunday"], height=28, fg_color=GLASS_CARD, button_color=GLASS_EDGE, variable=self.first_day_var)
        day_menu.grid(row=2, column=1, sticky="w")
        
        ctk.CTkLabel(container, text="Calendar View:", font=ctk.CTkFont(size=13), text_color="#A4B2C6").grid(row=3, column=0, sticky="w", pady=5, padx=(0, 15))
        self.cal_view_var = ctk.StringVar(value=self.settings.get("calendar_view", "Vertical"))
        view_menu = ctk.CTkOptionMenu(container, values=["Vertical", "Horizontal"], height=28, fg_color=GLASS_CARD, button_color=GLASS_EDGE, variable=self.cal_view_var)
        view_menu.grid(row=3, column=1, sticky="w")
        
        ctk.CTkLabel(container, text="Preferred Quality:", font=ctk.CTkFont(size=13), text_color="#A4B2C6").grid(row=4, column=0, sticky="w", pady=5, padx=(0, 15))
        self.quality_var = ctk.StringVar(value=self.settings.get("quality", "1080p"))
        quality_menu = ctk.CTkOptionMenu(container, values=["Any", "720p", "1080p", "2160p (4K)", "x265/HEVC"], height=28, fg_color=GLASS_CARD, button_color=GLASS_EDGE, variable=self.quality_var)
        quality_menu.grid(row=4, column=1, sticky="w")

        ctk.CTkLabel(container, text="Download Location:", font=ctk.CTkFont(size=13), text_color="#A4B2C6").grid(row=5, column=0, sticky="w", pady=5, padx=(0, 15))
        dir_frame = ctk.CTkFrame(container, fg_color="transparent")
        dir_frame.grid(row=5, column=1, sticky="w")
        
        self.dl_dir_var = ctk.StringVar(value=self.settings.get("download_dir", TORRENTS_DIR))
        self.dir_entry = ctk.CTkEntry(dir_frame, textvariable=self.dl_dir_var, width=250, height=28, state="disabled", fg_color=BG_BASE, border_color=GLASS_EDGE)
        self.dir_entry.pack(side="left", padx=(0, 10))
        
        browse_btn = ctk.CTkButton(dir_frame, text="Browse...", width=70, height=28, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, command=self.browse_directory)
        browse_btn.pack(side="left")

        ctk.CTkLabel(container, text="Auto-Fetch Window:", font=ctk.CTkFont(size=13), text_color="#A4B2C6").grid(row=6, column=0, sticky="w", pady=5, padx=(0, 15))
        af_frame = ctk.CTkFrame(container, fg_color="transparent")
        af_frame.grid(row=6, column=1, sticky="w")
        
        self.af_days_var = ctk.StringVar(value=str(self.settings.get("auto_fetch_days", 5)))
        af_menu = ctk.CTkOptionMenu(af_frame, values=["1", "2", "3", "5", "7", "14"], height=28, width=70, fg_color=GLASS_CARD, button_color=GLASS_EDGE, variable=self.af_days_var)
        af_menu.pack(side="left")
        ctk.CTkLabel(af_frame, text=" days", font=ctk.CTkFont(size=13), text_color="gray60").pack(side="left", padx=(5, 10))
        
        info_btn = ctk.CTkButton(af_frame, text="[?]", width=30, height=28, font=ctk.CTkFont(weight="bold"), fg_color="transparent", hover_color=GLASS_EDGE, border_width=1, border_color=GLASS_EDGE, command=self.show_autofetch_info)
        info_btn.pack(side="left")

        ctk.CTkLabel(container, text="MediaForge Integration:", font=ctk.CTkFont(size=13), text_color="#A4B2C6").grid(row=7, column=0, sticky="w", pady=5, padx=(0, 15))
        self.mf_json_var = ctk.BooleanVar(value=self.settings.get("create_mediaforge_json", True))
        mf_switch = ctk.CTkSwitch(container, text="Generate .json metadata on download", variable=self.mf_json_var, progress_color=ACCENT_COLOR)
        mf_switch.grid(row=7, column=1, sticky="w")

        save_btn = ctk.CTkButton(container, text="Save Settings", height=32, font=ctk.CTkFont(weight="bold"), fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, command=self.apply_settings)
        save_btn.grid(row=8, column=0, columnspan=2, sticky="w", pady=(15, 0))
        
        self.settings_msg = ctk.CTkLabel(container, text="", text_color="#2FA572", font=ctk.CTkFont(size=12))
        self.settings_msg.grid(row=9, column=0, columnspan=2, sticky="w", pady=(5, 0))

    def show_autofetch_info(self):
        info = ctk.CTkToplevel(self)
        info.title("Auto-Fetch Logic")
        info.geometry("550x550")
        info.transient(self)
        info.grab_set()
        
        info.configure(fg_color=BG_BASE)
        
        frame = ctk.CTkFrame(info, fg_color=GLASS_CARD, border_width=1, border_color=GLASS_EDGE, corner_radius=10)
        frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        text = (
            "How Auto-Fetch Works\n\n"
            "1. The Time Window:\n"
            "The app checks the official airdate of episodes for shows on 'Auto'. "
            "If the episode airs within your selected window (e.g., the past 5 days) "
            "or today, it flags it for download. Future episodes are ignored.\n\n"
            "2. The Memory Check:\n"
            "It checks your local history. If it already successfully downloaded "
            "the episode, it skips it to prevent duplicates.\n\n"
            "3. Smart Search & Fallback:\n"
            "It queries APIBay using multiple variations, removing confusing "
            "punctuation to ensure it finds a match.\n\n"
            "4. Selection Criteria:\n"
            "It filters out dead torrents (0 seeders), enforces your preferred "
            "quality setting, and downloads the file with the highest seeder count."
        )
        
        lbl = ctk.CTkLabel(frame, text=text, font=ctk.CTkFont(size=13), text_color="#A4B2C6", justify="left", wraplength=450)
        lbl.pack(padx=20, pady=20, anchor="nw")
        
        btn = ctk.CTkButton(frame, text="Got it", width=100, height=30, fg_color=ACCENT_COLOR, hover_color=ACCENT_HOVER, command=info.destroy)
        btn.pack(pady=20)

    def browse_directory(self):
        selected_dir = filedialog.askdirectory(title="Select Torrent Download Directory")
        if selected_dir:
            self.dl_dir_var.set(selected_dir)

    def apply_settings(self):
        new_first_day = self.first_day_var.get()
        new_cal_view = self.cal_view_var.get()
        needs_calendar_rebuild = new_first_day != self.settings.get("first_day") or new_cal_view != self.settings.get("calendar_view")
        
        self.settings["first_day"] = new_first_day
        self.settings["calendar_view"] = new_cal_view
        self.settings["quality"] = self.quality_var.get()
        self.settings["download_dir"] = self.dl_dir_var.get()
        self.settings["auto_fetch_days"] = int(self.af_days_var.get())
        self.settings["create_mediaforge_json"] = self.mf_json_var.get()
        self.save_settings()
        
        if needs_calendar_rebuild:
            self.build_calendar_ui()
            
        self.settings_msg.configure(text="Settings saved successfully!")
        self.after(3000, lambda: self.settings_msg.configure(text=""))

if __name__ == "__main__":
    app = TorlinkCalendarApp()
    app.mainloop()