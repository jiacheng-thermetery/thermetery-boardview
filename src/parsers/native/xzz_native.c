/* SPDX-License-Identifier: MIT
 *
 * This file is a port of dhuertas/DES (MIT-licensed). The same C code
 * is reproduced inside OpenBoardView under src/openboardview/Crypto/
 * des.c. Kept under MIT for upstream consistency — anyone can reuse
 * this DES port outside this project under the terms below.
 *
 *   Copyright (c) 2020 Dani Huertas (https://github.com/dhuertas/DES)
 *   Copyright (C) 2026 Thermetery Technology LLC (port to ctypes-
 *                     friendly buffer API; everything else is dhuertas).
 *
 * Full MIT permission notice: LICENSES/dhuertas-DES-MIT.txt.
 */

/* xzz_native: DES decryption fast path for the XZZPCB .pcb parser.
 *
 * The XZZPCB .pcb format encrypts the PART/PIN sub-stream with DES in
 * ECB mode (8-byte blocks, big-endian byte conversion). On a typical
 * MSI motherboard there are ~3000-4000 encrypted records totaling
 * ~1.5-2 MB; pure-Python DES takes 30-60 seconds on this, which is a
 * UX problem. This DLL drops it to a few hundred milliseconds.
 *
 * We do NOT cache decrypted output to disk. The decrypted plaintext
 * is the proprietary file's content — leaving it on disk creates an
 * IP/leakage hazard. Decrypt fast in memory each time instead.
 *
 * --- Attribution ---
 *
 * This file is a C re-implementation of dhuertas/DES
 * (https://github.com/dhuertas/DES, MIT). The same C code is reproduced
 * inside OpenBoardView under src/openboardview/Crypto/des.c. The DES
 * tables and algorithm follow FIPS PUB 46-3 verbatim.
 *
 *   Copyright (c) 2020 Dani Huertas
 *
 * The MIT permission notice for that work is reproduced under
 * LICENSES/dhuertas-DES-MIT.txt; this header is the in-source notice
 * required by the MIT terms.
 *
 * --- Build ---
 *
 *   gcc -O3 -shared -static-libgcc -Wl,--strip-all -o xzz_native.dll xzz_native.c
 *
 * --- API ---
 *
 *   int32_t xzz_des_decrypt_buffer(
 *       const uint8_t *in, size_t len,
 *       uint64_t key,
 *       uint8_t *out
 *   );
 *
 * Decrypts `len` bytes of `in` into `out` (caller-allocated, >= len)
 * using the 64-bit `key`. `len` is rounded down to the nearest
 * multiple of 8; trailing bytes (0..7) are copied through unchanged.
 * Returns 0 on success, -1 if `in`, `out`, or any required pointer is
 * null. The function is reentrant — no global state.
 *
 *   int32_t xzz_des_selftest(void);
 *
 * Runs the Rivest test vector (X0=0x9474B8E8C73BCA7D ->
 * X16=0x1B1A2DDB4C642438 after 16 alternating encrypt/decrypts).
 * Returns 0 if the vector matches, -1 otherwise. Used by the Python
 * shim during DLL load to refuse a broken build.
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifdef _WIN32
#  define EXPORT __declspec(dllexport)
#else
#  define EXPORT __attribute__((visibility("default")))
#endif

/* All table entries are 1-indexed (FIPS convention). The permute()
 * helper subtracts 1 internally. */

static const uint8_t IP[64] = {
    58, 50, 42, 34, 26, 18, 10,  2,
    60, 52, 44, 36, 28, 20, 12,  4,
    62, 54, 46, 38, 30, 22, 14,  6,
    64, 56, 48, 40, 32, 24, 16,  8,
    57, 49, 41, 33, 25, 17,  9,  1,
    59, 51, 43, 35, 27, 19, 11,  3,
    61, 53, 45, 37, 29, 21, 13,  5,
    63, 55, 47, 39, 31, 23, 15,  7,
};

