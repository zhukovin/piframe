import threading
from collections import deque
from dataclasses import dataclass
from itertools import islice
from typing import List, Tuple, Literal

import pygame
from PIL import Image, ImageOps
import random
from threading import Lock, Thread
from typing import Optional
from web_server import run_web

import faulthandler
import signal
import resource
import os
import logging


faulthandler.register(signal.SIGUSR1)

logger = logging.getLogger("py_frame")

ERROR_LOG_FILE = "app_errors.log"

Orientation = Literal["P", "L"]  # P = Portrait, L = Landscape

seconds_to_display = 15

MAX_HISTORY_SCREENS = 5  # or 20, tune as you like

THUMBNAIL_WIDTH = 160  # px, for the web UI's per-slide preview images


def log_mem(tag=""):
    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(f"[MEM {tag}] pid={os.getpid()} rss={usage.ru_maxrss} B")


_logging_configured = False


def setup_logging():
    """
    Configure a rotating file handler on the root logger so every
    exception -- caught-and-handled or truly uncaught, from any thread
    (main, image fetcher, Flask's web server) -- ends up in ERROR_LOG_FILE
    for later analysis, in addition to the usual terminal/journal output.

    Attached to the root logger (not just "py_frame") specifically so
    Flask's own app.logger, which propagates to root by default, is
    captured too without needing any changes in web_server.py. Safe to
    call more than once (e.g. across tests) -- only configures once.
    """
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True

    import sys
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(ERROR_LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    uncaught_logger = logging.getLogger("py_frame.uncaught")

    def log_uncaught_main_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        uncaught_logger.critical(
            "Uncaught exception in main thread",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = log_uncaught_main_exception

    def log_uncaught_thread_exception(args):
        uncaught_logger.critical(
            f"Uncaught exception in thread {args.thread.name!r}",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        threading.__excepthook__(args)

    threading.excepthook = log_uncaught_thread_exception


@dataclass
class Slide:
    path: str
    surface: pygame.Surface
    orientation: Orientation  # "P" or "L"
    load_bytes: int = 0       # raw file size read from disk/NFS
    load_seconds: float = 0.0  # time spent reading those bytes (excludes decode)


class SlideshowController:
    def __init__(self):
        # A Condition wraps a lock (acquire/release/context-manager behave
        # exactly like the plain Lock this used to be) but also lets
        # web_server's SSE stream block on wait_for(...) until render_loop
        # (or a request handler) calls bump_version(), instead of polling.
        self.lock = threading.Condition()
        # Current screen
        self.current_slides: list[Slide] = []
        self.current_pattern_type: Optional[int] = None

        # History
        self.history: list[tuple[list[Slide], int]] = []
        self.history_index: int = -1

        # Pending command from web: {"type": "...", "steps": int}
        self.pending_command: Optional[dict] = None

        # Exclusions. `pending_exclusions` holds paths that were marked but
        # not yet committed to exclusions_file -- they stay excluded (won't
        # be re-fetched) but remain visible/undoable for as long as they're
        # still reachable in `history` (see finalize_exclusions).
        self.excluded_paths: set[str] = set()
        self.pending_exclusions: set[str] = set()
        self.exclusions_file: str = "exclusions.txt"

        # paused state
        self.paused: bool = False
        # black-screen mode (screen fully black while slideshow paused)
        self.black_screen: bool = False

        # Diagnostics: whether the most recent photo load attempt succeeded,
        # and the recent read speed (bytes/sec) over the last few successful loads.
        self.drive_ok: bool = True
        self.download_bytes_per_sec: Optional[float] = None

        # Every load attempt (success or failure) is appended as a CSV row
        # here for later offline analysis of size/time/speed correlation.
        self.measurements_file: str = "load_measurements.csv"

        # Bumped (under self.lock) any time something /api/state reports
        # changes, so web clients can detect staleness (a mark request
        # against an outdated screen) and the SSE stream knows when to push.
        self.state_version: int = 0

        # path -> encoded PNG bytes for the web UI's per-slide thumbnails.
        # Populated by render_loop (see update_thumbnail_cache), the only
        # thread that's safe to call pygame/SDL surface functions from --
        # web_server's Flask thread just reads this dict, so no pygame
        # calls ever happen off the render thread.
        self.thumbnail_cache: dict[str, bytes] = {}

        # Wall-clock time.time() at which the current screen will
        # auto-advance, and how many seconds each screen gets -- published
        # by render_loop so the web UI can show a countdown/progress bar.
        # Not meaningful while paused/black_screen (no advance pending).
        self.current_end_time: float = 0.0
        self.seconds_to_display: int = 15

    def bump_version(self):
        """
        Must be called while holding self.lock. Marks /api/state-visible
        state as changed and wakes any thread blocked in self.lock.wait_for
        (the SSE stream in web_server.py).
        """
        self.state_version += 1
        self.lock.notify_all()


def make_old_paper_surface(size):
    w, h = size
    surf = pygame.Surface(size)
    base_color = (235, 222, 193)
    surf.fill(base_color)

    # Add subtle random noise spots
    for _ in range(8000):
        x = random.randrange(w)
        y = random.randrange(h)
        noise = random.randint(-10, 10)
        r = min(max(base_color[0] + noise, 0), 255)
        g = min(max(base_color[1] + noise, 0), 255)
        b = min(max(base_color[2] + noise, 0), 255)
        surf.set_at((x, y), (r, g, b))

    return surf


class ImageDecodeError(Exception):
    """
    Raised when a file was read successfully but its image content couldn't
    be decoded (corrupt file, unsupported format, etc). Deliberately
    distinct from I/O errors raised while reading the raw bytes, so callers
    can tell "one bad file" apart from "the drive/NFS mount is unreachable"
    instead of conflating both into the same failure signal.

    Carries the read measurements (the raw read succeeded even though the
    decode didn't), so callers logging load performance can still record a
    valid size/time/speed data point for the read itself.
    """
    def __init__(self, message: str, load_bytes: int = 0, load_seconds: float = 0.0):
        super().__init__(message)
        self.load_bytes = load_bytes
        self.load_seconds = load_seconds


def load_slide(path: str) -> Slide:
    """
    Load a slide from disk, rotate it according to EXIF orientation,
    classify it as Portrait (P) or Landscape (L), and convert to a
    pygame.Surface.
    """
    import io
    import time

    # 1) Read the raw bytes ourselves (timed) so callers can measure
    #    disk/NFS read speed separately from JPEG decode (CPU) time.
    #    Exceptions here (missing file, permission error, stale/disconnected
    #    NFS mount) are real I/O problems and propagate as-is.
    t0 = time.monotonic()
    with open(path, "rb") as f:
        raw = f.read()
    load_seconds = time.monotonic() - t0
    load_bytes = len(raw)

    # 2) Decode with Pillow. Failures here mean the file itself is
    #    corrupt/unsupported, not that the drive is unreachable, so they're
    #    wrapped in ImageDecodeError rather than left as a bare Exception.
    try:
        img = Image.open(io.BytesIO(raw))

        # Apply EXIF orientation (rotates/flip as needed)
        img = ImageOps.exif_transpose(img)

        # Classify after rotation
        width, height = img.size
        orientation: Orientation = "P" if height > width else "L"

        # Ensure mode is suitable for pygame
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        mode = img.mode
        size = img.size
        data = img.tobytes()

        # Convert to pygame surface
        if mode == "RGBA":
            surface = pygame.image.fromstring(data, size, mode).convert_alpha()
        else:  # "RGB"
            surface = pygame.image.fromstring(data, size, mode).convert()
    except Exception as e:
        raise ImageDecodeError(
            f"failed to decode {path}: {e}",
            load_bytes=load_bytes,
            load_seconds=load_seconds,
        ) from e

    return Slide(
        path=path,
        surface=surface,
        orientation=orientation,
        load_bytes=load_bytes,
        load_seconds=load_seconds,
    )


# ============================================================
# Pattern extraction (up to 5 entries)
# ============================================================

def classify_pattern_type(count_p: int, count_l: int) -> Optional[Tuple[int, int, int]]:
    """
    Given counts of Portrait/Landscape slides, decide which pattern type (if
    any) they satisfy and how many of each orientation that pattern needs:
        Type 1: PPP      (needs 3 Portrait, 0 Landscape)
        Type 2: PPLLL    (needs 2 Portrait, 3 Landscape)
        Type 3: PLLL     (needs 1 Portrait, 3 Landscape)
    Returns None if no valid pattern fits.
    """
    if count_p >= 3:
        return 1, 3, 0
    elif count_p >= 2 and count_l >= 3:
        return 2, 2, 3
    elif count_p >= 1 and count_l >= 3:
        return 3, 1, 3
    else:
        return None


def extract_pattern_from_deque(dq: deque[Slide]) -> Tuple[List[Slide], int]:
    """
    Examine up to the first 5 elements of dq.

    Find one of the patterns based on orientation:
        Type 1: PPP      (3 Portrait)
        Type 2: PPLLL    (2 Portrait, 3 Landscape)
        Type 3: PLLL     (1 Portrait, 3 Landscape)

    Remove the chosen Slide objects from dq and return them as a list
    (in the order they were encountered). Any scanned but unused items
    are pushed back to the *left* of dq in their original order.
    """
    window = list(islice(dq, 0, 5))
    if not window or window[0].orientation != "P":
        raise ValueError("Deque must start with a portrait (P) slide for pattern logic.")

    count_p = sum(1 for s in window if s.orientation == "P")
    count_l = sum(1 for s in window if s.orientation == "L")

    # Determine pattern type & needed counts
    classification = classify_pattern_type(count_p, count_l)
    if classification is None:
        raise ValueError("No valid PPP / PPLLL / PLLL pattern in first 5 entries.")
    pattern_type, needP, needL = classification

    # Select slides that satisfy the pattern, in order
    extracted: List[Slide] = []
    unused: List[Slide] = []
    p_left, l_left = needP, needL

    for slide in window:
        if slide.orientation == "P" and p_left > 0:
            extracted.append(slide)
            p_left -= 1
        elif slide.orientation == "L" and l_left > 0:
            extracted.append(slide)
            l_left -= 1
        else:
            unused.append(slide)

        if p_left == 0 and l_left == 0:
            break

    if p_left != 0 or l_left != 0:
        raise RuntimeError("Pattern selection logic failed.")

    # Remove all scanned items from dq
    for _ in range(len(window)):
        dq.popleft()

    # Put unused scanned items back on the *left* in correct order
    for slide in reversed(unused):
        dq.appendleft(slide)

    return extracted, pattern_type


# ============================================================
# Image fetcher thread
# ============================================================

def _record_load_attempt(controller: SlideshowController,
                         success: bool,
                         bytes_per_sec: Optional[float] = None,
                         update_drive_status: bool = True):
    """
    Record the outcome of one photo load attempt for the status overlay.
    Pass update_drive_status=False for a failure that reflects a bad file
    rather than a drive/connectivity problem (e.g. a corrupt image), so
    drive_ok and the speed estimate are left untouched.
    """
    if not update_drive_status:
        return
    with controller.lock:
        controller.drive_ok = success
        controller.download_bytes_per_sec = bytes_per_sec


def log_load_measurement(log_file: str, path: str, outcome: str, load_bytes: int = 0,
                         load_seconds: float = 0.0, error_type: str = ""):
    """
    Append one load-attempt measurement to a CSV file for later offline
    analysis of size/time/speed correlation (e.g. "why is loading time or
    transfer speed inconsistent"). One row per attempt, regardless of
    outcome:
        outcome="ok"             - full success; bytes/seconds/speed are valid
        outcome="decode_error"   - the raw read succeeded (bytes/seconds are
                                    still valid) but the image itself was
                                    corrupt/unsupported
        outcome="file_not_found" - this one path doesn't exist (deleted/
                                    renamed on the NAS, a stale list entry,
                                    etc) -- says nothing about whether the
                                    drive/mount itself is reachable
        outcome="io_error"       - some other read failure (disconnected
                                    mount, permission error, timeout, etc);
                                    no measurement was possible, so
                                    bytes/seconds are blank
    error_type is the underlying exception's class name (e.g.
    "FileNotFoundError", "UnidentifiedImageError", "ConnectionResetError"),
    for telling apart *why* a failure happened, not just that it did.
    """
    import csv
    from datetime import datetime

    has_measurement = outcome in ("ok", "decode_error")
    bytes_per_sec = load_bytes / load_seconds if has_measurement and load_seconds > 0 else ""

    file_exists = os.path.exists(log_file)
    with open(log_file, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "path", "outcome", "bytes", "seconds", "bytes_per_sec", "error_type"])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            path,
            outcome,
            load_bytes if has_measurement else "",
            f"{load_seconds:.4f}" if has_measurement else "",
            f"{bytes_per_sec:.2f}" if bytes_per_sec != "" else "",
            error_type,
        ])


