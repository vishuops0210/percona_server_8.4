"""
Finds and loads plugins.

A plugin is any class in the plugins/ folder that inherits from Plugin
(see plugin.py). To ADD a plugin you simply drop a new .py file into the
plugins/ folder containing such a class -- this loader picks it up
automatically. Nothing else in the agent needs to change.
"""

import importlib
import inspect
import pkgutil

import plugins            # the plugins/ folder (a Python package)
from plugin import Plugin


def load_plugins(config):
    """Return a list of ready-to-run plugin objects found in plugins/."""
    found = []

    # Walk through every .py module inside the plugins/ package.
    for module_info in pkgutil.iter_modules(plugins.__path__, prefix="plugins."):
        module = importlib.import_module(module_info.name)

        # Look at every class defined in that module.
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            # Keep only real plugins: subclasses of Plugin, but not Plugin itself.
            if issubclass(cls, Plugin) and cls is not Plugin:
                found.append(cls(config))

    return found