static const uint8_t PI[64] = {
    40,  8, 48, 16, 56, 24, 64, 32,
    39,  7, 47, 15, 55, 23, 63, 31,
    38,  6, 46, 14, 54, 22, 62, 30,
    37,  5, 45, 13, 53, 21, 61, 29,
    36,  4, 44, 12, 52, 20, 60, 28,
    35,  3, 43, 11, 51, 19, 59, 27,
    34,  2, 42, 10, 50, 18, 58, 26,
    33,  1, 41,  9, 49, 17, 57, 25,
};

static const uint8_t E_TBL[48] = {
    32,  1,  2,  3,  4,  5,
     4,  5,  6,  7,  8,  9,
     8,  9, 10, 11, 12, 13,
    12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21,
    20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29,
    28, 29, 30, 31, 32,  1,
};

static const uint8_t P_TBL[32] = {
    16,  7, 20, 21, 29, 12, 28, 17,
     1, 15, 23, 26,  5, 18, 31, 10,
     2,  8, 24, 14, 32, 27,  3,  9,
    19, 13, 30,  6, 22, 11,  4, 25,
};

static const uint8_t S_TBL[8][64] = {
    {
        14,  4, 13,  1,  2, 15, 11,  8,  3, 10,  6, 12,  5,  9,  0,  7,
         0, 15,  7,  4, 14,  2, 13,  1, 10,  6, 12, 11,  9,  5,  3,  8,
         4,  1, 14,  8, 13,  6,  2, 11, 15, 12,  9,  7,  3, 10,  5,  0,
        15, 12,  8,  2,  4,  9,  1,  7,  5, 11,  3, 14, 10,  0,  6, 13,
    }, {
        15,  1,  8, 14,  6, 11,  3,  4,  9,  7,  2, 13, 12,  0,  5, 10,
         3, 13,  4,  7, 15,  2,  8, 14, 12,  0,  1, 10,  6,  9, 11,  5,
         0, 14,  7, 11, 10,  4, 13,  1,  5,  8, 12,  6,  9,  3,  2, 15,
        13,  8, 10,  1,  3, 15,  4,  2, 11,  6,  7, 12,  0,  5, 14,  9,
    }, {
        10,  0,  9, 14,  6,  3, 15,  5,  1, 13, 12,  7, 11,  4,  2,  8,
        13,  7,  0,  9,  3,  4,  6, 10,  2,  8,  5, 14, 12, 11, 15,  1,
        13,  6,  4,  9,  8, 15,  3,  0, 11,  1,  2, 12,  5, 10, 14,  7,
         1, 10, 13,  0,  6,  9,  8,  7,  4, 15, 14,  3, 11,  5,  2, 12,
    }, {
         7, 13, 14,  3,  0,  6,  9, 10,  1,  2,  8,  5, 11, 12,  4, 15,
        13,  8, 11,  5,  6, 15,  0,  3,  4,  7,  2, 12,  1, 10, 14,  9,
        10,  6,  9,  0, 12, 11,  7, 13, 15,  1,  3, 14,  5,  2,  8,  4,
         3, 15,  0,  6, 10,  1, 13,  8,  9,  4,  5, 11, 12,  7,  2, 14,
    }, {
         2, 12,  4,  1,  7, 10, 11,  6,  8,  5,  3, 15, 13,  0, 14,  9,
        14, 11,  2, 12,  4,  7, 13,  1,  5,  0, 15, 10,  3,  9,  8,  6,
         4,  2,  1, 11, 10, 13,  7,  8, 15,  9, 12,  5,  6,  3,  0, 14,
        11,  8, 12,  7,  1, 14,  2, 13,  6, 15,  0,  9, 10,  4,  5,  3,
    }, {
        12,  1, 10, 15,  9,  2,  6,  8,  0, 13,  3,  4, 14,  7,  5, 11,
        10, 15,  4,  2,  7, 12,  9,  5,  6,  1, 13, 14,  0, 11,  3,  8,
         9, 14, 15,  5,  2,  8, 12,  3,  7,  0,  4, 10,  1, 13, 11,  6,
         4,  3,  2, 12,  9,  5, 15, 10, 11, 14,  1,  7,  6,  0,  8, 13,
    }, {
         4, 11,  2, 14, 15,  0,  8, 13,  3, 12,  9,  7,  5, 10,  6,  1,
        13,  0, 11,  7,  4,  9,  1, 10, 14,  3,  5, 12,  2, 15,  8,  6,
         1,  4, 11, 13, 12,  3,  7, 14, 10, 15,  6,  8,  0,  5,  9,  2,
         6, 11, 13,  8,  1,  4, 10,  7,  9,  5,  0, 15, 14,  2,  3, 12,
    }, {
        13,  2,  8,  4,  6, 15, 11,  1, 10,  9,  3, 14,  5,  0, 12,  7,
         1, 15, 13,  8, 10,  3,  7,  4, 12,  5,  6, 11,  0, 14,  9,  2,
         7, 11,  4,  1,  9, 12, 14,  2,  0,  6, 10, 13, 15,  3,  5,  8,
         2,  1, 14,  7,  4, 10,  8, 13, 15, 12,  9,  0,  3,  5,  6, 11,
    },
};