def image_fetcher_thread(
        file_paths: list[str],
        dq: deque[Slide],
        lock: threading.Lock,
        not_full: threading.Condition,
        producer_done: threading.Event,
        controller: SlideshowController,
        max_size: int = 5,
):
    import time

    print("Image fetcher thread native_id:", threading.get_native_id())

    # Rolling window of (bytes, seconds) for the last few successful reads,
    # used to report a live download-speed estimate (not an all-time average).
    recent_load_stats: deque[tuple[int, float]] = deque(maxlen=5)

    try:
        if not file_paths:
            producer_done.set()
            return

        idx = 0
        n = len(file_paths)

        while True:
            path = file_paths[idx]
            idx = (idx + 1) % n
            # path = random.choice(file_paths)

            with controller.lock:
                if path in controller.excluded_paths:
                    excluded = True
                else:
                    excluded = False
            if excluded:
                time.sleep(0.3)
                continue

            with not_full:
                while len(dq) >= max_size:
                    not_full.wait()

            try:
                slide = load_slide(path)
            except ImageDecodeError as e:
                # A bad/corrupt file, not a drive problem: don't flag the
                # drive down or discard the legitimate rolling speed
                # average, but the read itself succeeded so still log its
                # size/time as a valid measurement.
                logger.warning(f"Skipping unreadable image {path}", exc_info=True)
                _record_load_attempt(controller, success=False, update_drive_status=False)
                log_load_measurement(
                    controller.measurements_file, path, "decode_error",
                    load_bytes=e.load_bytes, load_seconds=e.load_seconds,
                    error_type=type(e.__cause__).__name__ if e.__cause__ else type(e).__name__,
                )
                time.sleep(0.5)
                continue
            except FileNotFoundError as e:
                # This one path doesn't exist -- e.g. deleted/renamed on the
                # NAS, or a stale list entry. Says nothing about whether the
                # drive/mount itself is reachable, so don't flag it down or
                # discard the rolling speed average (unlike a real io_error).
                logger.warning(f"Photo not found: {path}", exc_info=True)
                _record_load_attempt(controller, success=False, update_drive_status=False)
                log_load_measurement(
                    controller.measurements_file, path, "file_not_found",
                    error_type=type(e).__name__,
                )
                time.sleep(0.5)
                continue
            except Exception as e:
                logger.error(f"Failed to load {path}", exc_info=True)
                recent_load_stats.clear()
                _record_load_attempt(controller, success=False)
                log_load_measurement(
                    controller.measurements_file, path, "io_error",
                    error_type=type(e).__name__,
                )
                time.sleep(0.5)
                continue

            recent_load_stats.append((slide.load_bytes, slide.load_seconds))
            total_bytes = sum(b for b, _ in recent_load_stats)
            total_seconds = sum(s for _, s in recent_load_stats)
            bytes_per_sec = total_bytes / total_seconds if total_seconds > 0 else None

            _record_load_attempt(controller, success=True, bytes_per_sec=bytes_per_sec)
            log_load_measurement(
                controller.measurements_file, path, "ok",
                load_bytes=slide.load_bytes, load_seconds=slide.load_seconds,
            )

            with not_full:
                dq.append(slide)
                not_full.notify_all()

    finally:
        producer_done.set()


