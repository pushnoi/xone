#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only
"""Bounded recovery of one explicitly configured 045e:02e6 receiver after init-radio -110.

Uses the stock driver; only this receiver's interface and volatile MCU download
state are changed. State in /run prevents old events and own failures looping.
"""
import ctypes
from dataclasses import asdict
import fcntl
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time

from failure_detection import State, consume

USB = Path('/sys/bus/usb/devices')
DRIVER = Path('/sys/bus/usb/drivers/xone-dongle')
STATE_DIR = Path('/run/xbox-receiver-recovery')
STATE_FILE = STATE_DIR/'state.json'
MODULE = Path('/sys/module/xone_dongle')
IDENTITY = ('045e', '02e6', 'SET_RECEIVER_SERIAL_HERE')


def run(*args):
    return subprocess.check_output(args, text=True, timeout=5).strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(state):
    temporary = STATE_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(asdict(state))+'\n')
    temporary.replace(STATE_FILE)


def journal(*extra):
    data = run('journalctl', '-k', '-b', '-o', 'json', '--no-pager', *extra)
    return [json.loads(line) for line in data.splitlines() if line]


def identity(port):
    p = USB/port
    require(tuple((p/x).read_text().strip() for x in ('idVendor','idProduct','serial')) == IDENTITY,
            'receiver identity does not match')
    return (int((p/'busnum').read_text()), int((p/'devnum').read_text()),
            (p/'uevent').stat().st_ino)


def ready_for_recovery(candidate):
    p = USB/candidate.interface
    require(candidate.interface == candidate.port+':1.0', 'unexpected interface')
    generation = identity(candidate.port)
    if getattr(candidate,'bootstrap',False):
        require(time.monotonic()<=120,'boot bootstrap window expired')
        require((USB/candidate.port/'power/wakeup').read_text().strip()=='disabled','bootstrap receiver previously initialized')
        require(generation[1]==candidate.usb_device_number,'bootstrap USB generation does not match enumeration')
    require((p/'driver').resolve(strict=True) == DRIVER.resolve(), 'unexpected driver')
    require((p/'pairing').read_text().strip() == '0', 'pairing active; skip healthy receiver')
    clients = (p/'active_clients').read_text().splitlines()[0]
    require(clients.strip() == 'Active clients: 0', 'connected Xbox clients; skip')
    require(not list((p).glob('gip*')), 'GIP clients present; skip')
    loaded = (MODULE/'srcversion').read_text().strip()
    require(loaded == run('modinfo','-F','srcversion','xone_dongle'), 'custom driver loaded; skip')
    return generation


class Control(ctypes.Structure):
    _fields_ = [('requesttype',ctypes.c_uint8),('request',ctypes.c_uint8),
                ('value',ctypes.c_uint16),('index',ctypes.c_uint16),
                ('length',ctypes.c_uint16),('timeout',ctypes.c_uint32),
                ('data',ctypes.c_void_p)]


def control(fd, requesttype, request, value, index, length):
    buf = ctypes.create_string_buffer(max(length,1))
    arg = Control(requesttype,request,value,index,length,1000,
                  ctypes.addressof(buf) if length else None)
    libc = ctypes.CDLL(None,use_errno=True)
    libc.ioctl.argtypes = [ctypes.c_int,ctypes.c_ulong,ctypes.c_void_p]
    libc.ioctl.restype = ctypes.c_int
    result = libc.ioctl(fd,0xc0185500,ctypes.byref(arg))
    if result < 0:
        raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
    require(result == length, 'short USB control response')
    return buf.raw[:length]


