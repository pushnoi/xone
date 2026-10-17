# SPDX-License-Identifier: GPL-2.0-only
import unittest

from failure_detection import State, consume


BOOT = "boot-current"


def event(cursor, message=None, at=99, **fields):
    return dict(_BOOT_ID=BOOT, _TRANSPORT="kernel", __CURSOR=cursor,
                __MONOTONIC_TIMESTAMP=str(int(at * 1_000_000)),
                MESSAGE=message or (
                    "xone-dongle 7-1:1.0: "
                    "xone_dongle_fw_load: init radio failed: -110"), **fields)


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.state = State(BOOT)
        self.checked = []

    def run_events(self, records, guard=None, now=100, boot_id=BOOT, bootstrap=False):
        def allow(candidate):
            self.checked.append(candidate)
            return True
        return consume(records, self.state, boot_id=boot_id, now=now,
                       guard=guard or allow, bootstrap=bootstrap)

    def boot_events(self):
        return [event("enum", "usb 7-1: new high-speed USB device number 3 using xhci_hcd", at=1),
                event("failure", at=5)]

    def test_bootstrap_requires_opt_in_and_first_cursor(self):
        self.assertIsNone(self.run_events(self.boot_events(), now=40))
        # Consuming the error normally cannot be undone by enabling bootstrap.
        self.assertIsNone(self.run_events(self.boot_events(), now=40, bootstrap=True))
        self.state = State(BOOT, cursor="previous")
        self.assertIsNone(self.run_events(self.boot_events(), now=40, bootstrap=True))

    def test_bootstrap_carries_generation_and_uses_external_guard(self):
        checked = []
        def guard(candidate):
            checked.append(candidate)
            return candidate.bootstrap and candidate.usb_device_number == 3
        c = self.run_events(self.boot_events(), guard, now=40, bootstrap=True)
        self.assertEqual((c.cursor, c.bootstrap, c.usb_device_number), ("failure", True, 3))
        self.assertEqual(len(checked), 1)
        self.assertIsNone(self.run_events(self.boot_events(), now=41, bootstrap=True))

    def test_bootstrap_rejects_missing_generation_or_late_uptime(self):
        for records, now in (([event("failure", at=5)], 40),
                             (self.boot_events(), 120.01)):
            self.state = State(BOOT)
            self.assertIsNone(self.run_events(records, now=now, bootstrap=True))

    def test_bootstrap_never_widens_resume_failure_age(self):
        records = self.boot_events()
        records.insert(1, event("sleep", "PM: suspend entry (deep)", at=2))
        self.assertIsNone(self.run_events(records, now=40, bootstrap=True))

    def test_bootstrap_requires_guard_for_never_initialized_receiver(self):
        self.assertIsNone(self.run_events(self.boot_events(), lambda c: False,
                                         now=40, bootstrap=True))
        self.assertIsNone(self.state.attempted_epoch)
        self.assertEqual(self.state.cursor, "failure")

    def test_bootstrap_old_generation_invalidated_and_new_failure_can_match(self):
        records = self.boot_events() + [event("new", "usb 7-1: new high-speed USB device number 4 using xhci_hcd", at=8)]
        self.assertIsNone(self.run_events(records, now=40, bootstrap=True))
        self.state = State(BOOT)
        records.append(event("newfailure", at=10))
        c = self.run_events(records, now=40, bootstrap=True)
        self.assertEqual((c.cursor, c.usb_device_number), ("newfailure", 4))

    def test_fresh_initial_failure_does_not_need_bootstrap_extras(self):
        c = self.run_events([event("failure", at=5)], now=10, bootstrap=True)
        self.assertFalse(c.bootstrap)
        self.assertIsNone(c.usb_device_number)

    def test_exact_error_maps_interface_and_reserves_budget(self):
        c = self.run_events([event("a")])
        self.assertEqual((c.port, c.interface, c.resume_epoch),
                         ("7-1", "7-1:1.0", 0))
        self.assertEqual((self.state.cursor, self.state.attempted_epoch), ("a", 0))

    def test_prior_boot_stale_future_non_kernel_and_near_misses(self):
        records = [event("old", at=79), event("future", at=101)]
        prior = event("prior"); prior["_BOOT_ID"] = "old-boot"
        spoof = event("spoof"); spoof["_TRANSPORT"] = "stdout"
        records += [prior, spoof, event("wrong", "xone_dongle_fw_load: init radio failed: -110"),
                    event("code", "xone-dongle 7-1:1.0: xone_dongle_fw_load: init radio failed: -71")]
        self.assertIsNone(self.run_events(records))
        self.assertEqual(self.state.cursor, "code")
        self.assertEqual(self.checked, [])

    def test_bad_fields_fail_closed_and_valid_cursor_consumed(self):
        bad = event("bad"); bad["__MONOTONIC_TIMESTAMP"] = "invalid"
        no_cursor = event("gone"); del no_cursor["__CURSOR"]
        self.assertIsNone(self.run_events([bad, no_cursor]))
        self.assertEqual(self.state.cursor, "bad")

    def test_skip_or_guard_error_is_consumed_and_not_deferred(self):
        for guard in (lambda c: False, lambda c: 1 / 0):
            self.state = State(BOOT)
            self.assertIsNone(self.run_events([event("a")], guard))
            self.assertIsNone(self.run_events([event("a")]))
            self.assertIsNone(self.state.attempted_epoch)

    def test_own_failure_or_reenumerated_port_cannot_loop(self):
        self.assertIsNotNone(self.run_events([event("a")]))
        another = event("b"); another["MESSAGE"] = another["MESSAGE"].replace("7-1", "3-6.4")
        self.assertIsNone(self.run_events([another]))
        self.assertEqual(self.state.cursor, "b")
        self.assertEqual(len(self.checked), 1)

    def test_resume_allows_new_attempt_and_replay_does_not_add_epoch(self):
        self.run_events([event("a")])
        records = [event("r", "PM: suspend entry (s2idle)"), event("b")]
        self.assertEqual(self.run_events(records).resume_epoch, 1)
        self.assertIsNone(self.run_events(records))
        self.assertEqual(self.state.resume_epoch, 1)

    def test_pre_resume_failure_discarded_even_if_clock_age_is_fresh(self):
        self.assertIsNone(self.run_events([event("a"), event("r", "PM: suspend entry (s2idle)")]))
        self.assertEqual(self.checked, [])

    def test_only_latest_epoch_can_emit_and_newest_error_wins(self):
        c = self.run_events([event("a"), event("r", "PM: suspend entry (s2idle)"),
                             event("b"), event("c")])
        self.assertEqual((c.cursor, c.resume_epoch), ("c", 1))
        self.assertEqual(len(self.checked), 1)

    def test_boot_change_resets_budget_but_ignores_previous_boot(self):
        self.run_events([event("a")])
        fresh = event("b"); fresh["_BOOT_ID"] = "new-boot"
        c = self.run_events([event("a"), fresh], boot_id="new-boot")
        self.assertEqual((c.cursor, c.resume_epoch), ("b", 0))

    def test_boundary_and_dotted_usb_path(self):
        e = event("a", at=80); e["MESSAGE"] = e["MESSAGE"].replace("7-1", "3-6.4")
        self.assertEqual(self.run_events([e]).port, "3-6.4")

    def test_resume_failure_before_exit_is_retained(self):
        c = self.run_events([event("entry", "PM: suspend entry (s2idle)"),
                             event("failure"), event("exit", "PM: suspend exit")])
        self.assertEqual((c.cursor, c.resume_epoch), ("failure", 1))

    def test_reenumeration_disconnect_or_driver_reload_invalidates_error(self):
        for message in ("usb 7-1: USB disconnect, device number 3",
                        "usb 7-1: new high-speed USB device number 4 using xhci_hcd",
                        "usbcore: deregistering interface driver xone-dongle"):
            self.state = State(BOOT)
            self.assertIsNone(self.run_events([event("failure"), event("change", message)]))

    def test_other_usb_port_does_not_invalidate_failure(self):
        self.assertIsNotNone(self.run_events([event("failure"), event("other", "usb 3-3: USB disconnect, device number 2")]))

    def test_new_event_after_skipped_event_remains_eligible(self):
        self.run_events([event("a")], lambda c: False)
        self.assertIsNotNone(self.run_events([event("b")]))


if __name__ == "__main__":
    unittest.main()
