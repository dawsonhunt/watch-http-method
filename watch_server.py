"""
WATCH-Capable File Monitor API
===============================
A proof-of-concept API server implementing the WATCH protocol we designed.

Features:
  - Standard REST: GET files in a watched folder
  - WATCH: Subscribe to file change events via Server-Sent Events (SSE)
  - ALIVE: Client heartbeat system — miss your window, lose your subscription
  - UNWATCH: Clean unsubscribe
  - Auto-eviction of dead clients

Requirements:
    pip install flask watchdog

Usage:
    1. Edit WATCH_FOLDER below to point at whatever folder you want to monitor
    2. Run:  python watch_server.py
    3. Open the dashboard:  http://localhost:5050
    4. In another terminal, run the test client:  python watch_client.py
    5. Modify/add/delete files in the watched folder and see events stream in

Architecture:
    - Flask handles HTTP endpoints
    - watchdog monitors the filesystem for changes
    - SSE (Server-Sent Events) pushes updates to subscribed clients
    - A background thread checks heartbeat deadlines and evicts dead clients
"""

import os
import json
import time
import uuid
import threading
from datetime import datetime, timedelta, timezone
from queue import Queue, Empty

from flask import Flask, jsonify, request, Response, render_template_string
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ===========================================================================
# CONFIG — Change this to whatever folder you want to monitor
# ===========================================================================
WATCH_FOLDER = os.path.expanduser("~/watch-test")

# Server-defined heartbeat requirements
ALIVE_INTERVAL_SECONDS = 15       # Client must ping at least this often
ALIVE_GRACE_MULTIPLIER = 1.5      # Actual deadline = interval * multiplier
EVICTION_CHECK_SECONDS = 5        # How often the server checks for dead clients

# ===========================================================================

app = Flask(__name__)

# --- Subscriber Registry ---------------------------------------------------
# Each subscriber gets:
#   - a unique ID
#   - an SSE message queue
#   - a last_alive timestamp
#   - a created_at timestamp
subscribers = {}
sub_lock = threading.Lock()


def register_subscriber():
    """Create a new subscriber and return their ID."""
    sub_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc)
    subscribers[sub_id] = {
        "id": sub_id,
        "queue": Queue(),
        "last_alive": now,
        "created_at": now,
        "events_received": 0,
    }
    print(f"  [+] Subscriber {sub_id} connected ({len(subscribers)} total)")
    return sub_id


def remove_subscriber(sub_id, reason="unsubscribed"):
    """Remove a subscriber and clean up."""
    with sub_lock:
        sub = subscribers.pop(sub_id, None)
        if sub:
            print(f"  [-] Subscriber {sub_id} {reason} ({len(subscribers)} remaining)")


