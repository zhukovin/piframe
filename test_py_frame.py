"""
Comprehensive test suite for py_frame.py
This file extracts and runs existing tests from py_frame.py
"""
import pytest
from collections import deque
from itertools import product
import io
import pygame
import os
import sys
import logging
import tempfile
import threading
from unittest.mock import Mock, MagicMock, patch
from PIL import Image

import py_frame

# Import the test function and dependencies from py_frame
from py_frame import (
    Slide,
    SlideshowController,
    classify_pattern_type,
    extract_pattern_from_deque,
    make_old_paper_surface,
    load_slide,
    ImageDecodeError,
    smoothscale_safe,
    blit_scaled,
    build_mirror_fill,
    draw_slot_overlay,
    draw_exclusion_overlay,
    draw_status_overlay,
    compute_status_box_rect,
    format_speed,
    compute_pattern_rects,
    _pattern_slide_order,
    render_pattern,
    render_single_landscape,
    load_exclusions,
    load_display_config,
    finalize_exclusions,
    downscale_slide_to_screen,
    downscale_slides_to_screen,
    build_thumbnail_bytes,
    update_thumbnail_cache,
    read_file_list,
    image_fetcher_thread,
    log_load_measurement,
    setup_logging,
    main,
    Orientation,
    set_hdmi_power,
)


class DummySlide(Slide):
    """Slide subclass for testing, ignoring actual pygame surfaces."""

    def __init__(self, orientation: Orientation):
        self.path = ""
        self.surface = None  # type: ignore
        self.orientation = orientation


def reset_exclusion_icon_cache():
    """
    py_frame's exclusion-icon loader caches module-level state meant to
    persist for the life of the real process (loaded once, never torn
    down). Tests that pygame.init()/quit() repeatedly across classes need
    to reset it in setup_method (not just teardown_method) or a later
    class can inherit a surface tied to an already-torn-down display,
    which reads back as garbage/blank pixels -- discovered as a flaky
    failure in TestRenderPatternMarking that only reproduced when run
    after another class exercising the same icon.
    """
    import py_frame
    py_frame._exclusion_icon_original = None
    py_frame._exclusion_icon_load_attempted = False
    py_frame._exclusion_icon_scaled_cache = {}


def test_extract_pattern_all_len5():
    """
    For all 5-length orientation sequences starting with P,
    check that pattern extraction:
      - picks PPP if possible
      - else PPLLL if possible
      - else PLLL if possible
       - extracts correct counts and returns remaining correctly.
    """
    for bits in product("PL", repeat=5):
        seq = "".join(bits)
        if seq[0] != "P":
            continue

        # Determine expected pattern type and needed counts
        cP = seq.count("P")
        cL = seq.count("L")

        if cP >= 3:
            exp_type = 1
            needP, needL = 3, 0
        elif cP >= 2 and cL >= 3:
            exp_type = 2
            needP, needL = 2, 3
        elif cP >= 1 and cL >= 3:
            exp_type = 3
            needP, needL = 1, 3
        else:
            raise AssertionError(f"Unexpected no-pattern case for {seq}")

        dq = deque(DummySlide(o) for o in seq)
        extracted, out_type = extract_pattern_from_deque(dq)

        assert out_type == exp_type, f"{seq}: expected type {exp_type}, got {out_type}"
        assert sum(1 for s in extracted if s.orientation == "P") == needP
        assert sum(1 for s in extracted if s.orientation == "L") == needL

        # simulate expected remaining
        window = list(seq[:5])
        p_left, l_left = needP, needL
        unused = []
        for ch in window:
            if ch == "P" and p_left > 0:
                p_left -= 1
            elif ch == "L" and l_left > 0:
                l_left -= 1
            else:
                unused.append(ch)
            if p_left == 0 and l_left == 0:
                break
        expected_remaining = unused + list(seq[5:])
        actual_remaining = [s.orientation for s in dq]

        assert expected_remaining == actual_remaining, \
            f"{seq}: expected remaining {expected_remaining}, got {actual_remaining}"


class TestSlideshowController:
    """Test suite for SlideshowController class"""
    
    def test_initialization(self):
        """Test controller initializes with correct default values"""
        controller = SlideshowController()
        
        assert controller.current_slides == []
        assert controller.current_pattern_type is None
        assert controller.history == []
        assert controller.history_index == -1
        assert controller.pending_command is None
        assert controller.excluded_paths == set()
        assert controller.pending_exclusions == set()
        assert controller.exclusions_file == "exclusions.txt"
        assert controller.paused is False
        assert controller.black_screen is False
        assert controller.drive_ok is True
        assert controller.download_bytes_per_sec is None
        assert controller.measurements_file == "load_measurements.csv"
        assert controller.state_version == 0
        assert controller.thumbnail_cache == {}

    def test_pending_exclusions_management(self):
        """Test marking and unmarking paths as pending exclusion"""
        controller = SlideshowController()

        # Add marks
        controller.pending_exclusions.add("a.jpg")
        controller.pending_exclusions.add("c.jpg")
        assert "a.jpg" in controller.pending_exclusions
        assert "c.jpg" in controller.pending_exclusions
        assert "b.jpg" not in controller.pending_exclusions

        # Remove marks
        controller.pending_exclusions.remove("a.jpg")
        assert "a.jpg" not in controller.pending_exclusions
        assert "c.jpg" in controller.pending_exclusions

    def test_bump_version_increments_and_notifies(self):
        """Test bump_version increments state_version and wakes waiters
        (used by web_server's SSE stream to know when to push)"""
        controller = SlideshowController()
        woke = threading.Event()

        def waiter():
            with controller.lock:
                if controller.lock.wait_for(lambda: controller.state_version != 0, timeout=2):
                    woke.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        import time
        time.sleep(0.1)  # let the waiter block on wait_for first

        with controller.lock:
            controller.bump_version()

        t.join(timeout=2)
        assert controller.state_version == 1
        assert woke.is_set()
    
    def test_pause_state(self):
        """Test pause and play states"""
        controller = SlideshowController()
        
        assert controller.paused is False
        controller.paused = True
        assert controller.paused is True
        controller.paused = False
        assert controller.paused is False
    
    def test_black_screen_mode(self):
        """Test black screen mode"""
        controller = SlideshowController()
        
        assert controller.black_screen is False
        controller.black_screen = True
        assert controller.black_screen is True


class TestMakeOldPaperSurface:
    """Test suite for make_old_paper_surface function"""
    
    def setup_method(self):
        """Initialize pygame for each test"""
        pygame.init()
    
    def teardown_method(self):
        """Clean up pygame"""
        pygame.quit()
    
    def test_creates_surface_with_correct_size(self):
        """Test that surface is created with requested dimensions"""
        width, height = 100, 200
        surface = make_old_paper_surface((width, height))
        
        assert surface.get_width() == width
        assert surface.get_height() == height
    
    def test_surface_has_old_paper_base_color(self):
        """Test that surface has beige/old paper color as base"""
        surface = make_old_paper_surface((10, 10))
        
        # Get color at center (should be close to base color with some noise)
        center_color = surface.get_at((5, 5))
        base_color = (235, 222, 193)
        
        # Colors should be close to base (within noise range)
        for i in range(3):
            assert abs(center_color[i] - base_color[i]) <= 20


