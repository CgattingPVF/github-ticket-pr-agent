"""PyInstaller hook for this repository's local ``workflow`` module.

PyInstaller's contributed hooks include a hook for an unrelated PyPI package
also named ``workflow``.  An empty project hook prevents that package hook from
trying to copy distribution metadata that this local module does not have.
"""
