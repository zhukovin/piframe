#!/bin/bash
# Run this from your Mac to deploy without SSHing in yourself: connects to
# the Pi and runs its own deploy.sh (git pull --ff-only + restart the
# service). -t allocates a pty so sudo's password prompt still works
# over the SSH session if needed.
set -e

PI_HOST="pi@rpi"
PI_DIR="~/py-frame"

ssh -t "$PI_HOST" "cd $PI_DIR && ./deploy.sh"