class TestLoadSlide:
    """Test suite for load_slide function"""
    
    def setup_method(self):
        """Initialize pygame and create test images"""
        pygame.init()
        # Set a video mode for load_slide to work
        os.environ['SDL_VIDEODRIVER'] = 'dummy'
        pygame.display.set_mode((1, 1))
        self.temp_dir = tempfile.mkdtemp()
    
    def teardown_method(self):
        """Clean up test files and pygame"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        pygame.quit()
    
    def test_load_portrait_image(self):
        """Test loading a portrait orientation image"""
        # Create a portrait test image (height > width)
        img_path = os.path.join(self.temp_dir, "portrait.jpg")
        img = Image.new("RGB", (100, 200), color="red")
        img.save(img_path)
        
        slide = load_slide(img_path)
        
        assert slide.path == img_path
        assert slide.orientation == "P"
        assert slide.surface is not None
        assert slide.surface.get_width() == 100
        assert slide.surface.get_height() == 200
    
    def test_load_landscape_image(self):
        """Test loading a landscape orientation image"""
        # Create a landscape test image (width > height)
        img_path = os.path.join(self.temp_dir, "landscape.jpg")
        img = Image.new("RGB", (200, 100), color="blue")
        img.save(img_path)
        
        slide = load_slide(img_path)
        
        assert slide.path == img_path
        assert slide.orientation == "L"
        assert slide.surface is not None
        assert slide.surface.get_width() == 200
        assert slide.surface.get_height() == 100
    
    def test_load_square_image(self):
        """Test loading a square image (should be classified as landscape)"""
        img_path = os.path.join(self.temp_dir, "square.jpg")
        img = Image.new("RGB", (100, 100), color="green")
        img.save(img_path)

        slide = load_slide(img_path)

        assert slide.orientation == "L"  # Equal dimensions = landscape

    def test_load_slide_reports_read_size_and_timing(self):
        """Test that load_slide reports the raw bytes read and time taken,
        used for the download-speed diagnostics overlay"""
        img_path = os.path.join(self.temp_dir, "timed.jpg")
        img = Image.new("RGB", (100, 100), color="yellow")
        img.save(img_path)

        slide = load_slide(img_path)

        assert slide.load_bytes == os.path.getsize(img_path)
        assert slide.load_seconds >= 0

    def test_corrupt_file_raises_image_decode_error(self):
        """A file that reads fine but isn't a valid image should raise
        ImageDecodeError, not a bare exception, so callers can tell a bad
        file apart from a real drive/NFS problem"""
        bad_path = os.path.join(self.temp_dir, "corrupt.jpg")
        content = b"this is not a real jpeg file"
        with open(bad_path, "wb") as f:
            f.write(content)

        with pytest.raises(ImageDecodeError) as exc_info:
            load_slide(bad_path)

        # The raw read succeeded before decoding failed, so the exception
        # should still carry that valid measurement for logging purposes.
        assert exc_info.value.load_bytes == len(content)
        assert exc_info.value.load_seconds >= 0

    def test_missing_file_raises_plain_oserror_not_image_decode_error(self):
        """A missing file fails at the read stage (before decoding is even
        attempted), so it must NOT be wrapped as ImageDecodeError -- callers
        rely on that distinction to tell "bad file" apart from "can't reach
        the drive at all\""""
        missing_path = os.path.join(self.temp_dir, "does_not_exist.jpg")

        with pytest.raises(OSError) as exc_info:
            load_slide(missing_path)
        assert not isinstance(exc_info.value, ImageDecodeError)


class TestLogLoadMeasurement:
    """Test suite for log_load_measurement, which appends one CSV row per
    photo load attempt for later offline analysis of size/time/speed
    correlation"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "measurements.csv")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _read_rows(self):
        import csv
        with open(self.log_file, newline="") as f:
            return list(csv.DictReader(f))

    def test_writes_header_once_then_appends(self):
        log_load_measurement(self.log_file, "a.jpg", "ok", load_bytes=1000, load_seconds=0.5)
        log_load_measurement(self.log_file, "b.jpg", "ok", load_bytes=2000, load_seconds=1.0)

        with open(self.log_file) as f:
            lines = f.readlines()

        assert lines[0].strip() == "timestamp,path,outcome,bytes,seconds,bytes_per_sec,error_type"
        assert len(lines) == 3  # header + 2 rows

    def test_ok_outcome_records_full_measurement(self):
        log_load_measurement(self.log_file, "a.jpg", "ok", load_bytes=1000, load_seconds=0.5)

        row = self._read_rows()[0]
        assert row["path"] == "a.jpg"
        assert row["outcome"] == "ok"
        assert int(row["bytes"]) == 1000
        assert float(row["seconds"]) == 0.5
        assert float(row["bytes_per_sec"]) == 2000.0
        assert row["error_type"] == ""

    def test_decode_error_still_records_the_valid_read_measurement(self):
        log_load_measurement(
            self.log_file, "bad.jpg", "decode_error", load_bytes=500, load_seconds=0.25,
            error_type="UnidentifiedImageError",
        )

        row = self._read_rows()[0]
        assert row["outcome"] == "decode_error"
        assert int(row["bytes"]) == 500
        assert float(row["seconds"]) == 0.25
        assert float(row["bytes_per_sec"]) == 2000.0
        assert row["error_type"] == "UnidentifiedImageError"

    def test_io_error_leaves_measurement_fields_blank(self):
        log_load_measurement(self.log_file, "missing.jpg", "io_error", error_type="ConnectionResetError")

        row = self._read_rows()[0]
        assert row["outcome"] == "io_error"
        assert row["bytes"] == ""
        assert row["seconds"] == ""
        assert row["bytes_per_sec"] == ""
        assert row["error_type"] == "ConnectionResetError"

    def test_file_not_found_outcome_is_distinct_from_io_error(self):
        log_load_measurement(self.log_file, "gone.jpg", "file_not_found", error_type="FileNotFoundError")

        row = self._read_rows()[0]
        assert row["outcome"] == "file_not_found"
        assert row["error_type"] == "FileNotFoundError"
        assert row["bytes"] == ""
        assert row["seconds"] == ""


class TestSmoothscaleSafe:
    """Test suite for smoothscale_safe function"""
    
    def setup_method(self):
        """Initialize pygame"""
        pygame.init()
    
    def teardown_method(self):
        """Clean up pygame"""
        pygame.quit()
    
    def test_scale_24bit_surface(self):
        """Test scaling a 24-bit surface"""
        original = pygame.Surface((100, 100), depth=24)
        scaled = smoothscale_safe(original, (50, 50))
        
        assert scaled.get_width() == 50
        assert scaled.get_height() == 50
    
    def test_scale_32bit_surface(self):
        """Test scaling a 32-bit surface"""
        original = pygame.Surface((100, 100), flags=pygame.SRCALPHA, depth=32)
        scaled = smoothscale_safe(original, (200, 200))
        
        assert scaled.get_width() == 200
        assert scaled.get_height() == 200
    
    def test_scale_8bit_surface(self):
        """Test scaling an 8-bit surface (should be converted first)"""
        original = pygame.Surface((100, 100), depth=8)
        scaled = smoothscale_safe(original, (50, 50))
        
        assert scaled.get_width() == 50
        assert scaled.get_height() == 50


class TestBlitScaled:
    """Test suite for blit_scaled function"""
    
    def setup_method(self):
        """Initialize pygame"""
        pygame.init()
    
    def teardown_method(self):
        """Clean up pygame"""
        pygame.quit()
    
    def test_blit_scaled_fits_within_target(self):
        """Test that image is scaled to fit within target rect"""
        surface = pygame.Surface((800, 600))
        img = pygame.Surface((400, 300))
        target_rect = pygame.Rect(0, 0, 200, 200)
        
        # Should not raise any errors
        blit_scaled(surface, img, target_rect)
    
    def test_blit_scaled_preserves_aspect_ratio(self):
        """Test that aspect ratio is preserved when scaling"""
        surface = pygame.Surface((800, 600))
        # Wide image
        img = pygame.Surface((400, 100))
        target_rect = pygame.Rect(0, 0, 200, 200)
        
        # Should scale to fit width (200) and maintain aspect ratio
        blit_scaled(surface, img, target_rect)


