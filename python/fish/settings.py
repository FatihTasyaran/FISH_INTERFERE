"""
FISH settings reader.

Reads configuration from fish_settings.ini.
Shared by all FISH Python modules.
"""

import configparser
import os
from pathlib import Path

_SETTINGS_SEARCH_PATHS = [
    "/opt/ros/humble/fish/fish_settings.ini",
    os.path.expanduser("~/fish_interfere/fish_settings.ini"),
    os.path.join(os.path.dirname(__file__), "..", "..", "fish_settings.ini"),
]


def _find_settings_file() -> str:
    for path in _SETTINGS_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return ""


def load_settings() -> configparser.ConfigParser:
    config = configparser.ConfigParser()

    config.read_dict({
        "nsys": {
            "trace": "cuda,nvtx",
            "cuda_memory_usage": "true",
            "cudabacktrace": "kernel,memory,sync",
            "python_backtrace": "cuda",
            "python_sampling": "true",
            "pytorch": "autograd-nvtx",
            "sample": "process-tree",
            "cpuctxsw": "none",
            "export": "sqlite",
        },
        "daemon": {
            "poll_interval": "1.0",
            "settle_time": "3.0",
            "nsys_report_timeout": "120",
        },
        "trace": {
            "output_dir": "~/fish_traces",
        },
    })

    path = _find_settings_file()
    if path:
        config.read(path)

    return config


settings = load_settings()
