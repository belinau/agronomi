# AgroNomi Fleet

A multi-node farm telemetry and control fleet running on [Reticulum](https://reticulum.network/) mesh networking. Multiple ESP32 sensor and actuator nodes communicate over LoRa (RNode), WiFi, and BLE with a central hub, using encrypted LXMF messaging. Sensors deep-sleep between readings, actuators stay awake for instant command response — all coordinated through a self-organising mesh that requires no internet connection.

## Attributions