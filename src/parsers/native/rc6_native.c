/* SPDX-License-Identifier: MIT
 *
 * This file is a faithful port of `FZFile::decode` from OpenBoardView's
 * src/openboardview/FileFormats/FZFile.cpp. Kept under MIT for upstream
 * consistency — anyone can reuse this RC6-CFB-1 decoder outside this
 * project under the terms below.
 *
 *   Copyright (c) 2016 Chloridite and OpenBoardView contributors
 *   Copyright (C) 2026 Thermetery Technology LLC (port to a ctypes-
 *                     friendly buffer API; everything else is from
 *                     OpenBoardView's FZFile.cpp).
 *
 * Full MIT permission notice: LICENSES/OpenBoardView-MIT.txt.
 *
 * fz_parser native helper: RC6-CFB-1 decode used by ASUS .fz files.
 * The Python implementation in fz_parser.py runs at ~40 KB/s; this
 * C version runs at hundreds of MB/s. The 6-second cold-load time
 * on a typical ASUS .fz drops to a dozen-or-so milliseconds.
 *
 * Build (Windows, MinGW UCRT64):
 *   gcc -O3 -shared -static-libgcc -o rc6_native.dll rc6_native.c
 *
 * The Python side discovers the DLL by name in the same directory and
 * loads it via ctypes; if loading fails, fz_parser.py falls back to
 * pure Python automatically.
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifdef _WIN32
#  define EXPORT __declspec(dllexport)
#else
#  define EXPORT __attribute__((visibility("default")))
#endif

/* Standard portable rotate-left. The pattern
 *     (a << (b & 31)) | (a >> ((-b) & 31))
 * is recognised by GCC, Clang, and MSVC as a single ROL instruction
 * and avoids the b==0 undefined-behaviour edge case of (a >> (32 - b))
 * when b is 0 (UB because the shift amount is then 32 ≥ width). */
static inline uint32_t rotl32(uint32_t a, uint32_t b) {
    return (a << (b & 31)) | (a >> ((-b) & 31));
}

/* In-place RC6-CFB-1 decode, exactly matching OpenBoardView's
 * FZFile::decode. `key` is the 44-word RC6 expanded round-key
 * material — we don't run the RC6 key schedule ourselves. The
 * caller must have already validated the key against
 * OpenBoardView's parity table. */
EXPORT void rc6_decode(unsigned char *source, size_t size,
                       const uint32_t *key) {
    enum { LOGW = 5, R = 20 };

    uint32_t A = 0, B = 0, C = 0, D = 0;
    uint8_t ibuf[16] = {0};

    for (size_t pos = 0; pos < size; ++pos) {
        B += key[0];
        D += key[1];

        for (uint32_t i = 1; i <= R; ++i) {
            uint32_t t = rotl32(B * (2 * B + 1), LOGW);
            uint32_t u = rotl32(D * (2 * D + 1), LOGW);
            uint32_t newA = rotl32(A ^ t, u) + key[2 * i];
            uint32_t newC = rotl32(C ^ u, t) + key[2 * i + 1];
            /* (A, B, C, D) <- (B, newC, D, newA) */
            A = B;
            B = newC;
            C = D;
            D = newA;
        }

        A += key[2 * R + 2];
        C += key[2 * R + 3];

        uint8_t current = source[pos];
        source[pos] = (uint8_t)(current ^ (A & 0xFF));

        /* Slide ibuf left, append the saved CIPHERTEXT byte (CFB-1). */
        memmove(ibuf, ibuf + 1, 15);
        ibuf[15] = current;

        /* Reload (A, B, C, D) from the new ibuf as four little-endian
         * 32-bit ints. memcpy + uint32_t is the canonical incantation
         * that compilers fuse into 4 mov instructions. */
        memcpy(&A, ibuf + 0, 4);
        memcpy(&B, ibuf + 4, 4);
        memcpy(&C, ibuf + 8, 4);
        memcpy(&D, ibuf + 12, 4);
    }
}