def smoothscale_safe(img: pygame.Surface, size: tuple[int, int]) -> pygame.Surface:
    """
    Wrap pygame.transform.smoothscale so that the input surface is guaranteed
    to be 24- or 32-bit, as required by smoothscale.
    """
    # Ensure 24/32-bit surface
    if img.get_bitsize() not in (24, 32):
        # Convert to 32-bit with alpha (safe for anything)
        tmp = pygame.Surface(img.get_size(), flags=pygame.SRCALPHA, depth=32)
        tmp.blit(img, (0, 0))
        img = tmp

    return pygame.transform.smoothscale(img, size)


# ============================================================
# Rendering helpers (layouts)
# ============================================================

def blit_scaled(surface: pygame.Surface, img: pygame.Surface, target_rect: pygame.Rect):
    """Scale `img` to fit inside `target_rect` preserving aspect ratio, then blit centered."""
    iw, ih = img.get_width(), img.get_height()
    tw, th = target_rect.width, target_rect.height

    if iw == 0 or ih == 0:
        return

    scale = min(tw / iw, th / ih)
    new_w = max(1, int(iw * scale))
    new_h = max(1, int(ih * scale))

    scaled = smoothscale_safe(img, (new_w, new_h))

    x = target_rect.x + (tw - new_w) // 2
    y = target_rect.y + (th - new_h) // 2
    surface.blit(scaled, (x, y))


# Exclusion-marker icon (a "no eye" glyph), shown as an overlay over a
# marked photo instead of a colored border. Path is cwd-relative like the
# app's other file paths (exclusions_file, etc.) -- expected to be run
# from the repo root, same as everywhere else.
EXCLUSION_ICON_PATH = "pictures/eye-dont-show.png"

_exclusion_icon_original: Optional[pygame.Surface] = None
_exclusion_icon_load_attempted = False
_exclusion_icon_scaled_cache: dict[int, Optional[pygame.Surface]] = {}


def _load_exclusion_icon() -> Optional[pygame.Surface]:
    """
    Load and cache the exclusion-marker icon once. Returns None (and logs
    once) if the file is missing/unreadable, so a bad/absent icon file
    degrades to "no overlay drawn" rather than crashing the render loop.
    """
    global _exclusion_icon_original, _exclusion_icon_load_attempted
    if not _exclusion_icon_load_attempted:
        _exclusion_icon_load_attempted = True
        try:
            # convert_alpha() (not convert()) since the icon is a PNG with
            # real per-pixel transparency (a soft glow fading to
            # transparent at the edges) -- this preserves that alpha
            # channel, which a plain convert() would flatten away.
            _exclusion_icon_original = pygame.image.load(EXCLUSION_ICON_PATH).convert_alpha()
        except Exception:
            logger.warning(f"Could not load exclusion icon from {EXCLUSION_ICON_PATH!r}", exc_info=True)
            _exclusion_icon_original = None
    return _exclusion_icon_original


def draw_exclusion_overlay(screen: pygame.Surface, rect: pygame.Rect):
    """Blit the exclusion-marker icon centered over `rect`, scaled to
    roughly 40% of its shorter side, preserving the icon's own aspect
    ratio."""
    icon = _load_exclusion_icon()
    if icon is None:
        return

    target_edge = int(min(rect.width, rect.height) * 0.4)
    if target_edge <= 0:
        return

    scaled = _exclusion_icon_scaled_cache.get(target_edge)
    if scaled is None:
        iw, ih = icon.get_width(), icon.get_height()
        if iw <= 0 or ih <= 0:
            _exclusion_icon_scaled_cache[target_edge] = None
            return
        scale = target_edge / max(iw, ih)
        scaled = smoothscale_safe(icon, (max(1, int(iw * scale)), max(1, int(ih * scale))))
        # Unlike colorkey, real per-pixel alpha (from convert_alpha()
        # above) survives pygame.transform functions and blit()'s default
        # blending, so no extra step is needed here to preserve it.
        _exclusion_icon_scaled_cache[target_edge] = scaled

    if scaled is None:
        return

    x = rect.x + (rect.width - scaled.get_width()) // 2
    y = rect.y + (rect.height - scaled.get_height()) // 2
    screen.blit(scaled, (x, y))


def draw_slot_overlay(screen: pygame.Surface,
                      rect: pygame.Rect,
                      slot_index: int,
                      marked: bool,
                      font: pygame.font.Font):
    BLACK = (0, 0, 0)
    WHITE = (255, 255, 255)

    if marked:
        draw_exclusion_overlay(screen, rect)

    # label top-left -- black text with a white outline, no filled
    # background, matching the status overlay's style (see
    # draw_status_overlay).
    label = str(slot_index + 1)
    text_surf = font.render(label, True, BLACK)
    outline_surf = font.render(label, True, WHITE)
    padding = 4
    x = rect.x + 8 + padding
    y = rect.y + 8 + padding
    for dx in range(-STATUS_OUTLINE_PX, STATUS_OUTLINE_PX + 1):
        for dy in range(-STATUS_OUTLINE_PX, STATUS_OUTLINE_PX + 1):
            if dx == 0 and dy == 0:
                continue
            screen.blit(outline_surf, (x + dx, y + dy))
    screen.blit(text_surf, (x, y))


def format_speed(bytes_per_sec: Optional[float]) -> str:
    """
    Format a bytes/sec figure with an auto-scaled unit (Bps, KBps, MBps,
    GBps), rounded to 2 decimal places using round-half-up (not Python's
    default round-half-to-even, and not raw float rounding, since both can
    give surprising results right at a .xx5 boundary).
    """
    if bytes_per_sec is None:
        return "-- Bps"

    from decimal import Decimal, ROUND_HALF_UP

    units = ["Bps", "KBps", "MBps", "GBps"]
    value = bytes_per_sec

    for i, unit in enumerate(units):
        rounded = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if rounded < 1000 or i == len(units) - 1:
            return f"{rounded} {unit}"
        value /= 1000

    return f"{value:.2f} {units[-1]}"  # unreachable, satisfies static analysis


STATUS_PADDING = 8
STATUS_LINE_SPACING = 4
STATUS_MARGIN = 10
STATUS_OUTLINE_PX = 2
# Sized against the worst case of each line (not whichever happens to be
# showing right now), so the box's position/size stays stable across
# Paused/Playing or Drive: OK/DISCONNECTED transitions.
STATUS_FIXED_VOCABULARY = ["Paused", "Playing", "Drive: OK", "Drive: DISCONNECTED"]


