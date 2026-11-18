"""sebside: a tmux sidebar of cards."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sebside-tmux")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0+unknown"
