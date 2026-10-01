#include <stdint.h>
#include <string.h>
#include "driver/uart.h"
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs_flash.h"

/* Adapted from Espressif esp-csi csi_recv. No output GPIO is initialized. */
#define MAX_CSI 384
#define DEPTH 32
typedef struct {
    uint32_t sequence;
    uint64_t capture_us;
    uint16_t len;
    uint8_t invalid, ltf, channel;
    int8_t rssi;
    int8_t bytes[MAX_CSI];
} item_t;
static StaticQueue_t qctl;
static uint8_t qstorage[DEPTH * sizeof(item_t)];
static QueueHandle_t queue;
static const uint8_t source_mac[6] = {0x98,0xc3,0x77,0xb4,0x22,0x44};
static uint8_t receiver_mac[6];
static volatile uint32_t callbacks, enqueued, queue_full, oversize, serial_fail, source_filtered;

static void put16(uint8_t *p, uint16_t x) { p[0] = x; p[1] = x >> 8; }
static void put32(uint8_t *p, uint32_t x) { for (int i=0;i<4;i++) p[i] = x >> (8*i); }
static void put64(uint8_t *p, uint64_t x) { for (int i=0;i<8;i++) p[i] = x >> (8*i); }
static uint32_t crc32(const uint8_t *p, size_t n)
{
    uint32_t crc = 0xffffffff;
    for (size_t i=0;i<n;i++) {
        crc ^= p[i];
        for (int b=0;b<8;b++) crc = (crc >> 1) ^ (0xedb88320u & (0u-(crc & 1)));
    }
    return ~crc;
}
static void send_frame(uint8_t type, const uint8_t *payload, uint16_t size)
{
    uint8_t frame[8+30+MAX_CSI+4];
    if (size > 30+MAX_CSI) return;
    memcpy(frame, "CSI1", 4); frame[4]=1; frame[5]=type; put16(frame+6,size);
    memcpy(frame+8,payload,size);
    put32(frame+8+size,crc32(frame,8+size));
    if (uart_write_bytes(UART_NUM_0, frame, 12+size) != 12+size) serial_fail++;
}
static void callback(void *ctx, wifi_csi_info_t *info)
{
    if (!info) return;
    if (memcmp(info->mac,source_mac,6)) { source_filtered++; return; }
    uint32_t seq = callbacks++;
    if (!info->buf || !info->len || info->len > MAX_CSI || (info->len & 1)) { oversize++; return; }
    item_t item = {.sequence=seq,.capture_us=(uint64_t)esp_timer_get_time(),.len=info->len,
                   .invalid=info->first_word_invalid ? 1 : 0,.ltf=0,
                   .channel=info->rx_ctrl.channel,.rssi=info->rx_ctrl.rssi};
    memcpy(item.bytes, info->buf, item.len);
    if (xQueueSend(queue,&item,0) == pdTRUE) enqueued++;
    else queue_full++;
}
static void stream_task(void *ctx)
{
    item_t item;
    uint8_t payload[30+MAX_CSI];
    TickType_t heartbeat = xTaskGetTickCount();
    for (;;) {
        if (xQueueReceive(queue,&item,pdMS_TO_TICKS(50)) == pdTRUE) {
            memcpy(payload,source_mac,6); memcpy(payload+6,receiver_mac,6);
            put32(payload+12,item.sequence); put64(payload+16,item.capture_us);
            put16(payload+24,item.len); payload[26]=item.invalid; payload[27]=item.ltf;
            payload[28]=(uint8_t)item.rssi; payload[29]=item.channel;
            memcpy(payload+30,item.bytes,item.len);
            send_frame(1,payload,30+item.len);
        }
        if (xTaskGetTickCount()-heartbeat >= pdMS_TO_TICKS(1000)) {
            heartbeat = xTaskGetTickCount();
            memcpy(payload,receiver_mac,6);
            put32(payload+6,callbacks); put32(payload+10,enqueued);
            put32(payload+14,queue_full); put32(payload+18,oversize);
            put32(payload+22,serial_fail); put32(payload+26,source_filtered);
            send_frame(2,payload,30);
        }
    }
}
static void command_task(void *ctx)
{
    uint8_t frame[32]; size_t filled = 0;
    for (;;) {
        uint8_t ch;
        if (uart_read_bytes(UART_NUM_0,&ch,1,pdMS_TO_TICKS(50)) != 1) continue;
        if (filled == 0 && ch != 'C') continue;
        frame[filled++] = ch;
        if (filled <= 4 && memcmp(frame,"CSI1",filled)) { filled=0; continue; }
        if (filled < 8) continue;
        uint16_t len = (uint16_t)frame[6] | (uint16_t)frame[7]<<8;
        if (frame[4]!=1 || frame[5]!=3 || len!=8) { filled=0; continue; }
        if (filled < 12+len) continue;
        if (crc32(frame,8+len)==((uint32_t)frame[8+len] | (uint32_t)frame[9+len]<<8 |
                                  (uint32_t)frame[10+len]<<16 | (uint32_t)frame[11+len]<<24)) {
            uint8_t ack[5]; memcpy(ack,frame+8,4);
            uint16_t ttl=(uint16_t)frame[13] | (uint16_t)frame[14]<<8;
            ack[4]=(frame[12]<1 || frame[12]>4 || ttl<1 || ttl>2000 || frame[15]!=1) ? 3 : 2;
            send_frame(4,ack,5); /* Wiring unknown: permanently unavailable until separately enabled/verified. */
        }
        filled=0;
    }
}
void app_main(void)
{
    esp_err_t err=nvs_flash_init();
    if (err==ESP_ERR_NVS_NO_FREE_PAGES || err==ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase()); err=nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_read_mac(receiver_mac,ESP_MAC_WIFI_STA));
    queue=xQueueCreateStatic(DEPTH,sizeof(item_t),qstorage,&qctl);
    configASSERT(queue);
    uart_config_t uart={.baud_rate=921600,.data_bits=UART_DATA_8_BITS,.parity=UART_PARITY_DISABLE,
                        .stop_bits=UART_STOP_BITS_1,.flow_ctrl=UART_HW_FLOWCTRL_DISABLE};
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0,8192,8192,0,NULL,0));
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_0,&uart));
    /* Console is disabled: explicitly route both UART0 signals to the board's USB-UART pins. */
    ESP_ERROR_CHECK(uart_set_pin(UART_NUM_0, 43, 44, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    configASSERT(xTaskCreate(stream_task,"stream",4096,NULL,5,NULL)==pdPASS);
    configASSERT(xTaskCreate(command_task,"commands",3072,NULL,4,NULL)==pdPASS);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t config=WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&config));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA,WIFI_BW_HT20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_CSI_CHANNEL,WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    wifi_csi_config_t csi={.lltf_en=true,.htltf_en=true,.stbc_htltf2_en=false,
                           .ltf_merge_en=false,.channel_filter_en=true};
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(callback,NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}
