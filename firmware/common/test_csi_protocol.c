#include <assert.h>
#include <stdint.h>
#include "csi_protocol.h"

int main(void)
{
    const uint8_t test[] = "123456789";
    assert(csi_crc16(test, 9) == 0x29b1);
    uint8_t header[CSI_HEADER_LEN] = {0};
    csi_put32(header + 4, 0x12345678);
    csi_put16(header + 30, 0x1234);
    assert(header[4] == 0x78 && header[7] == 0x12);
    assert(header[30] == 0x34 && header[31] == 0x12);
    assert(CSI_FRAME_LEN(CSI_MAX_LEN) == 418);
}
