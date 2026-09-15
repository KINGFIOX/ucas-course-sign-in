"""Automatic, non-interactive UCAS course sign-in.

The one-pass sign-in routine lives in :mod:`daemon.autosign`, the hourly
scheduler in :mod:`daemon.daemon`, and the phone notifications in
:mod:`daemon.notify`. Only the scheduler is runnable (``python -m daemon`` / the
``daemon`` console script); :mod:`daemon.autosign` is a library module.
"""
