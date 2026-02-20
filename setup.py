#!/usr/bin/env python3
"""
Setup script for Battlezone Redux Lobby Monitor
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="bzr-monitor",
    version="1.0.0",
    author="GrizzlyOne95",
    description="A comprehensive external tool for monitoring Battlezone 98 Redux lobbies",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/GrizzlyOne95/Battlezone_LobbyMonitor",
    packages=find_packages(),
    py_modules=["bzr_monitor"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Development Status :: 4 - Beta",
        "Intended Audience :: End Users/Desktop",
        "Topic :: Games/Entertainment",
    ],
    python_requires=">=3.6",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "bzr-monitor=bzr_monitor:main",
        ],
    },
    include_package_data=True,
    package_data={
        "": [],
    },
)
