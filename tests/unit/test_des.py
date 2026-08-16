"""Tier 1: DES, against published vectors.

The vectors below are the long-standing NBS/FIPS DES validation values, not
values produced by this implementation. That distinction is the whole point: a
round-trip test against our own code would pass just as happily if every S-box
were transposed.

What these vectors do *not* cover is the VNC-specific bit reversal, which is
not part of DES at all. That is tested separately here, and confirmed against
a real VNC server -- an implementation we did not write -- in the container
tier.
"""

from __future__ import annotations

import unittest

from boot_err_shim.des import (
    encrypt_block,
    encrypt_ecb,
    key_schedule,
    vnc_key,
    vnc_response,
)

#: (key, plaintext, ciphertext), all hex. Published DES validation vectors.
VECTORS = [
    ("0000000000000000", "0000000000000000", "8CA64DE9C1B123A7"),
    ("FFFFFFFFFFFFFFFF", "FFFFFFFFFFFFFFFF", "7359B2163E4EDC58"),
    ("3000000000000000", "1000000000000001", "958E6E627A05557B"),
    ("1111111111111111", "1111111111111111", "F40379AB9E0EC533"),
    ("0123456789ABCDEF", "1111111111111111", "17668DFC7292532D"),
    ("1111111111111111", "0123456789ABCDEF", "8A5AE1F81AB8F2DD"),
    ("FEDCBA9876543210", "0123456789ABCDEF", "ED39D950FA74BCC4"),
    ("7CA110454A1A6E57", "01A1D6D039776742", "690F5B0D9A26939B"),
    ("0131D9619DC1376E", "5CD54CA83DEF57DA", "7A389D10354BD271"),
    ("07A1133E4A0B2686", "0248D43806F67172", "868EBB51CAB4599A"),
]


class TestPublishedVectors(unittest.TestCase):
    def test_every_vector(self) -> None:
        for key_hex, plain_hex, cipher_hex in VECTORS:
            with self.subTest(key=key_hex, plain=plain_hex):
                result = encrypt_block(
                    bytes.fromhex(plain_hex), key_schedule(bytes.fromhex(key_hex))
                )
                self.assertEqual(result.hex().upper(), cipher_hex)

    def test_the_vector_table_is_not_empty(self) -> None:
        # A table that got emptied would make the loop above vacuously green.
        self.assertGreaterEqual(len(VECTORS), 10)


class TestKeySchedule(unittest.TestCase):
    def test_sixteen_subkeys_of_forty_eight_bits(self) -> None:
        subkeys = key_schedule(b"\x01\x23\x45\x67\x89\xab\xcd\xef")
        self.assertEqual(len(subkeys), 16)
        for subkey in subkeys:
            self.assertEqual(len(subkey), 48)
            self.assertTrue(all(bit in (0, 1) for bit in subkey))

    def test_subkeys_differ_between_rounds(self) -> None:
        subkeys = key_schedule(b"\x01\x23\x45\x67\x89\xab\xcd\xef")
        self.assertEqual(len({tuple(k) for k in subkeys}), 16)

    def test_weak_key_produces_identical_subkeys(self) -> None:
        # An all-zero key is one of the four classic DES weak keys: C and D
        # are all zeros, so every rotation is a no-op. A schedule that did not
        # exhibit this would mean the rotations are wrong.
        subkeys = key_schedule(b"\x00" * 8)
        self.assertEqual(len({tuple(k) for k in subkeys}), 1)

    def test_wrong_key_length_is_rejected(self) -> None:
        for length in (0, 7, 9, 16):
            with self.subTest(length=length), self.assertRaises(ValueError):
                key_schedule(b"\x00" * length)


