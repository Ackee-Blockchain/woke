# Solc Version Manager (SVM)

Woke implements Solc Version Manager (SVM) to manage multiple installations of the *solc* compiler. Compiler versions are located in:

- `$HOME/.config/Woke/compilers` on Linux/MacOS,
- `%USERPROFILE%\Woke\compilers` on Windows.

The chosen version of *solc* is available under the `woke-solc` executable which acts as a wrapper for the *solc* executable.

!!! example

    ```console
    $ woke-solc --version
    solc, the solidity compiler commandline interface
    Version: 0.8.15+commit.e14f2714.Linux.g++
    ```

!!! info

    *solc* binaries are downloaded from the [Solidity repository](https://binaries.soliditylang.org) which limits the minimum version of *solc* that can be installed.

## Commands

All the listed commands are available under the `woke svm` subcommand (e.g. `woke svm list`).

| Command   | Description                                                                                                                                                                                                | Options                                                                               |
|:----------|:-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:--------------------------------------------------------------------------------------|
| `install` | Install the target version of *solc*. A version range may be provided which results into installation of the latest version matching the range (i.e. `woke svm install 0.7` installs the version `0.7.6`). | `--force` reinstall the version even if already installed.                            |
| `list`    | List installed versions of *solc*.                                                                                                                                                                         | `--all` list all available versions instead.                                          |
| `remove`  | Remove the target installed version of *solc*.                                                                                                                                                             | `--ignore-missing` do not raise an exception if the target  version is not installed. |
| `switch`  | Change the selected version of *solc* to the target version.                                                                                                                                               |                                                                                       |
| `use`     | Change the selected version of *solc* to the target version and install it if not already installed. A version range may be provided resulting into installation of the latest version matching the range. | `--force` reinstall the version even if already installed.                            |