class TestBuildMirrorFill:
    """Test suite for build_mirror_fill, which fills a slide's letterbox/
    pillarbox gap with a mirrored reflection of the image's own edge
    (instead of an unrelated stretched copy), tiled outward for gaps wider
    than the fitted image itself."""

    def setup_method(self):
        pygame.init()
        # .convert() (used internally) requires an active display mode.
        pygame.display.set_mode((1, 1))

    def teardown_method(self):
        pygame.quit()

    def test_no_gap_when_aspect_matches(self):
        """Same aspect ratio as the target -> no gap, just the scaled image"""
        img = pygame.Surface((40, 20)).convert()
        img.fill((10, 20, 30))

        result = build_mirror_fill(img, (80, 40))

        assert result.get_size() == (80, 40)
        assert result.get_at((0, 0))[:3] == (10, 20, 30)
        assert result.get_at((79, 39))[:3] == (10, 20, 30)

    def test_pillarbox_seam_is_a_continuous_mirror(self):
        """The column just outside the image's left edge should exactly
        match the image's own leftmost column -- a seamless reflection,
        not an arbitrary stretch"""
        img = pygame.Surface((10, 10)).convert()
        img.fill((200, 0, 0))
        for row in range(10):
            img.set_at((0, row), (0, 0, 200))  # distinct, trackable edge color

        rect_size = (100, 10)  # much wider than the fitted image
        result = build_mirror_fill(img, rect_size)

        scale = min(rect_size[0] / 10, rect_size[1] / 10)
        new_w = max(1, int(10 * scale))
        x = (rect_size[0] - new_w) // 2

        assert result.get_at((x, 5))[:3] == (0, 0, 200)
        assert result.get_at((x - 1, 5))[:3] == (0, 0, 200)

    def test_letterbox_seam_is_a_continuous_mirror(self):
        """Same continuity check, but for a vertical (letterboxed) gap"""
        img = pygame.Surface((10, 10)).convert()
        img.fill((200, 0, 0))
        for col in range(10):
            img.set_at((col, 0), (0, 200, 0))  # distinct top-edge color

        rect_size = (10, 100)  # much taller than the fitted image
        result = build_mirror_fill(img, rect_size)

        scale = min(rect_size[0] / 10, rect_size[1] / 10)
        new_h = max(1, int(10 * scale))
        y = (rect_size[1] - new_h) // 2

        assert result.get_at((5, y))[:3] == (0, 200, 0)
        assert result.get_at((5, y - 1))[:3] == (0, 200, 0)

    def test_gap_fully_covered_no_background_color_left_showing(self):
        """Even when the gap is much wider than one tile, tiling should
        cover it completely rather than leaving a sliver of the base fill
        color at the outer edge"""
        img = pygame.Surface((4, 10)).convert()
        img.fill((123, 45, 67))

        rect_size = (97, 10)  # deliberately not a clean multiple of the tile width
        result = build_mirror_fill(img, rect_size)

        for px in (0, 1, rect_size[0] - 1, rect_size[0] - 2):
            assert result.get_at((px, 5))[:3] == (123, 45, 67)

    def test_zero_area_rect_returns_minimal_surface(self):
        img = pygame.Surface((10, 10)).convert()
        img.fill((1, 2, 3))

        result = build_mirror_fill(img, (0, 0))

        assert result.get_size() == (1, 1)


class TestFormatSpeed:
    """Test suite for format_speed function"""

    def test_none_shows_placeholder(self):
        assert format_speed(None) == "-- Bps"

    def test_stays_in_bps_below_1000(self):
        assert format_speed(999) == "999.00 Bps"

    def test_switches_to_kbps_at_1000(self):
        assert format_speed(1000) == "1.00 KBps"

    def test_switches_to_mbps_just_over_1000_kbps(self):
        # 1001 KBps -> 1.00 MBps
        assert format_speed(1001 * 1000) == "1.00 MBps"

    def test_rounds_half_up_to_two_decimals(self):
        # 12.345 KBps -> 12.35 KBps (round-half-up, not banker's rounding
        # and not naive float rounding, which could give 12.34 instead)
        assert format_speed(12345) == "12.35 KBps"

    def test_rounding_can_push_into_the_next_unit(self):
        # 999.996 KBps rounds to 1000.00 KBps, which should instead
        # display as 1.00 MBps rather than showing "1000.00 KBps"
        assert format_speed(999996) == "1.00 MBps"

    def test_mbps_scale(self):
        assert format_speed(2_500_000) == "2.50 MBps"

    def test_gbps_scale(self):
        assert format_speed(1_500_000_000) == "1.50 GBps"


class TestDrawStatusOverlay:
    """Test suite for draw_status_overlay function"""

    def setup_method(self):
        """Initialize pygame"""
        pygame.init()
        self.font = pygame.font.SysFont(None, 24)

    def teardown_method(self):
        """Clean up pygame"""
        pygame.quit()

    def test_draws_black_text_with_white_outline_and_no_background_fill(self):
        """Test that the status text renders as black-with-white-outline,
        and that the box is no longer a solid filled background -- the
        original background must still show through around/between glyphs"""
        screen = pygame.Surface((400, 300))
        screen.fill((10, 10, 10))  # distinct from black/white so it's unambiguous

        box_rect = draw_status_overlay(screen, self.font, paused=False, drive_ok=True, download_bytes_per_sec=150.0)

        colors_in_box = set()
        for x in range(box_rect.x, box_rect.right, 2):
            for y in range(box_rect.y, box_rect.bottom, 2):
                colors_in_box.add(screen.get_at((x, y))[:3])

        assert (255, 255, 255) in colors_in_box, "expected a white outline pixel somewhere in the box"
        assert (0, 0, 0) in colors_in_box, "expected a black text pixel somewhere in the box"
        assert (10, 10, 10) in colors_in_box, "expected the original background to still show through (no fill)"

        # Well outside the box entirely: background is completely untouched
        far_pixel = screen.get_at((5, 5))[:3]
        assert far_pixel == (10, 10, 10)

    def test_handles_missing_download_speed(self):
        """Test that a None download_bytes_per_sec (e.g. before the first
        successful load) doesn't crash and still draws the text"""
        screen = pygame.Surface((400, 300))
        screen.fill((0, 0, 0))

        box_rect = draw_status_overlay(screen, self.font, paused=True, drive_ok=False, download_bytes_per_sec=None)

        colors_in_box = {screen.get_at((x, box_rect.centery))[:3] for x in range(box_rect.x, box_rect.right)}
        assert (255, 255, 255) in colors_in_box

    def test_box_stays_within_screen_bounds_on_small_screen(self):
        """Test that a very small screen doesn't cause the overlay to error out"""
        screen = pygame.Surface((50, 50))
        screen.fill((0, 0, 0))

        # Should not raise, even though the box may not fully fit
        draw_status_overlay(screen, self.font, paused=False, drive_ok=True, download_bytes_per_sec=42.0)

    def test_returns_its_box_rect(self):
        """Test that the box's rect is returned, so callers can lay out
        other diagnostics (e.g. the load-history histogram) relative to it"""
        screen = pygame.Surface((400, 300))

        box_rect = draw_status_overlay(screen, self.font, paused=False, drive_ok=True, download_bytes_per_sec=42.0)

        assert isinstance(box_rect, pygame.Rect)
        assert box_rect.right == 400 - 10  # 10px margin from the right edge
        assert box_rect.bottom == 300 - 10  # 10px margin from the bottom edge

    def test_box_width_is_stable_across_drive_status_transitions(self):
        """Test that the box's position/size doesn't change when the drive
        status text changes (e.g. "Drive: OK" -> "Drive: DISCONNECTED"), so
        a text-only refresh always erases/redraws the exact same region"""
        screen = pygame.Surface((800, 300))

        ok_rect = draw_status_overlay(screen, self.font, paused=False, drive_ok=True, download_bytes_per_sec=1000.0)
        disconnected_rect = draw_status_overlay(screen, self.font, paused=False, drive_ok=False, download_bytes_per_sec=None)

        assert ok_rect.width == disconnected_rect.width
        assert ok_rect.x == disconnected_rect.x

    def test_uses_the_given_box_rect_instead_of_recomputing(self):
        """Test that passing an explicit box_rect is honored verbatim (this
        is what lets render_loop snapshot/restore the exact same region
        every time, regardless of what the text currently says)"""
        screen = pygame.Surface((400, 300))
        explicit_rect = pygame.Rect(20, 30, 200, 90)

        returned_rect = draw_status_overlay(
            screen, self.font, paused=False, drive_ok=True, download_bytes_per_sec=1.0,
            box_rect=explicit_rect,
        )

        assert returned_rect == explicit_rect

    def test_computed_box_rect_matches_default_drawing_rect(self):
        """Test that compute_status_box_rect (called upfront by render_loop)
        produces the same rect draw_status_overlay would compute on its own"""
        screen = pygame.Surface((400, 300))

        expected = compute_status_box_rect(screen, self.font)
        actual = draw_status_overlay(screen, self.font, paused=False, drive_ok=True, download_bytes_per_sec=1.0)

        assert expected == actual


