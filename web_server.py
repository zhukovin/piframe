# web_server.py
import json
import time
from typing import TYPE_CHECKING

from flask import Flask, Response, request, jsonify

if TYPE_CHECKING:
    from py_frame import SlideshowController  # adjust import

MARK_ADVANCE_DELAY_SECONDS = 5


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
            "orientation": s.orientation,
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

        # render_loop is the only thread that ever touches pygame/SDL
        # surfaces (see update_thumbnail_cache in py_frame.py) -- this
        # route just reads bytes it already encoded, so there's no pygame
        # call here at all, and no cross-thread SDL risk.
        with controller.lock:
            png_bytes = controller.thumbnail_cache.get(path)

        if png_bytes is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        return Response(
            png_bytes,
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

  .pattern-grid {
    display: grid;
    gap: 4px;
    aspect-ratio: 16 / 9;
    margin-bottom: 12px;
  }

  .tile {
    position: relative;
    overflow: hidden;
    border-radius: 4px;
    border: 3px solid transparent;
    cursor: pointer;
    background: #222;
  }

  .tile.marked {
    border-color: #e63946;
  }

  .tile .thumb {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .tile .badge {
    position: absolute;
    bottom: 4px;
    right: 4px;
    background: rgba(0, 0, 0, 0.6);
    color: white;
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 4px;
    pointer-events: none;
  }

  .tile.marked .badge {
    background: #e63946;
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

// Mirrors compute_pattern_rects() in py_frame.py so the web UI grid
// matches the physical screen's arrangement: which columns/rows each
// slide occupies for a given pattern_type. pattern_type 2/3 group
// slides by orientation (all L's, then all P's) rather than using
// slides in their original order.
function layoutTiles(slides, patternType) {
  if (patternType !== 1 && patternType !== 2 && patternType !== 3) {
    // Solo landscape (0), empty, or anything unrecognized: one big tile.
    const tiles = slides.slice(0, 1).map(slide => ({ slide, col: '1', row: '1' }));
    return { columns: 1, rows: 1, tiles };
  }

  if (patternType === 1) {
    // PPP: 3 equal columns, original order, single row.
    const tiles = slides.slice(0, 3).map((slide, i) => ({ slide, col: String(i + 1), row: '1' }));
    return { columns: 3, rows: 1, tiles };
  }

  const Ls = slides.filter(s => s.orientation === 'L');
  const Ps = slides.filter(s => s.orientation === 'P');
  const tiles = [];

  if (patternType === 2) {
    // PPLLL: column 1 = 3 L's stacked; columns 2 & 3 = P's, full height.
    for (let i = 0; i < Math.min(3, Ls.length); i++) {
      tiles.push({ slide: Ls[i], col: '1', row: String(i + 1) });
    }
    if (Ps.length >= 1) tiles.push({ slide: Ps[0], col: '2', row: '1 / span 3' });
    if (Ps.length >= 2) tiles.push({ slide: Ps[1], col: '3', row: '1 / span 3' });
    return { columns: 3, rows: 3, tiles };
  }

  // PLLL: column 3 = P full height; top L spans columns 1-2; bottom two
  // L's split columns 1 and 2 in the bottom row.
  if (Ps.length >= 1) tiles.push({ slide: Ps[0], col: '3', row: '1 / span 2' });
  if (Ls.length >= 1) tiles.push({ slide: Ls[0], col: '1 / span 2', row: '1' });
  if (Ls.length >= 2) tiles.push({ slide: Ls[1], col: '1', row: '2' });
  if (Ls.length >= 3) tiles.push({ slide: Ls[2], col: '2', row: '2' });
  return { columns: 3, rows: 2, tiles };
}

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

  const patternType = data.slides.length > 0 ? data.slides[0].pattern_type : null;
  const layout = layoutTiles(data.slides, patternType);

  const grid = document.createElement('div');
  grid.className = 'pattern-grid';
  grid.style.gridTemplateColumns = `repeat(${layout.columns}, 1fr)`;
  grid.style.gridTemplateRows = `repeat(${layout.rows}, 1fr)`;

  // layoutTiles() builds `tiles` in the same order py_frame.py's
  // compute_pattern_rects() builds its rects list (grouped by orientation
  // for the PPLLL/PLLL patterns) -- number badges by *position in this
  // array*, not by slide.index (raw current_slides order), so the number
  // shown here matches the number drawn on the physical screen.
  layout.tiles.forEach(({ slide, col, row }, position) => {
    const tile = document.createElement('div');
    tile.className = 'tile' + (slide.marked ? ' marked' : '');
    tile.style.gridColumn = col;
    tile.style.gridRow = row;
    tile.title = slide.path;
    tile.onclick = () => toggleMark(slide.index, slide.path);

    const img = document.createElement('img');
    img.className = 'thumb';
    img.loading = 'lazy';
    img.src = '/api/thumbnail?path=' + encodeURIComponent(slide.path);
    img.alt = '';

    const badge = document.createElement('div');
    badge.className = 'badge';
    badge.textContent = slide.marked ? 'MARKED' : String(position + 1);

    tile.appendChild(img);
    tile.appendChild(badge);
    grid.appendChild(tile);
  });

  slotsDiv.innerHTML = '';
  slotsDiv.appendChild(grid);
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
