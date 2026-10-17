# SPDX-License-Identifier: GPL-2.0-only
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import recover as r
from failure_detection import Candidate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.usb = self.root/'devices'; self.usb.mkdir()
        self.driver = self.root/'driver'; self.driver.mkdir()
        self.interface = self.usb/'7-1:1.0'; self.interface.mkdir()
        (self.interface/'driver').symlink_to(self.driver)
        (self.interface/'pairing').write_text('0')
        self.generation = (7,3,100)
        self.candidate = Candidate('7-1','7-1:1.0','cursor',100,0)
        self.writes = []
        self.original_write = Path.write_text
        self.patches = [patch.object(r,'USB',self.usb),patch.object(r,'DRIVER',self.driver),
                        patch.object(r,'identity',return_value=self.generation),
                        patch.object(r,'journal',return_value=[{'__CURSOR':'cursor'}]),
                        patch.object(r,'ready_for_recovery',return_value=self.generation),
                        patch.object(r.time,'monotonic',return_value=101),
                        patch.object(r.time,'sleep'),patch.object(r.os,'open',return_value=99),
                        patch.object(r.os,'close'),patch.object(r.os,'fstat',return_value=type('S',(),{'st_rdev':os.makedev(189,770)})()),
                        patch.object(Path,'write_text',new=self.write)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def write(self, path, text=None, *args, **kwargs):
        # Path method replacement is passed its path explicitly by lambda below.
        raise AssertionError('unexpected binding')

    def do_write(self, path, text, *args, **kwargs):
        if path.parent == self.driver:
            self.writes.append((path.name,text))
            if path.name == 'unbind': (self.interface/'driver').unlink()
            if path.name == 'bind':
                (self.interface/'driver').symlink_to(self.driver)
                self.original_write(self.interface/'pairing','1')
        return self.original_write(path,text,*args,**kwargs)

    def execute(self, responses):
        # Keep all writes inside this temporary fake sysfs tree.
        with patch.object(Path,'write_text',new=lambda path,text,*a,**kw:self.do_write(path,text,*a,**kw)), \
             patch.object(r,'control',side_effect=responses) as transfer:
            r.recover(self.candidate,self.generation)
            return transfer.call_args_list

    def test_success_changes_only_target_interface_and_expected_volatile_register(self):
        calls=self.execute([b'',bytes.fromhex('00000080'),bytes(4),bytes(4)])
        self.assertEqual(self.writes,[('unbind','7-1:1.0'),('bind','7-1:1.0')])
        self.assertEqual([c.args[1:] for c in calls],[(0x40,1,1,0,0),(0xc0,0x47,0,0x230,4),
                                                   (0x40,0x46,0,0x230,4),(0xc0,0x47,0,0x230,4)])

    def test_failed_control_still_rebinds(self):
        with self.assertRaises(OSError): self.execute([OSError('USB failed')])
        self.assertEqual(self.writes[-1],('bind','7-1:1.0'))

    def test_unknown_register_is_not_written_and_still_rebinds(self):
        with self.assertRaisesRegex(RuntimeError,'unexpected reset-ready'):
            self.execute([b'',bytes.fromhex('44332211')])
        self.assertEqual(self.writes,[('unbind','7-1:1.0'),('bind','7-1:1.0')])

    def test_changed_generation_aborts_before_unbind(self):
        with patch.object(r,'ready_for_recovery',return_value=(7,4,101)):
            with self.assertRaisesRegex(RuntimeError,'generation changed'): self.execute([])
        self.assertEqual(self.writes,[])

    def test_reenumeration_after_initial_snapshot_aborts_before_unbind(self):
        with patch.object(r,'journal',return_value=[{'__CURSOR':'cursor'},
                  {'MESSAGE':'usb 7-1: new high-speed USB device number 4 using xhci_hcd'}]):
            with self.assertRaisesRegex(RuntimeError,'generation changed after failure'): self.execute([])
        self.assertEqual(self.writes,[])

    def test_missing_candidate_cursor_aborts_before_unbind(self):
        with patch.object(r,'journal',return_value=[]):
            with self.assertRaisesRegex(RuntimeError,'cursor unavailable'): self.execute([])
        self.assertEqual(self.writes,[])

    def test_expired_error_aborts_before_unbind(self):
        with patch.object(r.time,'monotonic',return_value=121):
            with self.assertRaisesRegex(RuntimeError,'expired'): self.execute([])
        self.assertEqual(self.writes,[])


if __name__ == '__main__': unittest.main()

class HardwareGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name)
        self.usb=root/'usb'; self.usb.mkdir()
        self.driver=root/'driver';self.driver.mkdir()
        self.module=root/'module';self.module.mkdir()
        (self.module/'srcversion').write_text('stock')
        p=self.usb/'7-1';p.mkdir();(p/'power').mkdir()
        for key,value in {'idVendor':'045e','idProduct':'02e6','serial':r.IDENTITY[2],'busnum':'7','devnum':'3','uevent':'','power/wakeup':'disabled'}.items():
            (p/key).write_text(value)
        interface=self.usb/'7-1:1.0';interface.mkdir()
        (interface/'driver').symlink_to(self.driver)
        (interface/'pairing').write_text('0')
        (interface/'active_clients').write_text('Active clients: 0\n')
        self.candidate=Candidate('7-1','7-1:1.0','cursor',10,0,bootstrap=True,usb_device_number=3)
        self.patches=[patch.object(r,'USB',self.usb),patch.object(r,'DRIVER',self.driver),
                      patch.object(r,'MODULE',self.module),patch.object(r,'run',return_value='stock'),
                      patch.object(r.time,'monotonic',return_value=100)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_bootstrap_requires_matching_device(self):
        self.assertEqual(r.ready_for_recovery(self.candidate)[:2],(7,3))
        (self.usb/'7-1/devnum').write_text('4')
        with self.assertRaisesRegex(RuntimeError,'generation'):r.ready_for_recovery(self.candidate)
    def test_bootstrap_refuses_previously_initialized_receiver(self):
        (self.usb/'7-1/power/wakeup').write_text('enabled')
        with self.assertRaisesRegex(RuntimeError,'previously initialized'):r.ready_for_recovery(self.candidate)
    def test_connected_controller_and_custom_module_each_block_recovery(self):
        (self.usb/'7-1:1.0/active_clients').write_text('Active clients: 1\n')
        with self.assertRaisesRegex(RuntimeError,'connected'):r.ready_for_recovery(self.candidate)
        (self.usb/'7-1:1.0/active_clients').write_text('Active clients: 0\n')
        (self.module/'srcversion').write_text('experimental')
        with self.assertRaisesRegex(RuntimeError,'custom driver'):r.ready_for_recovery(self.candidate)


class PublicConfigurationTests(unittest.TestCase):
    def test_unconfigured_example_refuses_before_reading_or_writing_host_state(self):
        with patch.object(r, 'IDENTITY', ('045e', '02e6', 'SET_RECEIVER_SERIAL_HERE')), \
             patch.object(Path, 'mkdir') as mkdir, patch.object(r, 'journal') as journal:
            with self.assertRaisesRegex(RuntimeError, 'Set IDENTITY'):
                r.main()
            mkdir.assert_not_called()
            journal.assert_not_called()
