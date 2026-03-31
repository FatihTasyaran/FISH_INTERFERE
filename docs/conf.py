project = 'FISH'
copyright = '2026, TU/e IRiS'
author = 'Fatih Aktas'

extensions = [
    'myst_parser',
]

myst_enable_extensions = [
    'colon_fence',
    'deflist',
]

templates_path = ['_templates']
exclude_patterns = ['_build']

html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']

source_suffix = {
    '.rst': 'restructuredtext',
    '.md': 'markdown',
}