def broadcast_event(event_type, data):
    """Push an event to ALL active subscribers."""
    message = {
        "event": event_type,
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with sub_lock:
        for sub_id, sub in subscribers.items():
            sub["queue"].put(message)
            sub["events_received"] += 1


# --- Filesystem Watcher ----------------------------------------------------
class FolderWatcher(FileSystemEventHandler):
    """Watches the target folder and broadcasts change events."""

    def _broadcast(self, event_type, event):
        # Skip hidden files and __pycache__
        path = event.src_path
        basename = os.path.basename(path)
        if basename.startswith(".") or "__pycache__" in path:
            return

        rel_path = os.path.relpath(path, WATCH_FOLDER)
        data = {
            "file": rel_path,
            "is_directory": event.is_directory,
        }
        print(f"  [*] {event_type}: {rel_path}")
        broadcast_event(event_type, data)

    def on_created(self, event):
        self._broadcast("file_created", event)

    def on_modified(self, event):
        self._broadcast("file_modified", event)

    def on_deleted(self, event):
        self._broadcast("file_deleted", event)

    def on_moved(self, event):
        data_extra = {"destination": os.path.relpath(event.dest_path, WATCH_FOLDER)}
        self._broadcast("file_moved", event)


# --- Dead Client Eviction Thread -------------------------------------------
def eviction_loop():
    """Background thread that checks for clients who missed their ALIVE."""
    while True:
        time.sleep(EVICTION_CHECK_SECONDS)
        now = datetime.now(timezone.utc)
        deadline = timedelta(seconds=ALIVE_INTERVAL_SECONDS * ALIVE_GRACE_MULTIPLIER)

        dead = []
        with sub_lock:
            for sub_id, sub in subscribers.items():
                if now - sub["last_alive"] > deadline:
                    dead.append(sub_id)

        for sub_id in dead:
            remove_subscriber(sub_id, reason="evicted (missed ALIVE deadline)")


# ===========================================================================
# API ENDPOINTS
# ===========================================================================

# --- GET /api/files — List files in the watched folder ---------------------
@app.route("/api/files", methods=["GET"])
def list_files():
    """Standard GET — list all files in the watched folder."""
    if not os.path.exists(WATCH_FOLDER):
        return jsonify({"error": f"Folder not found: {WATCH_FOLDER}"}), 404

    files = []
    for entry in os.scandir(WATCH_FOLDER):
        if not entry.name.startswith("."):
            files.append({
                "name": entry.name,
                "is_directory": entry.is_directory(),
                "size_bytes": entry.stat().st_size if entry.is_file() else None,
                "modified": datetime.fromtimestamp(
                    entry.stat().st_mtime
                ).isoformat(),
            })

    return jsonify({"folder": WATCH_FOLDER, "count": len(files), "files": files})


# --- WATCH /api/watch — Subscribe to file change events (SSE) -------------
@app.route("/api/watch", methods=["GET"])
def watch():
    """
    WATCH endpoint — returns a Server-Sent Events stream.

    The client connects once, and the server pushes events as they happen.
    The client MUST send periodic ALIVE pings to /api/alive/<id> to
    maintain the subscription.

    Response headers include the subscriber ID and heartbeat requirements.
    """
    sub_id = register_subscriber()

    def event_stream():
        # First message: subscription confirmation with ALIVE requirements
        welcome = {
            "event": "subscribed",
            "subscriber_id": sub_id,
            "alive_interval_seconds": ALIVE_INTERVAL_SECONDS,
            "alive_endpoint": f"/api/alive/{sub_id}",
            "unwatch_endpoint": f"/api/unwatch/{sub_id}",
            "message": (
                f"Send a POST to /api/alive/{sub_id} every "
                f"{ALIVE_INTERVAL_SECONDS}s to keep your subscription. "
                f"Grace period: {ALIVE_GRACE_MULTIPLIER}x."
            ),
        }
        yield f"data: {json.dumps(welcome)}\n\n"

        # Stream events until the client disconnects or gets evicted
        try:
            while sub_id in subscribers:
                try:
                    message = subscribers[sub_id]["queue"].get(timeout=1)
                    yield f"data: {json.dumps(message)}\n\n"
                except Empty:
                    # Send a comment to keep the connection alive (SSE spec)
                    yield ": heartbeat\n\n"
                except KeyError:
                    # Subscriber was evicted
                    break
        finally:
            remove_subscriber(sub_id, reason="disconnected")

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Subscriber-Id": sub_id,
            "X-Alive-Interval": str(ALIVE_INTERVAL_SECONDS),
        },
    )


# --- ALIVE /api/alive/<id> — Client heartbeat ping ------------------------
@app.route("/api/alive/<sub_id>", methods=["POST"])
def alive(sub_id):
    """
    ALIVE endpoint — the client's rent payment.

    Client must POST here periodically to maintain their WATCH subscription.
    No body required — the request itself is the signal.
    """
    with sub_lock:
        sub = subscribers.get(sub_id)
        if not sub:
            return jsonify({
                "error": "Subscriber not found — subscription may have expired",
                "action": "Re-subscribe via GET /api/watch",
            }), 404

        sub["last_alive"] = datetime.now(timezone.utc)

    return jsonify({
        "status": "alive",
        "subscriber_id": sub_id,
        "alive_interval_seconds": ALIVE_INTERVAL_SECONDS,
    })


# --- UNWATCH /api/unwatch/<id> — Clean unsubscribe ------------------------
@app.route("/api/unwatch/<sub_id>", methods=["POST"])
def unwatch(sub_id):
    """UNWATCH — cleanly end a subscription."""
    if sub_id in subscribers:
        remove_subscriber(sub_id, reason="unsubscribed (client request)")
        return jsonify({"status": "unsubscribed", "subscriber_id": sub_id})
    return jsonify({"error": "Subscriber not found"}), 404


