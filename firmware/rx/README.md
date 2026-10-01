# CSI receiver

ESP32-S3, ESP-IDF 5.3. Match `CSI_CHANNEL` with transmitter (default 11). Empty `CSI_EXPECTED_TX_MAC` accepts only `30:30:f9:67:34:34` or `98:c3:77:b4:22:44`; otherwise set exact MAC. UART0 TX at 921600 baud is binary only; ESP-IDF console disabled. Configure bootloader flashing separately; boot ROM still emits startup bytes before application stream.

Frame: 32-byte little-endian header, signed int8 I/Q CSI payload, little-endian CRC-16 CCITT over header and payload. `rx_seq` increments for accepted CSI callbacks before queue admission; gaps mark queue overflow. `rx_drop_count` snapshots drops at enqueue time. `tx_seq` comes from ESP-NOW payload when available, otherwise `0xffffffff`. `first_word_invalid` reports radio flag, raw bytes remain unmodified. Full queue drops newest CSI item. CSI max length 384; larger samples ignored.

Build: `idf.py set-target esp32s3 && idf.py build`. Flash and verify actual board identity and RF sample rates with physical hardware; builds alone do not establish acquisition.
