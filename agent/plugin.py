"""
The Plugin base class -- the "contract" every plugin follows.

A plugin does its work inside run(), and must stop when the stop flag is set.
The agent core runs each plugin's run() in its own background thread.
"""


class Plugin:
    # A short name shown in logs. Each plugin overrides this.
    name = "unnamed"

    def __init__(self, config):
        # The whole config dictionary is handed to every plugin, so a plugin
        # can read its own section from it.
        self.config = config

    def run(self, stop_event):
        """
        Do the plugin's work until stop_event is set.

        stop_event is an on/off flag. Loop while it is OFF, and return as soon
        as it turns ON, so the agent can shut down cleanly.
        """
        raise NotImplementedError("each plugin must implement run()")
