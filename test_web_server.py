"""
Comprehensive test suite for web_server.py
Tests for Flask web interface and API endpoints
"""
import json
import pytest
import pygame
from web_server import create_app, _parse_int_field, build_state_payload
from py_frame import SlideshowController, Slide


class TestWebServer:
    """Test suite for web server endpoints"""

    def setup_method(self):
        """Setup test client and controller"""
        pygame.init()
        pygame.display.set_mode((1, 1))

        self.controller = SlideshowController()
        self.app = create_app(self.controller)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def teardown_method(self):
        """Clean up pygame"""
        pygame.quit()

    def _add_current_slides(self, count, orientation="L"):
        self.controller.current_slides = [
            Slide(path=f"test{i}.jpg", surface=pygame.Surface((10, 10)), orientation=orientation)
            for i in range(count)
        ]

    def _mark(self, slot, path, expected_version=None):
        if expected_version is None:
            expected_version = self.controller.state_version
        return self.client.post('/api/mark', json={
            'slot': slot, 'path': path, 'expected_version': expected_version,
        })

    def test_api_state_empty(self):
        """Test /api/state endpoint with no slides"""
        response = self.client.get('/api/state')

        assert response.status_code == 200
        data = response.get_json()
        assert 'slides' in data
        assert 'paused' in data
        assert 'black' in data
        assert 'version' in data
        assert len(data['slides']) == 0
        assert data['paused'] is False
        assert data['black'] is False
        assert data['version'] == 0

    def test_api_state_with_slides(self):
        """Test /api/state endpoint with slides, marked derived from
        pending_exclusions rather than a per-visit slot set"""
        slide1 = Slide(path="test1.jpg", surface=pygame.Surface((100, 100)), orientation="L")
        slide2 = Slide(path="test2.jpg", surface=pygame.Surface((100, 100)), orientation="P")

        self.controller.current_slides = [slide1, slide2]
        self.controller.current_pattern_type = 1
        self.controller.pending_exclusions.add("test1.jpg")

        response = self.client.get('/api/state')

        assert response.status_code == 200
        data = response.get_json()
        assert len(data['slides']) == 2
        assert data['slides'][0]['path'] == "test1.jpg"
        assert data['slides'][0]['marked'] is True
        assert data['slides'][1]['path'] == "test2.jpg"
        assert data['slides'][1]['marked'] is False
        assert data['slides'][0]['pattern_type'] == 1

    def test_api_state_paused(self):
        """Test /api/state reflects paused state"""
        self.controller.paused = True
        self.controller.black_screen = True

        response = self.client.get('/api/state')

        assert response.status_code == 200
        data = response.get_json()
        assert data['paused'] is True
        assert data['black'] is True

    def test_api_mark_valid_slot(self):
        """Test /api/mark marks a slide by (slot, path) and echoes the
        marked state back"""
        self._add_current_slides(3)
        response = self._mark(2, "test2.jpg")

        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert data['marked'] is True
        assert "test2.jpg" in self.controller.pending_exclusions
        assert "test2.jpg" in self.controller.excluded_paths

    def test_api_mark_toggle(self):
        """Test /api/mark toggles marking on repeated calls, each against
        the freshly-bumped version"""
        self._add_current_slides(2)

        r1 = self._mark(1, "test1.jpg")
        assert r1.get_json()['marked'] is True
        assert "test1.jpg" in self.controller.pending_exclusions

        r2 = self._mark(1, "test1.jpg")
        assert r2.get_json()['marked'] is False
        assert "test1.jpg" not in self.controller.pending_exclusions
        assert "test1.jpg" not in self.controller.excluded_paths

    def test_api_mark_sets_advance_delay_only_on_fresh_mark(self):
        """Marking (not unmarking) should push min_next_advance_time out
        a few seconds so the screen doesn't change mid-decision"""
        self._add_current_slides(1)
        assert self.controller.min_next_advance_time == 0.0

        self._mark(0, "test0.jpg")
        assert self.controller.min_next_advance_time > 0.0

        delay_after_mark = self.controller.min_next_advance_time
        self.controller.min_next_advance_time = 0.0  # reset to detect unmark behavior
        self._mark(0, "test0.jpg")  # this call unmarks
        assert self.controller.min_next_advance_time == 0.0

    def test_api_mark_stale_version_rejected(self):
        """A mark request against an outdated version must be rejected
        with 409 and must not change any state -- this is what stops a
        stale web click (issued before the screen advanced) from
        excluding the wrong photo."""
        self._add_current_slides(2)
        response = self._mark(0, "test0.jpg", expected_version=999)

        assert response.status_code == 409
        data = response.get_json()
        assert data['ok'] is False
        assert data['error'] == 'stale'
        assert self.controller.pending_exclusions == set()

    def test_api_mark_wrong_path_for_slot_rejected(self):
        """Even with a correct version, a path that doesn't match what's
        actually in that slot right now must be rejected -- guards against
        a race between fetching state and posting the mark."""
        self._add_current_slides(2)
        response = self._mark(0, "not-actually-there.jpg")

        assert response.status_code == 409
        data = response.get_json()
        assert data['ok'] is False
        assert self.controller.pending_exclusions == set()

    def test_api_mark_invalid_slot_negative(self):
        """Test /api/mark with invalid negative slot"""
        self._add_current_slides(1)
        response = self._mark(-1, "test0.jpg")

        assert response.status_code == 400
        data = response.get_json()
        assert data['ok'] is False
        assert 'error' in data

    def test_api_mark_invalid_slot_too_large(self):
        """Test /api/mark with a slot beyond the number of current slides"""
        self._add_current_slides(3)
        response = self._mark(3, "test3.jpg")

        assert response.status_code == 400
        data = response.get_json()
        assert data['ok'] is False

    def test_api_mark_no_current_slides(self):
        """Test /api/mark rejects any slot when there are no current slides
        (guards against marking a slot with no photo behind it)"""
        response = self._mark(0, "anything.jpg")

        assert response.status_code == 400
        data = response.get_json()
        assert data['ok'] is False

    def test_api_mark_non_numeric_slot(self):
        """Test /api/mark with a non-numeric slot returns 400 instead of crashing"""
        self._add_current_slides(3)
        response = self.client.post('/api/mark', json={
            'slot': 'abc', 'path': 'test0.jpg', 'expected_version': 0,
        })

        assert response.status_code == 400
        data = response.get_json()
        assert data['ok'] is False

    def test_api_mark_non_numeric_expected_version(self):
        """Test /api/mark with a non-numeric expected_version returns 400"""
        self._add_current_slides(3)
        response = self.client.post('/api/mark', json={
            'slot': 0, 'path': 'test0.jpg', 'expected_version': 'abc',
        })

        assert response.status_code == 400
        data = response.get_json()
        assert data['ok'] is False

    def test_api_mark_missing_expected_version_is_treated_as_stale(self):
        """Omitting expected_version defaults it to -1, which will never
        match a real version, so the request is safely rejected rather
        than silently trusted"""
        self._add_current_slides(3)
        response = self.client.post('/api/mark', json={'slot': 0, 'path': 'test0.jpg'})

        assert response.status_code == 409

    def test_api_command_next(self):
        """Test /api/command endpoint with next command"""
        response = self.client.post('/api/command', json={'cmd': 'next', 'steps': 1})

        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert self.controller.pending_command == {'type': 'next', 'steps': 1}

    def test_api_command_prev(self):
        """Test /api/command endpoint with prev command"""
        response = self.client.post('/api/command', json={'cmd': 'prev', 'steps': 3})

        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert self.controller.pending_command == {'type': 'prev', 'steps': 3}

    def test_api_command_pause(self):
        """Test /api/command endpoint with pause command"""
        response = self.client.post('/api/command', json={'cmd': 'pause'})

        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert self.controller.pending_command == {'type': 'pause'}

    def test_api_command_play(self):
        """Test /api/command endpoint with play command"""
        response = self.client.post('/api/command', json={'cmd': 'play'})

        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert self.controller.pending_command == {'type': 'play'}

    def test_api_command_screen_off(self):
        """Test /api/command endpoint with screen_off command"""
        response = self.client.post('/api/command', json={'cmd': 'screen_off'})

        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert self.controller.pending_command['type'] == 'screen_off'

    def test_api_command_screen_on(self):
        """Test /api/command endpoint with screen_on command"""
        response = self.client.post('/api/command', json={'cmd': 'screen_on'})

        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert self.controller.pending_command['type'] == 'screen_on'

    def test_api_command_default_steps(self):
        """Test /api/command with default steps value"""
        response = self.client.post('/api/command', json={'cmd': 'next'})

        assert response.status_code == 200
        assert self.controller.pending_command['steps'] == 1

    def test_api_command_invalid(self):
        """Test /api/command with invalid command"""
        response = self.client.post('/api/command', json={'cmd': 'invalid_cmd'})

        assert response.status_code == 400
        data = response.get_json()
        assert data['ok'] is False
        assert 'error' in data

    def test_api_command_non_numeric_steps(self):
        """Test /api/command with a non-numeric steps value returns 400 instead of crashing"""
        response = self.client.post('/api/command', json={'cmd': 'next', 'steps': 'abc'})

        assert response.status_code == 400
        data = response.get_json()
        assert data['ok'] is False

    def test_api_command_missing_cmd(self):
        """Test /api/command with missing cmd parameter"""
        response = self.client.post('/api/command', json={})

        assert response.status_code == 400

    def test_api_thumbnail_found_on_current_screen(self):
        """Test /api/thumbnail returns image bytes for a currently-shown slide"""
        self._add_current_slides(1)
        response = self.client.get('/api/thumbnail?path=test0.jpg')

        assert response.status_code == 200
        assert response.mimetype == 'image/png'
        assert len(response.data) > 0

    def test_api_thumbnail_found_in_history(self):
        """Test /api/thumbnail falls back to scanning history for a photo
        that's no longer on the current screen"""
        old_slide = Slide(path="old.jpg", surface=pygame.Surface((40, 20)), orientation="L")
        self.controller.history = [([old_slide], 0)]

        response = self.client.get('/api/thumbnail?path=old.jpg')

        assert response.status_code == 200
        assert response.mimetype == 'image/png'

    def test_api_thumbnail_not_found(self):
        """Test /api/thumbnail 404s for a path that isn't visible anywhere"""
        response = self.client.get('/api/thumbnail?path=nope.jpg')

        assert response.status_code == 404

    def test_api_thumbnail_missing_path_param(self):
        """Test /api/thumbnail 400s without a path query param"""
        response = self.client.get('/api/thumbnail')

        assert response.status_code == 400

    def test_index_page(self):
        """Test that / endpoint returns HTML page"""
        response = self.client.get('/')

        assert response.status_code == 200
        assert b'<!DOCTYPE html>' in response.data
        assert b'Frame Control' in response.data
        assert b'/api/state' in response.data
        assert b'Pause' in response.data
        assert b'Play' in response.data

    def test_index_page_has_controls(self):
        """Test that index page has the toggle controls, and that
        shuffle/random-start (now config-only) is gone"""
        response = self.client.get('/')
        html = response.data.decode('utf-8')

        assert 'Pause' in html
        assert 'Prev' in html
        assert 'Next' in html
        assert 'Screen Off' in html
        assert 'Shuffle' not in html
        assert 'Random Start' not in html

    def test_index_page_has_javascript(self):
        """Test that index page includes JavaScript for interaction,
        including the SSE stream that replaced polling"""
        response = self.client.get('/')
        html = response.data.decode('utf-8')

        assert 'renderState' in html
        assert 'toggleMark' in html
        assert 'sendCommand' in html
        assert 'EventSource' in html
        assert '/api/stream' in html


