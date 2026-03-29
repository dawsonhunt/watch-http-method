"""
WATCH Test Client
=================
Connects to the WATCH server, subscribes to file events,
and maintains the ALIVE heartbeat.

Usage:
    python watch_client.py              — Normal mode (sends ALIVE pings)
    python watch_client.py --skip-alive — Skip heartbeats (test eviction)

The --skip-alive flag lets you verify that the server properly evicts
clients who stop paying their "rent."
"""

import sys
import json
import time
import threading
import requests

SERVER = "http://localhost:5050"
subscriber_id = None
running = True


def listen_for_events():
    """Connect to the WATCH endpoint and stream events."""
    global subscriber_id, running

    print(f"\n  Connecting to WATCH server at {SERVER}/api/watch ...\n")

    try:
        response = requests.get(f"{SERVER}/api/watch", stream=True)
        for line in response.iter_lines():
            if not running:
                break
            if not line:
                continue

            decoded = line.decode("utf-8")

            # SSE format: lines starting with "data: " contain our JSON
            if decoded.startswith("data: "):
                payload = json.loads(decoded[6:])
                event = payload.get("event", "unknown")

                if event == "subscribed":
                    subscriber_id = payload["subscriber_id"]
                    interval = payload["alive_interval_seconds"]
                    print(f"  [SETUP]     WATCH connection pending")
                    print(f"  [SETUP]     Subscriber ID: {subscriber_id}")
                    print(f"  [SETUP]     ALIVE Interval: {interval}s")
                    print(f"  [SETUP]     Heartbeat endpoint: {payload['alive_endpoint']}")
                    print(f"\n  {'='*50}")
                    print(f"  Listening for Dispatches... (Ctrl+C to UNWATCH)")
                    print(f"  {'='*50}\n")
                else:
                    timestamp = payload.get("timestamp", "")
                    file_info = payload.get("data", {}).get("file", "")
                    print(f"  [DISPATCH]  {event:20s}  {file_info}  ({timestamp[:19]})")

    except requests.exceptions.ConnectionError:
        print("\n  Could not connect to server. Is watch_server.py running?")
        running = False
    except Exception as e:
        if running:
            print(f"\n  Connection lost: {e}")
        running = False


def send_alive_pings(interval=15):
    """Background thread: send ALIVE pings to maintain subscription."""
    global running

    # Wait for subscription to be established
    while subscriber_id is None and running:
        time.sleep(0.5)

    if not running:
        return

    print(f"  [HEARTBEAT] Starting heartbeat every {interval}s\n")

    while running:
        time.sleep(interval)
        if not running or subscriber_id is None:
            break
        try:
            print(f"  [HEARTBEAT] Sent \u2192 /api/alive/{subscriber_id}")
            resp = requests.post(f"{SERVER}/api/alive/{subscriber_id}")
            data = resp.json()
            if resp.status_code == 200:
                print(f"  [ECHO]      \u2190 200 OK, ALIVE interval: "
                      f"{data.get('alive_interval_seconds', '?')}s")
            else:
                print(f"  [ECHO]      \u2190 REJECTED: {data}")
                running = False
        except Exception as e:
            print(f"  [HEARTBEAT] Failed to send: {e}")
            running = False


def unwatch():
    """Clean unsubscribe."""
    if subscriber_id:
        try:
            requests.post(f"{SERVER}/api/unwatch/{subscriber_id}")
            print(f"  [UNWATCH]   Unsubscribed ({subscriber_id})")
        except Exception:
            pass


def main():
    global running

    skip_alive = "--skip-alive" in sys.argv

    if skip_alive:
        print("\n  *** SKIP-ALIVE MODE ***")
        print("  Heartbeats disabled — server should evict this client.\n")

    # Start the event listener in a background thread
    listener = threading.Thread(target=listen_for_events, daemon=True)
    listener.start()

    # Start ALIVE pings (unless testing eviction)
    if not skip_alive:
        alive_thread = threading.Thread(
            target=send_alive_pings, args=(15,), daemon=True
        )
        alive_thread.start()

    # Keep running until Ctrl+C
    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n  [UNWATCH]   Terminating WATCH connection...")
        running = False
        unwatch()
        print("  Done.\n")


if __name__ == "__main__":
    main()
