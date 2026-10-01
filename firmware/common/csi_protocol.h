#ifndef CSI_PROTOCOL_H
#define CSI_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

/* Wire format: little-endian integers; CRC low byte first. Offsets fixed, no struct padding. */
#define CSI_HEADER_LEN 32u
#define CSI_MAX_LEN 384u
#define CSI_FRAME_LEN(n) (CSI_HEADER_LEN + (n) + 2u)

static inline uint16_t csi_crc16(const uint8_t *data, size_t length)
{
    uint16_t crc = 0xffff;
    for (size_t i = 0; i < length; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (unsigned bit = 0; bit < 8; bit++) {
            crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static inline void csi_put16(uint8_t *p, uint16_t n)
{
    p[0] = (uint8_t)n;
    p[1] = (uint8_t)(n >> 8);
}

static inline void csi_put32(uint8_t *p, uint32_t n)
{
    for (unsigned i = 0; i < 4; i++) p[i] = (uint8_t)(n >> (8 * i));
}

#endif
