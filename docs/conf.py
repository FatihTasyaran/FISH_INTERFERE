import os
import sys

# -- Path setup -------------------------------------------------------------
#
# Make both the runtime `fish` package and the flat `postprocess` modules
# importable so autodoc can introspect their source.
_HERE = os.path.abspath(os.path.dirname(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'python'))
# Both the repo root (so `import postprocess.model` works) and the
# `postprocess/` directory itself (so intra-package bare imports like
# `from utils import ...` still resolve) are on sys.path.
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'postprocess'))

# -- Project information ----------------------------------------------------
project = 'FISH'
copyright = '2026, TU/e IRiS'
author = 'Fatih Aktas'

# -- General configuration --------------------------------------------------
extensions = [
    'myst_parser',
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.napoleon',
    'sphinx.ext.inheritance_diagram',
    'sphinx.ext.graphviz',
    'sphinx.ext.viewcode',
    'sphinx.ext.intersphinx',
]

myst_enable_extensions = [
    'colon_fence',
    'deflist',
]

templates_path = ['_templates']
exclude_patterns = ['_build', '**/__pycache__']

source_suffix = {
    '.rst': 'restructuredtext',
    '.md': 'markdown',
}

# -- HTML output ------------------------------------------------------------
html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']

# -- autodoc / autosummary --------------------------------------------------
autosummary_generate = True

autodoc_default_options = {
    'members': True,
    'undoc-members': True,
    'show-inheritance': True,
    'special-members': '__init__',
}

# Napoleon: accept Google- (and NumPy-) style docstrings.
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False

# Mock heavy / optional runtime deps so docs build on any machine.  Every
# module in python/fish and postprocess currently imports cleanly in this
# environment, but the autodoc build may run elsewhere; list the hard
# external deps here so import fails don't break the build.
autodoc_mock_imports = [
    'pymongo',
    'influxdb_client_3',
    'lttng',
    'rclpy',
    'launch',
    'launch_ros',
    'launch_ros.actions',
    'launch_ros.descriptions',
    'launch.actions',
    'launch.utilities',
    'launch.launch_description_sources',
    'ament_index_python',
    'ros2cli',
    'babeltrace2',
    'yaml',
    'matplotlib',
    'numpy',
]

# -- intersphinx ------------------------------------------------------------
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
}

# -- graphviz / inheritance diagrams ---------------------------------------
graphviz_output_format = 'svg'
inheritance_graph_attrs = {
    'rankdir': 'TB',
    'size': '"8.0, 12.0"',
    'fontsize': 12,
    'bgcolor': 'transparent',
}
