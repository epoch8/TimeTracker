"""Epoch8 additions to TimeTracker that live outside the upstream code (see design doc 000, section 8).

Each subpackage is one feature with its own blueprint, templates and tests in tests/e8. Upstream code only
gets the one line that registers the blueprint, so merges from DRYTRIX/TimeTracker stay small.
"""