class TestDrawSlotOverlay:
    """Test suite for draw_slot_overlay: no border is drawn (replaced by
    an icon overlay for marked slots, see TestDrawExclusionOverlay);
    only the slot number label is drawn directly by this function."""

    def setup_method(self):
        pygame.init()
        pygame.display.set_mode((1, 1))
        reset_exclusion_icon_cache()
        self.font = pygame.font.SysFont(None, 24)

    def teardown_method(self):
        pygame.quit()

    def test_no_border_drawn_unmarked(self):
        screen = pygame.Surface((200, 150))
        screen.fill((10, 20, 30))
        rect = pygame.Rect(10, 10, 180, 130)

        draw_slot_overlay(screen, rect, 0, marked=False, font=self.font)

        # The rect's edge (where a 3px border used to be drawn) must still
        # be the plain background color, not a border color.
        assert screen.get_at((rect.x, rect.centery))[:3] == (10, 20, 30)
        assert screen.get_at((rect.centerx, rect.y))[:3] == (10, 20, 30)

    def test_no_border_drawn_marked(self):
        screen = pygame.Surface((200, 150))
        screen.fill((10, 20, 30))
        rect = pygame.Rect(10, 10, 180, 130)

        draw_slot_overlay(screen, rect, 0, marked=True, font=self.font)

        assert screen.get_at((rect.x, rect.centery))[:3] == (10, 20, 30)
        assert screen.get_at((rect.centerx, rect.y))[:3] == (10, 20, 30)


class TestDrawExclusionOverlay:
    """Test suite for draw_exclusion_overlay, the icon shown over a marked
    photo (pictures/dont-show-icon.jpeg) in place of the old colored
    border, and for the missing-file fallback."""

    def setup_method(self):
        pygame.init()
        pygame.display.set_mode((1, 1))
        reset_exclusion_icon_cache()

    def teardown_method(self):
        pygame.quit()

    def test_draws_something_over_the_rect_center(self):
        """Test that some non-background pixel shows up near the center
        of the rect once the (real, repo-provided) icon is drawn"""
        screen = pygame.Surface((300, 300))
        screen.fill((10, 20, 30))
        rect = pygame.Rect(0, 0, 300, 300)

        draw_exclusion_overlay(screen, rect)

        colors = {
            screen.get_at((x, y))[:3]
            for x in range(rect.x, rect.right, 4)
            for y in range(rect.y, rect.bottom, 4)
        }
        assert len(colors) > 1, "expected the icon to have drawn something over the background"

    def test_background_still_shows_through_via_colorkey(self):
        """Test that the icon's white background is keyed out rather than
        painted as an opaque square over the photo"""
        screen = pygame.Surface((300, 300))
        screen.fill((10, 20, 30))
        rect = pygame.Rect(0, 0, 300, 300)

        draw_exclusion_overlay(screen, rect)

        # Corners of the icon's bounding box are background in the source
        # artwork (a roughly circular/eye shape) -- background color
        # should still be visible there, not solid white.
        corner = screen.get_at((rect.x + 2, rect.y + 2))[:3]
        assert corner == (10, 20, 30)

    def test_missing_icon_file_does_not_raise(self):
        import py_frame
        original_path = py_frame.EXCLUSION_ICON_PATH
        py_frame.EXCLUSION_ICON_PATH = "pictures/does-not-exist.jpeg"
        try:
            screen = pygame.Surface((300, 300))
            screen.fill((10, 20, 30))
            rect = pygame.Rect(0, 0, 300, 300)

            draw_exclusion_overlay(screen, rect)  # should not raise

            assert screen.get_at((rect.centerx, rect.centery))[:3] == (10, 20, 30)
        finally:
            py_frame.EXCLUSION_ICON_PATH = original_path

    def test_tiny_rect_does_not_raise(self):
        screen = pygame.Surface((10, 10))
        rect = pygame.Rect(0, 0, 1, 1)

        draw_exclusion_overlay(screen, rect)  # should not raise


class TestComputePatternRects:
    """Test suite for compute_pattern_rects function"""
    
    def setup_method(self):
        """Initialize pygame and create test slides"""
        pygame.init()
        self.screen = pygame.Surface((900, 600))
        
        # Create dummy slides with surfaces
        self.portrait_slide = Slide(
            path="p.jpg",
            surface=pygame.Surface((100, 200)),
            orientation="P"
        )
        self.landscape_slide = Slide(
            path="l.jpg",
            surface=pygame.Surface((200, 100)),
            orientation="L"
        )
    
    def teardown_method(self):
        """Clean up pygame"""
        pygame.quit()
    
    def test_pattern_type_1_ppp(self):
        """Test PPP pattern (3 portraits side by side)"""
        slides = [self.portrait_slide, self.portrait_slide, self.portrait_slide]
        rects = compute_pattern_rects(self.screen, slides, pattern_type=1)
        
        assert len(rects) == 3
        # Each should be 1/3 of screen width
        for i, (surf, rect) in enumerate(rects):
            assert rect.width == 300  # 900 / 3
            assert rect.height == 600
            assert rect.x == i * 300
    
    def test_pattern_type_2_pplll(self):
        """Test PPLLL pattern (3 L stacked, 2 P full-height)"""
        slides = [
            self.portrait_slide, self.portrait_slide,
            self.landscape_slide, self.landscape_slide, self.landscape_slide
        ]
        rects = compute_pattern_rects(self.screen, slides, pattern_type=2)
        
        # Should have 5 rects: 3 landscapes + 2 portraits
        assert len(rects) == 5
    
    def test_pattern_type_3_plll(self):
        """Test PLLL pattern (1 P + 3 L)"""
        slides = [
            self.portrait_slide,
            self.landscape_slide, self.landscape_slide, self.landscape_slide
        ]
        rects = compute_pattern_rects(self.screen, slides, pattern_type=3)

        # Should have 4 rects
        assert len(rects) == 4


class TestPatternSlideOrder:
    """Test suite for _pattern_slide_order: must match the order
    compute_pattern_rects actually assigns slides to rects, since
    render_pattern relies on the two staying in sync to attribute marks
    to the correct rect (see TestRenderPatternMarking for the regression
    this guards against)."""

    def setup_method(self):
        pygame.init()
        self.screen = pygame.Surface((900, 600))

    def teardown_method(self):
        pygame.quit()

    def _slide(self, path, orientation):
        size = (100, 200) if orientation == "P" else (200, 100)
        return Slide(path=path, surface=pygame.Surface(size), orientation=orientation)

    def test_matches_compute_pattern_rects_order_type_1(self):
        slides = [self._slide(f"s{i}.jpg", "P") for i in range(3)]
        rects = compute_pattern_rects(self.screen, slides, pattern_type=1)
        ordered = _pattern_slide_order(slides, pattern_type=1)

        assert [s.surface for s in ordered] == [surf for surf, _ in rects]

    def test_matches_compute_pattern_rects_order_type_2(self):
        # Deliberately interleaved/raw order, not grouped by orientation --
        # this is what current_slides actually looks like in production.
        slides = [
            self._slide("p1.jpg", "P"), self._slide("l1.jpg", "L"),
            self._slide("p2.jpg", "P"), self._slide("l2.jpg", "L"),
            self._slide("l3.jpg", "L"),
        ]
        rects = compute_pattern_rects(self.screen, slides, pattern_type=2)
        ordered = _pattern_slide_order(slides, pattern_type=2)

        assert [s.surface for s in ordered] == [surf for surf, _ in rects]
        assert [s.path for s in ordered] == ["l1.jpg", "l2.jpg", "l3.jpg", "p1.jpg", "p2.jpg"]

    def test_matches_compute_pattern_rects_order_type_3(self):
        slides = [
            self._slide("l1.jpg", "L"), self._slide("p1.jpg", "P"),
            self._slide("l2.jpg", "L"), self._slide("l3.jpg", "L"),
        ]
        rects = compute_pattern_rects(self.screen, slides, pattern_type=3)
        ordered = _pattern_slide_order(slides, pattern_type=3)

        assert [s.surface for s in ordered] == [surf for surf, _ in rects]
        assert [s.path for s in ordered] == ["p1.jpg", "l1.jpg", "l2.jpg", "l3.jpg"]


