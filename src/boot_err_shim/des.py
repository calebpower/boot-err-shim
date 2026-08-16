"""DES, for VNC authentication only.

The standard library has no DES, and RFB security type 2 needs it: the server
sends a 16-byte challenge, the client encrypts it with the password as key, and
returns the 16-byte result.

Do not reach for this as a general cipher. DES is comprehensively broken and is
present here solely because the RFB protocol specifies it; iDRAC will not
negotiate anything better on this security type.

**The VNC quirk.** RFB reverses the bit order within each byte of the key
before use -- a detail widely believed to have started as a bug in an early
implementation and then become load-bearing. Get it wrong and authentication
fails against every real server while a round-trip test against your own code
passes happily, which is why :func:`vnc_key_schedule` is tested separately from
the cipher.
"""

from __future__ import annotations

# -- permutation tables (FIPS 46-3) -------------------------------------

_PC1 = (
    57, 49, 41, 33, 25, 17,  9,
     1, 58, 50, 42, 34, 26, 18,
    10,  2, 59, 51, 43, 35, 27,
    19, 11,  3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15,
     7, 62, 54, 46, 38, 30, 22,
    14,  6, 61, 53, 45, 37, 29,
    21, 13,  5, 28, 20, 12,  4,
)  # fmt: skip

_PC2 = (
    14, 17, 11, 24,  1,  5,
     3, 28, 15,  6, 21, 10,
    23, 19, 12,  4, 26,  8,
    16,  7, 27, 20, 13,  2,
    41, 52, 31, 37, 47, 55,
    30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53,
    46, 42, 50, 36, 29, 32,
)  # fmt: skip

_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)

_IP = (
    58, 50, 42, 34, 26, 18, 10, 2,
    60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6,
    64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17,  9, 1,
    59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5,
    63, 55, 47, 39, 31, 23, 15, 7,
)  # fmt: skip

_IP_INV = (
    40, 8, 48, 16, 56, 24, 64, 32,
    39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30,
    37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28,
    35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26,
    33, 1, 41,  9, 49, 17, 57, 25,
)  # fmt: skip

_E = (
    32,  1,  2,  3,  4,  5,
     4,  5,  6,  7,  8,  9,
     8,  9, 10, 11, 12, 13,
    12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21,
    20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29,
    28, 29, 30, 31, 32,  1,
)  # fmt: skip

_P = (
    16,  7, 20, 21, 29, 12, 28, 17,
     1, 15, 23, 26,  5, 18, 31, 10,
     2,  8, 24, 14, 32, 27,  3,  9,
    19, 13, 30,  6, 22, 11,  4, 25,
)  # fmt: skip

_S_BOXES = (
    (
        (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7),
        (0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8),
        (4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0),
        (15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    ),
    (
        (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10),
        (3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5),
        (0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15),
        (13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    ),
    (
        (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8),
        (13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1),
        (13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7),
        (1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    ),
    (
        (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15),
        (13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9),
        (10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4),
        (3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    ),
    (
        (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9),
        (14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6),
        (4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14),
        (11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    ),
    (
        (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11),
        (10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8),
        (9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6),
        (4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    ),
    (
        (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1),
        (13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6),
        (1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2),
        (6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    ),
    (
        (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7),
        (1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2),
        (7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8),
        (2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
    ),
)

BLOCK_SIZE = 8


def _bits(data: bytes) -> list[int]:
    out: list[int] = []
    for byte in data:
        out.extend((byte >> shift) & 1 for shift in range(7, -1, -1))
    return out


def _unbits(bits: list[int]) -> bytes:
    out = bytearray()
    for index in range(0, len(bits), 8):
        byte = 0
        for bit in bits[index : index + 8]:
            byte = (byte << 1) | bit
        out.append(byte)
    return bytes(out)


def _permute(bits: list[int], table: tuple[int, ...]) -> list[int]:
    return [bits[position - 1] for position in table]


def key_schedule(key: bytes) -> list[list[int]]:
    """Sixteen 48-bit subkeys from an 8-byte key."""
    if len(key) != BLOCK_SIZE:
        raise ValueError(f"DES key must be {BLOCK_SIZE} bytes, got {len(key)}")

    permuted = _permute(_bits(key), _PC1)
    left, right = permuted[:28], permuted[28:]

    subkeys = []
    for shift in _SHIFTS:
        left = left[shift:] + left[:shift]
        right = right[shift:] + right[:shift]
        subkeys.append(_permute(left + right, _PC2))
    return subkeys


def _feistel(right: list[int], subkey: list[int]) -> list[int]:
    expanded = _permute(right, _E)
    xored = [a ^ b for a, b in zip(expanded, subkey, strict=True)]

    out: list[int] = []
    for box in range(8):
        chunk = xored[box * 6 : box * 6 + 6]
        row = (chunk[0] << 1) | chunk[5]
        column = (chunk[1] << 3) | (chunk[2] << 2) | (chunk[3] << 1) | chunk[4]
        value = _S_BOXES[box][row][column]
        out.extend(((value >> shift) & 1 for shift in range(3, -1, -1)))

    return _permute(out, _P)


def encrypt_block(block: bytes, subkeys: list[list[int]]) -> bytes:
    """Encrypt one 8-byte block with a prepared key schedule."""
    if len(block) != BLOCK_SIZE:
        raise ValueError(f"DES block must be {BLOCK_SIZE} bytes, got {len(block)}")

    bits = _permute(_bits(block), _IP)
    left, right = bits[:32], bits[32:]

    for subkey in subkeys:
        left, right = right, [a ^ b for a, b in zip(left, _feistel(right, subkey), strict=True)]

    return _unbits(_permute(right + left, _IP_INV))


def encrypt_ecb(data: bytes, key: bytes) -> bytes:
    """Encrypt whole blocks in ECB mode. Length must be a multiple of 8."""
    if len(data) % BLOCK_SIZE:
        raise ValueError(f"data must be a multiple of {BLOCK_SIZE} bytes")
    subkeys = key_schedule(key)
    return b"".join(
        encrypt_block(data[i : i + BLOCK_SIZE], subkeys)
        for i in range(0, len(data), BLOCK_SIZE)
    )


def _reverse_bits(byte: int) -> int:
    return int(f"{byte:08b}"[::-1], 2)


def vnc_key(password: bytes) -> bytes:
    """Turn a password into the 8-byte DES key RFB actually uses.

    Truncated or zero-padded to 8 bytes, then **each byte's bits are
    reversed**. The reversal is the VNC-specific part; without it the
    handshake fails against every real server even though the cipher itself is
    perfectly correct.
    """
    padded = password[:BLOCK_SIZE].ljust(BLOCK_SIZE, b"\x00")
    return bytes(_reverse_bits(byte) for byte in padded)


def vnc_response(challenge: bytes, password: bytes) -> bytes:
    """The 16-byte answer to an RFB security type 2 challenge."""
    if len(challenge) != 16:
        raise ValueError(f"VNC challenge must be 16 bytes, got {len(challenge)}")
    return encrypt_ecb(challenge, vnc_key(password))
