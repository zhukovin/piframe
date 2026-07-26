# web_server.py
import json
import time
from typing import TYPE_CHECKING

from flask import Flask, Response, request, jsonify, send_file

if TYPE_CHECKING:
    from py_frame import SlideshowController  # adjust import

# Must match py_frame.EXCLUSION_ICON_PATH (kept as a separate literal here,
# rather than importing it, to avoid a circular import with py_frame --
# py_frame imports run_web from this module at load time).
EXCLUSION_ICON_PATH = "pictures/eye-dont-show.png"


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
    if controller.paused or controller.black_screen:
        # No advance is pending -- nothing for the client to count down to.
        seconds_remaining = None
    else:
        seconds_remaining = round(max(0.0, controller.current_end_time - time.time()), 1)

    return {
        "slides": slides,
        "paused": controller.paused,
        "black": controller.black_screen,
        "version": controller.state_version,
        "seconds_remaining": seconds_remaining,
        "seconds_per_screen": controller.seconds_to_display,
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

    @app.route("/api/exclusion-icon")
    def api_exclusion_icon():
        # Same icon drawn over a marked photo on the physical screen (see
        # draw_exclusion_overlay in py_frame.py), served as-is for the web
        # UI to overlay on marked tiles too.
        try:
            return send_file(EXCLUSION_ICON_PATH, mimetype="image/png")
        except OSError:
            return jsonify({"ok": False, "error": "not found"}), 404

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
                # Marking pauses the slideshow so the screen doesn't change
                # out from under the user mid-review -- they resume
                # manually (Play) once they're done marking.
                controller.paused = True
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

  #advance-bar-track {
    height: 6px;
    border-radius: 3px;
    background: #e0e0e0;
    overflow: hidden;
    margin: 4px 0 14px 0;
  }

  #advance-bar-track.hidden {
    visibility: hidden;
  }

  #advance-bar-fill {
    height: 100%;
    width: 100%;
    background: #2c7be5;
    transition: width 0.2s linear;
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
    cursor: pointer;
    background: #222;
    user-select: none;
    -webkit-user-select: none;
  }

  .tile.pressing {
    filter: brightness(0.6);
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

  .tile .exclusion-icon {
    position: absolute;
    top: 50%;
    left: 50%;
    width: 40%;
    height: auto;
    transform: translate(-50%, -50%);
    pointer-events: none;
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
  <div id="advance-bar-track"><div id="advance-bar-fill"></div></div>
  <div id="slots"></div>

<script>
let latestVersion = 0;
let latestPaused = false;
let latestBlack = false;

// Advance countdown/progress bar. SSE pushes are event-driven (only sent
// on real state changes, roughly once per screen), not a steady per-
// second tick -- so instead of relying on push frequency, we compute a
// local deadline once per push (from Date.now(), no server clock sync
// needed) and animate smoothly between pushes with our own interval.
let advanceDeadlineMs = null;   // Date.now()-based ms timestamp, or null if no countdown
let advanceTotalSeconds = 15;

function tickAdvanceBar() {
  const track = document.getElementById('advance-bar-track');
  const fill = document.getElementById('advance-bar-fill');
  if (advanceDeadlineMs === null) {
    track.classList.add('hidden');
    return;
  }
  track.classList.remove('hidden');
  const remainingMs = Math.max(0, advanceDeadlineMs - Date.now());
  const fraction = advanceTotalSeconds > 0
    ? Math.min(1, remainingMs / (advanceTotalSeconds * 1000))
    : 0;
  fill.style.width = (fraction * 100) + '%';
}

setInterval(tickAdvanceBar, 200);

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

const THUMB_RETRY_DELAY_MS = 500;
const THUMB_RETRY_MAX_ATTEMPTS = 8;  // ~4s -- render_loop fills in one new thumbnail per iteration

// render_loop generates a new screen's thumbnails gradually (one per
// iteration) rather than all at once, so a photo that just appeared may
// not have a cached thumbnail yet. Retry on load failure instead of
// leaving a permanently broken image icon.
function setThumbSrcWithRetry(img, path, attempt) {
  attempt = attempt || 0;
  img.onerror = () => {
    if (attempt >= THUMB_RETRY_MAX_ATTEMPTS) return;
    setTimeout(() => setThumbSrcWithRetry(img, path, attempt + 1), THUMB_RETRY_DELAY_MS);
  };
  img.src = '/api/thumbnail?path=' + encodeURIComponent(path) + '&attempt=' + attempt;
}

function renderState(data) {
  latestVersion = data.version;
  latestPaused = data.paused;
  latestBlack = data.black;

  if (data.seconds_remaining === null || data.seconds_remaining === undefined) {
    advanceDeadlineMs = null;
  } else {
    advanceDeadlineMs = Date.now() + data.seconds_remaining * 1000;
    advanceTotalSeconds = data.seconds_per_screen;
  }
  tickAdvanceBar();

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
    tile.className = 'tile';
    tile.style.gridColumn = col;
    tile.style.gridRow = row;
    tile.title = slide.path;
    attachTapHandlers(
      tile,
      () => toggleMark(slide.index, slide.path),
      () => copyPathToClipboard(slide.path),
    );

    const img = document.createElement('img');
    img.className = 'thumb';
    img.loading = 'lazy';
    img.alt = '';
    setThumbSrcWithRetry(img, slide.path);

    const badge = document.createElement('div');
    badge.className = 'badge';
    badge.textContent = String(position + 1);

    tile.appendChild(img);
    if (slide.marked) {
      const icon = document.createElement('img');
      icon.className = 'exclusion-icon';
      icon.src = '/api/exclusion-icon';
      icon.alt = 'marked for exclusion';
      tile.appendChild(icon);
    }
    tile.appendChild(badge);
    grid.appendChild(tile);
  });

  slotsDiv.innerHTML = '';
  slotsDiv.appendChild(grid);
}