class TestRenderPatternMarking:
    """Regression tests for a bug where clicking thumbnail N in the web UI
    highlighted a DIFFERENT photo on the physical screen for the PPLLL/
    PLLL patterns. Root cause: render_pattern checked `idx in marks`
    where idx was the rect's position (compute_pattern_rects groups by
    orientation) but marks held current_slides indices (raw order) --
    two different orderings for these pattern types. Fixed by keying
    marks by path instead of index."""

    def setup_method(self):
        pygame.init()
        pygame.display.set_mode((1, 1))
        reset_exclusion_icon_cache()
        self.screen = pygame.Surface((900, 600))
        self.font = pygame.font.SysFont(None, 24)

    def teardown_method(self):
        pygame.quit()

    def _slide(self, path, orientation, color):
        size = (100, 200) if orientation == "P" else (200, 100)
        surf = pygame.Surface(size)
        surf.fill(color)
        return Slide(path=path, surface=surf, orientation=orientation)

    @staticmethod
    def _colors_near_center(screen, rect, radius=16, step=4):
        # draw_slot_overlay always draws a slot-number label in the rect's
        # top-left corner regardless of marked state, so sampling the
        # whole rect would show >1 color either way -- sample only a
        # small window around the center, which the label never reaches
        # but the (40%-of-min-dimension) exclusion icon always does.
        return {
            screen.get_at((x, y))[:3]
            for x in range(rect.centerx - radius, rect.centerx + radius, step)
            for y in range(rect.centery - radius, rect.centery + radius, step)
        }

    def test_mark_highlights_the_correct_rect_pplll(self):
        # Raw current_slides order: interleaved, NOT grouped by
        # orientation -- this is what production actually looks like.
        p1 = self._slide("p1.jpg", "P", (10, 10, 10))
        l1 = self._slide("l1.jpg", "L", (20, 20, 20))
        p2 = self._slide("p2.jpg", "P", (30, 30, 30))
        l2 = self._slide("l2.jpg", "L", (40, 40, 40))
        l3 = self._slide("l3.jpg", "L", (50, 50, 50))
        slides = [p1, l1, p2, l2, l3]

        rects = compute_pattern_rects(self.screen, slides, pattern_type=2)
        p2_rect = next(rect for surf, rect in rects if surf is p2.surface)

        render_pattern(self.screen, slides, pattern_type=2, background=None,
                        font=self.font, marks={"p2.jpg"})

        colors_at_p2 = self._colors_near_center(self.screen, p2_rect)
        assert len(colors_at_p2) > 1, "expected the exclusion icon over the marked photo's rect"

        for surf, rect in rects:
            if surf is p2.surface:
                continue
            actual = self.screen.get_at((rect.centerx, rect.centery))[:3]
            expected = surf.get_at((0, 0))[:3]
            assert actual == expected, f"icon incorrectly drawn on unmarked rect {rect}"

    def test_mark_highlights_the_correct_rect_plll(self):
        l1 = self._slide("l1.jpg", "L", (20, 20, 20))
        p1 = self._slide("p1.jpg", "P", (30, 30, 30))
        l2 = self._slide("l2.jpg", "L", (40, 40, 40))
        l3 = self._slide("l3.jpg", "L", (50, 50, 50))
        slides = [l1, p1, l2, l3]

        rects = compute_pattern_rects(self.screen, slides, pattern_type=3)
        l3_rect = next(rect for surf, rect in rects if surf is l3.surface)

        render_pattern(self.screen, slides, pattern_type=3, background=None,
                        font=self.font, marks={"l3.jpg"})

        colors_at_l3 = self._colors_near_center(self.screen, l3_rect)
        assert len(colors_at_l3) > 1, "expected the exclusion icon over the marked photo's rect"

        for surf, rect in rects:
            if surf is l3.surface:
                continue
            actual = self.screen.get_at((rect.centerx, rect.centery))[:3]
            expected = surf.get_at((0, 0))[:3]
            assert actual == expected, f"icon incorrectly drawn on unmarked rect {rect}"


class TestLoadExclusions:
    """Test suite for load_exclusions function"""

    def setup_method(self):
        """Create test controller pointing at a temp exclusions file"""
        self.temp_dir = tempfile.mkdtemp()
        self.controller = SlideshowController()
        self.controller.exclusions_file = os.path.join(self.temp_dir, "exclusions.txt")

    def teardown_method(self):
        """Clean up test files"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_missing_file_is_noop(self):
        """Test that a missing exclusions file leaves excluded_paths empty"""
        load_exclusions(self.controller)

        assert self.controller.excluded_paths == set()

    def test_load_populates_excluded_paths(self):
        """Test that existing exclusions file paths are loaded"""
        with open(self.controller.exclusions_file, "w") as f:
            f.write("test1.jpg\n")
            f.write("test2.jpg\n")

        load_exclusions(self.controller)

        assert self.controller.excluded_paths == {"test1.jpg", "test2.jpg"}

    def test_load_skips_blank_lines(self):
        """Test that blank lines in the exclusions file are ignored"""
        with open(self.controller.exclusions_file, "w") as f:
            f.write("test1.jpg\n")
            f.write("\n")
            f.write("   \n")
            f.write("test2.jpg\n")

        load_exclusions(self.controller)

        assert self.controller.excluded_paths == {"test1.jpg", "test2.jpg"}

    def test_load_then_finalize_appends(self):
        """Test that paths loaded at startup persist and newly committed
        pending exclusions append to the file"""
        with open(self.controller.exclusions_file, "w") as f:
            f.write("old.jpg\n")

        load_exclusions(self.controller)
        assert "old.jpg" in self.controller.excluded_paths

        # "new.jpg" was marked (as /api/mark would do) and has since
        # scrolled out of view entirely, so it's ready to commit.
        self.controller.pending_exclusions = {"new.jpg"}
        self.controller.excluded_paths.add("new.jpg")
        self.controller.current_slides = []
        self.controller.history = []

        finalize_exclusions(self.controller)

        assert self.controller.excluded_paths == {"old.jpg", "new.jpg"}
        assert self.controller.pending_exclusions == set()
        with open(self.controller.exclusions_file) as f:
            lines = [l.strip() for l in f if l.strip()]
        assert lines == ["old.jpg", "new.jpg"]


class TestLoadDisplayConfig:
    """Test suite for load_display_config function"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "py-frame.conf")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write(self, content):
        with open(self.config_path, "w") as f:
            f.write(content)

    def test_missing_file_defaults_to_shuffle_true(self):
        assert load_display_config(self.config_path) is True

    def test_reads_shuffle_true(self):
        self._write("[display]\nshuffle = true\n")
        assert load_display_config(self.config_path) is True

    def test_reads_shuffle_false(self):
        self._write("[display]\nshuffle = false\n")
        assert load_display_config(self.config_path) is False

    def test_missing_section_falls_back_to_default(self):
        self._write("[schedule]\nstart = 22:00\nend = 07:00\n")
        assert load_display_config(self.config_path) is True

    def test_malformed_file_falls_back_to_default_instead_of_crashing(self):
        self._write("[display]\nshuffle = not_a_bool\n")
        assert load_display_config(self.config_path) is True


class TestClassifyPatternType:
    """Test suite for classify_pattern_type, the shared P/L threshold
    classifier used by extract_pattern_from_deque"""

    def test_ppp(self):
        assert classify_pattern_type(count_p=3, count_l=0) == (1, 3, 0)

    def test_pplll(self):
        assert classify_pattern_type(count_p=2, count_l=3) == (2, 2, 3)

    def test_plll(self):
        assert classify_pattern_type(count_p=1, count_l=3) == (3, 1, 3)

    def test_more_than_needed_still_matches_highest_priority_pattern(self):
        # 5 P's and 0 L's still satisfies PPP (only needs 3 P's)
        assert classify_pattern_type(count_p=5, count_l=0) == (1, 3, 0)

    def test_no_match_returns_none(self):
        assert classify_pattern_type(count_p=0, count_l=5) is None
        assert classify_pattern_type(count_p=1, count_l=2) is None
        assert classify_pattern_type(count_p=0, count_l=0) is None


