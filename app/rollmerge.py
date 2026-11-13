#!/usr/bin/env python3
"""
Roll Merge: put film scans from several cameras back into shooting order.

Each roll from the lab is already in order (frame 1, 2, 3...). The only
question is how the rolls from different cameras interleave, so this tool
shows the next frame from each camera and you click whichever came next.

Runs a small web server on this computer only (127.0.0.1) and opens the
interface in the default browser. Original files are never modified:
export writes renamed copies to a new folder.
"""

import hashlib
from contextlib import contextmanager
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from PIL import Image, ImageOps

try:
    import piexif
except ImportError:  # export still works, just without EXIF capture times
    piexif = None

VERSION = "1.0"
IMAGE_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}
JPEG_EXTS = {".jpg", ".jpeg"}
OUTPUT_MARKER = ".rollmerge-output"
HERE = os.path.dirname(os.path.abspath(__file__))
THUMB_SIZES = {"t": 480, "p": 1600}
SECONDS_BETWEEN_PHOTOS = 10

Image.MAX_IMAGE_PIXELS = None  # big film scans are expected, not a threat


# ---------------------------------------------------------------- storage

def data_dir():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or home
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    path = os.path.join(base, "RollMerge")
    os.makedirs(os.path.join(path, "thumbs"), exist_ok=True)
    os.makedirs(os.path.join(path, "states"), exist_ok=True)
    return path


DATA = data_dir()
THUMBS = os.path.join(DATA, "thumbs")
STATES = os.path.join(DATA, "states")


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def norm_root(path):
    return os.path.normcase(os.path.abspath(path))


def state_path(root):
    return os.path.join(STATES, sha1(norm_root(root)) + ".json")


def load_state(root):
    try:
        with open(state_path(root), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_state(root, state):
    state["root"] = root
    state["updated"] = time.time()
    path = state_path(root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def recent_folders():
    out = []
    for name in os.listdir(STATES):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(STATES, name), "r", encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        root = st.get("root")
        if not root or not os.path.isdir(root):
            continue
        total = sum(r.get("count", 0) for lane in st.get("lanes", []) for r in lane.get("rolls", []))
        out.append({
            "root": root,
            "name": os.path.basename(root.rstrip("/\\")) or root,
            "placed": len(st.get("seq", [])),
            "total": total,
            "updated": st.get("updated", 0),
        })
    out.sort(key=lambda r: r["updated"], reverse=True)
    return out[:6]


def clean_old_thumbs(max_age_days=45):
    cutoff = time.time() - max_age_days * 86400
    with CACHE_GATE.clear():
        _clean_old(cutoff)


def _clean_old(cutoff):
    for dirpath, _dirs, files in os.walk(THUMBS):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------- scanning

_num_re = re.compile(r"(\d+)")


def natural_key(text):
    """Sort 'frame2' before 'frame10', and '12' before '12A' before '13'."""
    key = []
    for part in _num_re.split(text):
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part.lower()))
    return key


def scan(root, max_depth=3):
    """Find every folder holding images. Each one is treated as a roll."""
    rolls = []
    files = []

    def walk(folder, depth):
        if os.path.exists(os.path.join(folder, OUTPUT_MARKER)):
            return  # a folder Roll Merge exported earlier
        try:
            entries = sorted(os.scandir(folder), key=lambda e: natural_key(e.name))
        except OSError:
            return
        images, subdirs = [], []
        for e in entries:
            if e.name.startswith("."):  # hidden files and macOS "._" files
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(e.path)
                elif e.is_file() and os.path.splitext(e.name)[1].lower() in IMAGE_EXTS:
                    images.append(e)
            except OSError:
                continue
        if images:
            rel = os.path.relpath(folder, root)
            key = "." if rel == "." else rel.replace("\\", "/")
            name = os.path.basename(root.rstrip("/\\")) if key == "." else key
            rolls.append({"key": key, "name": name, "start": len(files), "count": len(images)})
            for e in images:
                files.append(e.path)
        if depth < max_depth:
            for s in subdirs:
                walk(s, depth + 1)

    walk(root, 0)
    return rolls, files


