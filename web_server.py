# web_server.py
import io
import json
import time
from typing import TYPE_CHECKING

import pygame
from flask import Flask, Response, request, jsonify

if TYPE_CHECKING:
    from py_frame import SlideshowController  # adjust import

MARK_ADVANCE_DELAY_SECONDS = 5
THUMBNAIL_WIDTH = 160


def _parse_int_field(data: dict, key: str, default: int):
    """
    Parse an integer field from a request's JSON body. Returns
    (value, None) on success, or (None, error_response) on failure, where
    error_response is a ready-to-return (jsonify(...), 400) tuple.
    """
    try:
        return int(data.get(key, default)), None
    except (TypeError, ValueError):
        return None, (jsonify({"ok": False, "error": f"invalid {key}"}), 400)


def build_state_payload(controller: "SlideshowController") -> dict:
    """
    Build the JSON-able state dict shared by /api/state (single fetch) and
    /api/stream (SSE push), so the two representations never drift apart.
    Must be called while holding controller.lock.
    """
    slides = [
        {
            "index": i,
            "path": s.path,
            "marked": s.path in controller.pending_exclusions,
            "pattern_type": controller.current_pattern_type,
        }
        for i, s in enumerate(controller.current_slides)
    ]
    return {
        "slides": slides,
        "paused": controller.paused,
        "black": controller.black_screen,
        "version": controller.state_version,
    }


def _find_slide_surface(controller: "SlideshowController", path: str):
    """
    Look up a Slide's already-decoded surface by path: first among the
    slides on screen right now, then in history (most recent first) so a
    thumbnail can still be fetched for a photo the user just navigated
    away from. Must be called while holding controller.lock.
    """
    for s in controller.current_slides:
        if s.path == path:
            return s.surface
    for slides, _ in reversed(controller.history):
        for s in slides:
            if s.path == path:
                return s.surface
    return None


