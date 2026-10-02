"""quality-analytics — レビュー指摘を集めて分類し、レビュアー・成果物・真因の傾向を分析する."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("quality-analytics")
except PackageNotFoundError:
    __version__ = "0.0.0+local"