class TestFinalizeExclusions:
    """Test suite for finalize_exclusions, the commit sweep. /api/mark is
    what adds/removes paths from pending_exclusions/excluded_paths
    immediately; finalize_exclusions only decides when a pending mark
    becomes permanent -- once its path is no longer reachable via
    current_slides or history (undo is no longer possible), it's written
    to exclusions_file and dropped from pending_exclusions."""

    def setup_method(self):
        pygame.init()
        self.temp_dir = tempfile.mkdtemp()
        self.controller = SlideshowController()
        self.controller.exclusions_file = os.path.join(self.temp_dir, "exclusions.txt")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        pygame.quit()

    def test_finalize_no_pending_is_a_noop(self):
        self.controller.current_slides = [
            Slide(path="test1.jpg", surface=pygame.Surface((10, 10)), orientation="L")
        ]

        finalize_exclusions(self.controller)

        assert self.controller.excluded_paths == set()
        assert not os.path.exists(self.controller.exclusions_file)

    def test_pending_path_still_on_current_screen_is_not_committed(self):
        """A photo marked on the screen currently displayed must not be
        committed yet -- it's still undo-able."""
        self.controller.current_slides = [
            Slide(path="test1.jpg", surface=pygame.Surface((10, 10)), orientation="L")
        ]
        self.controller.pending_exclusions = {"test1.jpg"}
        self.controller.excluded_paths = {"test1.jpg"}

        finalize_exclusions(self.controller)

        assert self.controller.pending_exclusions == {"test1.jpg"}
        assert not os.path.exists(self.controller.exclusions_file)

    def test_pending_path_still_in_history_is_not_committed(self):
        """A marked photo that scrolled off-screen but is still reachable
        via Prev (i.e. still in history) must stay undo-able."""
        old_slide = Slide(path="old.jpg", surface=pygame.Surface((10, 10)), orientation="L")
        self.controller.current_slides = [
            Slide(path="new.jpg", surface=pygame.Surface((10, 10)), orientation="L")
        ]
        self.controller.history = [([old_slide], 0)]
        self.controller.pending_exclusions = {"old.jpg"}
        self.controller.excluded_paths = {"old.jpg"}

        finalize_exclusions(self.controller)

        assert self.controller.pending_exclusions == {"old.jpg"}
        assert not os.path.exists(self.controller.exclusions_file)

    def test_pending_path_gone_from_current_and_history_is_committed(self):
        """Once a marked photo has scrolled entirely out of reach, it
        should be written to exclusions_file and dropped from pending."""
        self.controller.current_slides = [
            Slide(path="new.jpg", surface=pygame.Surface((10, 10)), orientation="L")
        ]
        self.controller.history = [
            ([Slide(path="mid.jpg", surface=pygame.Surface((10, 10)), orientation="L")], 0)
        ]
        self.controller.pending_exclusions = {"old.jpg"}
        self.controller.excluded_paths = {"old.jpg"}

        finalize_exclusions(self.controller)

        assert self.controller.pending_exclusions == set()
        assert self.controller.excluded_paths == {"old.jpg"}
        with open(self.controller.exclusions_file) as f:
            lines = [l.strip() for l in f if l.strip()]
        assert lines == ["old.jpg"]

    def test_only_paths_that_are_gone_get_committed(self):
        """A mix of still-visible and fully-scrolled-off pending exclusions
        -- only the latter should be committed."""
        self.controller.current_slides = [
            Slide(path="visible.jpg", surface=pygame.Surface((10, 10)), orientation="L")
        ]
        self.controller.history = []
        self.controller.pending_exclusions = {"visible.jpg", "gone.jpg"}
        self.controller.excluded_paths = {"visible.jpg", "gone.jpg"}

        finalize_exclusions(self.controller)

        assert self.controller.pending_exclusions == {"visible.jpg"}
        with open(self.controller.exclusions_file) as f:
            lines = [l.strip() for l in f if l.strip()]
        assert lines == ["gone.jpg"]


class TestDownscaleSlideToScreen:
    """Test suite for downscale_slide_to_screen function"""
    
    def setup_method(self):
        """Initialize pygame"""
        pygame.init()
    
    def teardown_method(self):
        """Clean up pygame"""
        pygame.quit()
    
    def test_downscale_large_slide(self):
        """Test that large slide is downscaled"""
        slide = Slide(
            path="large.jpg",
            surface=pygame.Surface((2000, 1500)),
            orientation="L"
        )
        
        downscale_slide_to_screen(slide, 1920, 1080)
        
        # Should be scaled down
        assert slide.surface.get_width() <= 1920
        assert slide.surface.get_height() <= 1080
    
    def test_no_downscale_small_slide(self):
        """Test that small slide is not upscaled"""
        slide = Slide(
            path="small.jpg",
            surface=pygame.Surface((100, 100)),
            orientation="L"
        )
        original_w = slide.surface.get_width()
        original_h = slide.surface.get_height()
        
        downscale_slide_to_screen(slide, 1920, 1080)
        
        # Should remain the same
        assert slide.surface.get_width() == original_w
        assert slide.surface.get_height() == original_h


class TestBuildThumbnailBytes:
    """Test suite for build_thumbnail_bytes, which must only ever be
    called from render_loop's thread -- it's the pygame/SDL surface work
    that used to run in web_server.py's Flask thread and silently
    produced no image on at least one Pi (untested cross-thread pygame
    call). Kept here as a plain pygame-in, bytes-out function so it's
    easy to unit test in isolation from any threading concerns."""

    def setup_method(self):
        pygame.init()
        pygame.display.set_mode((1, 1))

    def teardown_method(self):
        pygame.quit()

    def test_returns_valid_png_bytes(self):
        surface = pygame.Surface((400, 300))
        surface.fill((10, 20, 30))

        data = build_thumbnail_bytes(surface, width=100)

        assert data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG file signature

    def test_scales_to_requested_width_preserving_aspect(self):
        surface = pygame.Surface((400, 200))  # 2:1 aspect

        data = build_thumbnail_bytes(surface, width=100)
        result = pygame.image.load(io.BytesIO(data))

        assert result.get_width() == 100
        assert result.get_height() == 50  # 400x200 scaled to width 100

    def test_handles_non_24_32_bit_surface(self):
        """Regression test: some real-world photos decode into a Slide
        surface that isn't 24/32-bit (observed live: an 8-bit source
        raised "ValueError: Only 24-bit or 32-bit surfaces can be
        smoothly scaled" from a raw pygame.transform.smoothscale call).
        The rest of the codebase already handles this via
        smoothscale_safe() (used for the on-screen render path); this
        must go through the same helper instead of calling
        pygame.transform.smoothscale directly."""
        surface = pygame.Surface((400, 300), depth=8)
        assert surface.get_bitsize() == 8

        data = build_thumbnail_bytes(surface, width=100)

        assert data[:8] == b"\x89PNG\r\n\x1a\n"


class TestUpdateThumbnailCache:
    """Test suite for update_thumbnail_cache: generates missing entries,
    leaves already-cached ones alone, and prunes anything no longer
    reachable via current_slides/history."""

    def setup_method(self):
        pygame.init()
        pygame.display.set_mode((1, 1))
        self.controller = SlideshowController()

    def teardown_method(self):
        pygame.quit()

    def _slide(self, path, size=(40, 20), orientation="L"):
        surf = pygame.Surface(size)
        surf.fill((50, 60, 70))
        return Slide(path=path, surface=surf, orientation=orientation)

    def test_generates_thumbnail_for_new_slide(self):
        slide = self._slide("a.jpg")

        update_thumbnail_cache(self.controller, [slide])

        assert "a.jpg" in self.controller.thumbnail_cache
        assert self.controller.thumbnail_cache["a.jpg"][:8] == b"\x89PNG\r\n\x1a\n"

    def test_does_not_regenerate_already_cached_path(self):
        slide = self._slide("a.jpg")
        self.controller.thumbnail_cache["a.jpg"] = b"already-there"

        update_thumbnail_cache(self.controller, [slide])

        assert self.controller.thumbnail_cache["a.jpg"] == b"already-there"

    def test_prunes_paths_no_longer_visible(self):
        self.controller.thumbnail_cache["gone.jpg"] = b"stale"
        self.controller.history = []

        update_thumbnail_cache(self.controller, [self._slide("current.jpg")])

        assert "gone.jpg" not in self.controller.thumbnail_cache
        assert "current.jpg" in self.controller.thumbnail_cache

    def test_keeps_paths_still_in_history(self):
        old_slide = self._slide("old.jpg")
        self.controller.thumbnail_cache["old.jpg"] = b"still-good"
        self.controller.history = [([old_slide], 0)]

        update_thumbnail_cache(self.controller, [self._slide("current.jpg")])

        assert self.controller.thumbnail_cache["old.jpg"] == b"still-good"

    def test_max_to_generate_caps_work_done_per_call(self):
        """Regression test: render_loop used to generate a whole screen's
        thumbnails in one blocking batch, freezing pause/mark/next/prev
        responsiveness for however long that took (observed live: 2-3s
        for 5 photos on a Pi 3B). max_to_generate lets render_loop spread
        that work across iterations instead -- one call should only ever
        produce up to that many new entries."""
        slides = [self._slide(f"s{i}.jpg") for i in range(5)]

        update_thumbnail_cache(self.controller, slides, max_to_generate=1)
        assert len(self.controller.thumbnail_cache) == 1

        update_thumbnail_cache(self.controller, slides, max_to_generate=1)
        assert len(self.controller.thumbnail_cache) == 2

        # Repeated calls progressively finish the rest.
        update_thumbnail_cache(self.controller, slides, max_to_generate=1)
        update_thumbnail_cache(self.controller, slides, max_to_generate=1)
        update_thumbnail_cache(self.controller, slides, max_to_generate=1)
        assert set(self.controller.thumbnail_cache) == {s.path for s in slides}


