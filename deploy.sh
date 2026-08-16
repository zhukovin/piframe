set -e

git pull --ff-only
sudo systemctl restart piframe.service
