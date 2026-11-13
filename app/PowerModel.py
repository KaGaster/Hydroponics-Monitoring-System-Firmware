"""Power use estimated from the power tree: nothing on the board measures current.

Sources:
- Load currents and the converter chain: "Electrical System Study/Power-Tree
  Analysis.xlsx" (nominal values, i.e. without its x1.2 safety coefficient).
- ESP32-S3 CPU current: ESP32-S3 Series Datasheet v2.2, Table 5-9, Modem-sleep
  at 240 MHz with peripheral clocks enabled: 47.6 mA with both cores in WAITI,
  81.3 mA with both cores running. The measured load (get_diagnostics)
  interpolates between the two.
- Radio averages are ASSUMPTIONS (the datasheet only gives peaks: BLE RX 93 mA,
  Wi-Fi TX up to 340 mA): the radios are duty-cycled, so their average depends
  on connection and beacon intervals. Check them with a USB power meter.

Not included: battery charging (CN3303 / boost) and the grow lamp itself (the
relay only switches it; only its coil is on this board).
"""

from dataclasses import dataclass

V_USB = 5.0
V_DIODE_NODE = 4.475         # 5 V after the 1N5822 (D1 / D2)
V_3V3 = 3.3
ETA_3V3_BUCK = 0.85          # P3V3 (MP1584)
ETA_5V_BUCK = 0.85           # P5V00 (MP1584), battery path only
V_BATTERY = 11.1             # LiPo 3S nominal
BATTERY_AH = 5.2             # 5200 mAh, nominal capacity
BATTERY_AUX_A = 0.0000375    # backup-path battery auxiliaries, idle

ESP_IDLE_A = 0.0476          # both cores WAITI, 240 MHz
ESP_BUSY_PER_CORE_A = (0.0813 - 0.0476) / 2
ESP_CPU_LOAD_DEFAULT = 10    # % per core when the firmware does not report it
BLE_CONNECTED_A = 0.010      # assumption: connection events on a mostly idle link
WIFI_CONNECTED_A = 0.020     # assumption: associated station in modem sleep

# Sensors on the 3.3 V rail (power tree, nominal A), by firmware sensor key.
SENSORS_3V3 = {"bme280": 0.000714, "ds18b20": 0.0015, "sgp30": 0.0488, "light": 0.00033}
# Sensors on the USB 5 V rail: off in low power (the rail is gone).
SENSORS_5V = {"hcsr04": 0.015, "ph": 0.010, "tds": 0.006}
RELAY_COIL_A = 0.080         # SRD-05VDC on 5 V

BATTERY_MODES = ("transition_to_low", "low_power")
USB_MODES = ("normal", "transition_to_normal")


@dataclass(frozen=True)
class Rail:
    name: str
    volts: float
    amps: float              # current drawn by the loads on this rail

    @property
    def watts(self) -> float:
        return self.volts * self.amps


@dataclass(frozen=True)
class Estimate:
    rails: tuple[Rail, ...]
    source: str              # "usb", "battery" or "unknown"
    input_volts: float
    input_amps: float        # drawn from the source (USB 5 V or the LiPo)
    cpu_measured: bool
    runtime_h: float | None  # on battery only

    @property
    def input_watts(self) -> float:
        return self.input_volts * self.input_amps


def esp_current(cpu: list[dict] | None, ble: bool, wifi: bool) -> float:
    loads = [core["load"] for core in cpu] if cpu else [ESP_CPU_LOAD_DEFAULT] * 2
    amps = ESP_IDLE_A + sum(ESP_BUSY_PER_CORE_A * min(100, max(0, load)) / 100 for load in loads[:2])
    return amps + (BLE_CONNECTED_A if ble else 0.0) + (WIFI_CONNECTED_A if wifi else 0.0)


def estimate(state: dict, diagnostics: dict | None, ble_connected: bool = True) -> Estimate:
    mode = state.get("mode", "unknown")
    enabled = set(state.get("enabled", []))
    source = "battery" if mode in BATTERY_MODES else "usb" if mode in USB_MODES else "unknown"
    on_usb = source != "battery"
    wifi = on_usb and state.get("wifi", {}).get("state") == "connected"
    cpu = (diagnostics or {}).get("cpu")

    i_3v3 = esp_current(cpu, ble_connected, wifi) + sum(a for key, a in SENSORS_3V3.items() if key in enabled)
    i_5v = sum(a for key, a in SENSORS_5V.items() if key in enabled) if on_usb else 0.0
    i_relay = RELAY_COIL_A if on_usb and state.get("safety", {}).get("relay") else 0.0
    rails = (Rail("3.3 V (ESP32-S3 and I²C / 1-Wire sensors)", V_3V3, i_3v3),
             Rail("5 V sensors (HC-SR04, pH, TDS)", V_USB, i_5v),
             Rail("Relay coil", V_USB, i_relay))

    # P3V3 buck input, at the diode node; the diode passes the same current.
    i_buck_in = V_3V3 * i_3v3 / (ETA_3V3_BUCK * V_DIODE_NODE)
    if on_usb:
        return Estimate(rails, source, V_USB, i_buck_in + i_5v + i_relay, cpu is not None, None)
    # Battery: P5V00 buck feeds the 5 V node (Q2 and D2 pass the same current).
    i_battery = V_USB * i_buck_in / (ETA_5V_BUCK * V_BATTERY) + BATTERY_AUX_A
    return Estimate(rails, source, V_BATTERY, i_battery, cpu is not None, BATTERY_AH / i_battery)
