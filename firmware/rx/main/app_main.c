#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "csi_protocol.h"
#include "driver/uart.h"
#include "driver/usb_serial_jtag.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#define QUEUE_DEPTH 32
/* Static queue storage stays in internal DRAM; callback copies driver-owned CSI before return. */
typedef struct {
    uint32_t rx_seq, tx_seq, timestamp, drops;
    uint8_t mac[6], channel, first_word_invalid;
    int8_t rssi, noise_floor;
    uint16_t len;
    int8_t csi[CSI_MAX_LEN];
} csi_item_t;

static StaticQueue_t queue_control;
static uint8_t queue_storage[QUEUE_DEPTH * sizeof(csi_item_t)];
static QueueHandle_t queue;
static uint32_t rx_seq, rx_drop_count;
static uint8_t expected_mac[6];
static bool exact_mac;
static const uint8_t board_macs[2][6] = {
    {0x30, 0x30, 0xf9, 0x67, 0x34, 0x34},
    {0x98, 0xc3, 0x77, 0xb4, 0x22, 0x44},
};

static bool parse_mac(const char *text, uint8_t mac[6])
{
    unsigned v[6];
    char tail;
    if (sscanf(text, "%2x:%2x:%2x:%2x:%2x:%2x%c",
               &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &tail) != 6) return false;
    for (unsigned i = 0; i < 6; i++) mac[i] = (uint8_t)v[i];
    return true;
}

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf || !info->len || info->len > CSI_MAX_LEN) return;
    if (exact_mac ? memcmp(info->mac, expected_mac, 6) != 0 :
        memcmp(info->mac, board_macs[0], 6) != 0 && memcmp(info->mac, board_macs[1], 6) != 0) return;

    /* Wi-Fi task context: no malloc, wait, log or borrowed pointers in queue. */
    csi_item_t item = {0};
    item.rx_seq = rx_seq++;
    item.tx_seq = UINT32_MAX;
    if (info->payload && info->payload_len >= 19 && info->rx_ctrl.sig_mode == 1) {
        /* ESP-NOW action payload: driver-provided prefix (15), then probe LE32. */
        const uint8_t *p = info->payload + 15;
        item.tx_seq = (uint32_t)p[0] | (uint32_t)p[1] << 8 |
                      (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
    }
    item.timestamp = info->rx_ctrl.timestamp;
    memcpy(item.mac, info->mac, 6);
    item.rssi = info->rx_ctrl.rssi;
    item.noise_floor = info->rx_ctrl.noise_floor;
    item.channel = info->rx_ctrl.channel;
    item.first_word_invalid = info->first_word_invalid ? 1 : 0;
    item.drops = rx_drop_count;
    item.len = info->len;
    memcpy(item.csi, info->buf, item.len);
    if (xQueueSend(queue, &item, 0) != pdTRUE) rx_drop_count++;
}

static void csi_stream_task(void *arg)
{
    csi_item_t item;
    static uint8_t frame[CSI_FRAME_LEN(CSI_MAX_LEN)];
    while (xQueueReceive(queue, &item, portMAX_DELAY) == pdTRUE) {
        frame[0] = 0xaa; frame[1] = 0x55;
        frame[2] = 1; frame[3] = CSI_HEADER_LEN;
        csi_put32(frame + 4, item.rx_seq);
        csi_put32(frame + 8, item.tx_seq);
        csi_put32(frame + 12, item.timestamp);
        memcpy(frame + 16, item.mac, 6);
        frame[22] = (uint8_t)item.rssi;
        frame[23] = (uint8_t)item.noise_floor;
        frame[24] = item.channel;
        frame[25] = item.first_word_invalid;
        csi_put32(frame + 26, item.drops);
        csi_put16(frame + 30, item.len);
        memcpy(frame + CSI_HEADER_LEN, item.csi, item.len);
        uint16_t crc = csi_crc16(frame, CSI_HEADER_LEN + item.len);
        csi_put16(frame + CSI_HEADER_LEN + item.len, crc);
        /* Write to UART0 and USB Serial/JTAG so frames flow regardless of port type. */
        int size = CSI_FRAME_LEN(item.len);
        uart_write_bytes(UART_NUM_0, frame, size);
        usb_serial_jtag_write_bytes(frame, size, pdMS_TO_TICKS(10));
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    if (CONFIG_CSI_EXPECTED_TX_MAC[0]) {
        configASSERT(parse_mac(CONFIG_CSI_EXPECTED_TX_MAC, expected_mac));
        exact_mac = true;
    }
    queue = xQueueCreateStatic(QUEUE_DEPTH, sizeof(csi_item_t), queue_storage, &queue_control);
    configASSERT(queue != NULL);
    uart_config_t uart = {.baud_rate = 921600, .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE, .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE};
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, 4096, 4096, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_0, &uart));
    usb_serial_jtag_driver_config_t usb_cfg = {
        .rx_buffer_size = 1024,
        .tx_buffer_size = 4096,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb_cfg));
    configASSERT(xTaskCreate(csi_stream_task, "csi_stream", 4096, NULL, 5, NULL) == pdPASS);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_CSI_CHANNEL, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    wifi_csi_config_t csi = {.lltf_en = true, .htltf_en = true,
        .stbc_htltf2_en = false, .ltf_merge_en = false, .channel_filter_en = true};
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}