# --- GET /api/status — Server status and active subscribers ----------------
@app.route("/api/status", methods=["GET"])
def status():
    """Check server status, active subscribers, and their health."""
    now = datetime.now(timezone.utc)
    deadline = timedelta(seconds=ALIVE_INTERVAL_SECONDS * ALIVE_GRACE_MULTIPLIER)

    sub_list = []
    with sub_lock:
        for sub_id, sub in subscribers.items():
            time_since_alive = (now - sub["last_alive"]).total_seconds()
            sub_list.append({
                "id": sub_id,
                "seconds_since_alive": round(time_since_alive, 1),
                "healthy": time_since_alive < deadline.total_seconds(),
                "events_received": sub["events_received"],
                "connected_since": sub["created_at"].isoformat(),
            })

    return jsonify({
        "watch_folder": WATCH_FOLDER,
        "alive_interval_seconds": ALIVE_INTERVAL_SECONDS,
        "grace_multiplier": ALIVE_GRACE_MULTIPLIER,
        "active_subscribers": len(sub_list),
        "subscribers": sub_list,
    })


# --- Dashboard (simple HTML for visual monitoring) -------------------------
@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML,
                                  folder=WATCH_FOLDER,
                                  interval=ALIVE_INTERVAL_SECONDS,
                                  grace=ALIVE_GRACE_MULTIPLIER)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>WATCH Server Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Courier New', monospace; background: #0a0a0a; color: #e0e0e0; padding: 2rem; }
  h1 { color: #5dcaa5; margin-bottom: 0.5rem; font-size: 1.4rem; }
  .subtitle { color: #888; margin-bottom: 2rem; font-size: 0.85rem; }
  .panel { background: #151515; border: 1px solid #2a2a2a; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
  .panel h2 { color: #5dcaa5; font-size: 1rem; margin-bottom: 1rem; border-bottom: 1px solid #2a2a2a; padding-bottom: 0.5rem; }
  .config-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; }
  .config-item { background: #1a1a1a; padding: 0.75rem; border-radius: 4px; }
  .config-item .label { color: #888; font-size: 0.75rem; text-transform: uppercase; }
  .config-item .value { color: #5dcaa5; font-size: 1.1rem; margin-top: 0.25rem; }
  #events { max-height: 500px; overflow-y: auto; }
  .event { padding: 0.5rem 0; border-bottom: 1px solid #1a1a1a; font-size: 0.85rem; display: flex; gap: 1rem; }
  .event .time { color: #555; min-width: 80px; }
  .event .type { min-width: 120px; font-weight: bold; }
  .event .type.file_created { color: #5dcaa5; }
  .event .type.file_modified { color: #f0997b; }
  .event .type.file_deleted { color: #ed93b1; }
  .event .type.file_moved { color: #85b7eb; }
  .event .type.subscribed { color: #afa9ec; }
  .event .detail { color: #aaa; }
  .status { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status.on { background: #5dcaa5; }
  .status.off { background: #555; }
  #sub-count { color: #5dcaa5; }
  .empty { color: #555; font-style: italic; padding: 1rem 0; }
  .btn { background: #5dcaa5; color: #0a0a0a; border: none; padding: 0.5rem 1rem;
         border-radius: 4px; cursor: pointer; font-family: inherit; font-weight: bold; }
  .btn:hover { background: #7ddbb8; }
  .btn.disconnect { background: #ed93b1; }
  .btn.disconnect:hover { background: #f4b0c6; }
  #connection-status { font-size: 0.85rem; margin-top: 0.5rem; }
</style>
</head>
<body>
  <h1>WATCH Server Dashboard</h1>
  <p class="subtitle">Proof-of-concept WATCH protocol implementation</p>

  <div class="panel">
    <h2>Server configuration</h2>
    <div class="config-grid">
      <div class="config-item">
        <div class="label">Watched folder</div>
        <div class="value">{{ folder }}</div>
      </div>
      <div class="config-item">
        <div class="label">ALIVE interval</div>
        <div class="value">{{ interval }}s</div>
      </div>
      <div class="config-item">
        <div class="label">Grace multiplier</div>
        <div class="value">{{ grace }}x</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>
      <span class="status" id="conn-dot"></span>
      Live event stream — <span id="sub-count">0</span> events
    </h2>
    <div style="margin-bottom: 1rem;">
      <button class="btn" id="connect-btn" onclick="toggleConnection()">Connect (WATCH)</button>
      <span id="connection-status"></span>
    </div>
    <div id="events"><div class="empty">Click Connect to start watching...</div></div>
  </div>

  <div class="panel">
    <h2>Test commands</h2>
    <p style="color:#888; font-size:0.85rem; line-height:1.6;">
      Open a terminal and try these in the watched folder:<br><br>
      <code style="color:#5dcaa5;">echo "hello" > ~/watch-test/test.txt</code> — triggers file_created<br>
      <code style="color:#f0997b;">echo "updated" >> ~/watch-test/test.txt</code> — triggers file_modified<br>
      <code style="color:#ed93b1;">rm ~/watch-test/test.txt</code> — triggers file_deleted
    </p>
  </div>

<script>
let eventSource = null;
let subId = null;
let aliveTimer = null;
let eventCount = 0;

function toggleConnection() {
  if (eventSource) { disconnect(); }
  else { connect(); }
}

function connect() {
  eventSource = new EventSource('/api/watch');
  document.getElementById('conn-dot').className = 'status on';
  document.getElementById('connect-btn').textContent = 'Disconnect (UNWATCH)';
  document.getElementById('connect-btn').className = 'btn disconnect';
  document.getElementById('connection-status').textContent = 'Connecting...';
  document.getElementById('events').innerHTML = '';

  eventSource.onmessage = function(e) {
    const msg = JSON.parse(e.data);

    if (msg.event === 'subscribed') {
      subId = msg.subscriber_id;
      document.getElementById('connection-status').textContent =
        'Subscribed as ' + subId + ' — sending ALIVE every {{ interval }}s';

      // Start the ALIVE heartbeat (client's responsibility!)
      aliveTimer = setInterval(() => {
        fetch('/api/alive/' + subId, { method: 'POST' })
          .then(r => r.json())
          .then(d => console.log('ALIVE:', d))
          .catch(() => disconnect());
      }, {{ interval }} * 1000);
    }

    addEvent(msg);
  };

  eventSource.onerror = function() {
    document.getElementById('connection-status').textContent = 'Connection lost';
    disconnect();
  };
}

function disconnect() {
  if (aliveTimer) { clearInterval(aliveTimer); aliveTimer = null; }
  if (subId) {
    fetch('/api/unwatch/' + subId, { method: 'POST' }).catch(() => {});
    subId = null;
  }
  if (eventSource) { eventSource.close(); eventSource = null; }
  document.getElementById('conn-dot').className = 'status off';
  document.getElementById('connect-btn').textContent = 'Connect (WATCH)';
  document.getElementById('connect-btn').className = 'btn';
}

function addEvent(msg) {
  eventCount++;
  document.getElementById('sub-count').textContent = eventCount;

  const div = document.createElement('div');
  div.className = 'event';

  const time = new Date().toLocaleTimeString();
  const detail = msg.data ? (msg.data.file || '') : (msg.subscriber_id || '');

  div.innerHTML =
    '<span class="time">' + time + '</span>' +
    '<span class="type ' + msg.event + '">' + msg.event + '</span>' +
    '<span class="detail">' + detail + '</span>';

  const container = document.getElementById('events');
  container.insertBefore(div, container.firstChild);
}
</script>
</body>
</html>
"""


# ===========================================================================
# STARTUP
# ===========================================================================
if __name__ == "__main__":
    # Ensure the watch folder exists
    os.makedirs(WATCH_FOLDER, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"  WATCH Server")
    print(f"{'='*60}")
    print(f"  Watching:    {WATCH_FOLDER}")
    print(f"  ALIVE:       every {ALIVE_INTERVAL_SECONDS}s "
          f"(grace: {ALIVE_GRACE_MULTIPLIER}x)")
    print(f"  Dashboard:   http://localhost:5050")
    print(f"  API:         http://localhost:5050/api/files")
    print(f"  WATCH:       http://localhost:5050/api/watch")
    print(f"  Status:      http://localhost:5050/api/status")
    print(f"{'='*60}\n")

    # Start filesystem watcher
    observer = Observer()
    observer.schedule(FolderWatcher(), WATCH_FOLDER, recursive=True)
    observer.start()

    # Start dead-client eviction thread
    eviction_thread = threading.Thread(target=eviction_loop, daemon=True)
    eviction_thread.start()

    try:
        app.run(debug=False, port=5050, threaded=True)
    finally:
        observer.stop()
        observer.join()
