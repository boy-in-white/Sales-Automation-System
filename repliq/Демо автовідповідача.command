#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -u -B responder.py --demo
else
  /usr/bin/python3 -u -B responder.py --demo
fi
print ""
read -r "repliq_close?Натисни Enter, щоб закрити це вікно…"