static const uint8_t PC1[56] = {
    57, 49, 41, 33, 25, 17,  9,
     1, 58, 50, 42, 34, 26, 18,
    10,  2, 59, 51, 43, 35, 27,
    19, 11,  3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15,
     7, 62, 54, 46, 38, 30, 22,
    14,  6, 61, 53, 45, 37, 29,
    21, 13,  5, 28, 20, 12,  4,
};

static const uint8_t PC2[48] = {
    14, 17, 11, 24,  1,  5,
     3, 28, 15,  6, 21, 10,
    23, 19, 12,  4, 26,  8,
    16,  7, 27, 20, 13,  2,
    41, 52, 31, 37, 47, 55,
    30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53,
    46, 42, 50, 36, 29, 32,
};

static const uint8_t ITER_SHIFT[16] = {
    1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1,
};

/* Apply a 1-indexed permutation table. `value` is `n_in_bits` wide;
 * the output is `table_len` bits with bit 0 = MSB of result. */
static inline uint64_t
permute(uint64_t value, const uint8_t *table, int table_len, int n_in_bits)
{
    uint64_t out = 0;
    for (int i = 0; i < table_len; i++) {
        out = (out << 1) | ((value >> (n_in_bits - table[i])) & 1ULL);
    }
    return out;
}

/* Compute the 16 round subkeys from a 64-bit DES key.
 * `subkeys[i]` is 48 bits (low bits of u64). */
static void
des_compute_subkeys(uint64_t key, uint64_t subkeys[16])
{
    uint64_t pc1 = permute(key, PC1, 56, 64);
    uint32_t C = (uint32_t)((pc1 >> 28) & 0x0FFFFFFFU);
    uint32_t D = (uint32_t) (pc1        & 0x0FFFFFFFU);

    for (int i = 0; i < 16; i++) {
        unsigned shift = ITER_SHIFT[i];
        C = ((C << shift) | (C >> (28 - shift))) & 0x0FFFFFFFU;
        D = ((D << shift) | (D >> (28 - shift))) & 0x0FFFFFFFU;
        uint64_t cd = ((uint64_t)C << 28) | (uint64_t)D;
        subkeys[i] = permute(cd, PC2, 48, 56);
    }
}

/* One DES block. `subkeys` are in encryption order — for decrypt the
 * caller flips them, or we apply them in reverse order here. To keep
 * this routine simple we always run forward order; decrypt callers
 * pass a reversed subkey array. */
