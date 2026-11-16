#pragma once

// Copy this file to secrets.h and edit locally. secrets.h is ignored by Git.
#define QUELLA_WIFI_SSID "your-wifi-ssid"
#define QUELLA_WIFI_PASSWORD "your-wifi-password"

// Required for every mutating endpoint. Use a long, random LAN-only key.
#define QUELLA_API_KEY "replace-with-a-long-random-api-key"
#define QUELLA_GATEWAY_HOSTNAME "quella-gateway"

// UDP-only raw telemetry destination. Replace with the Homeserver IPv4 address.
// If omitted or set to 0.0.0.0, telemetry stays disabled while REST/WS remain active.
#define QUELLA_TELEMETRY_HOST "10.0.1.10"
#define QUELLA_TELEMETRY_PORT 42042

// Optional static network configuration. Set to 0 to use DHCP.
#define QUELLA_WIFI_USE_STATIC_IP 0
#define QUELLA_WIFI_IP_A 10
#define QUELLA_WIFI_IP_B 0
#define QUELLA_WIFI_IP_C 1
#define QUELLA_WIFI_IP_D 27
#define QUELLA_WIFI_GATEWAY_A 10
#define QUELLA_WIFI_GATEWAY_B 0
#define QUELLA_WIFI_GATEWAY_C 1
#define QUELLA_WIFI_GATEWAY_D 1
#define QUELLA_WIFI_SUBNET_A 255
#define QUELLA_WIFI_SUBNET_B 255
#define QUELLA_WIFI_SUBNET_C 255
#define QUELLA_WIFI_SUBNET_D 0
#define QUELLA_WIFI_DNS_A 10
#define QUELLA_WIFI_DNS_B 0
#define QUELLA_WIFI_DNS_C 255
#define QUELLA_WIFI_DNS_D 2

// USB-to-ESP WiFi diagnosis. With 1, UART0 is text-only and QG/HTTP/WebSocket
// are not started. Use only with DIP 5+6 ON and Mega link 1+2 OFF.
#ifndef QUELLA_WIFI_SERIAL_DEBUG
#define QUELLA_WIFI_SERIAL_DEBUG 0
#endif

// Optional normal-mode link logging on ESP8266 TX-only Serial1 / GPIO2.
#ifndef QUELLA_SERIAL1_LINK_DEBUG
#define QUELLA_SERIAL1_LINK_DEBUG 0
#endif