const LONG_PRESS_MS = 550;

// A short tap toggles the mark; a long press (touch or mouse-held) copies
// the photo's path instead. Both share one press/release state machine
// rather than a native click listener, since ordering a click handler
// against a synthetic touch-then-click sequence is unreliable -- this
// way onTap only ever fires from a genuinely short, unmoved press.
function attachTapHandlers(el, onTap, onLongPress) {
  let timer = null;
  let firedLongPress = false;
  let moved = false;

  function start() {
    // Touch devices often fire a synthetic mousedown shortly after
    // touchstart -- clear any timer that's already running instead of
    // leaking it (overwriting `timer` below without this would make the
    // first one uncancelable, firing onLongPress a second time later).
    clearTimer();
    firedLongPress = false;
    moved = false;
    el.classList.add('pressing');
    timer = setTimeout(() => {
      firedLongPress = true;
      timer = null;
      el.classList.remove('pressing');
      onLongPress();
    }, LONG_PRESS_MS);
  }

  function clearTimer() {
    el.classList.remove('pressing');
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function onMove() {
    moved = true;
    clearTimer();
  }

  function end() {
    const wasLongPress = firedLongPress;
    clearTimer();
    if (!wasLongPress && !moved) {
      onTap();
    }
  }

  el.addEventListener('touchstart', start, { passive: true });
  el.addEventListener('touchend', end);
  el.addEventListener('touchmove', onMove, { passive: true });
  el.addEventListener('touchcancel', clearTimer);

  el.addEventListener('mousedown', start);
  el.addEventListener('mouseup', end);
  el.addEventListener('mouseleave', clearTimer);
}

function copyPathToClipboard(path) {
  if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
    navigator.clipboard.writeText(path).then(
      () => flashStatus('Path copied'),
      () => legacyCopyToClipboard(path),
    );
  } else {
    // navigator.clipboard requires a secure context (https, or
    // localhost) -- this page is normally loaded over plain http on the
    // LAN, so that API is unavailable and we fall back to the older
    // execCommand technique.
    legacyCopyToClipboard(path);
  }
}

function legacyCopyToClipboard(path) {
  const textarea = document.createElement('textarea');
  textarea.value = path;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  document.body.removeChild(textarea);

  if (ok) {
    flashStatus('Path copied');
  } else {
    // Last resort so the user isn't left with nothing -- the prompt's
    // text is pre-selected, so it's still a one-tap manual copy.
    window.prompt('Copy path:', path);
  }
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
