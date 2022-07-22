# Woke

Woke is a Python-based development and testing framework for Solidity.

## Features
- LSP server
- property-based fuzzer

## Installation
Woke requires Python 3.7 or higher.

### Using pip

```shell
pip3 install abch-woke
```

The property-based fuzzer can be installed as an extra dependency. Due to the dependency on [eth-brownie](https://eth-brownie.readthedocs.io), it is recommended to install it into a [virtual environment](https://docs.python.org/3/library/venv.html).

```shell
pip3 install abch-woke[fuzzer]
```
