"""Falcon/BMS-compatible LZSS primitives."""

from __future__ import annotations

from collections import defaultdict, deque


INDEX_BIT_COUNT = 12
LENGTH_BIT_COUNT = 4
WINDOW_SIZE = 1 << INDEX_BIT_COUNT
BREAK_EVEN = (1 + INDEX_BIT_COUNT + LENGTH_BIT_COUNT) // 9
MAX_MATCH_LENGTH = (1 << LENGTH_BIT_COUNT) + BREAK_EVEN


class LzssError(RuntimeError):
    """Raised when an LZSS payload is malformed."""


def lzss_compress(uncompressed: bytes) -> bytes:
    """Encode a Falcon/BMS-compatible LZSS stream with greedy matches."""
    if not uncompressed:
        return b""

    output = bytearray()
    recent_by_key: dict[bytes, deque[int]] = defaultdict(deque)
    cursor = 0

    while cursor < len(uncompressed):
        flag_offset = len(output)
        output.append(0)
        flag_byte = 0

        for bit_index in range(8):
            if cursor >= len(uncompressed):
                break

            best_start, best_length = _find_best_match(uncompressed, cursor, recent_by_key)
            if best_start >= 0 and best_length >= BREAK_EVEN + 1:
                window_pos = (1 + best_start) & (WINDOW_SIZE - 1)
                length_code = best_length - (BREAK_EVEN + 1)
                output.append((length_code << 4) | ((window_pos >> 8) & 0x0F))
                output.append(window_pos & 0xFF)
                consumed = best_length
            else:
                flag_byte |= 1 << bit_index
                output.append(uncompressed[cursor])
                consumed = 1

            _remember_positions(uncompressed, cursor, consumed, recent_by_key)
            cursor += consumed

        output[flag_offset] = flag_byte

    return bytes(output)


def lzss_expand(compressed: bytes, uncompressed_size: int) -> tuple[bytes, int]:
    """Decompress a Falcon/BMS LZSS stream and return bytes plus consumed input size."""
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
        match_length = (match_length >> 4) + BREAK_EVEN

        if match_length < remaining:
            copy_count = match_length + 1
            remaining -= copy_count
        else:
            copy_count = 0
            remaining = 0

        for i in range(copy_count):
            value = window[(match_position + i) & (WINDOW_SIZE - 1)]
            output.append(value)
            window[current_position] = value
            current_position = (current_position + 1) & (WINDOW_SIZE - 1)

    return bytes(output), input_index


def _find_best_match(
    uncompressed: bytes,
    cursor: int,
    recent_by_key: dict[bytes, deque[int]],
) -> tuple[int, int]:
    remaining = len(uncompressed) - cursor
    if remaining < BREAK_EVEN + 1:
        return -1, 0

    key = bytes(uncompressed[cursor : cursor + 2])
    candidates = recent_by_key.get(key)
    if not candidates:
        return -1, 0

    min_start = max(0, cursor - WINDOW_SIZE)
    while candidates and candidates[0] < min_start:
        candidates.popleft()
    if not candidates:
        return -1, 0

    best_start = -1
    best_length = 0
    max_length = min(MAX_MATCH_LENGTH, remaining)
    checked = 0
    for candidate in reversed(candidates):
        match_length = _match_length(uncompressed, candidate, cursor, max_length)
        if match_length > best_length:
            best_start = candidate
            best_length = match_length
            if best_length == max_length:
                break
        checked += 1
        if checked >= 64:
            break
    return best_start, best_length


def _match_length(uncompressed: bytes, start: int, cursor: int, max_length: int) -> int:
    produced: list[int] = []
    for offset in range(max_length):
        source_index = start + offset
        if source_index < cursor:
            value = uncompressed[source_index]
        else:
            value = produced[source_index - cursor]
        if value != uncompressed[cursor + offset]:
            return offset
        produced.append(value)
    return max_length


def _remember_positions(
    uncompressed: bytes,
    start: int,
    length: int,
    recent_by_key: dict[bytes, deque[int]],
) -> None:
    limit = len(uncompressed) - 1
    for position in range(start, min(start + length, limit)):
        recent_by_key[bytes(uncompressed[position : position + 2])].append(position)
