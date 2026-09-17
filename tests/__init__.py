"""Repository test package.

This prevents an unrelated third-party ``tests`` package in site-packages from
capturing imports such as ``tests.conftest`` during full-suite collection.
"""