static uint64_t
des_block(uint64_t input, const uint64_t subkeys[16])
{
    uint64_t init_perm = permute(input, IP, 64, 64);
    uint32_t L = (uint32_t)((init_perm >> 32) & 0xFFFFFFFFU);
    uint32_t R = (uint32_t) (init_perm        & 0xFFFFFFFFU);

    for (int round = 0; round < 16; round++) {
        uint64_t s_input = permute((uint64_t)R, E_TBL, 48, 32);
        s_input ^= subkeys[round];

        uint32_t s_output = 0;
        for (int j = 0; j < 8; j++) {
            uint32_t chunk = (uint32_t)((s_input >> (42 - 6 * j)) & 0x3FU);
            uint32_t row = ((chunk >> 4) & 0x2U) | (chunk & 0x1U);
            uint32_t col = (chunk >> 1) & 0xFU;
            s_output = (s_output << 4) | (S_TBL[j][16 * row + col] & 0xFU);
        }

        uint32_t f_res = (uint32_t)permute((uint64_t)s_output, P_TBL, 32, 32);
        uint32_t newR = L ^ f_res;
        L = R;
        R = newR;
    }

    uint64_t pre_output = ((uint64_t)R << 32) | (uint64_t)L;
    return permute(pre_output, PI, 64, 64);
}

/* Public entry point: decrypt `len` bytes of `in` into `out` using
 * `key`. `len` is rounded down to a multiple of 8 for the DES blocks;
 * any 1..7 trailing bytes are copied through unchanged so the output
 * has the same total length as the input (matching the pure-Python
 * fallback behavior in xzzpcb_parser._des_decrypt_buf). */
EXPORT int32_t
xzz_des_decrypt_buffer(const uint8_t *in, size_t len, uint64_t key, uint8_t *out)
{
    if (in == NULL || out == NULL) {
        return -1;
    }

    /* Subkeys for decryption: encryption order reversed. */
    uint64_t subkeys[16];
    des_compute_subkeys(key, subkeys);
    uint64_t dec_subkeys[16];
    for (int i = 0; i < 16; i++) {
        dec_subkeys[i] = subkeys[15 - i];
    }

    size_t whole_blocks = len & ~(size_t)7;
    for (size_t i = 0; i < whole_blocks; i += 8) {
        uint64_t b = ((uint64_t)in[i + 0] << 56) |
                     ((uint64_t)in[i + 1] << 48) |
                     ((uint64_t)in[i + 2] << 40) |
                     ((uint64_t)in[i + 3] << 32) |
                     ((uint64_t)in[i + 4] << 24) |
                     ((uint64_t)in[i + 5] << 16) |
                     ((uint64_t)in[i + 6] <<  8) |
                     ((uint64_t)in[i + 7] <<  0);
        uint64_t r = des_block(b, dec_subkeys);
        out[i + 0] = (uint8_t)(r >> 56);
        out[i + 1] = (uint8_t)(r >> 48);
        out[i + 2] = (uint8_t)(r >> 40);
        out[i + 3] = (uint8_t)(r >> 32);
        out[i + 4] = (uint8_t)(r >> 24);
        out[i + 5] = (uint8_t)(r >> 16);
        out[i + 6] = (uint8_t)(r >>  8);
        out[i + 7] = (uint8_t)(r >>  0);
    }
    if (whole_blocks < len) {
        memcpy(out + whole_blocks, in + whole_blocks, len - whole_blocks);
    }
    return 0;
}

/* Self-test: Rivest's classic vector. Returns 0 on success, -1 on
 * failure. Called by the Python shim during DLL load to detect a
 * miscompiled or otherwise broken build. */
EXPORT int32_t
xzz_des_selftest(void)
{
    uint64_t x = 0x9474B8E8C73BCA7DULL;
    uint64_t enc_subkeys[16], dec_subkeys[16];
    for (int i = 0; i < 16; i++) {
        des_compute_subkeys(x, enc_subkeys);
        if (i % 2 == 0) {
            x = des_block(x, enc_subkeys);
        } else {
            for (int j = 0; j < 16; j++) dec_subkeys[j] = enc_subkeys[15 - j];
            x = des_block(x, dec_subkeys);
        }
    }
    return (x == 0x1B1A2DDB4C642438ULL) ? 0 : -1;
}