class TestSetHdmiPower:
    """Test suite for set_hdmi_power, which cuts/restores the physical HDMI
    signal via vcgencmd so the monitor sleeps itself (distinct from
    black_screen mode, which just paints black pixels)."""

    def test_on_invokes_vcgencmd_with_1(self):
        with patch("subprocess.run") as mock_run:
            set_hdmi_power(True)

        args = mock_run.call_args[0][0]
        assert args == ["vcgencmd", "display_power", "1"]

    def test_off_invokes_vcgencmd_with_0(self):
        with patch("subprocess.run") as mock_run:
            set_hdmi_power(False)

        args = mock_run.call_args[0][0]
        assert args == ["vcgencmd", "display_power", "0"]

    def test_missing_vcgencmd_does_not_raise(self):
        """Developing off the Pi (no vcgencmd on PATH) shouldn't crash the
        render loop over a cosmetic feature -- just log a warning."""
        with patch("subprocess.run", side_effect=OSError("not found")):
            set_hdmi_power(True)  # should not raise

    def test_timeout_does_not_raise(self):
        import subprocess as subprocess_module
        with patch("subprocess.run", side_effect=subprocess_module.TimeoutExpired(cmd="vcgencmd", timeout=5)):
            set_hdmi_power(False)  # should not raise


class TestReadFileList:
    """Test suite for read_file_list function"""
    
    def setup_method(self):
        """Create test file list"""
        self.temp_dir = tempfile.mkdtemp()
        self.list_path = os.path.join(self.temp_dir, "test.list")
    
    def teardown_method(self):
        """Clean up test files"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_read_jpg_files(self):
        """Test reading JPG files from list"""
        with open(self.list_path, "w") as f:
            f.write("image1.jpg\n")
            f.write("image2.JPG\n")
            f.write("image3.jpeg\n")
            f.write("image4.JPEG\n")
        
        paths = read_file_list(self.list_path)
        
        assert len(paths) == 4
        assert "image1.jpg" in paths
        assert "image2.JPG" in paths
        assert "image3.jpeg" in paths
        assert "image4.JPEG" in paths
    
    def test_skip_comments_and_empty_lines(self):
        """Test that comments and empty lines are skipped"""
        with open(self.list_path, "w") as f:
            f.write("# This is a comment\n")
            f.write("\n")
            f.write("image1.jpg\n")
            f.write("  \n")
            f.write("image2.jpg\n")
        
        paths = read_file_list(self.list_path)
        
        assert len(paths) == 2
    
    def test_skip_non_jpg_files(self):
        """Test that non-JPG files are filtered out"""
        with open(self.list_path, "w") as f:
            f.write("image1.jpg\n")
            f.write("image2.png\n")
            f.write("image3.gif\n")
            f.write("video.mp4\n")
        
        paths = read_file_list(self.list_path)

        assert len(paths) == 1
        assert paths[0] == "image1.jpg"

    def test_volumes_prefix_rewritten_to_nasus(self):
        """Mac-mounted paths (/Volumes/...) get rewritten to the relative
        nasus/ path the Pi's NFS symlink expects, so the same list file
        works unmodified whether it was built on macOS or the Pi"""
        with open(self.list_path, "w") as f:
            f.write("/Volumes/MyNAS/photo/2020/img.jpg\n")
            f.write("nasus/photo/already-relative.jpg\n")

        paths = read_file_list(self.list_path)

        assert "nasus/MyNAS/photo/2020/img.jpg" in paths
        assert "nasus/photo/already-relative.jpg" in paths
        assert not any(p.startswith("/Volumes/") for p in paths)

    def test_empty_file_list(self):
        """Test reading an empty file list"""
        with open(self.list_path, "w") as f:
            f.write("")

        paths = read_file_list(self.list_path)

        assert len(paths) == 0

    def test_shuffles_the_full_list_not_just_a_rotation(self):
        """Test that the display order is a genuine shuffle (random.shuffle),
        not the old behavior of rotating by a random offset"""
        expected = [f"image{i}.jpg" for i in range(10)]
        with open(self.list_path, "w") as f:
            for name in expected:
                f.write(name + "\n")

        with patch("random.shuffle") as mock_shuffle:
            read_file_list(self.list_path)

        mock_shuffle.assert_called_once()
        shuffled_arg = mock_shuffle.call_args[0][0]
        assert sorted(shuffled_arg) == sorted(expected)

    def test_shuffle_preserves_every_entry_exactly_once(self):
        """Test that shuffling doesn't drop or duplicate any path"""
        expected = [f"image{i}.jpg" for i in range(50)]
        with open(self.list_path, "w") as f:
            for name in expected:
                f.write(name + "\n")

        paths = read_file_list(self.list_path)

        assert sorted(paths) == sorted(expected)

    def test_shuffle_false_rotates_by_random_offset_preserving_relative_order(self):
        """Test that shuffle=False restores the original rotation behavior:
        same relative order as the file, just starting from a random point"""
        expected = [f"image{i}.jpg" for i in range(10)]
        with open(self.list_path, "w") as f:
            for name in expected:
                f.write(name + "\n")

        with patch("random.randrange", return_value=3):
            paths = read_file_list(self.list_path, shuffle=False)

        assert paths == expected[3:] + expected[:3]

    def test_shuffle_false_does_not_call_random_shuffle(self):
        """Test that shuffle=False takes the rotation path, not the shuffle one"""
        with open(self.list_path, "w") as f:
            f.write("image1.jpg\nimage2.jpg\n")

        with patch("random.shuffle") as mock_shuffle:
            read_file_list(self.list_path, shuffle=False)

        mock_shuffle.assert_not_called()


class TestSetupLogging:
    """Test suite for setup_logging, which routes every exception (caught
    and logged, or truly uncaught) to a rotating log file for later
    analysis, in addition to the normal terminal/journal output"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "test_errors.log")

        # setup_logging mutates process-wide state (root logger handlers,
        # sys.excepthook, threading.excepthook, and its own "already ran"
        # guard) -- snapshot it all so each test starts clean and other
        # tests/files aren't affected by what runs here.
        self.root_logger = logging.getLogger()
        self.prev_handlers = list(self.root_logger.handlers)
        self.prev_level = self.root_logger.level
        self.prev_sys_excepthook = sys.excepthook
        self.prev_thread_excepthook = threading.excepthook
        self.prev_configured = py_frame._logging_configured
        py_frame._logging_configured = False

    def teardown_method(self):
        for h in list(self.root_logger.handlers):
            if h not in self.prev_handlers:
                self.root_logger.removeHandler(h)
        self.root_logger.setLevel(self.prev_level)
        sys.excepthook = self.prev_sys_excepthook
        threading.excepthook = self.prev_thread_excepthook
        py_frame._logging_configured = self.prev_configured

        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_logged_exception_appears_in_the_file(self):
        """Test that logger.error(..., exc_info=True) anywhere in the app
        ends up in the log file with a full traceback"""
        with patch("py_frame.ERROR_LOG_FILE", self.log_file):
            setup_logging()

        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("py_frame").error("something failed", exc_info=True)

        with open(self.log_file) as f:
            content = f.read()

        assert "something failed" in content
        assert "ValueError: boom" in content

    def test_is_idempotent(self):
        """Test that calling setup_logging twice doesn't add duplicate
        handlers (e.g. across multiple test runs in the same process)"""
        with patch("py_frame.ERROR_LOG_FILE", self.log_file):
            setup_logging()
            handlers_after_first = len(self.root_logger.handlers)
            setup_logging()
            handlers_after_second = len(self.root_logger.handlers)

        assert handlers_after_first == handlers_after_second

    def test_uncaught_thread_exception_is_logged(self):
        """Test that an uncaught exception in a background thread is
        logged via threading.excepthook, not just silently printed"""
        with patch("py_frame.ERROR_LOG_FILE", self.log_file):
            setup_logging()

        def boom():
            raise RuntimeError("thread boom")

        t = threading.Thread(target=boom)
        t.start()
        t.join()

        with open(self.log_file) as f:
            content = f.read()

        assert "Uncaught exception in thread" in content
        assert "RuntimeError: thread boom" in content


class TestMainEmptyFileList:
    """Test suite for main()'s handling of an empty/invalid photo list"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.list_path = os.path.join(self.temp_dir, "empty.list")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_main_exits_when_no_valid_photos(self):
        """main() should print an error and exit(1) instead of silently
        starting a slideshow with nothing to show"""
        with open(self.list_path, "w") as f:
            f.write("not_a_photo.txt\n")

        # setup_logging() has real side effects (creates a log file in the
        # cwd, mutates sys.excepthook/threading.excepthook) that are
        # irrelevant to this test and would otherwise leak into the repo.
        with patch("py_frame.setup_logging"):
            with patch("sys.argv", ["py_frame.py", self.list_path]):
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 1


