#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 -B monobank_setup.py
read -r '?Натисни Enter, щоб закрити це вікно.'