def compute_status_box_rect(screen: pygame.Surface, font: pygame.font.Font) -> pygame.Rect:
    """
    Compute the status overlay's bottom-right rect. Fixed size/position for
    a given screen+font (independent of the current paused/drive_ok/speed
    values), so callers can snapshot this exact region once and reuse it to
    erase old text before redrawing -- there's no background box to do that
    erasing for them anymore.
    """
    stable_widths = [font.size(text)[0] for text in STATUS_FIXED_VOCABULARY]
    box_w = max(stable_widths) + 2 * STATUS_PADDING + 2 * STATUS_OUTLINE_PX
    line_height = font.get_height()
    box_h = (
        line_height * 3
        + STATUS_LINE_SPACING * 2
        + 2 * STATUS_PADDING
        + 2 * STATUS_OUTLINE_PX
    )
    screen_w, screen_h = screen.get_size()
    return pygame.Rect(
        screen_w - box_w - STATUS_MARGIN,
        screen_h - box_h - STATUS_MARGIN,
        box_w,
        box_h,
    )


def draw_status_overlay(screen: pygame.Surface,
                        font: pygame.font.Font,
                        paused: bool,
                        drive_ok: bool,
                        download_bytes_per_sec: Optional[float],
                        box_rect: Optional[pygame.Rect] = None) -> pygame.Rect:
    """
    Draw the status text -- Playing/Paused, drive mount health, and recent
    read speed -- black with a white outline, no filled background, in the
    bottom-right corner. Drawn last (on top of everything, including
    black-screen mode) so stalls can be diagnosed without waiting for the
    next slide change.

    Pass the same box_rect (from compute_status_box_rect) every call so the
    text is always positioned identically; this is what the caller
    snapshots/restores to erase old text before redrawing (see render_loop).
    """
    BLACK = (0, 0, 0)
    WHITE = (255, 255, 255)

    if box_rect is None:
        box_rect = compute_status_box_rect(screen, font)

    lines = [
        "Paused" if paused else "Playing",
        "Drive: OK" if drive_ok else "Drive: DISCONNECTED",
        format_speed(download_bytes_per_sec),
    ]
    text_surfaces = [font.render(line, True, BLACK) for line in lines]
    outline_surfaces = [font.render(line, True, WHITE) for line in lines]

    right_edge = box_rect.right - STATUS_PADDING - STATUS_OUTLINE_PX
    y = box_rect.y + STATUS_PADDING + STATUS_OUTLINE_PX
    for text_surf, outline_surf in zip(text_surfaces, outline_surfaces):
        x = right_edge - text_surf.get_width()
        for dx in range(-STATUS_OUTLINE_PX, STATUS_OUTLINE_PX + 1):
            for dy in range(-STATUS_OUTLINE_PX, STATUS_OUTLINE_PX + 1):
                if dx == 0 and dy == 0:
                    continue
                screen.blit(outline_surf, (x + dx, y + dy))
        screen.blit(text_surf, (x, y))
        y += text_surf.get_height() + STATUS_LINE_SPACING

    return box_rect


def _tile_mirror_strip(surf: pygame.Surface, scaled: pygame.Surface, flipped: pygame.Surface,
                        horizontal: bool, near_edge: int, far_edge: int, tile_extent: int,
                        cross_offset: int):
    """
    Fill the gap on one side of `scaled` (e.g. the strip left of a
    pillarboxed image) by tiling alternating original/flipped copies
    outward from `near_edge`, so the tile touching the image is always the
    mirrored one -- continuous with the image at the seam -- and each
    tile beyond that keeps alternating, like a hall of mirrors.

    horizontal: True to tile left/right (varying x), False to tile
    up/down (varying y).
    near_edge: the coordinate (x or y) of the seam against the image.
    far_edge: the coordinate (x or y) to tile toward (0 for the
    left/top side, the rect's width/height for the right/bottom side).
    tile_extent: width (horizontal) or height (vertical) of one tile,
    i.e. the scaled image's own width/height in that axis.
    cross_offset: the fixed x (vertical tiling) or y (horizontal tiling)
    position shared by every tile -- the image's own offset in that axis.
    """
    if tile_extent <= 0:
        return
    going_negative = far_edge < near_edge
    pos = near_edge
    use_flipped = True
    while (pos > far_edge) if going_negative else (pos < far_edge):
        tile = flipped if use_flipped else scaled
        tile_pos = pos - tile_extent if going_negative else pos
        if horizontal:
            surf.blit(tile, (tile_pos, cross_offset))
        else:
            surf.blit(tile, (cross_offset, tile_pos))
        pos = pos - tile_extent if going_negative else pos + tile_extent
        use_flipped = not use_flipped


def build_mirror_fill(img: pygame.Surface, rect_size: tuple[int, int]) -> pygame.Surface:
    """
    Render one slide's contribution to the blurred background: the image
    scaled to fit rect_size (the same aspect-preserving fit blit_scaled
    uses for the sharp image drawn on top), with the surrounding letterbox/
    pillarbox gap filled by mirroring the image's own edge outward instead
    of an unrelated stretched copy -- so the background meets the visible
    image as a continuous reflection, blending far more smoothly once
    blurred. The two sides of a gap are filled independently, each as a
    reflection of the edge it's adjacent to.
    """
    tw, th = rect_size
    surf = pygame.Surface((max(tw, 1), max(th, 1))).convert()

    iw, ih = img.get_width(), img.get_height()
    if iw == 0 or ih == 0 or tw <= 0 or th <= 0:
        return surf

    scale = min(tw / iw, th / ih)
    new_w = max(1, int(iw * scale))
    new_h = max(1, int(ih * scale))
    scaled = smoothscale_safe(img, (new_w, new_h))

    x = (tw - new_w) // 2
    y = (th - new_h) // 2

    # Handled independently (not if/elif): float rounding in the scale
    # calculation above can leave a negligible (<=1px) gap on the "wrong"
    # axis alongside the real one, so both need filling to avoid an
    # unfilled sliver -- see build_mirror_fill's tests for the case this
    # guards against.
    if new_w < tw:
        # Pillarboxed: fill the left/right gaps, tiling horizontally.
        flipped = pygame.transform.flip(scaled, True, False)
        _tile_mirror_strip(surf, scaled, flipped, True, x, 0, new_w, y)
        _tile_mirror_strip(surf, scaled, flipped, True, x + new_w, tw, new_w, y)
    if new_h < th:
        # Letterboxed: fill the top/bottom gaps, tiling vertically.
        flipped = pygame.transform.flip(scaled, False, True)
        _tile_mirror_strip(surf, scaled, flipped, False, y, 0, new_h, x)
        _tile_mirror_strip(surf, scaled, flipped, False, y + new_h, th, new_h, x)

    surf.blit(scaled, (x, y))
    return surf


