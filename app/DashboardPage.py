"""Dashboard: live metric cards, lamp and alarm status, link summary, controller hardware."""

import time

import flet as ft

import Theme
from Catalog import METRICS, format_value, live_health
from Charts import Sparkline
from Meter import target_for, unit_label
from PowerModel import estimate
from Store import now_ms

SPARK_WINDOW_MS = 10 * 60 * 1000
CORE_ROLES = ("Core 0 · radios, settings", "Core 1 · Safety, acquisition")


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:,.0f} kB"


def _level(value: float, warn: float, bad: float) -> str:
    return Theme.BAD if value >= bad else Theme.WARN if value >= warn else Theme.GOOD


class HardwarePanel:
    """Controller hardware: CPU load, RAM, storage (get_diagnostics) and the power estimate."""

    def __init__(self):
        def big():
            return ft.Text("—", size=20, weight=ft.FontWeight.BOLD, color=Theme.TEXT)

        def bar():
            return ft.ProgressBar(value=0, height=8, border_radius=4, bgcolor=Theme.tint(Theme.NEUTRAL, 0.18))

        def figure():
            return ft.Text("—", size=13, weight=ft.FontWeight.W_600, color=Theme.TEXT)

        def labelled(label, text, line_bar):
            return [ft.Row([Theme.caption(label), text], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), line_bar]
        self.status = Theme.caption("")
        self.cpu_bars = [bar() for _ in CORE_ROLES]
        self.cpu_texts = [figure() for _ in CORE_ROLES]
        self.ram, self.ram_bar, self.ram_detail = big(), bar(), Theme.caption("")
        self.psram_text, self.psram_bar = figure(), bar()
        self.nvs_text, self.nvs_bar = figure(), bar()
        self.app_partition = Theme.caption("")
        self.power, self.power_detail = big(), Theme.caption("")

        cpu_rows = []
        for role, cpu_bar, text in zip(CORE_ROLES, self.cpu_bars, self.cpu_texts):
            cpu_rows += labelled(role, text, cpu_bar)
        storage_rows = (labelled("PSRAM", self.psram_text, self.psram_bar)
                        + labelled("NVS settings", self.nvs_text, self.nvs_bar) + [self.app_partition])
        self.control = ft.Column([
            ft.Row([Theme.title("Controller hardware"), self.status], wrap=True, spacing=14,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([
                self._card(ft.Icons.MEMORY_ROUNDED, "CPU load", cpu_rows),
                self._card(ft.Icons.DEVELOPER_BOARD_ROUNDED, "RAM", [self.ram, self.ram_bar, self.ram_detail]),
                self._card(ft.Icons.SD_STORAGE_OUTLINED, "Storage", storage_rows),
                self._card(ft.Icons.BOLT_ROUNDED, "Power (estimate)", [
                    self.power, self.power_detail,
                    Theme.caption("Estimated from the power tree, not measured. Excludes battery charging and "
                                  "the lamp.", 11)]),
            ], wrap=True, spacing=12, run_spacing=12, vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=12)

    @staticmethod
    def _card(icon, label, controls) -> ft.Control:
        return Theme.card([ft.Row([ft.Icon(icon, size=18, color=Theme.MUTED), Theme.overline(label)], spacing=6),
                           *controls], width=300, padding=16, spacing=8)

    def paint(self, session) -> None:
        d = session.diagnostics
        if not session.diagnostics_supported:
            self.status.value = "This firmware does not report hardware diagnostics (needs 0.10.0 or later)."
        elif d is None:
            self.status.value = "Waiting for the controller…" if session.can_command else "Needs Bluetooth"
        else:
            age = time.monotonic() - session.diagnostics_at
            self.status.value = f"Updated {Theme.ago(age)}" + ("" if session.diagnostics_fresh else " · stale")
        self._cpu(d)
        self._ram(d)
        self._storage(d)
        self._power(session)

    def _cpu(self, d) -> None:
        cores = d["cpu"] if d else None
        for i, (cpu_bar, text) in enumerate(zip(self.cpu_bars, self.cpu_texts)):
            if not cores or i >= len(cores):
                cpu_bar.value, text.value = 0, "—"
                continue
            load, peak = cores[i]["load"], cores[i]["peak"]
            cpu_bar.value, cpu_bar.color = load / 100, _level(peak, 60, 80)
            text.value = f"{load} % · peak {peak} %"

    def _ram(self, d) -> None:
        heap = d["heap"] if d else None
        if heap is None or not heap["total"]:
            self.ram.value, self.ram_bar.value, self.ram_detail.value = "—", 0, ""
            return
        used = 100 * (heap["total"] - heap["free"]) / heap["total"]
        self.ram.value = f"{used:.0f} % used"
        self.ram_bar.value, self.ram_bar.color = used / 100, _level(used, 80, 90)
        self.ram_detail.value = f"{_size(heap['free'])} free of {_size(heap['total'])}"

    @staticmethod
    def _fill(line_bar, text, used, total, label) -> None:
        if not total:
            line_bar.value, text.value = 0, "—"
            return
        pct = 100 * used / total
        line_bar.value, line_bar.color = pct / 100, _level(pct, 80, 90)
        text.value = label

    def _storage(self, d) -> None:
        psram, flash = (d["psram"], d["flash"]) if d else (None, None)
        if psram:
            used = psram["total"] - psram["free"]
            self._fill(self.psram_bar, self.psram_text, used, psram["total"],
                       f"{_size(used)} of {_size(psram['total'])}")
        else:
            self._fill(self.psram_bar, self.psram_text, 0, 0, "")
        if flash:
            self._fill(self.nvs_bar, self.nvs_text, flash["nvs_used"], flash["nvs_total"],
                       f"{flash['nvs_used']} of {flash['nvs_total']} entries")
            self.app_partition.value = f"App partition {_size(flash['app_partition'])}"
        else:
            self._fill(self.nvs_bar, self.nvs_text, 0, 0, "")
            self.app_partition.value = ""

    def _power(self, session) -> None:
        if session.state is None:
            self.power.value, self.power_detail.value = "—", ""
            return
        e = estimate(session.state, session.diagnostics, session.connected)
        self.power.value = f"≈ {e.input_watts:.2f} W"
        source = {"usb": "USB 5 V supply", "battery": "Battery (LiPo 3S)"}.get(e.source, "Source unknown")
        detail = f"{source} · {e.input_amps * 1000:,.0f} mA at {e.input_volts:g} V"
        if e.runtime_h is not None:
            detail += f"\nAbout {e.runtime_h:.0f} h on a full battery"
        if not e.cpu_measured:
            detail += "\nCPU load not reported: assumed"
        self.power_detail.value = detail


class MetricCard:
    def __init__(self, info):
        self.info = info
        self.value = ft.Text("—", size=30, weight=ft.FontWeight.BOLD, color=Theme.TEXT)
        self.unit = ft.Text("", size=13, color=Theme.MUTED)
        self.health = ft.Container()
        self.band = ft.ProgressBar(value=0, height=6, border_radius=3, bgcolor=Theme.tint(Theme.NEUTRAL, 0.18))
        self.band_label = ft.Text("", size=11, color=Theme.MUTED)
        self.updated = ft.Text("", size=11, color=Theme.MUTED)
        self.spark = Sparkline(110, 34)
        self.control = Theme.card([
            ft.Row([ft.Icon(getattr(ft.Icons, info.icon, ft.Icons.SENSORS_ROUNDED), color=ft.Colors.PRIMARY,
                            size=20),
                    ft.Text(info.label, size=14, weight=ft.FontWeight.W_600, color=Theme.TEXT, expand=True),
                    self.health], spacing=8),
            ft.Row([ft.Row([self.value, self.unit], spacing=6, tight=True,
                           vertical_alignment=ft.CrossAxisAlignment.END, expand=True),
                    self.spark.control], vertical_alignment=ft.CrossAxisAlignment.END),
            self.band,
            ft.Row([self.band_label, self.updated], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ], width=300, padding=16, spacing=10)

    def paint(self, reading: dict | None, enabled: bool, age_s: float | None, spark: list) -> None:
        if not enabled:
            health, value, unit = "DISABLED", None, ""
        elif reading is None:
            health, value, unit = "NO_DATA", None, ""
        else:
            health, value, unit = reading["health"], reading["value"], reading["unit"]
            # Age it here too: the last frame's value must not stay "Live" once frames stop.
            health = live_health(self.info.key, health, age_s)
            if health == "STALE":
                value = None
        target = target_for(self.info.key, unit)
        # No label for normal operation (Live): only states that need attention, Uncalibrated included.
        self.health.content = None if health in Theme.QUIET_HEALTH else Theme.health_chip(health)
        self.value.value = format_value(self.info.key, value)
        self.unit.value = unit_label(unit) if value is not None else ""
        status = target.status(value, unit) if target and value is not None else None
        color = Theme.BAND_COLOR.get(status, Theme.SEED if value is not None else Theme.NEUTRAL)
        self.value.color = Theme.TEXT if value is not None else Theme.MUTED
        self.band.value = target.fraction(value) if target and value is not None and unit == target.unit else 0
        self.band.color = color
        if status:
            self.band_label.value = f"{status} · {target.caption}"
        elif target and unit and unit != target.unit:
            self.band_label.value = f"{unit_label(unit)} · calibrate on the Calibration page"
        else:
            self.band_label.value = target.caption if target else ""
        if not enabled:
            self.updated.value = "Switched off"
        elif reading is None:
            self.updated.value = "Waiting for data"
        else:
            self.updated.value = Theme.ago(age_s)
        self.spark.set_points(spark if enabled else [], color)


class DashboardPage:
    name = "Dashboard"

    def __init__(self, app):
        self.app = app
        self.cards = {info.key: MetricCard(info) for info in METRICS}
        self.lamp_value = ft.Text("", size=20, weight=ft.FontWeight.BOLD, color=Theme.TEXT)
        self.lamp_reason = Theme.caption("")
        self.link_value = ft.Text("", size=20, weight=ft.FontWeight.BOLD, color=Theme.TEXT)
        self.link_detail = Theme.caption("")
        self.alarm_value = ft.Text("", size=20, weight=ft.FontWeight.BOLD, color=Theme.TEXT)
        self.alarm_detail = Theme.caption("")
        self.power_value = ft.Text("", size=20, weight=ft.FontWeight.BOLD, color=Theme.TEXT)
        self.power_detail = Theme.caption("")
        self.recorded_value = ft.Text("", size=20, weight=ft.FontWeight.BOLD, color=Theme.TEXT)
        self.recorded_detail = Theme.caption("")
        self.alarm_banner = ft.Column([], spacing=8)
        self.hardware = HardwarePanel()
        self.root = None
        self.live = False
        self.last_spark = 0.0
        self.spark_cache: dict[str, list] = {}

    def build(self) -> ft.Control:
        self.live = self._has_data()
        if not self.live:
            self.root = Theme.empty_state(
                ft.Icons.SENSORS_ROUNDED, "No controller connected",
                "Connect to your ESP32 controller over Bluetooth. Recorded history stays available in Metrics.",
                [ft.FilledButton("Find a controller", icon=ft.Icons.SEARCH_ROUNDED,
                                 on_click=lambda e: self.app.navigate("Connection")),
                 ft.OutlinedButton("Open Metrics", icon=ft.Icons.INSIGHTS_ROUNDED,
                                   on_click=lambda e: self.app.navigate("Metrics"))])
            return self.root

        def summary(icon, label, value, detail):
            return Theme.card([ft.Row([ft.Icon(icon, size=18, color=Theme.MUTED), Theme.overline(label)], spacing=6),
                               value, detail], width=228, padding=16, spacing=6)

        self.root = ft.Column([
            self.alarm_banner,
            ft.Row([
                summary(ft.Icons.LINK_ROUNDED, "Data link", self.link_value, self.link_detail),
                summary(ft.Icons.POWER_ROUNDED, "Power", self.power_value, self.power_detail),
                summary(ft.Icons.LIGHTBULB_OUTLINE_ROUNDED, "Grow lamp", self.lamp_value, self.lamp_reason),
                summary(ft.Icons.NOTIFICATIONS_ACTIVE_OUTLINED, "Alarms", self.alarm_value, self.alarm_detail),
                summary(ft.Icons.STORAGE_ROUNDED, "Recorded here", self.recorded_value, self.recorded_detail),
            ], wrap=True, spacing=12, run_spacing=12),
            ft.Row([Theme.title("Live readings"),
                    Theme.caption("Bands are indicative, not crop advice. Values update every 3 s.")],
                   wrap=True, spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([card.control for card in self.cards.values()], wrap=True, spacing=12, run_spacing=12),
            self.hardware.control,
        ], spacing=16)
        self.refresh("all")
        return self.root

    def _has_data(self) -> bool:
        return self.app.session.connected or self.app.session.state is not None

    def needs_rebuild(self) -> bool:
        return self.root is not None and self.live != self._has_data()

    def refresh(self, kind: str) -> list:
        s = self.app.session
        if self.root is None or not self.live:
            return []
        state = s.state or {}
        safety = state.get("safety", {})
        self.link_value.value = "Bluetooth" if s.connected else "Offline"
        if s.connected:
            self.link_detail.value = ("No telemetry: readings are not updating" if not s.telemetry_fresh
                                      else "Commands available" if s.can_command else "State stale")
        else:
            self.link_detail.value = "Reconnecting…" if s.ble_reconnecting else "Not connected"
        label, color, _icon = Theme.power_style(s.power_mode)
        self.power_value.value = {"normal": "Normal", "low_power": "Low power", "degraded": "Degraded",
                                  "unknown": "Not reported"}.get(s.power_mode, "Switching…")
        self.power_value.color = color
        self.power_detail.value = ("External 5 V off: pH, TDS, level and Wi-Fi paused"
                                   if s.power_mode in ("low_power", "transition_to_low") else label)
        relay = safety.get("relay")
        self.lamp_value.value = "On" if relay else "Off"
        self.lamp_value.color = Theme.GOOD if relay else Theme.TEXT
        self.lamp_reason.value = f"Reason: {safety.get('lamp_reason') or 'unknown'}"
        latched, active = safety.get("latched", []), safety.get("active", [])
        held = sorted(set(latched) | set(active))
        self.alarm_value.value = "None" if not held else f"{len(held)} rule{'s' if len(held) > 1 else ''}"
        self.alarm_value.color = Theme.BAD if held else Theme.GOOD
        self.alarm_detail.value = ("Interlock: lamp held off" if safety.get("interlock")
                                   else "No interlock" if state else "No state yet")
        self.recorded_value.value = f"{s.store.count(s.node) if s.node else 0:,}"
        self.recorded_detail.value = "samples for this controller"
        self._alarm_banner(latched, active)
        self.hardware.paint(s)

        enabled = s.enabled
        refresh_spark = time.monotonic() - self.last_spark > 9 or kind == "all"
        if refresh_spark and s.node:
            since = now_ms() - SPARK_WINDOW_MS
            self.spark_cache = {key: s.store.series(s.node, key, since, None, 60) for key in self.cards}
            self.last_spark = time.monotonic()
        for key, card in self.cards.items():
            reading = s.readings.get(key)
            sensor = card.info.sensor
            card.paint(reading, sensor in enabled, s.reading_age_s(reading) if reading else None,
                       self.spark_cache.get(key, []))
        return [self.root]

    def _alarm_banner(self, latched: list, active: list) -> None:
        rows = []
        for rule in sorted(set(latched) | set(active)):
            still = rule in active
            text = (f"Alarm rule {rule} is active: the condition is still present." if still else
                    f"Alarm rule {rule} is latched. The condition has cleared; reset it to release the interlock.")
            reset = ft.FilledButton("Reset", icon=ft.Icons.RESTART_ALT_ROUNDED,
                                    disabled=still or not self.app.session.can_command,
                                    tooltip=None if self.app.session.can_command else "Needs Bluetooth",
                                    on_click=lambda e, r=rule: self.app.run(self._reset, r))
            rows.append(Theme.banner(text, Theme.BAD, ft.Icons.WARNING_AMBER_ROUNDED, reset))
        self.alarm_banner.controls = rows

    async def _reset(self, rule: int) -> None:
        try:
            await self.app.session.reset_latch(rule)
            self.app.notice(f"Alarm rule {rule} reset.")
        except Exception as error:
            self.app.notice(self.app.explain(error))
