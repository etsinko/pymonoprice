#!/usr/bin/env python

import os
import sys

VERSION = '0.4'

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup


if sys.argv[-1] == 'publish':
    os.system('python3 setup.py sdist && twine upload dist/*')
    sys.exit()


setup(
    version=VERSION,
    download_url='https://github.com/etsinko/pymonoprice/archive/{}.tar.gz'.format(VERSION),
)
