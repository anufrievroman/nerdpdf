from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nerdpdf")
except PackageNotFoundError:  # running from a source checkout that isn't installed
    __version__ = "unknown"
