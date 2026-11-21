"""The Deck address the PC app accepts: a zero-padded address must match the Deck's replies."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "decky", "joycon2studio"))

import joycon2_link as jl  # noqa: E402


def make_link():
    return jl.PcLink(0, lambda: b"", lambda *a: None, lambda *a: None, lambda *a: None)


class DeckAddressTests(unittest.TestCase):
    def test_zero_padded_octets_are_read_as_decimal(self):
        self.assertEqual(jl.normalize_ipv4("192.168.001.020"), "192.168.1.20")
        self.assertEqual(jl.normalize_ipv4("010.000.000.008"), "10.0.0.8")

    def test_plain_and_spaced_addresses_are_unchanged(self):
        self.assertEqual(jl.normalize_ipv4("192.168.1.20"), "192.168.1.20")
        self.assertEqual(jl.normalize_ipv4("  10.0.0.5 "), "10.0.0.5")
        self.assertEqual(jl.normalize_ipv4("0.0.0.0"), "0.0.0.0")
        self.assertEqual(jl.normalize_ipv4("255.255.255.255"), "255.255.255.255")

    def test_anything_else_is_refused(self):
        bad_addresses = ["", "192.168.1", "192.168.1.300", "192.168.1.2.3", "192.168.1.x", "1..1.1",
                         "-1.0.0.1", "192.168.1.1234", "١٩٢.1.1.1", "192.168.1.20\t"]
        for bad in bad_addresses:
            with self.subTest(address=bad):
                with self.assertRaises(ValueError):
                    jl.normalize_ipv4(bad)

    def test_configure_stores_the_canonical_address(self):
        link = make_link()
        link.configure("192.168.001.020", jl.new_pin())
        self.assertEqual(link.deck_ip, "192.168.1.20")

    def test_configure_refuses_a_bad_address_and_keeps_the_old_one(self):
        link = make_link()
        link.configure("192.168.1.20", jl.new_pin())
        with self.assertRaises(ValueError):
            link.configure("192.168.1.300", jl.new_pin())
        self.assertEqual(link.deck_ip, "192.168.1.20")

    def test_a_refused_pairing_code_changes_nothing(self):
        link = make_link()
        link.configure("192.168.1.20", "042917")
        before = (link.pin, link.w, link.deck_ip)
        with self.assertRaises(ValueError):
            link.configure("192.168.1.20", "12ab")
        with self.assertRaises(ValueError):
            link.configure("192.168.1.300", "042917")
        self.assertEqual((link.pin, link.w, link.deck_ip), before)

    def test_check_accepts_good_values_and_stores_nothing(self):
        link = make_link()
        jl.PcLink.check("192.168.001.020", "042917")  # no error
        self.assertIsNone(link.pin)
        self.assertEqual(link.deck_ip, "")

    def test_check_refuses_bad_values(self):
        with self.assertRaises(ValueError):
            jl.PcLink.check("192.168.1.300", "042917")
        with self.assertRaises(ValueError):
            jl.PcLink.check("192.168.1.20", "12ab")


if __name__ == "__main__":
    unittest.main()
