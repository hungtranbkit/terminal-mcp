#pragma once
// Copy to config.local.h (ignored). Never use a Terminal MCP admin credential.
#define DECK_WIFI_SSID ""
#define DECK_WIFI_PASSWORD ""
#define DECK_API_URL "https://deck.example.local:8443/device/v1/status"
#define DECK_DEVICE_TOKEN ""
// CA issuing the server certificate, with hostname SAN matching DECK_API_URL.
#define DECK_ROOT_CA ""
#define DECK_BOOT_DEMO true
#define DECK_TOUCH_SWAP_XY false
#define DECK_TOUCH_FLIP_X false
#define DECK_TOUCH_FLIP_Y false