def create_app(controller: "SlideshowController") -> Flask:
    app = Flask(__name__)

    @app.route("/api/state")
    def api_state():
        with controller.lock:
            payload = build_state_payload(controller)
        return jsonify(payload)

    @app.route("/api/stream")
    def api_stream():
        def event_stream():
            last_seen = -1
            while True:
                with controller.lock:
                    controller.lock.wait_for(
                        lambda: controller.state_version != last_seen, timeout=15
                    )
                    payload = build_state_payload(controller)
                    last_seen = controller.state_version
                yield f"data: {json.dumps(payload)}\n\n"

        return Response(event_stream(), mimetype="text/event-stream")

    @app.route("/api/thumbnail")
    def api_thumbnail():
        path = request.args.get("path", "")
        if not path:
            return jsonify({"ok": False, "error": "missing path"}), 400

        with controller.lock:
            surface = _find_slide_surface(controller, path)
            if surface is not None:
                w, h = surface.get_width(), surface.get_height()
                if w > 0 and h > 0:
                    scale = THUMBNAIL_WIDTH / w
                    thumb = pygame.transform.smoothscale(
                        surface, (THUMBNAIL_WIDTH, max(1, round(h * scale)))
                    )
                else:
                    thumb = None
            else:
                thumb = None

        if thumb is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        buf = io.BytesIO()
        pygame.image.save(thumb, buf, "thumb.png")
        return Response(
            buf.getvalue(),
            mimetype="image/png",
            headers={"Cache-Control": "max-age=300"},
        )

    @app.route("/api/mark", methods=["POST"])
    def api_mark():
        data = request.json or {}
        slot, error = _parse_int_field(data, "slot", -1)
        if error:
            return error
        expected_version, error = _parse_int_field(data, "expected_version", -1)
        if error:
            return error
        path = data.get("path")

        with controller.lock:
            if expected_version != controller.state_version:
                return jsonify({
                    "ok": False,
                    "error": "stale",
                    "current_version": controller.state_version,
                }), 409

            if not (0 <= slot < len(controller.current_slides)):
                return jsonify({"ok": False, "error": "invalid slot"}), 400

            actual_path = controller.current_slides[slot].path
            if path != actual_path:
                return jsonify({"ok": False, "error": "stale"}), 409

            if actual_path in controller.pending_exclusions:
                controller.pending_exclusions.discard(actual_path)
                controller.excluded_paths.discard(actual_path)
                marked = False
            else:
                controller.pending_exclusions.add(actual_path)
                controller.excluded_paths.add(actual_path)
                controller.min_next_advance_time = time.time() + MARK_ADVANCE_DELAY_SECONDS
                marked = True

            controller.bump_version()

        return jsonify({"ok": True, "marked": marked})

    @app.route("/api/command", methods=["POST"])
    def api_command():
        data = request.json or {}
        cmd = data.get("cmd")

        if cmd not in ("next", "prev", "pause", "play", "screen_off", "screen_on"):
            return jsonify({"ok": False, "error": "bad cmd"}), 400

        steps, error = _parse_int_field(data, "steps", 1)
        if error:
            return error

        with controller.lock:
            if cmd in ("pause", "play"):
                controller.pending_command = {"type": cmd}
            else:
                controller.pending_command = {"type": cmd, "steps": steps}

        return jsonify({"ok": True})

    @app.route("/")
    def index():
        return """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Frame Control</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {
    font-family: sans-serif;
    margin: 10px;
  }

  .controls {
    display: grid;
    grid-template-columns: 1fr 1fr;   /* 2 buttons per row */
    gap: 12px;
    margin-bottom: 16px;
  }

  .controls button {
    font-size: 20px;
    padding: 18px 10px;              /* tall buttons */
    border-radius: 10px;
    border: none;
    background: #2c7be5;
    color: white;
    cursor: pointer;
  }

  .controls button:active {
    background: #1a5dc9;
  }

  .controls button:focus {
    outline: none;
  }

  .controls button.wide {
    grid-column: 1 / -1;
  }

  #status {
    margin: 10px 0;
    font-weight: bold;
  }

  #status.flash {
    color: #c0392b;
  }

  .slot {
    border: 1px solid #ccc;
    padding: 8px;
    margin-bottom: 8px;
    border-radius: 6px;
  }

  .slot.marked {
    border-color: red;
    background: #ffecec;
  }

  .thumb {
    display: block;
    width: 100%;
    max-width: 220px;
    border-radius: 4px;
    margin: 6px 0;
  }

  .path {
    font-size: 11px;
    color: #666;
    word-break: break-all;
    margin-bottom: 6px;
  }
</style>
</head>
<body>
    <div class="controls">
      <button id="btn-playpause" class="wide">Pause</button>

      <button onclick="sendCommand('prev', 1)"><<&nbsp;Prev</button>
      <button onclick="sendCommand('next', 1)">Next&nbsp;>></button>

      <button id="btn-screen" class="wide">Screen Off</button>
    </div>
  <div id="status"></div>
  <div id="slots"></div>

<script>
let latestVersion = 0;
let latestPaused = false;
let latestBlack = false;

function renderState(data) {
  latestVersion = data.version;
  latestPaused = data.paused;
  latestBlack = data.black;

  const slotsDiv = document.getElementById('slots');
  const statusDiv = document.getElementById('status');

  let status = data.paused ? "PAUSED" : "PLAYING";
  if (data.black) status += " (SCREEN OFF)";
  statusDiv.textContent = "Status: " + status;

  document.getElementById('btn-playpause').textContent = data.paused ? 'Play' : 'Pause';
  document.getElementById('btn-screen').textContent = data.black ? 'Screen On' : 'Screen Off';

  slotsDiv.innerHTML = '';
  data.slides.forEach(slide => {
    const div = document.createElement('div');
    div.className = 'slot' + (slide.marked ? ' marked' : '');

    const label = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = 'Slot ' + (slide.index + 1);
    label.appendChild(b);

    const img = document.createElement('img');
    img.className = 'thumb';
    img.loading = 'lazy';
    img.src = '/api/thumbnail?path=' + encodeURIComponent(slide.path);
    img.alt = '';

    const pathDiv = document.createElement('div');
    pathDiv.className = 'path';
    pathDiv.textContent = slide.path;

    const btn = document.createElement('button');
    btn.textContent = slide.marked ? 'Unmark' : 'Mark';
    btn.onclick = () => toggleMark(slide.index, slide.path);

    div.appendChild(label);
    div.appendChild(img);
    div.appendChild(pathDiv);
    div.appendChild(btn);
    slotsDiv.appendChild(div);
  });
}

function flashStatus(message) {
  const statusDiv = document.getElementById('status');
  const prevText = statusDiv.textContent;
  statusDiv.textContent = message;
  statusDiv.classList.add('flash');
  setTimeout(() => {
    statusDiv.textContent = prevText;
    statusDiv.classList.remove('flash');
  }, 1500);
}

async function toggleMark(slot, path) {
  const res = await fetch('/api/mark', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({slot, path, expected_version: latestVersion})
  });
  if (res.status === 409) {
    flashStatus('Screen changed — try again');
  }
}

async function sendCommand(cmd, steps) {
  const body = { cmd };
  if (steps !== undefined) {
    body.steps = steps;
  }
  await fetch('/api/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
}

document.getElementById('btn-playpause').onclick = () => sendCommand(latestPaused ? 'play' : 'pause');
document.getElementById('btn-screen').onclick = () => sendCommand(latestBlack ? 'screen_on' : 'screen_off');

async function initialLoad() {
  const res = await fetch('/api/state');
  renderState(await res.json());
}

function connectStream() {
  const es = new EventSource('/api/stream');
  es.onmessage = (e) => renderState(JSON.parse(e.data));
}

initialLoad();
connectStream();
</script>
</body>
</html>
"""

    return app


def run_web(controller: "SlideshowController"):
    import threading
    print("Web server thread native_id:", threading.get_native_id())

    app = create_app(controller)
    # host=0.0.0.0 so phones on LAN can reach it
    app.run(host="0.0.0.0", port=7654, threaded=True)