def build_blurred_background(screen_size, slide_rects):
    """
    screen_size: (W, H)
    slide_rects: list of (surface, rect) for each image in its final position

    Returns a pygame.Surface with a blurred, image-derived background that
    covers the whole screen and fills gaps smoothly.
    """
    W, H = screen_size
    temp = pygame.Surface((W, H)).convert()

    # Base fill: use average-ish color from first slide if available,
    # so even areas with no rectangles have a nice tint.
    if slide_rects:
        first_surf = slide_rects[0][0]
        # scale to 1x1 to get a crude average color
        tiny = smoothscale_safe(first_surf, (1, 1))
        avg_color = tiny.get_at((0, 0))[:3]
    else:
        avg_color = (0, 0, 0)

    temp.fill(avg_color)

    # Each rect's gap (letterbox/pillarbox) is filled with a mirrored
    # reflection of the image's own edge rather than a stretched copy, so
    # the background meets the sharp image drawn on top as a continuous
    # reflection instead of an unrelated derived image.
    for slide_surface, rect in slide_rects:
        if rect.width > 0 and rect.height > 0:
            filled = build_mirror_fill(slide_surface, (rect.width, rect.height))
            temp.blit(filled, rect.topleft)

    # Now blur by downscaling and upscaling
    factor = 8  # tweak for blur strength & performance
    small_w = max(1, W // factor)
    small_h = max(1, H // factor)

    small = smoothscale_safe(temp, (small_w, small_h))
    blurred = smoothscale_safe(small, (W, H))

    return blurred


def render_single_landscape(screen: pygame.Surface,
                            slide: Slide,
                            background: Optional[pygame.Surface],
                            font: pygame.font.Font,
                            marks: set[str]):
    if background is not None:
        screen.blit(background, (0, 0))
    else:
        screen.fill((0, 0, 0))

    rect = screen.get_rect()
    blit_scaled(screen, slide.surface, rect)

    # slot index 0 always
    draw_slot_overlay(screen, rect, 0, (slide.path in marks), font)


def _pattern_slide_order(slides: List[Slide], pattern_type: int) -> List[Slide]:
    """
    The order compute_pattern_rects assigns slides to the rects it
    builds. Type 1 (PPP) keeps slides in their original order; types 2
    (PPLLL) and 3 (PLLL) group by orientation (all L's, in original
    relative order, then all P's) before laying out columns -- NOT the
    same order as `slides` itself. render_pattern uses this to correctly
    attribute a mark (keyed by path) to the rect actually drawing that
    photo, rather than assuming rects[i] corresponds to slides[i], which
    is only true for pattern_type 0/1.
    """
    if pattern_type == 1:
        return list(slides[:3])

    Ls = [s for s in slides if s.orientation == "L"]
    Ps = [s for s in slides if s.orientation == "P"]

    if pattern_type == 2:
        return Ls[:3] + Ps[:2]

    if pattern_type == 3:
        if not Ps or not Ls:
            return []
        return [Ps[0]] + Ls[:3]

    return []


def compute_pattern_rects(screen: pygame.Surface, slides: List[Slide], pattern_type: int) -> List[
    tuple[pygame.Surface, pygame.Rect]]:
    """
    Render slides according to pattern type:
      1: PPP   -> 3 portrait images side-by-side, full screen (3 columns)
      2: PPLLL -> Column 1: 3 L stacked; Column 2 & 3: 2 P full-height
      3: PLLL  -> Column 3: P full-height; Columns 1-2: top L spans both,
                              bottom two L's share the bottom half (one in col1, one in col2)
    """
    W, H = screen.get_width(), screen.get_height()
    col_w = W // 3
    rects: List[tuple[pygame.Surface, pygame.Rect]] = []

    if pattern_type == 1:
        # PPP: 3 images → columns 0,1,2 full-height
        for idx, slide in enumerate(slides[:3]):
            rect = pygame.Rect(idx * col_w, 0, col_w, H)
            rects.append((slide.surface, rect))

    elif pattern_type == 2:
        # PPLLL: 3 L stacked in first column, 2 P full-height in columns 2 and 3
        Ls = [s for s in slides if s.orientation == "L"]
        Ps = [s for s in slides if s.orientation == "P"]

        # Column 1: 3 L stacked
        if len(Ls) >= 3:
            h3 = H // 3
            for i in range(3):
                rect = pygame.Rect(
                    0,
                    i * h3,
                    col_w,
                    h3 if i < 2 else H - 2 * h3
                )
                rects.append((Ls[i].surface, rect))

        # Columns 2 & 3: P full-height
        if len(Ps) >= 1:
            rects.append((Ps[0].surface, pygame.Rect(col_w, 0, col_w, H)))
        if len(Ps) >= 2:
            rects.append((Ps[1].surface, pygame.Rect(2 * col_w, 0, col_w, H)))

    elif pattern_type == 3:
        # PLLL: 1 P, 3 L
        Ls = [s for s in slides if s.orientation == "L"]
        Ps = [s for s in slides if s.orientation == "P"]
        if not Ps or not Ls:
            return rects

        # Column 3: P full-height
        rects.append((Ps[0].surface, pygame.Rect(2 * col_w, 0, col_w, H)))

        # Top L spans columns 1+2, top half
        rects.append((Ls[0].surface, pygame.Rect(0, 0, 2 * col_w, H // 2)))

        # Bottom two Ls share bottom half in columns 1 & 2
        if len(Ls) >= 2:
            rects.append((Ls[1].surface, pygame.Rect(0, H // 2, col_w, H // 2)))
        if len(Ls) >= 3:
            rects.append((Ls[2].surface, pygame.Rect(col_w, H // 2, col_w, H // 2)))

    return rects


def render_pattern(screen: pygame.Surface,
                   slides: list[Slide],
                   pattern_type: int,
                   background: Optional[pygame.Surface],
                   font: pygame.font.Font,
                   marks: set[str]):
    if background is not None:
        screen.blit(background, (0, 0))
    else:
        screen.fill((0, 0, 0))

    rects = compute_pattern_rects(screen, slides, pattern_type)
    # rects[i] doesn't correspond to slides[i] for pattern_type 2/3 (see
    # _pattern_slide_order) -- look up each rect's actual photo by
    # position in that same order, so marks (keyed by path) land on the
    # correct rect instead of an arbitrary one.
    ordered_slides = _pattern_slide_order(slides, pattern_type)
    for idx, (surf, rect) in enumerate(rects):
        blit_scaled(screen, surf, rect)
        marked = idx < len(ordered_slides) and ordered_slides[idx].path in marks
        draw_slot_overlay(screen, rect, idx, marked, font)


def load_exclusions(controller: SlideshowController):
    """
    Populate controller.excluded_paths from controller.exclusions_file, if it exists.
    Called once at startup so exclusions persist across restarts.
    """
    if not os.path.exists(controller.exclusions_file):
        return

    with open(controller.exclusions_file, "r") as f:
        for line in f:
            path = line.strip()
            if path:
                controller.excluded_paths.add(path)


CONFIG_FILE = "py-frame.conf"
DEFAULT_NIGHT_START = (22, 0)
DEFAULT_NIGHT_END = (7, 0)
DEFAULT_SHUFFLE = True


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, _, minute_str = value.strip().partition(":")
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {value!r}")
    return hour, minute


def load_schedule_config(path: str = CONFIG_FILE) -> tuple[tuple[int, int], tuple[int, int]]:
    """
    Read the auto-scheduling night window (start, end) from an INI-style
    config file, e.g.:

        [schedule]
        start = 22:00
        end = 07:00

    Falls back to the DEFAULT_NIGHT_START/END window when the file is
    missing or malformed, so a bad or absent config never blocks startup.
    """
    if not os.path.exists(path):
        return DEFAULT_NIGHT_START, DEFAULT_NIGHT_END

    import configparser

    parser = configparser.ConfigParser()
    try:
        parser.read(path)
        start = _parse_hhmm(parser.get("schedule", "start", fallback="22:00"))
        end = _parse_hhmm(parser.get("schedule", "end", fallback="07:00"))
    except (configparser.Error, ValueError):
        logger.warning(f"Could not parse {path}, using default schedule 22:00-07:00", exc_info=True)
        return DEFAULT_NIGHT_START, DEFAULT_NIGHT_END

    return start, end


def load_display_config(path: str = CONFIG_FILE) -> bool:
    """
    Read the startup photo order from an INI-style config file, e.g.:

        [display]
        shuffle = true

    `shuffle = true` fully randomizes display order; `shuffle = false` keeps
    the list's original order but starts from a random point and wraps
    around. Falls back to DEFAULT_SHUFFLE when the file/section/key is
    missing or malformed. This replaces the old web UI shuffle toggle --
    the order is now fixed for the life of the process, set once at startup.
    """
    if not os.path.exists(path):
        return DEFAULT_SHUFFLE

    import configparser

    parser = configparser.ConfigParser()
    try:
        parser.read(path)
        return parser.getboolean("display", "shuffle", fallback=DEFAULT_SHUFFLE)
    except (configparser.Error, ValueError):
        logger.warning(f"Could not parse {path}, using default shuffle={DEFAULT_SHUFFLE}", exc_info=True)
        return DEFAULT_SHUFFLE


def is_within_night_window(hour: int, minute: int, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """
    True if hour:minute falls within the [start, end) night window.
    Handles windows that wrap past midnight (e.g. 22:00 -> 07:00) as well
    as same-day windows (e.g. 13:00 -> 15:00). A zero-length window
    (start == end) never counts as night.
    """
    now_minutes = hour * 60 + minute
    start_minutes = start[0] * 60 + start[1]
    end_minutes = end[0] * 60 + end[1]

    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def set_hdmi_power(on: bool):
    """
    Turn the physical HDMI output on/off via vcgencmd, so the monitor
    itself loses signal and goes into its own sleep mode -- distinct from
    black_screen mode, which just paints black pixels while HDMI stays
    live and the monitor's backlight stays on.

    Best-effort: logs a warning and does nothing if vcgencmd isn't
    available (e.g. developing off the Pi) rather than crashing the
    render loop over a cosmetic feature.
    """
    import subprocess
    try:
        subprocess.run(
            ["vcgencmd", "display_power", "1" if on else "0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning(
            f"Could not set HDMI power {'on' if on else 'off'} via vcgencmd",
            exc_info=True,
        )


def finalize_exclusions(controller: SlideshowController):
    """
    Commit sweep: /api/mark already adds/removes paths from
    excluded_paths + pending_exclusions immediately (so the fetcher stops
    re-queuing a marked photo right away). This just decides when a pending
    exclusion becomes permanent -- once a marked path is no longer present
    in current_slides or anywhere in history (i.e. it's scrolled further
    back than Prev can reach, capped at MAX_HISTORY_SCREENS), it's written
    to exclusions_file and dropped from pending_exclusions. Until that
    point it stays undo-able via /api/mark. Called once per screen advance.
    """
    with controller.lock:
        if not controller.pending_exclusions:
            return

        visible_paths = {s.path for s in controller.current_slides}
        for slides, _ in controller.history:
            visible_paths.update(s.path for s in slides)

        newly_committed = [p for p in controller.pending_exclusions if p not in visible_paths]
        for p in newly_committed:
            controller.pending_exclusions.discard(p)

    if newly_committed:
        with open(controller.exclusions_file, "a") as f:
            for p in newly_committed:
                f.write(p + "\n")


def downscale_slide_to_screen(slide: Slide, max_w: int, max_h: int):
    """
    Ensure slide.surface fits within (max_w, max_h).
    If it's larger, downscale it (preserving aspect ratio) IN-PLACE.
    This reduces memory usage for history and current slides.
    """
    surf = slide.surface
    w, h = surf.get_width(), surf.get_height()

    # Already small enough
    if w <= max_w and h <= max_h:
        return

    # Compute scale factor to fit within screen
    scale = min(max_w / w, max_h / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))

    slide.surface = smoothscale_safe(surf, (new_w, new_h))


def downscale_slides_to_screen(slides: list[Slide], max_w: int, max_h: int):
    for s in slides:
        downscale_slide_to_screen(s, max_w, max_h)


def build_thumbnail_bytes(surface: pygame.Surface, width: int = THUMBNAIL_WIDTH) -> bytes:
    """
    Downscale an already-decoded slide surface into a small encoded PNG
    for the web UI. Must only be called from the thread that owns
    pygame's video context (the render_loop thread) -- pygame/SDL surface
    operations aren't guaranteed safe to call from another thread, which
    is why this used to live in web_server.py's Flask request handler.

    Encoded via Pillow (already a hard dependency, used by load_slide)
    rather than pygame.image.save(..., "png"): saving PNG through pygame
    requires the system's SDL_image to have been built with save support
    (SDL_image >= 2.0.2), which isn't guaranteed on every Pi image --
    Pillow bundles its own PNG encoder, so this works everywhere.
    """
    import io

    w, h = surface.get_width(), surface.get_height()
    scale = width / w
    thumb = smoothscale_safe(surface, (width, max(1, round(h * scale))))

    raw = pygame.image.tostring(thumb, "RGB")
    img = Image.frombytes("RGB", thumb.get_size(), raw)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def update_thumbnail_cache(controller: SlideshowController, slides: list[Slide],
                            max_to_generate: Optional[int] = None):
    """
    Generate and cache a thumbnail for any of `slides` not already in
    controller.thumbnail_cache, then drop cached entries for paths no
    longer reachable via current_slides or history, so the cache stays
    small over a long-running slideshow.

    `max_to_generate` caps how many NEW thumbnails this call will
    generate (None = unlimited). render_loop calls this every iteration
    with a small cap so a multi-photo screen's thumbnails fill in
    gradually across iterations instead of blocking the loop -- and with
    it, responsiveness to pause/mark/next/prev -- for however long a full
    batch takes on slower hardware (observed live: 2-3s for a 5-photo
    screen on a Pi 3B, during which a fresh mark's pause request sat
    unprocessed).
    """
    with controller.lock:
        already_cached = set(controller.thumbnail_cache)
    to_generate = [s for s in slides if s.path not in already_cached]
    if max_to_generate is not None:
        to_generate = to_generate[:max_to_generate]

    new_thumbs: dict[str, bytes] = {}
    for s in to_generate:
        try:
            new_thumbs[s.path] = build_thumbnail_bytes(s.surface)
        except Exception:
            logger.warning(f"Failed to build thumbnail for {s.path!r}", exc_info=True)

    with controller.lock:
        controller.thumbnail_cache.update(new_thumbs)
        visible_paths = {s.path for s in slides}
        for hist_slides, _ in controller.history:
            visible_paths.update(s.path for s in hist_slides)
        for stale_path in [p for p in controller.thumbnail_cache if p not in visible_paths]:
            del controller.thumbnail_cache[stale_path]


def render_loop(
        dq: deque[Slide],
        lock: threading.Lock,
        not_full: threading.Condition,
        producer_done: threading.Event,
        controller: SlideshowController,
        seconds_to_display: int = 15,
        night_start: tuple[int, int] = DEFAULT_NIGHT_START,
        night_end: tuple[int, int] = DEFAULT_NIGHT_END,
):
    import time
    import os
    import platform

    controller.seconds_to_display = seconds_to_display

    # Must set env var BEFORE pygame.init()
    if platform.system() == "Darwin":  # macOS
        # Put window at top-left of primary display
        os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"

    pygame.init()

    if platform.system() == "Darwin":
        # On macOS: use a borderless window the size of the main desktop,
        # so other monitors remain usable.
        display_sizes = pygame.display.get_desktop_sizes()
        main_w, main_h = display_sizes[0]  # assume first is main screen
        screen = pygame.display.set_mode((main_w, main_h), pygame.NOFRAME)
    else:
        # On Pi (and other platforms) keep true fullscreen
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)

    pygame.mouse.set_visible(False)
    font = pygame.font.SysFont(None, 40)
    status_font = pygame.font.SysFont(None, 46)
    status_box_rect = compute_status_box_rect(screen, status_font)
    # Snapshot of whatever's behind the status text, taken right after each
    # full scene redraw (before the text is drawn). There's no background
    # box anymore, so when only the text changes (e.g. the Kbps figure),
    # this is what erases the old glyphs before the new ones are drawn --
    # otherwise their white outlines would ghost through each other.
    clean_status_corner: Optional[pygame.Surface] = None

    current_slides: list[Slide] = []
    current_pattern_type: Optional[int] = None
    current_background: Optional[pygame.Surface] = None
    current_end_time: float = 0.0

    last_marks: set[str] = set()
    first_run = True
    prev_is_night: Optional[bool] = None  # for schedule transitions
    last_status_render_time: float = 0.0
    STATUS_REFRESH_SECONDS = 1.0

    running = True
    while running:
        now = time.time()

        # --- Time-based schedule (configured via py-frame.conf) => screen off ---
        lt = time.localtime(now)
        is_night = is_within_night_window(lt.tm_hour, lt.tm_min, night_start, night_end)

        # prev_is_night starts as None (unknown) rather than seeded to
        # is_night, so a restart that happens *during* the night window
        # still applies black_screen/paused immediately below instead of
        # silently skipping it until the next boundary crossing -- e.g. a
        # power-outage reboot at 2am shouldn't leave the screen bright.
        night_transition = prev_is_night is None or is_night != prev_is_night
        if night_transition:
            # We just crossed the boundary (day -> night or night -> day),
            # or this is the first tick after startup.
            if is_night:
                # Entering night: auto screen_off + pause
                with controller.lock:
                    controller.black_screen = True
                    controller.paused = True
                    controller.bump_version()
                set_hdmi_power(False)
            else:
                # Leaving night: auto screen_on + resume
                with controller.lock:
                    controller.black_screen = False
                    controller.paused = False
                    controller.bump_version()
                set_hdmi_power(True)
            prev_is_night = is_night

        # --- Handle keyboard / ESC ---
        for event in pygame.event.get():
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
                continue
            elif event.type == pygame.QUIT:
                running = False
                continue

        # --- Take pending web command + snapshot marks + black_screen ---
        with controller.lock:
            cmd = controller.pending_command
            controller.pending_command = None
            paused = controller.paused
            black_screen = controller.black_screen
            pending_exclusions_snapshot = set(controller.pending_exclusions)
            drive_ok = controller.drive_ok
            download_bytes_per_sec = controller.download_bytes_per_sec

        # Detect mark changes (compare snapshots). /api/mark now applies
        # immediately server-side, so this just catches "something the user
        # marked/unmarked needs to be reflected on the physical screen."
        marks_changed = (pending_exclusions_snapshot != last_marks)
        last_marks = pending_exclusions_snapshot

        force_next = False
        force_prev = False
        steps = 1

        if cmd:
            ctype = cmd.get("type")
            if ctype == "next":
                force_next = True
                steps = max(1, int(cmd.get("steps", 1)))
            elif ctype == "prev":
                force_prev = True
                steps = max(1, int(cmd.get("steps", 1)))
            elif ctype == "pause":
                with controller.lock:
                    controller.paused = True
                    controller.bump_version()
                paused = True
            elif ctype == "play":
                with controller.lock:
                    controller.paused = False
                    controller.bump_version()
                paused = False
            elif ctype == "screen_off":
                with controller.lock:
                    controller.black_screen = True
                    controller.paused = True
                    controller.bump_version()
                black_screen = True
                paused = True
                set_hdmi_power(False)
            elif ctype == "screen_on":
                with controller.lock:
                    controller.black_screen = False
                    controller.paused = False
                    controller.bump_version()
                black_screen = False
                paused = False
                set_hdmi_power(True)

        # Fill in thumbnails a little at a time, one per iteration, rather
        # than as one blocking batch (see update_thumbnail_cache's
        # docstring). Prioritize the currently-displayed screen, but also
        # include whatever's already buffered in the fetcher's deque --
        # pre-warming those during the many idle iterations of a normal
        # ~15s display window means a photo's thumbnail is usually already
        # cached by the time it's actually shown, instead of every single
        # screen change starting from zero cached thumbnails (visible in
        # the browser as a burst of 404s that then resolve on retry).
        if current_slides or dq:
            with not_full:
                upcoming = list(dq)
            update_thumbnail_cache(controller, current_slides + upcoming, max_to_generate=1)

        # --- Decide if slideshow should advance ---
        need_advance = False
        backward = False

        if force_prev:
            need_advance = True
            backward = True
        elif force_next:
            need_advance = True
            backward = False
        elif (not current_slides) or (now >= current_end_time and not paused and not black_screen):
            # auto-advance only if not paused and not in black-screen mode
            # (marking a photo pauses via /api/mark, so this naturally
            # holds still while the user is reviewing/marking).
            need_advance = True
            backward = False

        need_to_render = False

        if first_run:
            need_to_render = True
            first_run = False

        if marks_changed and not black_screen:
            need_to_render = True

        # If we just turned screen_on/off, we should redraw
        if cmd and cmd.get("type") in ("screen_off", "screen_on"):
            need_to_render = True

        # Automatic day/night boundary crossing also needs an immediate
        # redraw -- otherwise the last slide stays on screen (only the
        # status corner keeps refreshing) until something else changes it.
        if night_transition:
            need_to_render = True

        # Refresh just the diagnostics corner periodically even if nothing
        # else changed, so drive/speed issues show up promptly instead of
        # waiting for the next slide change. This is a much cheaper partial
        # update than a full scene redraw (see need_to_render handling
        # below), since only three lines of text change.
        need_status_refresh = (now - last_status_render_time) >= STATUS_REFRESH_SECONDS

        # --- Slide switching logic ---
        if need_advance:
            print("needs redraw")
            finalize_exclusions(controller)

            if backward:
                # history backwards
                with controller.lock:
                    if controller.history:
                        controller.history_index = max(
                            0,
                            controller.history_index - steps
                            if controller.history_index >= 0
                            else len(controller.history) - 1 - steps
                        )
                        idx = controller.history_index
                        slides, ptype = controller.history[idx]
                    else:
                        slides, ptype = [], None

                current_slides = slides
                current_pattern_type = ptype

                if current_slides and current_pattern_type is not None:
                    # (optional) downscale to screen size if you've added that helper
                    screen_w, screen_h = screen.get_size()
                    downscale_slides_to_screen(current_slides, screen_w, screen_h)

                    if current_pattern_type == 0:
                        rects = [(current_slides[0].surface, screen.get_rect())]
                    else:
                        rects = compute_pattern_rects(
                            screen, current_slides, current_pattern_type
                        )

                    current_background = build_blurred_background(
                        screen.get_size(), rects
                    )
                    # A fresh timestamp, not the `now` captured at the top
                    # of this iteration -- building the background/rects
                    # above can itself take a while on slower hardware, and
                    # using the stale `now` would start the countdown
                    # already partway elapsed once the screen actually
                    # becomes visible a moment later.
                    current_end_time = time.time() + seconds_to_display

                    with controller.lock:
                        controller.current_slides = current_slides
                        controller.current_pattern_type = current_pattern_type
                        controller.current_end_time = current_end_time
                        controller.bump_version()

                need_to_render = True

            else:  # --- Forward direction ---
                with controller.lock:
                    hist_len = len(controller.history)
                    idx = controller.history_index

                # Freshly initialized every iteration (not left over from a
                # prior one) so the "skip render, stay stalled" trailer
                # below only ever applies to the actual stall case, not the
                # history-replay branch.
                need_more_images = False

                if hist_len > 0 and 0 <= idx < hist_len - 1:
                    new_index = min(hist_len - 1, idx + steps)
                    with controller.lock:
                        controller.history_index = new_index
                        slides, ptype = controller.history[new_index]

                    current_slides = slides
                    current_pattern_type = ptype

                else:
                    # need a new pattern from deque
                    with not_full:
                        if len(dq) == 0 and producer_done.is_set():
                            running = False
                            continue
                        elif len(dq) >= 5:
                            first = dq[0]
                            if first.orientation == "L":
                                slide = dq.popleft()
                                not_full.notify_all()
                                current_slides = [slide]
                                current_pattern_type = 0
                            else:
                                slides, ptype = extract_pattern_from_deque(dq)
                                not_full.notify_all()
                                current_slides = slides
                                current_pattern_type = ptype
                        else:
                            # not enough images buffered yet
                            need_more_images = True

                    if need_more_images:
                        # Back off briefly instead of busy-spinning the CPU
                        # while waiting for the fetcher thread to refill the
                        # buffer (lock already released above). Deliberately
                        # NOT `continue`-ing here (as this used to): that
                        # jumped straight back to the top of the outer loop,
                        # skipping the status-corner refresh further below
                        # for as long as the stall lasted -- so a stalled/
                        # disconnected drive never showed "Drive:
                        # DISCONNECTED" on screen during an outage, it just
                        # silently froze on the last photo. need_more_images
                        # is checked below instead, so the status refresh
                        # still runs every iteration even while stalled.
                        time.sleep(0.2)
                    else:
                        if current_slides and current_pattern_type is not None:
                            with controller.lock:
                                # Enforce max history size
                                if len(controller.history) >= MAX_HISTORY_SCREENS:
                                    # Drop the oldest entry
                                    controller.history.pop(0)
                                    # Adjust index because we removed index 0
                                    controller.history_index = max(0, controller.history_index - 1)
                                controller.history.append((current_slides, current_pattern_type))
                                controller.history_index = len(controller.history) - 1

                if not need_more_images:
                    need_to_render = True

                    # --- Build blurred background for new screen ---
                    if current_slides and current_pattern_type is not None:
                        screen_w, screen_h = screen.get_size()
                        downscale_slides_to_screen(current_slides, screen_w, screen_h)

                        if current_pattern_type == 0:
                            rects = [(current_slides[0].surface, screen.get_rect())]
                        else:
                            rects = compute_pattern_rects(
                                screen, current_slides, current_pattern_type
                            )

                        current_background = build_blurred_background(
                            screen.get_size(), rects
                        )
                        # Fresh timestamp -- see comment on the equivalent
                        # line in the backward branch above.
                        current_end_time = time.time() + seconds_to_display

                        with controller.lock:
                            controller.current_slides = current_slides
                            controller.current_pattern_type = current_pattern_type
                            controller.current_end_time = current_end_time
                            controller.bump_version()

        # --- Render only when needed ---
        if need_to_render:
            print("Rendering")

            if black_screen:
                # Show pure black, regardless of slides
                screen.fill((0, 0, 0))
            else:
                if current_slides and current_pattern_type is not None:
                    with controller.lock:
                        marks_copy = set(controller.pending_exclusions)

                    if current_pattern_type == 0:
                        render_single_landscape(
                            screen,
                            current_slides[0],
                            current_background,
                            font,
                            marks_copy,
                        )
                    else:
                        render_pattern(
                            screen,
                            current_slides,
                            current_pattern_type,
                            current_background,
                            font,
                            marks_copy,
                        )
                else:
                    # no slides, just clear
                    screen.fill((0, 0, 0))

            # Snapshot whatever's behind the status text (before drawing
            # it), so a later text-only refresh can erase the old glyphs
            # cleanly -- there's no background box to do that anymore.
            safe_status_rect = status_box_rect.clip(screen.get_rect())
            if safe_status_rect.width > 0 and safe_status_rect.height > 0:
                clean_status_corner = screen.subsurface(safe_status_rect).copy()
            else:
                clean_status_corner = None

            # Diagnostics overlay: drawn last, on top of everything
            # (including black-screen mode) so stalls are visible.
            draw_status_overlay(screen, status_font, paused, drive_ok, download_bytes_per_sec, box_rect=status_box_rect)

            pygame.display.flip()
            last_status_render_time = now

        elif need_status_refresh:
            # Nothing else changed: erase the old status text using the
            # clean snapshot, redraw it, and push only that region --
            # instead of re-rendering and flipping the whole scene purely
            # to refresh three lines of text.
            if clean_status_corner is not None:
                screen.blit(clean_status_corner, safe_status_rect.topleft)
            draw_status_overlay(screen, status_font, paused, drive_ok, download_bytes_per_sec, box_rect=status_box_rect)
            pygame.display.update(status_box_rect)
            last_status_render_time = now

        if need_advance:
            log_mem("after_advance")

        time.sleep(0.2)   # keeps CPU load low without hammering the Pi

    pygame.quit()


# ============================================================
# Main entry
# ============================================================


def read_file_list(list_path: str, shuffle: bool = True) -> List[str]:
    """
    Read the photo list file and decide the display order:
      shuffle=True  - fully randomize the order (kept entirely in memory;
                       nothing downstream re-reads the source file, so
                       there's no need to persist the shuffled order).
      shuffle=False - keep the file's original relative order, but start
                       from a random point and wrap around.
    """
    VOLUMES_PREFIX = "/Volumes/"

    paths: List[str] = []
    with open(list_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(VOLUMES_PREFIX):
                line = "nasus/" + line[len(VOLUMES_PREFIX):]
            # Only keep JPG/JPEG
            if line.lower().endswith((".jpg", ".jpeg")):
                paths.append(line)

    if not paths:
        return paths

    if shuffle:
        random.shuffle(paths)
    else:
        offset = random.randrange(len(paths))
        paths = paths[offset:] + paths[:offset]

    return paths


def main():
    import sys

    setup_logging()

    print("Main thread native_id:", threading.get_native_id())

    if len(sys.argv) < 2:
        print("Usage: python slideshow.py file_list.txt")
        sys.exit(1)

    controller = SlideshowController()
    load_exclusions(controller)
    night_start, night_end = load_schedule_config()
    shuffle_enabled = load_display_config()

    list_path = sys.argv[1]
    file_paths = read_file_list(list_path, shuffle=shuffle_enabled)

    if not file_paths:
        print(f"No .jpg/.jpeg entries found in {list_path}; nothing to display.")
        sys.exit(1)

    shared_deque: deque[Slide] = deque()
    lock = threading.Lock()
    not_full = threading.Condition(lock)
    producer_done = threading.Event()

    # start web server in background
    web_thread = Thread(target=run_web, args=(controller,), daemon=True)
    web_thread.start()

    # Start fetcher thread
    fetcher = threading.Thread(
        target=image_fetcher_thread,
        args=(file_paths, shared_deque, lock, not_full, producer_done, controller, 5),
        daemon=True,
    )
    fetcher.start()

    # Start render loop in main thread
    render_loop(shared_deque, lock, not_full, producer_done, controller, seconds_to_display,
                night_start=night_start, night_end=night_end)


if __name__ == "__main__":
    main()
