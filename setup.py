from setuptools import setup, find_packages
from pathlib import Path

setup(
    name="xuloom",
    version="0.1.36",
    description="Xuloom — General-purpose Agent framework for desktop and CLI",
    long_description=Path(__file__).with_name("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    url="https://github.com/Dheudb/xuloom",
    license="MIT",
    packages=find_packages(include=["xuloom", "xuloom.*"]),
    package_data={"xuloom.skills": ["builtin/*/SKILL.md", "builtin/*/agents/*.md"]},
    python_requires=">=3.9",
    install_requires=[
        "anthropic>=0.40.0",
        "openai>=1.0.0",
        "rich>=13.0.0",
        "prompt_toolkit>=3.0,<4",
        "pydantic>=2.0.0",
        "httpx",
        "beautifulsoup4",
        "lxml",
        "ddgs>=9.16,<10; python_version >= '3.10'",
        "duckduckgo-search; python_version < '3.10'",
        "gitpython",
        "wcwidth",
        "pymupdf",
        "Pillow>=10.0",
        "PyYAML>=6.0",
    ],
    entry_points={
        "console_scripts": [
            "xuloom=xuloom.main:main",
        ],
    },
    extras_require={
        "dev": ["pyflakes>=3.4,<5", "paramiko>=3.4,<6"],
        "mcp": ["mcp>=1.30,<2; python_version >= '3.10'"],
        "secrets": ["cryptography>=41,<47", "SecretStorage>=3.3,<4; sys_platform == 'linux'"],
        "browser": ["playwright>=1.40"],
        "tokenizers": ["tiktoken", "transformers>=4.0"],
    },
)
