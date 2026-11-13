"""Power estimate against hand calculations from the power tree and the ESP32-S3 datasheet."""

import pytest

from PowerModel import (BATTERY_AH, BATTERY_AUX_A, BLE_CONNECTED_A, ESP_BUSY_PER_CORE_A, ESP_IDLE_A, RELAY_COIL_A,
                        SENSORS_3V3, SENSORS_5V, WIFI_CONNECTED_A, esp_current, estimate)

ALL = ["bme280", "ds18b20", "light", "hcsr04", "ph", "tds", "sgp30"]
IDLE_CPU = {"cpu": [{"load": 0, "peak": 0}, {"load": 0, "peak": 0}]}


def state(mode="normal", relay=False, wifi="unconfigured", enabled=ALL):
    return {"mode": mode, "enabled": list(enabled), "wifi": {"state": wifi}, "safety": {"relay": relay}}


def buck_input(i_3v3):
    """P3V3 buck input current at the 4.475 V diode node."""
    return 3.3 * i_3v3 / (0.85 * 4.475)


def test_esp_current_follows_cpu_load():
    assert esp_current(IDLE_CPU["cpu"], False, False) == pytest.approx(0.0476)
    busy = [{"load": 100, "peak": 100}] * 2
    assert esp_current(busy, False, False) == pytest.approx(0.0813)  # datasheet: dual core running
    assert esp_current(busy, True, True) == pytest.approx(0.0813 + BLE_CONNECTED_A + WIFI_CONNECTED_A)


def test_normal_mode_on_usb():
    e = estimate(state(), IDLE_CPU, ble_connected=False)
    i_3v3 = ESP_IDLE_A + sum(SENSORS_3V3.values())
    assert e.source == "usb" and e.input_volts == 5.0 and e.runtime_h is None
    assert e.input_amps == pytest.approx(buck_input(i_3v3) + sum(SENSORS_5V.values()))
    assert e.cpu_measured


def test_relay_coil_adds_its_current():
    off = estimate(state(relay=False), IDLE_CPU)
    on = estimate(state(relay=True), IDLE_CPU)
    assert on.input_amps - off.input_amps == pytest.approx(RELAY_COIL_A)


def test_disabled_sensors_and_wifi():
    base = estimate(state(), IDLE_CPU)
    no_sgp = estimate(state(enabled=[k for k in ALL if k != "sgp30"]), IDLE_CPU)
    assert base.input_amps - no_sgp.input_amps == pytest.approx(buck_input(SENSORS_3V3["sgp30"]))
    no_ph = estimate(state(enabled=[k for k in ALL if k != "ph"]), IDLE_CPU)
    assert base.input_amps - no_ph.input_amps == pytest.approx(SENSORS_5V["ph"])
    wifi = estimate(state(wifi="connected"), IDLE_CPU)
    assert wifi.input_amps - base.input_amps == pytest.approx(buck_input(WIFI_CONNECTED_A))


def test_low_power_runs_from_the_battery_without_5v_loads():
    e = estimate(state(mode="low_power", relay=True, wifi="connected"), IDLE_CPU, ble_connected=False)
    i_3v3 = ESP_IDLE_A + sum(SENSORS_3V3.values())  # no Wi-Fi, no relay, no 5 V sensors
    i_battery = 5.0 * buck_input(i_3v3) / (0.85 * 11.1) + BATTERY_AUX_A
    assert e.source == "battery" and e.input_volts == 11.1
    assert e.input_amps == pytest.approx(i_battery)
    assert e.runtime_h == pytest.approx(BATTERY_AH / i_battery)
    assert [rail.amps for rail in e.rails[1:]] == [0.0, 0.0]


def test_unknown_mode_and_missing_cpu_load():
    e = estimate(state(mode="unknown"), None)
    assert e.source == "unknown" and e.runtime_h is None
    assert not e.cpu_measured
    assert e.rails[0].amps > ESP_IDLE_A + 2 * ESP_BUSY_PER_CORE_A * 0.05  # 10 % per core assumed