# ---------------------------------------------------------------- thumbnails

class CacheGate:
    """Previews may be made or read by many threads at once ("use"). Clearing ("clear") waits for
    those to finish and holds new ones back until it is done, so the two never collide."""

    def __init__(self):
        self._cond = threading.Condition()
        self._users = 0
        self._clearing = False

    @contextmanager
    def use(self):
        with self._cond:
            while self._clearing:
                self._cond.wait()
            self._users += 1
        try:
            yield
        finally:
            with self._cond:
                self._users -= 1
                self._cond.notify_all()

    @contextmanager
    def clear(self):
        with self._cond:
            while self._clearing:
                self._cond.wait()
            self._clearing = True
            while self._users:
                self._cond.wait()
        try:
            yield
        finally:
            with self._cond:
                self._clearing = False
                self._cond.notify_all()


CACHE_GATE = CacheGate()

_key_locks = {}
_key_locks_guard = threading.Lock()
_decode_slots = threading.BoundedSemaphore(max(2, min(4, (os.cpu_count() or 2))))


def _lock_for(key):
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock


def _to_rgb(im):
    if im.mode in ("RGB", "L"):
        return im
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        return im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    return im.convert("RGB")


def thumb_file(path, size_key):
    st = os.stat(path)
    key = sha1("%s|%d|%d|%s|v1" % (path, st.st_size, st.st_mtime_ns, size_key))
    return key, os.path.join(THUMBS, key[:2], key + ".jpg")


def thumbnail(path, size_key):
    key, out = thumb_file(path, size_key)
    folder = os.path.dirname(out)
    if os.path.exists(out):
        return out
    with _lock_for(key):
        if os.path.exists(out):
            return out
        os.makedirs(folder, exist_ok=True)
        px = THUMB_SIZES[size_key]
        with _decode_slots:
            with Image.open(path) as im:
                if im.format == "JPEG":
                    im.draft("RGB", (px, px))  # decode at reduced size: much faster
                im = ImageOps.exif_transpose(im)
                im = _to_rgb(im)
                im.thumbnail((px, px), Image.LANCZOS)
                tmp = out + ".%d.tmp" % threading.get_ident()
                im.save(tmp, "JPEG", quality=82)
        os.replace(tmp, out)
    return out


def thumbnail_bytes(path, size_key):
    """Make (if needed) and read one preview, safely alongside cache clearing."""
    with CACHE_GATE.use():
        with open(thumbnail(path, size_key), "rb") as fh:
            return fh.read()


def cache_usage(paths=None):
    """Bytes and files used by previews: all of them, or only those of the given photos."""
    total = count = 0
    if paths is None:
        for dirpath, _dirs, files in os.walk(THUMBS):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f)); count += 1
                except OSError:
                    pass
        return total, count
    for path in paths:
        for size_key in THUMB_SIZES:
            try:
                total += os.path.getsize(thumb_file(path, size_key)[1]); count += 1
            except OSError:
                pass
    return total, count


def clear_previews(paths=None):
    """Delete previews (all, or only those of the given photos). Returns (bytes freed, files)."""
    freed = count = 0
    with CACHE_GATE.clear():
        if paths is None:
            targets = []
            for dirpath, _dirs, files in os.walk(THUMBS):
                targets += [os.path.join(dirpath, f) for f in files]
        else:
            targets = []
            for path in paths:
                for size_key in THUMB_SIZES:
                    try:
                        targets.append(thumb_file(path, size_key)[1])
                    except OSError:
                        pass
        for t in targets:
            try:
                size = os.path.getsize(t)
                os.remove(t)
                freed += size; count += 1
            except OSError:
                pass
        if paths is None:  # tidy away the empty sub-folders too
            for name in os.listdir(THUMBS):
                try:
                    os.rmdir(os.path.join(THUMBS, name))
                except OSError:
                    pass
    return freed, count


