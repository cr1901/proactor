# Usage

1. Compile `echo.exe` (requires `rustc` and `cargo`)
  ```
  cd $ROOT/echo
  cargo build --release
  cargo install --path . --root $ROOT
  ```
  * `echo.exe` should be in the `bin/` folder for this script to work.

2. Run `python3 proactor.py`. The script should hang during exit, regardless
  of whether the `msvc` or `mingw` python is used.
  * Use `python3 proactor.py -w` to enable a workaround that will allow the
  script to exit cleanly.
