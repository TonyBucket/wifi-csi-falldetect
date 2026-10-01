"""UART0 RX must be explicitly routed when ESP-IDF console is disabled."""

from pathlib import Path


source = (Path(__file__).parent / "main/app_main.c").read_text()
assert "uart_set_pin(UART_NUM_0, 43, 44, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE)" in source
