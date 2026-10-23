from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("dbt-pane")
except PackageNotFoundError:
    __version__ = "0.0.0"
