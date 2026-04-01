# WATCH Protocol — Reference Implementation

A working proof-of-concept for the WATCH HTTP method proposed in
*draft-hunt-httpbis-watch-method-00*, an Internet-Draft submitted to the IETF.

## What is WATCH?

WATCH is a proposed addition to the HTTP protocol that lets a client
subscribe to change notifications on a resource. Instead of repeatedly
polling with GET requests ("anything new? no. anything new? no."),
the client subscribes once and the server pushes updates as they happen.

The protocol introduces five key concepts:

| Term | Role | Description |
|------|------|-------------|
| **WATCH Connection** | Both | The persistent client-server subscription relationship |
| **Heartbeat** | Client → Server | Periodic "I'm still here" ping (empty body, ~200 bytes) |
| **Echo** | Server → Client | Server's acknowledgment of a Heartbeat |
| **Dispatch** | Server → Client | Event payload pushed when the watched resource changes |
| **ALIVE Interval** | Server-defined | Maximum time between Heartbeats before eviction |

## What's in This Package

| File | Description |
|------|-------------|
| `watch_server.py` | Flask-based WATCH server with filesystem monitoring, ALIVE eviction, and web dashboard |
| `watch_client.py` | Command-line client that subscribes, displays Dispatches, and maintains Heartbeats |
| `README.md` | This file |

## Requirements

- Python 3.10+
- pip packages: `flask`, `watchdog`, `requests`

```bash
pip install flask watchdog requests
```

## Quick Start

You'll need three terminal windows.

### Terminal 1 — Start the server

```bash
python watch_server.py
```

The server monitors `~/watch-test` for file changes (the folder is created
automatically if it doesn't exist). You'll see:

```
============================================================
  WATCH Server
============================================================
  Watching:    /home/you/watch-test
  ALIVE:       every 15s (grace: 1.5x)
  Dashboard:   http://localhost:5050
============================================================
```

### Terminal 2 — Start the client

```bash
python watch_client.py
```

The client connects, receives its Setup Phase parameters, and begins
sending Heartbeats:

```
  [SETUP]     WATCH connection pending
  [SETUP]     Subscriber ID: c7968bf6
  [SETUP]     ALIVE Interval: 15s

  ==================================================
  Listening for Dispatches... (Ctrl+C to UNWATCH)
  ==================================================

  [HEARTBEAT] Sent → /api/alive/c7968bf6
  [ECHO]      ← 200 OK, ALIVE interval: 15s
```

### Terminal 3 — Trigger Dispatches

Create, modify, and delete files in the watched folder:

```bash
echo "hello world" > ~/watch-test/test.txt
echo "updated" >> ~/watch-test/test.txt
rm ~/watch-test/test.txt
```

The client immediately displays each Dispatch:

```
  [DISPATCH]  file_created          test.txt  (2026-03-28T14:32:07)
  [DISPATCH]  file_modified         test.txt  (2026-03-28T14:32:09)
  [DISPATCH]  file_deleted          test.txt  (2026-03-28T14:32:12)
```

Heartbeats continue independently between Dispatches — the WATCH
connection persists.

## Testing Eviction

To verify that the server properly evicts clients who stop sending
Heartbeats, run the client with the `--skip-alive` flag:

```bash
python watch_client.py --skip-alive
```

The client will connect and receive a subscriber ID, but since it
never sends a Heartbeat, the server will terminate the WATCH connection
after approximately 22.5 seconds (15s ALIVE Interval × 1.5 grace
multiplier).

## Testing UNWATCH

Press `Ctrl+C` in the client terminal. The client sends a clean
UNWATCH request before exiting:

```
  [UNWATCH]   Terminating WATCH connection...
  [UNWATCH]   Unsubscribed (c7968bf6)
  Done.
```

## Web Dashboard

Open `http://localhost:5050` in your browser for a visual dashboard
that acts as its own WATCH client. It connects via Server-Sent Events,
sends its own Heartbeats via JavaScript, and displays Dispatches in
real time. You can use it alongside the command-line client — both
are independent subscribers paying their own rent.

## Configuration

Edit the following variables at the top of `watch_server.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCH_FOLDER` | `~/watch-test` | Directory to monitor for changes |
| `ALIVE_INTERVAL_SECONDS` | `15` | How often clients must send Heartbeats |
| `ALIVE_GRACE_MULTIPLIER` | `1.5` | Tolerance factor before eviction |
| `EVICTION_CHECK_SECONDS` | `5` | How often the server checks for dead clients |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web dashboard |
| `/api/files` | GET | List files in the watched folder |
| `/api/watch` | GET | Subscribe to file change Dispatches (SSE stream) |
| `/api/alive/<id>` | POST | Send a Heartbeat (receive an Echo) |
| `/api/unwatch/<id>` | POST | Cleanly terminate a WATCH connection |
| `/api/status` | GET | Server status and active subscriber health |

## Windows Notes

On Windows, the `~/watch-test` path resolves to your user folder
(e.g., `C:\Users\YourName\watch-test`). If this causes issues, edit
the `WATCH_FOLDER` variable in `watch_server.py` to an absolute path.

## Authors

- **Dawson Hunt** — Independent Researcher, Houston, TX
- **Claude** — AI Research Assistant, Anthropic

## License

This reference implementation is provided as a companion to
*draft-watch-http-method-03*. It is released for review, testing,
and discussion purposes.
