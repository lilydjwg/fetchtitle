#!/usr/bin/env python3

from setuptools import setup, find_packages
import fetchtitle

setup(
  name = 'fetchtitle',
  version = fetchtitle.__version__,
  packages = find_packages(),
  install_requires = ['aiohttp', 'async_timeout'],

  author = 'lilydjwg',
  author_email = 'lilydjwg@gmail.com',
  description = 'Asynchronized URL information retriever',
  license = 'GPLv2',
  keywords = 'webpage title parser http url',
  url = fetchtitle.__url__,
)
