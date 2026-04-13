"""Falcon/BMS-compatible LZSS decompressor.

This implementation mirrors the behavior of the native code in
Lzss/LzssNative/lzss.cpp (LZSS_Expand).
"""

from __future__ import annotations


INDEX_BIT_COUNT = 12
LENGTH_BIT_COUNT = 4
WINDOW_SIZE = 1 << INDEX_BIT_COUNT
BREAK_EVEN = (1 + INDEX_BIT_COUNT + LENGTH_BIT_COUNT) // 9


class LzssError(RuntimeError):
    """Raised when the compressed stream is malformed."""


def lzss_compress_literals(uncompressed: bytes) -> bytes:
    """Encode a valid Falcon/BMS LZSS stream using literals only."""
    if not uncompressed:
        return b""

    output = bytearray()
    cursor = 0
    while cursor < len(uncompressed):
        chunk = uncompressed[cursor : cursor + 8]
        output.append((1 << len(chunk)) - 1)
        output.extend(chunk)
        cursor += len(chunk)
    return bytes(output)


def lzss_expand(compressed: bytes, uncompressed_size: int) -> tuple[bytes, int]:
    """Decompress a Falcon/BMS LZSS stream.

    Args:
        compressed: LZSS-compressed bytes.
        uncompressed_size: Expected decompressed size passed to LZSS_Expand.

    Returns:
        A tuple (decompressed_bytes, consumed_input_bytes).
    """
    if uncompressed_size < 0:
        raise ValueError("uncompressed_size must be >= 0")
    if uncompressed_size == 0:
        return b"", 0
    if not compressed:
        raise LzssError("compressed input is empty")

    window = bytearray(WINDOW_SIZE)
    output = bytearray()

    input_index = 1
    flag_bit_mask = 1
    flag_byte = compressed[0]
    current_position = 1
    remaining = uncompressed_size

    while remaining > 0:
        consumed_flag_byte = False
        if flag_bit_mask == 0x100:
            if input_index >= len(compressed):
                raise LzssError("unexpected end of input while reading flag byte")
            flag_bit_mask = 1
            flag_byte = compressed[input_index]
            consumed_flag_byte = True

        flag_bit_mask <<= 1
        is_literal = (flag_byte & (flag_bit_mask >> 1)) != 0

        if is_literal:
            if consumed_flag_byte:
                input_index += 1
            if input_index >= len(compressed):
                raise LzssError("unexpected end of input while reading literal")

            value = compressed[input_index]
            input_index += 1

            output.append(value)
            remaining -= 1

            window[current_position] = value
            current_position = (current_position + 1) & (WINDOW_SIZE - 1)
            continue

        if consumed_flag_byte:
            input_index += 1
        if input_index + 1 >= len(compressed):
            raise LzssError("unexpected end of input while reading match pair")

        match_length = compressed[input_index]
        input_index += 1
        match_position = compressed[input_index]
        input_index += 1

        match_position |= (match_length & 0x0F) << 8
        match_length >>= 4
        match_length += BREAK_EVEN

        if match_length < remaining:
            copy_count = match_length + 1
            remaining -= copy_count
        else:
            # Matches the native end-case behavior exactly.
            remaining = 0
            copy_count = 0

        for i in range(copy_count):
            value = window[(match_position + i) & (WINDOW_SIZE - 1)]
            output.append(value)
            window[current_position] = value
            current_position = (current_position + 1) & (WINDOW_SIZE - 1)

    return bytes(output), input_index