class TestBlockHandling(unittest.TestCase):
    def test_wrong_block_length_is_rejected(self) -> None:
        subkeys = key_schedule(b"\x00" * 8)
        for length in (0, 7, 9):
            with self.subTest(length=length), self.assertRaises(ValueError):
                encrypt_block(b"\x00" * length, subkeys)

    def test_ecb_over_two_blocks(self) -> None:
        key = bytes.fromhex("0123456789ABCDEF")
        plain = bytes.fromhex("1111111111111111") * 2
        result = encrypt_ecb(plain, key)
        # ECB: identical blocks encrypt identically. That is a weakness of the
        # mode, but RFB specifies it, so assert it holds rather than wish
        # otherwise.
        self.assertEqual(result[:8], result[8:])
        self.assertEqual(result[:8].hex().upper(), "17668DFC7292532D")

    def test_ecb_rejects_a_partial_block(self) -> None:
        with self.assertRaises(ValueError):
            encrypt_ecb(b"\x00" * 9, b"\x00" * 8)

    def test_ecb_of_nothing_is_nothing(self) -> None:
        self.assertEqual(encrypt_ecb(b"", b"\x00" * 8), b"")


class TestVncKeyQuirk(unittest.TestCase):
    """The bit reversal RFB applies to the key. Not part of DES."""

    def test_bits_are_reversed_within_each_byte(self) -> None:
        # 0x01 -> 0b00000001 -> 0b10000000 -> 0x80
        self.assertEqual(vnc_key(b"\x01"), b"\x80" + b"\x00" * 7)
        # 0x02 -> 0b00000010 -> 0b01000000 -> 0x40
        self.assertEqual(vnc_key(b"\x02"), b"\x40" + b"\x00" * 7)

    def test_palindromic_bytes_are_unchanged(self) -> None:
        # 0xFF and 0x00 reverse to themselves, so a test using only those
        # would pass even with the reversal removed entirely.
        self.assertEqual(vnc_key(b"\xff" * 8), b"\xff" * 8)

    def test_a_realistic_password(self) -> None:
        # 'p' = 0x70 = 0b01110000 -> 0b00001110 = 0x0e
        self.assertEqual(vnc_key(b"p")[0], 0x0E)

    def test_short_password_is_zero_padded(self) -> None:
        self.assertEqual(len(vnc_key(b"ab")), 8)
        self.assertEqual(vnc_key(b"ab")[2:], b"\x00" * 6)

    def test_long_password_is_truncated_to_eight(self) -> None:
        # RFB ignores anything past the eighth character. Operators who set a
        # 20-character iDRAC password should know only 8 of it matters.
        self.assertEqual(vnc_key(b"abcdefghIGNORED"), vnc_key(b"abcdefgh"))

    def test_empty_password_is_all_zero_key(self) -> None:
        self.assertEqual(vnc_key(b""), b"\x00" * 8)

    def test_reversal_is_its_own_inverse(self) -> None:
        for password in (b"secret12", b"\x01\x23\x45\x67\x89\xab\xcd\xef"):
            with self.subTest(password=password):
                self.assertEqual(vnc_key(vnc_key(password)), password)


class TestVncResponse(unittest.TestCase):
    def test_response_is_sixteen_bytes(self) -> None:
        self.assertEqual(len(vnc_response(b"\x00" * 16, b"secret")), 16)

    def test_challenge_must_be_sixteen_bytes(self) -> None:
        for length in (0, 8, 15, 17, 32):
            with self.subTest(length=length), self.assertRaises(ValueError):
                vnc_response(b"\x00" * length, b"secret")

    def test_different_passwords_give_different_responses(self) -> None:
        challenge = bytes(range(16))
        self.assertNotEqual(
            vnc_response(challenge, b"alpha"), vnc_response(challenge, b"bravo")
        )

    def test_different_challenges_give_different_responses(self) -> None:
        self.assertNotEqual(
            vnc_response(bytes(range(16)), b"same"),
            vnc_response(bytes(range(16, 32)), b"same"),
        )

    def test_it_is_deterministic(self) -> None:
        challenge = bytes(range(16))
        self.assertEqual(
            vnc_response(challenge, b"secret"), vnc_response(challenge, b"secret")
        )

    def test_the_reversal_actually_changes_the_answer(self) -> None:
        # Guards against someone "simplifying" vnc_key into a plain pad. The
        # failure mode is silent locally and total against a real server.
        challenge = bytes(range(16))
        without_reversal = encrypt_ecb(challenge, b"secret".ljust(8, b"\x00"))
        self.assertNotEqual(vnc_response(challenge, b"secret"), without_reversal)


if __name__ == "__main__":
    unittest.main()
