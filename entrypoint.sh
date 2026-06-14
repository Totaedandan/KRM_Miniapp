#!/bin/bash
# Удаляем старый lock файл Xvfb если остался
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb :99 -screen 0 1280x1200x24 -ac &
XVFB_PID=$!
sleep 2

if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "ERROR: Xvfb failed to start"
    exit 1
fi

export DISPLAY=:99
echo "Xvfb started (pid=$XVFB_PID), DISPLAY=:99"

exec python main.py