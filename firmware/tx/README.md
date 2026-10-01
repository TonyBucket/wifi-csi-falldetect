# CSI transmitter

ESP32-S3, ESP-IDF 5.3. ESP-NOW broadcast HT20/MCS0 on `CSI_CHANNEL` (default 11). `CSI_TX_RATE_HZ` defaults to 100; actual radio callback rate may differ. Sends monotonically increasing LE32 sequence payload, reports 1-second `tx_count`/`err_count` on UART0 console. Wi-Fi STA uses hardware MAC (not spoofed). Build: `idf.py set-target esp32s3 && idf.py build`.
