from setuptools import setup, find_packages

setup(
    name="codewatch",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "radon>=6.0",
        "networkx>=3.0",
        "pydantic>=2.0",
        "click>=8.0",
        "pyyaml>=6.0",
        "python-dotenv>=1.0",
        "anthropic>=0.25",
        "openai>=1.0",
        "httpx>=0.27",
    ],
    entry_points={
        "console_scripts": [
            "codewatch=codewatch.cli:cli",
        ],
    },
    python_requires=">=3.11",
)
