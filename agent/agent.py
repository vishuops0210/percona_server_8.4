#!/usr/bin/env python3
"""
Verified Restore agent.

The agent is a small core plus plugins. The core does very little on its own:
it loads the configuration, loads every plugin it finds in the plugins/ folder,
starts each one in the background, and then waits until you stop it with Ctrl+C.

All real work lives in plugins. The first plugin is "heartbeat", which sends a
small "I am alive" message to a URL on a schedule.
"""

import argparse
import json
import os
import signal
import threading

import plugins_loader


def load_config(path):
    """Read the JSON config file. If it does not exist, return an empty config."""
    if not os.path.exists(path):
        print(f"[agent] config file '{path}' not found, using defaults")
        return {}
    with open(path, "r") as f:
        return json.load(f)


def main():
    # --config lets you point the agent at a different config file.
    parser = argparse.ArgumentParser(description="Verified Restore agent")
    parser.add_argument("--config", default="config.json", help="path to the config file")
    args = parser.parse_args()

    print("Verified Restore agent starting...")
    config = load_config(args.config)

    # "stop" is an on/off flag, starts OFF. Plugins watch it; when it turns ON,
    # everything shuts down.
    stop = threading.Event()

    def handle_stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, handle_stop)   # Ctrl+C
    signal.signal(signal.SIGTERM, handle_stop)  # system / service stop

    # Find and create every plugin in the plugins/ folder.
    loaded = plugins_loader.load_plugins(config)
    if loaded:
        print(f"[agent] loaded plugins: {', '.join(p.name for p in loaded)}")
    else:
        print("[agent] no plugins found")

    # Start each plugin in its own background thread.
    threads = []
    for plugin in loaded:
        t = threading.Thread(target=plugin.run, args=(stop,), name=plugin.name, daemon=True)
        t.start()
        threads.append(t)

    print("Agent is running. Press Ctrl+C to stop.")
    stop.wait()  # wait here until Ctrl+C / SIGTERM

    print("\nAgent stopping...")
    # Give the plugins a moment to finish their current loop cleanly.
    for t in threads:
        t.join(timeout=5)
    print("Goodbye.")


if __name__ == "__main__":
    main()
