# Raspberry Pi slideshow 

This project is to create a photo frame driven by Raspberry Pi connected to an external 
generic monitor. The Python code reads in a file that lists photos to display.
The photos are read from a remote NAS on the same LAN via an NFS mounted folder.


WiFi or Ethernet config of the RPi is out of scope of this guide. Just configure networking
that you prefer and put it on the same LAN as the NAS that stores the photos.

## RPi SSH Authentication

User: pi  
Pass: pi

## Testing

This project includes a comprehensive test suite with **70% coverage** and **48 tests** covering all critical business logic.

**Quick Start:**
```bash
# Install dependencies (includes testing tools)
pip3 install -r requirements.txt

# Run tests
pytest test_py_frame.py test_web_server.py -v

# View coverage report
pytest --cov=. --cov-report=html
open htmlcov/index.html
```

**Documentation:**
- [TESTING_QUICKSTART.md](TESTING_QUICKSTART.md) - Quick guide for running tests
- [TESTING.md](TESTING.md) - Complete coverage analysis
- [COVERAGE_SUMMARY.md](COVERAGE_SUMMARY.md) - Visual coverage overview
- [FINAL_REPORT.md](FINAL_REPORT.md) - Executive summary

## How to use the photo frame
On your mobile device open this in your browser to control the slideshow:
```
http://rpi:7654
```
If you see a photo that you would rather skip next time, mark it (each slot shows a thumbnail
so you can tell them apart) and hit Unmark to undo. A mark takes effect immediately, but only
becomes permanent once that photo scrolls out of reach (the last few screens, via Prev) --
until then, navigating back to it still shows it marked and Unmark reverses it. Marking a photo
automatically pauses the slideshow, so it won't change out from under you while you're
reviewing/marking -- hit Play when you're done to resume. The page updates instantly (no
manual refresh) as the frame advances.

The screen goes automatically dark at 22:00 and goes back on at 7:00 in the morning.
You can manually turn it on and off at any moment.

## Configuration (`py-frame.conf`)

An optional INI-style file, read once at startup:

```
[schedule]
start = 22:00
end = 7:00

[display]
shuffle = true
```

- `[schedule]` controls the night auto screen-off window (see above); a wrap-past-midnight
  window like `22:00 -> 07:00` is supported.
- `[display] shuffle` sets the photo display order: `true` fully randomizes it, `false` keeps
  the list's original order but starts from a random point and wraps around. This replaced the
  old web UI Shuffle/Random Start toggle -- it's config-only now, applied once at startup, so
  restart `py-frame.service` after changing it.

A missing file, section, or key falls back to sensible defaults rather than blocking startup.

## Install required Python libraries

**Option 1: Using requirements.txt (Recommended)**
```bash
pip3 install -r requirements.txt
```

**Option 2: Manual installation**
```bash
pip3 install pillow pygame flask
```

For development and testing, also install:
```bash
pip3 install pytest pytest-cov coverage
``` 

## Get the code on RPi

Clone the repo directly into the target folder:
```
git clone https://github.com/zhukovin/py-frame.git ~/py-frame
cd ~/py-frame
```

No SSH keys or GitHub auth needed — the repo is public and the RPi only ever pulls, never pushes.

### Updating the code later
```
cd ~/py-frame
git pull --ff-only
sudo systemctl restart py-frame.service
```
`git pull --ff-only` always fetches the exact latest commit (unlike curling the raw files,
which can briefly serve a stale cached copy right after a push), and updates every file in one
step. The `--ff-only` refuses to create a merge commit or silently rewrite history if the RPi's
local copy has diverged (e.g. someone edited a file directly on the RPi) — it just fails loudly
instead, so you notice and can decide how to reconcile it.

## Mount NAS photo folder using NFS

### Enable and configure NFS on NAS

* Login to NAS web UI and navigate to `Control Panel - File Services - NFS`.
* Enable NFS service and select NFSv3 (it might work with up to v4.1, but I did not try).
* Keep Advanced Settings as they are

![NFS Settings](./pictures/nfs.jpg)

### Configure Shared Folder NFS Permissions

* Click Shared Folder on the NFS settings page.
* Right-click on `photo` folder, choose `Edit` and go to `NFS Permissions`.
* Create a rule:
  * Hostname or IP: 192.168.1.201 (also try using `rpi` host name) -
     this is your RPi's IP.
  * Privilege: Read only
  * Squash: Map all users to admin (admin must have access to `photo`; see below)
  * Security: sys
  * Enable asynchronous
  * The rest of the settings might be not needed, but I set them too. 

