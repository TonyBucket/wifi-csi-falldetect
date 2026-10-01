"""Static firmware contract check; live RF capture requires both boards."""
from pathlib import Path

ROOT = Path(__file__).parent
tx = (ROOT / "csi_tx/main/espnow_csi_send.c").read_text() if (ROOT / "csi_tx/main/espnow_csi_send.c").exists() else "".join(p.read_text() for p in (ROOT / "csi_tx/main").glob("*.c"))
rx = "".join(p.read_text() for p in (ROOT / "csi_rx/main").glob("*.c"))

assert "0x1a, 0x00, 0x00, 0x00, 0x00, 0x00" in tx and "CONFIG_SEND_FREQUENCY               100" in tx
assert "esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC)" not in rx
assert "info->payload_len >= 19" in rx and "memcpy(&frame.seq, info->payload + 15, sizeof(frame.seq))" in rx
assert "xQueueSend(" in rx and "xQueueReceive(" in rx
assert 'CSI_DATA,%lu,' in rx and 'ets_printf("]\\\"\\n")' in rx
for name in ("csi_tx", "csi_rx"):
    config = (ROOT / name / "sdkconfig.defaults").read_text()
    for setting in ('CONFIG_IDF_TARGET="esp32s3"', "CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y", "CONFIG_SPIRAM=y", "CONFIG_SPIRAM_MODE_OCT=y", "CONFIG_SPIRAM_SPEED_80M=y"):
        assert setting in config, (name, setting)
print("CSI firmware source contract OK")