class Prefetcher:
    """Builds small thumbnails in the background so browsing feels instant."""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=max(2, min(4, (os.cpu_count() or 2))))
        self.gen = 0

    def start(self, files, gen):
        self.gen = gen
        for path in files:
            self.pool.submit(self._one, path, gen)

    def _one(self, path, gen):
        if gen != self.gen:
            return
        try:
            with CACHE_GATE.use():
                thumbnail(path, "t")
        except Exception:
            pass


# ---------------------------------------------------------------- export

def safe_name_part(text):
    # Windows can't have a name that ends in a space or dot, so trim those (also after cutting to length)
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text).strip(" .\t")
    return text[:80].rstrip(" .\t")


def unique_folder(path):
    path = path.rstrip("/\\")
    if not os.path.exists(path) or (os.path.isdir(path) and not os.listdir(path)):
        return path
    n = 2
    while os.path.exists("%s %d" % (path, n)):
        n += 1
    return "%s %d" % (path, n)


def mark_output_folder(folder):
    marker = os.path.join(folder, OUTPUT_MARKER)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("Folder created by Roll Merge. Safe to delete.\n")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(marker, 0x02)  # hidden
        except Exception:
            pass


def copy_jpeg_with_time(src, dst, when):
    """Copy a JPEG and set its capture time, without re-compressing the image."""
    stamp = when.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
    try:
        exif = piexif.load(src)
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif.setdefault("0th", {})[piexif.ImageIFD.DateTime] = stamp
    exif.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = stamp
    exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = stamp
    try:
        blob = piexif.dump(exif)
    except Exception:  # unusual existing tags: keep only the times
        blob = piexif.dump({
            "0th": {piexif.ImageIFD.DateTime: stamp},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: stamp, piexif.ExifIFD.DateTimeDigitized: stamp},
        })
    with open(src, "rb") as fh:
        data = fh.read()
    piexif.insert(blob, data, dst)


JOBS = {}


def run_export(job, items, outdir, date_text, prefix, clear_after=None):
    try:
        try:
            day = datetime.strptime(date_text, "%Y-%m-%d")
        except (TypeError, ValueError):
            day = datetime.now()
        start = day.replace(hour=9, minute=0, second=0, microsecond=0)
        outdir = unique_folder(outdir)
        os.makedirs(outdir, exist_ok=True)
        mark_output_folder(outdir)
        job["outdir"] = outdir
        width = max(4, len(str(len(items))))
        prefix = safe_name_part(prefix or "")
        log_rows = ["folder,new_name,camera,roll,original_file"]
        exif_skipped = 0
        for i, item in enumerate(items):
            src = item["path"]
            stem, ext = os.path.splitext(os.path.basename(src))
            number = str(i + 1).zfill(width)
            new_name = ("%s %s%s" % (prefix, number, ext)) if prefix else ("%s_%s%s" % (number, stem, ext))
            target_dir = os.path.join(outdir, item["folder"]) if item.get("folder") else outdir
            os.makedirs(target_dir, exist_ok=True)
            dst = os.path.join(target_dir, new_name)
            when = start + timedelta(seconds=SECONDS_BETWEEN_PHOTOS * i)
            done = False
            if ext.lower() in JPEG_EXTS and piexif is not None:
                try:
                    copy_jpeg_with_time(src, dst, when)
                    done = True
                except Exception:
                    done = False
            if not done:
                shutil.copyfile(src, dst)
                exif_skipped += 1
            ts = time.mktime(when.timetuple())
            os.utime(dst, (ts, ts))
            log_rows.append(",".join('"%s"' % str(v).replace('"', "'") for v in (item.get("folder", ""), new_name, item["lane"], item["roll"], src)))
            job["done"] = i + 1
        with open(os.path.join(outdir, "Roll Merge order.csv"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(log_rows) + "\n")
        job["exif_skipped"] = exif_skipped
        if clear_after:
            try:
                job["previews_freed"] = clear_previews(clear_after)[0]
            except Exception:
                job["previews_freed"] = None
        job["finished"] = True
    except Exception as exc:
        job["error"] = str(exc)
        job["finished"] = True


# ---------------------------------------------------------------- folder picker

def pick_folder(prompt):
    """Show the computer's own folder picker. Returns a path, None if cancelled."""
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "%s")' % prompt.replace('"', "")
        r = subprocess.run(["osascript", "-e", "activate", "-e", script],
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            return r.stdout.strip().rstrip("/") or None
        if "-128" in r.stderr:  # user pressed Cancel
            return None
        raise RuntimeError(r.stderr.strip() or "folder picker failed")

    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); r.update()\n"
        "p = filedialog.askdirectory(parent=r, title=%r, mustexist=True)\n"
        "print(p or '')\n" % prompt
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            path = r.stdout.strip()
            return os.path.normpath(path) if path else None
    except (OSError, subprocess.SubprocessError):
        pass
    if os.name == "nt":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description = '%s';"
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }" % prompt.replace("'", "")
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            return r.stdout.strip() or None
    raise RuntimeError("No folder picker available on this computer")