![NFS Permissions](./pictures/nfs-perms.jpg)

### Make sure `admin` can access the shared folder

Since all incoming user names (like `pi`) are mapped/squashed to nasus' `admin`,
the `admin` must have access to photo folder:

![Admin Permissions](./pictures/admin-perms.jpg)

### Mount NAS NFS on RPi

#### First, try it manually
On `RPi` add NAS to hosts to force IPv4 resolution. For some reason,
`host nasus` on my RPi resolves to IPv6 only that does not works with NFS v3.
```
sudo nano /etc/hosts
```
Add:
```
192.168.1.189   nasus
```
Make sure the mountpoint exists:
```
sudo mkdir -p /mnt/nasus/photo
```
On `RPi` run:
```
sudo mount -t nfs -o vers=3 nasus:/volume1/photo /mnt/nasus/photo
```
Check that NFS  mount works and files are visible:
```
ls /mnt/nasus/photo
```
You should see photo's content and no errors.

#### Auto-mount NAS photo folder on RPi boot
Create a systemd service that waits for network + NAS, then mounts:
```
sudo nano /etc/systemd/system/nasus-photo-mount.service
```
Paste this:
```
[Unit]
Description=Mount NAS photo share after network is ready
After=network.target
Wants=network.target

[Service]
Type=oneshot

# Wait for a default route AND NAS to respond, then mount.
# Retries for ~5 minutes.
ExecStartPre=/bin/sh -c '\
  for i in $(seq 1 300); do \
    ip route | grep -q "^default" || { sleep 1; continue; }; \
    ping -4 -c1 -W1 192.168.1.189 >/dev/null 2>&1 && exit 0; \
    sleep 1; \
  done; \
  exit 1'

# Don’t remount if already mounted
ExecStart=/bin/sh -c 'mountpoint -q /mnt/nasus/photo || /bin/mount -t nfs -o vers=3,_netdev,noatime,nolock,tcp,soft,timeo=50,retrans=2 nasus:/volume1/photo /mnt/nasus/photo'

RemainAfterExit=yes

# If NAS wasn’t ready in time, keep retrying in the background after boot
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Enable it:
```
sudo systemctl daemon-reload
sudo systemctl enable --now nasus-photo-mount.service
```
Check:
```
systemctl status nasus-photo-mount.service
mount | grep /mnt/nasus/photo || true
ls /mnt/nasus/photo
```

Reboot RPi and check if you can still list photo files.

After reboot run this for diagnostics:
```
systemctl status nasus-photo-mount.service
journalctl -u nasus-photo-mount.service -b --no-pager
mount | grep /mnt/nasus/photo || true
```

### Link mounted NAS folder to the target folder

The photos are listed as relative paths starting with `nasus/photo/...` in
both `photo.xxx.list` and `exclusions.txt`. This means that we need to map `nasus`
folder inside the target folder.

```
ln -s /mnt/nasus ~/py-frame/nasus
```

Check that it is mapped correctly:

```
ls -l ~/py-frame
```

You should see:
```
lrwxrwxrwx 1 pi pi      10 Dec 12 16:13 nasus -> /mnt/nasus
```

Check that photos are accessible:
```
ls -l ~/py-frame/nasus/photo
```

You should see something like:
```
total 1264
drwxrwxrwx  33 1026 users   4096 Mar 16  2023  2002
drwxrwxrwx 133 1026 users  12288 Dec  7  2024  2003
drwxrwxrwx 105 1026 users   4096 May 18  2022  2004
drwxrwxrwx  69 1026 users   4096 Mar 16  2023  2005
```

## Make a list of photos to display
This configuration uses relative paths, but absolute paths might be used as well.
Just be mindful about paths recorded in exclusions.txt if you decide to change
how you list the files (old paths might not work and might need migration).

Improvise from here:
```
find nasus/photo/Camera\ Media/Camera\ Alexey -type f \( -iname '*.jpg' -o -iname '*.jpeg' \) >photo.alex.list
```
or
```
ls nasus/photo/Camera\ Media/Camera\ Irina/20{10,11,12,13,14,15,16,17}/*.{jpg,JPG,jpeg,JPEG} > photo.irina.list
ls nasus/photo/Camera\ Media/Camera\ Irina/20{18,19,20,21,22,23,24,25}/*.{jpg,JPG,jpeg,JPEG} >> photo.irina.list
```

The list is read from `photo.list` file.

## Make slideshow start on RPi boot

### Step 1
Create file py-frame.service:

```
sudo nano /etc/systemd/system/py-frame.service
```

with this content:

```
[Unit]
Description=Raspberry Pi Photo Frame Slideshow (console)
After=network.target local-fs.target
Conflicts=getty@tty1.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/py-frame
ExecStart=/usr/bin/python /home/pi/py-frame/py_frame.py /home/pi/py-frame/photo.list

# Bind the service to the main console (tty1)
StandardInput=tty
StandardOutput=tty
StandardError=journal
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes

# Tell SDL/pygame to use the framebuffer
Environment=SDL_VIDEODRIVER=fbcon
Environment=SDL_FBDEV=/dev/fb0
Environment=SDL_NOMOUSE=1

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Step 2
Disable TTY1:

```
sudo systemctl disable getty@tty1.service
```

### Step 3
```
sudo systemctl enable py-frame.service
```
Reboot to check if slideshow starts on boot.

```
reboot
```

### Useful commands
After changing `py-frame.service` reload it:
```
sudo systemctl daemon-reload
```

To start/stop the service:
```
sudo systemctl start py-frame.service
sudo systemctl stop  py-frame.service
```

Get status:
```
sudo systemctl status py-frame.service --no-pager
```

# Troubleshooting

## Fix intermittent WiFi drops by pinning to a specific band (e.g. 2.4GHz)

Symptom: the frame freezes on the last photo with `Drive: DISCONNECTED` in the status
corner, and the Pi itself stops responding to `ping`/SSH for a while before recovering on
its own. If your router is dual/tri-band (2.4GHz + 5GHz, sometimes + 6GHz) advertising the
same SSID on all of them, it band-steers clients between radios -- and if the Pi ends up on
a higher band with a weak signal at its physical location, it'll flap. Pinning `wpa_supplicant`
to one specific radio's BSSID (MAC address) stops the router from moving it around.

### 1. Find the BSSID (MAC address) of each band

From the Pi itself, scan for every radio broadcasting your SSID:
```bash
sudo iw dev wlan0 scan | grep -E "^BSS|SSID:|freq:|signal:"
```
Each result block is one radio/band of your router (same SSID, different `BSS` line = different
BSSID). `freq: 2412`-`2484` is 2.4GHz, `freq: 5xxx` is 5GHz, `freq: 6xxx` is 6GHz. Note the
`BSS <mac address>` line for the band you want (2.4GHz has the best range/wall penetration,
so it's usually the right choice for a stationary device like this frame).

Alternatively, your router's admin UI usually lists a separate BSSID per band on its wireless
clients/status page -- that's often easier to read than a raw scan.

### 2. Pin `wpa_supplicant` to that BSSID

```bash
sudo nano /etc/wpa_supplicant/wpa_supplicant.conf
```
Add a `bssid` line inside your existing `network={...}` block (leave `ssid`/`psk`/everything
else as-is):
```
network={
    ssid="YourNetworkName"
    psk=...
    bssid=3c:bd:c5:33:67:4a
    key_mgmt=WPA-PSK
}
```

Apply it without a reboot:
```bash
sudo wpa_cli -i wlan0 reconfigure
```

### 3. Verify

```bash
iw dev wlan0 link
```
Confirm `Connected to <the bssid you pinned>`, a frequency in the expected band, and check
`signal:` -- anything better than about -70 dBm is a solid, stable link. This survives reboots
since it's read from `wpa_supplicant.conf` at boot.

```
dmesg -T | tail -n 100
watch -n 5 free -h
pgrep python
vcgencmd measure_volts
vcgencmd measure_temp
stress --cpu 4 --timeout 3000
hostname -I
arp -a
ifconfig getifaddr wlan0
rfkill list
sudo rfkill unblock all
systemctl status mnt-nasus-photo.mount
journalctl -u mnt-nasus-photo.mount -b
```
Enable SSH on RPi:
```
sudo raspi-config
```

Measure file download speed:
```
dd if=~/nasus/photo/2020/VID_20200120_083451.mp4 of=/dev/null bs=4M status=progress
```