class TestApiStream:
    """Test suite for /api/stream, the SSE push that replaced 3s polling"""

    def setup_method(self):
        pygame.init()
        pygame.display.set_mode((1, 1))
        self.controller = SlideshowController()
        self.app = create_app(self.controller)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def teardown_method(self):
        pygame.quit()

    @staticmethod
    def _decode_event(chunk) -> dict:
        if isinstance(chunk, bytes):
            chunk = chunk.decode('utf-8')
        assert chunk.startswith('data: ')
        return json.loads(chunk[len('data: '):].strip())

    def test_stream_sends_current_state_immediately(self):
        """The very first message shouldn't require a version change --
        a client connecting for the first time needs state right away"""
        response = self.client.get('/api/stream')
        assert response.mimetype == 'text/event-stream'

        payload = self._decode_event(next(response.response))
        assert payload['version'] == 0
        assert payload['slides'] == []
        response.close()

    def test_stream_pushes_again_after_bump_version(self):
        """A version bump (as render_loop/api_mark perform on any visible
        state change) should unblock the next queued message"""
        response = self.client.get('/api/stream')
        next(response.response)  # consume the immediate first message

        with self.controller.lock:
            self.controller.paused = True
            self.controller.bump_version()

        payload = self._decode_event(next(response.response))
        assert payload['paused'] is True
        assert payload['version'] == 1
        response.close()


class TestBuildStatePayload:
    """Test suite for build_state_payload, shared by /api/state and /api/stream"""

    def setup_method(self):
        pygame.init()
        pygame.display.set_mode((1, 1))
        self.controller = SlideshowController()

    def teardown_method(self):
        pygame.quit()

    def test_payload_shape(self):
        with self.controller.lock:
            payload = build_state_payload(self.controller)

        assert set(payload.keys()) == {"slides", "paused", "black", "version"}


class TestParseIntField:
    """Test suite for _parse_int_field, the helper shared by
    api_mark and api_command for parsing user-supplied integer fields"""

    def setup_method(self):
        self.controller = SlideshowController()
        self.app = create_app(self.controller)

    def test_valid_int_parses(self):
        with self.app.app_context():
            value, error = _parse_int_field({"slot": 3}, "slot", -1)
        assert value == 3
        assert error is None

    def test_missing_key_uses_default(self):
        with self.app.app_context():
            value, error = _parse_int_field({}, "slot", -1)
        assert value == -1
        assert error is None

    def test_non_numeric_returns_400_error(self):
        with self.app.app_context():
            value, error = _parse_int_field({"slot": "abc"}, "slot", -1)
        assert value is None
        response, status = error
        assert status == 400


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