class _StopFetcher(Exception):
    """Sentinel used to break image_fetcher_thread's infinite loop in tests."""
    pass


class TestImageFetcherThreadThrottling:
    """Test suite for image_fetcher_thread's busy-loop throttling on skip/failure paths"""

    def setup_method(self):
        self.controller = SlideshowController()
        # Needed for the success-path test, which performs a real load_slide()
        # call ending in Surface.convert(), which requires a display mode.
        os.environ['SDL_VIDEODRIVER'] = 'dummy'
        pygame.init()
        pygame.display.set_mode((1, 1))

        self.measurements_dir = tempfile.mkdtemp()
        self.controller.measurements_file = os.path.join(self.measurements_dir, "measurements.csv")

    def teardown_method(self):
        pygame.quit()
        import shutil
        shutil.rmtree(self.measurements_dir, ignore_errors=True)

    def _read_measurement_rows(self):
        import csv
        with open(self.controller.measurements_file, newline="") as f:
            return list(csv.DictReader(f))

    def _run_with_bounded_sleep(self, file_paths, max_calls=3):
        """Run image_fetcher_thread with time.sleep mocked to raise after
        max_calls, so the otherwise-infinite loop stops deterministically."""
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= max_calls:
                raise _StopFetcher()

        dq = deque()
        lock = threading.Lock()
        not_full = threading.Condition(lock)
        producer_done = threading.Event()

        def quiet_excepthook(args):
            if args.exc_type is not _StopFetcher:
                threading.__excepthook__(args)

        prev_excepthook = threading.excepthook
        threading.excepthook = quiet_excepthook
        try:
            with patch("time.sleep", side_effect=fake_sleep):
                t = threading.Thread(
                    target=image_fetcher_thread,
                    args=(file_paths, dq, lock, not_full, producer_done, self.controller, 5),
                    daemon=True,
                )
                t.start()
                t.join(timeout=2)
        finally:
            threading.excepthook = prev_excepthook

        assert not t.is_alive(), "fetcher thread did not stop after the bounded sleep raised"
        return sleep_calls, dq

    def test_excluded_path_throttles_instead_of_busy_looping(self):
        """All paths excluded -> should sleep between skips, not spin"""
        self.controller.excluded_paths.add("excluded.jpg")

        sleep_calls, dq = self._run_with_bounded_sleep(["excluded.jpg"])

        assert sleep_calls, "expected time.sleep to be called while skipping excluded paths"
        assert all(c == 0.3 for c in sleep_calls)
        assert len(dq) == 0

    def test_load_failure_throttles_instead_of_busy_looping(self):
        """Unloadable path (e.g. missing file) -> should sleep between retries, not spin"""
        sleep_calls, dq = self._run_with_bounded_sleep(
            ["/nonexistent/path/does_not_exist.jpg"]
        )

        assert sleep_calls, "expected time.sleep to be called after a failed load"
        assert all(c == 0.5 for c in sleep_calls)
        assert len(dq) == 0

    def test_generic_load_failure_marks_drive_not_ok(self):
        """A genuine I/O failure (not a missing file) should flag the drive
        as unreadable and clear the speed estimate, so the on-screen
        diagnostics reflect the stall"""
        self.controller.drive_ok = True
        self.controller.download_bytes_per_sec = 123.0

        with patch("py_frame.load_slide", side_effect=ConnectionResetError("simulated reset")):
            self._run_with_bounded_sleep(["irrelevant-path.jpg"])

        assert self.controller.drive_ok is False
        assert self.controller.download_bytes_per_sec is None

        rows = self._read_measurement_rows()
        assert rows[-1]["outcome"] == "io_error"
        assert rows[-1]["error_type"] == "ConnectionResetError"
        assert rows[-1]["bytes"] == ""
        assert rows[-1]["seconds"] == ""

    def test_file_not_found_does_not_mark_drive_disconnected(self):
        """A single missing file (deleted/renamed on the NAS, a stale list
        entry, etc) should NOT be reported as a drive disconnect -- it says
        nothing about whether the mount itself is reachable"""
        self.controller.drive_ok = True
        self.controller.download_bytes_per_sec = 123.0

        self._run_with_bounded_sleep(["/nonexistent/path/does_not_exist.jpg"])

        # Recorded distinctly from a generic io_error...
        rows = self._read_measurement_rows()
        assert rows[-1]["outcome"] == "file_not_found"
        assert rows[-1]["error_type"] == "FileNotFoundError"
        assert rows[-1]["bytes"] == ""
        assert rows[-1]["seconds"] == ""

        # ...and the drive/speed diagnostics are left untouched.
        assert self.controller.drive_ok is True
        assert self.controller.download_bytes_per_sec == 123.0

    def test_corrupt_file_does_not_mark_drive_disconnected(self):
        """A bad/corrupt file (readable, but not a valid image) should NOT
        be reported as a drive disconnect -- only genuine I/O failures
        (missing file, unreadable mount, etc) should flip drive_ok"""
        temp_dir = tempfile.mkdtemp()
        try:
            bad_path = os.path.join(temp_dir, "corrupt.jpg")
            with open(bad_path, "wb") as f:
                f.write(b"not a real jpeg")

            self.controller.drive_ok = True
            self.controller.download_bytes_per_sec = 123.0

            self._run_with_bounded_sleep([bad_path])

            # The read succeeded, so it's still logged as a valid
            # size/time measurement (just flagged as a decode error)...
            rows = self._read_measurement_rows()
            assert rows[-1]["outcome"] == "decode_error"
            assert int(rows[-1]["bytes"]) == len(b"not a real jpeg")
            assert float(rows[-1]["seconds"]) >= 0

            # ...but the drive itself is not reported as disconnected, and
            # the existing speed estimate survives (unlike a real I/O failure).
            assert self.controller.drive_ok is True
            assert self.controller.download_bytes_per_sec == 123.0
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_successful_load_marks_drive_ok_and_reports_speed(self):
        """A successful load should flag the drive as OK and compute a
        Kbps estimate from the read time/size"""
        temp_dir = tempfile.mkdtemp()
        try:
            img_path = os.path.join(temp_dir, "photo.jpg")
            Image.new("RGB", (50, 50), color="blue").save(img_path)

            self.controller.drive_ok = False
            self.controller.download_bytes_per_sec = None

            dq = deque()
            lock = threading.Lock()
            not_full = threading.Condition(lock)
            producer_done = threading.Event()

            def fake_notify_all():
                raise _StopFetcher()

            def quiet_excepthook(args):
                if args.exc_type is not _StopFetcher:
                    threading.__excepthook__(args)

            prev_excepthook = threading.excepthook
            threading.excepthook = quiet_excepthook
            prev_notify_all = not_full.notify_all
            not_full.notify_all = fake_notify_all
            try:
                t = threading.Thread(
                    target=image_fetcher_thread,
                    args=([img_path], dq, lock, not_full, producer_done, self.controller, 5),
                    daemon=True,
                )
                t.start()
                t.join(timeout=2)
            finally:
                threading.excepthook = prev_excepthook
                not_full.notify_all = prev_notify_all

            assert not t.is_alive(), "fetcher thread did not stop after the bounded notify_all raised"
            assert self.controller.drive_ok is True
            assert self.controller.download_bytes_per_sec is not None
            assert self.controller.download_bytes_per_sec >= 0

            rows = self._read_measurement_rows()
            assert rows[-1]["outcome"] == "ok"
            assert int(rows[-1]["bytes"]) == os.path.getsize(img_path)
            assert float(rows[-1]["seconds"]) >= 0
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