def reveal(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # noqa: only exists on Windows
    else:
        subprocess.Popen(["xdg-open", path])


# ---------------------------------------------------------------- web server

class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.gen = 0
        self.root = None
        self.rolls = []
        self.files = []


SESSION = Session()
PREFETCH = Prefetcher()
TOKEN = secrets.token_urlsafe(16)


def open_folder(path):
    path = os.path.abspath(os.path.expanduser(path.strip().strip('"').strip("'")))
    if not os.path.isdir(path):
        raise ValueError("That folder doesn't exist: %s" % path)
    rolls, files = scan(path)
    with SESSION.lock:
        SESSION.gen += 1
        SESSION.root = path
        SESSION.rolls = rolls
        SESSION.files = files
        gen = SESSION.gen
    PREFETCH.start(files, gen)
    root_name = os.path.basename(path.rstrip("/\\")) or path
    parent = os.path.dirname(path.rstrip("/\\")) or path
    return {
        "root": path,
        "rootName": root_name,
        "gen": gen,
        "rolls": rolls,
        "names": [os.path.basename(f) for f in files],
        "saved": load_state(path),
        "defaultOutdir": os.path.join(parent, root_name + " (sorted)"),
        "today": datetime.now().strftime("%Y-%m-%d"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "RollMerge/" + VERSION

    def log_message(self, *args):
        pass

    # -- helpers
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json", {"Cache-Control": "no-store"})

    def _error(self, msg, code=400):
        self._json({"error": msg}, code)

    def _authed(self, query):
        return self.headers.get("X-Token") == TOKEN or query.get("t", [""])[0] == TOKEN

    # -- routes
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                body = fh.read()
            return self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})
        if not self._authed(q):
            return self._error("forbidden", 403)
        if url.path == "/img":
            return self._image(q)
        if url.path == "/api/job":
            job = JOBS.get(q.get("id", [""])[0])
            return self._json(job or {"error": "unknown job"})
        if url.path == "/api/recent":
            return self._json(recent_folders())
        if url.path == "/api/cache":
            if q.get("scope", [""])[0] == "wedding":
                with SESSION.lock:
                    files = list(SESSION.files)
                total, count = cache_usage(files)
            else:
                total, count = cache_usage()
            return self._json({"bytes": total, "files": count})
        return self._error("not found", 404)

    def _image(self, q):
        try:
            gen = int(q.get("g", ["-1"])[0])
            idx = int(q.get("id", ["-1"])[0])
            size = q.get("s", ["t"])[0]
        except ValueError:
            return self._error("bad request")
        with SESSION.lock:
            if gen != SESSION.gen or not (0 <= idx < len(SESSION.files)) or size not in THUMB_SIZES:
                return self._error("not found", 404)
            path = SESSION.files[idx]
        try:
            body = thumbnail_bytes(path, size)
        except Exception as exc:
            return self._error("Couldn't read %s: %s" % (os.path.basename(path), exc), 500)
        self._send(200, body, "image/jpeg", {"Cache-Control": "private, max-age=86400"})

    def do_POST(self):
        url = urlparse(self.path)
        if not self._authed(parse_qs(url.query)):
            return self._error("forbidden", 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._error("bad json")
        try:
            return self._post(url.path, body)
        except Exception as exc:
            return self._error(str(exc), 500)

    def _post(self, path, body):
        if path == "/api/pick-folder":
            try:
                chosen = pick_folder(body.get("prompt") or "Choose a folder")
            except Exception as exc:
                return self._json({"unavailable": True, "error": str(exc)})
            return self._json({"path": chosen})

        if path == "/api/open":
            try:
                return self._json(open_folder(body.get("path") or ""))
            except ValueError as exc:
                return self._error(str(exc))

        if path == "/api/state":
            root = body.get("root")
            if not root or norm_root(root) != norm_root(SESSION.root or ""):
                return self._error("folder not open")
            save_state(SESSION.root, body.get("state") or {})
            return self._json({"ok": True})

        if path == "/api/export":
            with SESSION.lock:
                if body.get("gen") != SESSION.gen:
                    return self._error("The folder changed. Please reopen it.")
                files = list(SESSION.files)
            items = []
            for entry in body.get("order") or []:
                idx = entry.get("id")
                if not isinstance(idx, int) or not (0 <= idx < len(files)):
                    return self._error("bad photo id")
                items.append({"path": files[idx], "lane": entry.get("lane", ""), "roll": entry.get("roll", ""),
                              "folder": safe_name_part(str(entry.get("folder") or ""))})
            if not items:
                return self._error("Place at least one photo before exporting.")
            if len({e["path"] for e in items}) != len(items):
                return self._error("A photo is listed twice. Please reopen the folder and try again.")
            # banished photos were deliberately taken out of the sort, so they don't count as missing
            placed_ids = {entry.get("id") for entry in body.get("order") or []}
            banished = {i for i in body.get("banished") or []
                        if isinstance(i, int) and 0 <= i < len(files) and i not in placed_ids}
            if len(items) + len(banished) != len(files) and not body.get("partial"):
                return self._error("Every photo must be placed before exporting (or tick the box to leave the rest out).")
            outdir = (body.get("outdir") or "").strip()
            if not outdir:
                return self._error("Choose where to save the sorted copies.")
            outdir = os.path.abspath(os.path.expanduser(outdir))
            root = norm_root(SESSION.root)
            if norm_root(outdir) == root:
                return self._error("Please save the copies to a different folder from the originals.")
            job_id = secrets.token_hex(6)
            job = JOBS[job_id] = {"done": 0, "total": len(items), "finished": False, "error": None, "outdir": outdir}
            clear_after = files if body.get("clearPreviews") else None  # every photo of this wedding
            threading.Thread(target=run_export, args=(job, items, outdir, body.get("date"), body.get("prefix"), clear_after),
                             daemon=True).start()
            return self._json({"job": job_id})

        if path == "/api/clear-cache":
            freed, count = clear_previews()
            return self._json({"freed": freed, "files": count})

        if path == "/api/reveal":
            target = body.get("path") or ""
            if os.path.isdir(target):
                reveal(target)
            return self._json({"ok": True})

        if path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None

        return self._error("not found", 404)


def free_port(preferred=8765):
    for port in list(range(preferred, preferred + 20)) + [0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_browser = "--no-browser" in sys.argv
    threading.Thread(target=clean_old_thumbs, daemon=True).start()
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = "http://127.0.0.1:%d/?t=%s" % (port, TOKEN)
    if args:
        url += "&open=" + quote(os.path.abspath(args[0]), safe="")
    print("")
    print("  Roll Merge is running.")
    print("  If your browser didn't open, copy this link into it:")
    print("  " + url)
    print("")
    print("  Keep this window open while you work. Close it (or press Quit in the app) when you're done.")
    print("")
    sys.stdout.flush()
    if not no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("  Roll Merge has stopped.")


if __name__ == "__main__":
    main()
