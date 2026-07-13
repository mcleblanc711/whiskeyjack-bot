"""whiskeyjack-bot: a Metaculus MiniBench forecasting pipeline built as an
attribution instrument — an immutable, replayable record of every forecast,
its evidence, and its outcome.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("whiskeyjack-bot")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0+uninstalled"
