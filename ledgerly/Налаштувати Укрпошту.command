#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -B ukrposhta_setup.py
else
  python3 -B ukrposhta_setup.py
fi
read -r '?Натисни Enter, щоб закрити це вікно.'
