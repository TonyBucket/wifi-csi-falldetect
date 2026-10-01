#include <stdint.h>
#include <string.h>
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

/* Adapted from Espressif esp-csi csi_send; hardware STA MAC remains factory MAC. */
static const uint8_t broadcast[6] = {255,255,255,255,255,255};

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
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
    ESP_ERROR_CHECK(esp_now_init());
    esp_now_peer_info_t peer = {.channel=CONFIG_CSI_CHANNEL, .ifidx=WIFI_IF_STA};
    memcpy(peer.peer_addr, broadcast, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate = {.phymode=WIFI_PHY_MODE_HT20, .rate=WIFI_PHY_RATE_MCS0_LGI};
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(broadcast, &rate));
    uint32_t sequence = 0;
    TickType_t next = xTaskGetTickCount();
    for (;;) {
        const uint8_t payload[4] = {sequence, sequence >> 8, sequence >> 16, sequence >> 24};
        sequence++;
        (void)esp_now_send(broadcast, payload, sizeof(payload));
        TickType_t interval = pdMS_TO_TICKS(1000 / CONFIG_CSI_TX_RATE_HZ);
        vTaskDelayUntil(&next, interval ? interval : 1);
    }
}
