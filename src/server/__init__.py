"""Automatic, non-interactive UCAS course sign-in.

The one-pass sign-in routine lives in :mod:`server.autosign`, the hourly
scheduler in :mod:`server.server`, and the phone notifications in
:mod:`server.notify`. Only the scheduler is runnable (``python -m server`` / the
``server`` console script); :mod:`server.autosign` is a library module.
"""
