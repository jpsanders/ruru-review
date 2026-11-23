"""ruru: a pull request reviewer for code written by AI agents."""

__all__ = ["__version__"]

try:
    from ruru_review._version import version as __version__
except ImportError:  # pragma: no cover - a source tree that was never built or installed
    __version__ = "0.0.0"
