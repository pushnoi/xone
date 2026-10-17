# Receiver recovery helper

Optional recovery/test tool for the `045e:02e6` adapter and the exact error
`xone_dongle_fw_load: init radio failed: -110`. This accompanies
[PR #225](https://github.com/dlundqvist/xone/pull/225); the MCU reset/full-load
approach comes from that PR. It does not modify the kernel driver or participate
in the normal xone build/install process.

Tested on one x86-64 Bazzite machine with MT76 `0x7612`, firmware build
`201509162123`, image `44.20261012.1`, kernel `7.2.9-ogc3.1.fc44.x86_64` and
packaged xonedo source `982cbcb019ae4d2bee5ae69385223409ee555c88`.
It is not a general adapter fix. In particular, `045e:02fe`/`0x7613` is not
supported or tested. The PR now has a separate failure report for that model.

## What was tested

The packaged driver repeatedly selected the warm firmware path and failed
radio initialization. Rebinding, USB reset and increasing the timeout did not
recover it. Testing the change from PR #225 restored the radio and an existing
controller pairing.

A separate userspace experiment then reset the MCU while the receiver's driver
interface was unbound. Register `0x230` returned `0x80000000`; clearing that
volatile marker to zero made the packaged driver take the full firmware-load
path on rebind. The helper below automates that experiment only after the
specific failure. It does not write firmware files, EEPROM or pairing storage.

The installed timer recovered a reproduced failure. Both paired controllers
later reconnected across a reboot and after normal power-on following deep
S3 sleep, using the packaged driver and helper. Those healthy boot/resume
cycles did not require recovery. They are not tests of the PR build.
The public copy only replaces the private target serial with a placeholder,
adds an unconfigured-target refusal, and updates comments/license metadata.
The reset/detection logic is unchanged. The original had 31 passing unit tests
on Bazzite; this public copy has 32 passing unit tests including the new refusal.

## Read and test without touching a receiver

Python 3.10+ is required. From this directory, without sudo:

```sh
python3 -m unittest discover -v
```

Tests use temporary fake sysfs trees and mocked USB transfers. They do not
exercise hardware. `recover.py` is the host-changing entry point; inspect it
before running as root.

The service polls the kernel journal every five seconds. Recovery is limited
to one attempt per boot/sleep epoch, for one explicitly selected serial. It
checks the bound driver, no active pairing or Xbox clients, USB generation,
and loaded/installed module source agreement. It rechecks before unbinding.
The usual error age limit is 20 seconds; a guarded initial boot scan can handle
an error up to 120 seconds old. It will not reset a healthy idle receiver.
It attempts to rebind in `finally`, including on SIGTERM; SIGKILL or power loss
cannot run that cleanup. The hardcoded usbfs ioctl ABI is limited to the tested
64-bit layout, checked before any USB mutation.

This targets the observed driver's sysfs attributes and log format. Missing
attributes or a different error will skip recovery. A matching module
srcversion only means the loaded and installed modules agree; it does not
prove compatibility with every future driver version.

## Configure and install on the affected host

Keep controllers disconnected for the initial setup. Read the actual adapter's
`idVendor`, `idProduct` and `serial` under `/sys/bus/usb/devices/`. Edit only the
third value of `IDENTITY` in `recover.py`, replacing
`SET_RECEIVER_SERIAL_HERE` with that receiver's serial. Keep `045e` and `02e6`
unchanged. The default refuses to run. No port or bus number is hardcoded in
the recovery target; those come from the failure event and live checks.

These commands install the helper only, not the driver:

```sh
sudo install -d -m 0755 /usr/local/lib/xbox-receiver-recovery
sudo install -m 0644 recover.py failure_detection.py /usr/local/lib/xbox-receiver-recovery/
sudo install -m 0644 xbox-receiver-recovery.service xbox-receiver-recovery.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/xbox-receiver-recovery.service /etc/systemd/system/xbox-receiver-recovery.timer
sudo /usr/bin/python3 /usr/local/lib/xbox-receiver-recovery/recover.py --seed
sudo systemctl daemon-reload
sudo systemctl enable --now xbox-receiver-recovery.timer
```

`--seed` skips existing journal history when first installed. It does not
repair an already failed receiver from an old event. After a reboot the helper
uses a new boot ID and can detect a new failure. Do not delete its runtime state
to force retries. Test the normal failure scenario when interruption is safe,
then verify actual controller reconnection; a ready radio alone is insufficient.

On the tested Bazzite layout `/usr/local` resolves to `/var/usrlocal`, and the
units and enablement live in `/etc`. These survive normal forward image updates
([OSTree documentation](https://ostreedev.github.io/ostree/atomic-upgrades/)).
No custom kernel module is installed. This does not guarantee future runtime
compatibility or units on a historical deployment predating installation.

## Inspect or remove

```sh
systemctl status xbox-receiver-recovery.timer --no-pager
sudo journalctl -b -u xbox-receiver-recovery.service --no-pager
sudo cat /run/xbox-receiver-recovery/state.json
```

To stop future checks:

```sh
sudo systemctl disable --now xbox-receiver-recovery.timer
systemctl is-active xbox-receiver-recovery.service
```

Wait until the oneshot service is inactive before removing its files. Do not
interrupt a recovery in progress. Then:

```sh
sudo rm /etc/systemd/system/xbox-receiver-recovery.service /etc/systemd/system/xbox-receiver-recovery.timer
sudo rm /usr/local/lib/xbox-receiver-recovery/recover.py /usr/local/lib/xbox-receiver-recovery/failure_detection.py
sudo systemctl daemon-reload
```

The stock driver and pairing remain in place. Runtime state disappears on reboot.

Licensed under GPL-2.0-only; see the repository's `LICENSE`.
