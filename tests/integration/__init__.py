"""Integration tier: exercises the CLI end to end over the service and provider layers.

Subprocess, transport, and provider boundaries are faked -- nothing here needs a phone or
a network. Discovered by the normal `python -m unittest discover -s tests` run because
this directory is a package; see tests/README.md for the tier definitions.
"""