def recover(candidate, expected_generation):
    require(ctypes.sizeof(Control)==24, 'unsupported USB control ABI')
    require(ready_for_recovery(candidate)==expected_generation, 'receiver generation changed')
    max_age = 120 if getattr(candidate,'bootstrap',False) else 20
    require(0 <= time.monotonic()-candidate.monotonic_seconds <= max_age, 'failure event expired')
    if getattr(candidate,'bootstrap',False):
        require(time.monotonic() <= 120,'boot bootstrap window expired')
    intervening = journal('--cursor',candidate.cursor)
    require(intervening and intervening[0].get('__CURSOR')==candidate.cursor, 'candidate cursor unavailable')
    for event in intervening[1:]:
        message=event.get('MESSAGE','')
        require(not re.match(r'^usb '+re.escape(candidate.port)+r': (?:USB disconnect|new .* USB device number)',message), 'USB generation changed after failure')
        require(message != 'usbcore: deregistering interface driver xone-dongle','driver reloaded after failure')
        require(not message.startswith('PM: suspend entry ('),'new sleep cycle after failure')
    require(ready_for_recovery(candidate)==expected_generation,'receiver changed during journal recheck')
    interface = candidate.interface
    p = USB/candidate.port
    detached = False
    try:
        (DRIVER/'unbind').write_text(interface)
        detached = True
        require(identity(candidate.port)==expected_generation, 'receiver changed after unbind')
        require(not (USB/interface/'driver').exists(), 'driver unexpectedly rebound')
        bus, device, _ = expected_generation
        fd = os.open(f'/dev/bus/usb/{bus:03}/{device:03}',os.O_RDWR|os.O_CLOEXEC)
        try:
            require(os.fstat(fd).st_rdev==os.makedev(189,(bus-1)*128+device-1),'USB node mismatch')
            require(identity(candidate.port)==expected_generation,'receiver changed while opening')
            control(fd,0x40,1,1,0,0)  # MCU reset to ROM, as PR225/xow.
            time.sleep(.01)
            register = int.from_bytes(control(fd,0xc0,0x47,0,0x230,4),'little')
            require(register in (0,0x80000000),'unexpected reset-ready register')
            if register:
                # Same zero write as stock firmware loading, before its cold/warm
                # branch, to force the full RAM upload after the MCU ROM reset.
                control(fd,0x40,0x46,0,0x230,4)
            require(control(fd,0xc0,0x47,0,0x230,4)==bytes(4),'download marker not cleared')
        finally:
            os.close(fd)
    finally:
        if detached:
            for _ in range(50):
                try:
                    identity(candidate.port)
                    break
                except FileNotFoundError:
                    time.sleep(.2)
            identity(candidate.port)  # Refuse replacement hardware.
            if not (USB/interface/'driver').exists():
                (DRIVER/'bind').write_text(interface)
    # Recreated interface attributes cannot retain the old pairing value.
    for _ in range(60):
        if (USB/interface/'pairing').read_text().strip() == '1' or list((USB/interface).glob('gip*')):
            print(f'Recovered {interface}: stock driver radio ready; controller reconnect remains an external test',flush=True)
            return
        time.sleep(.2)
    raise RuntimeError('radio readiness not confirmed after one recovery; no automatic retry')


def main():
    require(IDENTITY[2] not in ('', 'SET_RECEIVER_SERIAL_HERE'),
            'Set IDENTITY to your own 045e:02e6 receiver serial before use')
    require(os.geteuid()==0,'root required')
    STATE_DIR.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (STATE_DIR/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip().replace('-','')
        state = State(**json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else State(boot)
        if state.boot_id != boot:
            state = State(boot)
        if '--seed' in sys.argv:
            end = journal('-n','1')
            state.cursor = end[-1]['__CURSOR'] if end else None
            save(state)
            print('Initialized at current journal end; historical failures skipped')
            return
        # A tightly guarded first-boot path handles delayed userspace startup.
        bootstrap = state.cursor is None and time.monotonic() <= 120
        records = journal('--cursor',state.cursor) if state.cursor else (journal() if bootstrap else journal('--since','20 seconds ago'))
        if state.cursor and (not records or records[0].get('__CURSOR') != state.cursor):
            end = journal('-n','1')
            state.cursor = end[-1]['__CURSOR'] if end else None
            save(state)
            print('Saved journal cursor unavailable; skipped ambiguous history')
            return
        generations = {}
        def guard(candidate):
            generations[candidate.cursor] = ready_for_recovery(candidate)
            return True
        candidate = consume(records,state,boot_id=boot,now=time.monotonic(),guard=guard,bootstrap=bootstrap)
        save(state)  # Includes reserved attempt before any USB mutation.
        if candidate is None:
            return
        time.sleep(.25)
        recover(candidate,generations[candidate.cursor])


if __name__ == '__main__':
    # Catch service stop/timeout so a detached receiver gets its finally rebind.
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(InterruptedError('service stopped')))
    main()
