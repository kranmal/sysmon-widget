"""Transparent desktop system monitor: CPU, RAM, GPU, temps, disk, WiFi, network.

Drag with left mouse. Right-click for menu (opacity, click-through, quit).
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

import psutil
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

HIST = 60  # samples (1/s)
W, H = 300, 560
LLM_UNIT = "llama-server"
LLM_URL = "http://127.0.0.1:8080/health"


def hwmon(name):
    for p in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(f"{p}/name").read().strip() == name:
                return p
        except OSError:
            pass
    return None


def rd(path, default=None, scale=1.0):
    try:
        return float(open(path).read().strip()) / scale
    except (OSError, ValueError):
        return default


def human(bps):
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:5.1f} {unit}"
        bps /= 1024
    return f"{bps:5.1f} TB/s"


def fmt_size(b):
    g = b / 2**30
    if g >= 1000:
        return f"{g / 1024:.2f}T"
    if g >= 10:
        return f"{g:.0f}G"
    if g >= 1:
        return f"{g:.1f}G"
    return f"{b / 2**20:.0f}M"


def scan_disks():
    """Return [{name, model, size, parts:[{label, mount|None, size, locked}]}]."""
    try:
        out = subprocess.run(
            ["lsblk", "-b", "-J", "-e7,11", "-o",
             "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,LABEL"],
            capture_output=True, text=True, timeout=3).stdout
        nodes = json.loads(out)["blockdevices"]
    except Exception:
        return []
    disks = []
    for d in nodes:
        if d.get("type") != "disk" or d["name"].startswith("zram"):
            continue
        parts = []
        for c in d.get("children") or [d]:
            fs = c.get("fstype")
            mps = [m for m in (c.get("mountpoints") or []) if m and not m.startswith("[")]
            size = int(c.get("size") or 0)
            if mps:
                mp = "/var/home" if "/var/home" in mps else min(mps, key=len)
                parts.append({"label": mp, "mount": mp, "size": size})
            elif fs == "crypto_LUKS":
                parts.append({"label": f"{c['name']} (LUKS)", "mount": None,
                              "size": size, "locked": True})
        disks.append({"name": d["name"], "model": (d.get("model") or "").strip(),
                      "size": int(d.get("size") or 0), "parts": parts})
    return disks


def find_gpu_dev():
    for p in glob.glob("/sys/class/drm/card*/device"):
        if os.path.exists(f"{p}/gpu_busy_percent"):
            return p
    return None


class Monitor(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("sysmon")
        self.resize(W, H)

        self.k10 = hwmon("k10temp")
        self.amd = hwmon("amdgpu")
        self.nvme = hwmon("nvme")
        self.nct = next((h for h in (hwmon(n) for n in
                         ("nct6797", "nct6798", "nct6796", "nct6795", "it8688", "it8686", "it8628"))
                         if h), None)
        self.gpu_dev = find_gpu_dev()
        self.wifi_if = next(
            (i for i in psutil.net_if_stats() if i.startswith("wl")), None
        )
        self.eth_if = next(
            (i for i in sorted(psutil.net_if_stats())
             if i.startswith(("en", "eth")) and not i.startswith(("enx_",))), None
        )
        self.rates = {}

        self.bg_alpha = 90
        self.click_through = False
        self.hist = {k: deque([0.0] * HIST, maxlen=HIST)
                     for k in ("cpu", "ram", "gpu", "vram", "down", "up")}
        self.cores = [0.0] * psutil.cpu_count()
        self.s = {}
        self.ssid = "-"
        self._ssid_t = 0
        self._net = psutil.net_io_counters(pernic=True)
        self._net_t = time.time()
        psutil.cpu_percent(percpu=True)
        self.disks = []
        self._disk_t = 0.0
        self.llm_state = "off"   # off | loading | running | stopping | failed
        self.llm_busy_until = 0.0
        self.llm_ram = 0.0
        self.btn_rect = QRectF(W - 14 - 64, 0, 64, 20)
        threading.Thread(target=self.llm_poll, daemon=True).start()

        t = QTimer(self)
        t.timeout.connect(self.sample)
        t.start(1000)
        self.sample()

    # ---- llm ----
    def llm_poll(self):
        while True:
            try:
                st = subprocess.run(
                    ["systemctl", "--user", "is-active", LLM_UNIT],
                    capture_output=True, text=True, timeout=3).stdout.strip()
                if st == "active":
                    try:
                        with urllib.request.urlopen(LLM_URL, timeout=0.5) as r:
                            state = "running" if r.status == 200 else "loading"
                    except Exception:
                        state = "loading"
                elif st in ("activating", "reloading"):
                    state = "loading"
                elif st == "deactivating":
                    state = "stopping"
                elif st == "failed":
                    state = "failed"
                else:
                    state = "off"
                ram = 0.0
                for pr in psutil.process_iter(["name", "memory_info"]):
                    if pr.info["name"] == "llama-server" and pr.info["memory_info"]:
                        ram += pr.info["memory_info"].rss / 2**30
                if time.time() > self.llm_busy_until:
                    self.llm_state = state
                self.llm_ram = ram
            except Exception:
                pass
            time.sleep(1.5)

    def llm_toggle(self):
        on = self.llm_state in ("running", "loading")
        action = "stop" if on else "start"
        subprocess.Popen(["systemctl", "--user", action, LLM_UNIT],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.llm_state = "stopping" if on else "loading"
        self.llm_busy_until = time.time() + 3
        self.update()

    # ---- data ----
    def sample(self):
        s = self.s
        self.cores = psutil.cpu_percent(percpu=True)
        s["cpu"] = sum(self.cores) / len(self.cores)
        freq = psutil.cpu_freq()
        s["cpu_mhz"] = freq.current if freq else 0
        s["cpu_t"] = rd(f"{self.k10}/temp1_input", scale=1000) if self.k10 else None
        s["load"] = os.getloadavg()[0]

        vm = psutil.virtual_memory()
        s["ram"] = vm.percent
        s["ram_used"] = vm.used / 2**30
        s["ram_total"] = vm.total / 2**30
        sw = psutil.swap_memory()
        s["swap_used"] = sw.used / 2**30

        if self.gpu_dev:
            s["gpu"] = rd(f"{self.gpu_dev}/gpu_busy_percent", 0)
            vu = rd(f"{self.gpu_dev}/mem_info_vram_used", 0)
            vt = rd(f"{self.gpu_dev}/mem_info_vram_total", 1)
            s["vram_used"], s["vram_total"] = vu / 2**30, vt / 2**30
            s["vram"] = 100 * vu / vt if vt else 0
        if self.amd:
            s["gpu_t"] = rd(f"{self.amd}/temp1_input", scale=1000)
            s["gpu_w"] = rd(f"{self.amd}/power1_average",
                            rd(f"{self.amd}/power1_input", scale=1e6), 1e6)
            s["gpu_mhz"] = rd(f"{self.amd}/freq1_input", scale=1e6)
            s["gpu_fan"] = rd(f"{self.amd}/fan1_input")
        if self.nvme:
            s["nvme_t"] = rd(f"{self.nvme}/temp1_input", scale=1000)
        if self.nct:
            s["mb_t"] = rd(f"{self.nct}/temp1_input", scale=1000)

        if time.time() - self._disk_t > 10 or not self.disks:
            self._disk_t = time.time()
            self.disks = scan_disks()
        rows = 0
        for d in self.disks:
            rows += 1
            for pt in d["parts"]:
                rows += 1
                if pt.get("mount"):
                    try:
                        u = psutil.disk_usage(pt["mount"])
                        pt.update(total=u.total, used=u.used, free=u.free,
                                  pct=u.percent)
                    except OSError:
                        pt["pct"] = None
        s["eth"] = None
        if self.eth_if:
            base = f"/sys/class/net/{self.eth_if}"
            try:
                carrier = open(f"{base}/carrier").read().strip() == "1"
            except OSError:
                carrier = False
            speed = rd(f"{base}/speed") if carrier else None
            ip = ""
            if carrier:
                for a in psutil.net_if_addrs().get(self.eth_if, []):
                    if a.family.name == "AF_INET":
                        ip = a.address
            s["eth"] = {"up": carrier, "speed": speed, "ip": ip}
        eth_extra = 22 + (22 if s["eth"] and s["eth"]["up"] else 0) if self.eth_if else 0
        need = 504 + eth_extra + 22 * sum(1 for _ in self.disks) + 30 * (rows - len(self.disks))
        if need != self.height():
            self.resize(W, need)

        now = time.time()
        cur = psutil.net_io_counters(pernic=True)
        dt = max(now - self._net_t, 1e-3)
        down = up = 0.0
        self.rates = {}
        for nic, c in cur.items():
            if nic == "lo" or nic.startswith(("veth", "podman", "docker", "br-")):
                continue
            p = self._net.get(nic)
            if p:
                down += (c.bytes_recv - p.bytes_recv) / dt
                up += (c.bytes_sent - p.bytes_sent) / dt
                self.rates[nic] = ((c.bytes_recv - p.bytes_recv) / dt,
                                   (c.bytes_sent - p.bytes_sent) / dt)
        self._net, self._net_t = cur, now
        s["down"], s["up"] = down, up

        s["wifi"] = None
        if self.wifi_if and now - self._ssid_t > 3:
            self._ssid_t = now
            self.ssid, self._sig = "-", None
            try:
                out = subprocess.run(
                    ["nmcli", "-t", "-e", "yes", "-f", "active,ssid,signal",
                     "dev", "wifi"], capture_output=True, text=True,
                    timeout=2).stdout
                for l in out.splitlines():
                    if l.startswith("yes:"):
                        _, rest = l.split(":", 1)
                        ssid, sig = rest.rsplit(":", 1)
                        self.ssid, self._sig = ssid.replace("\\:", ":"), float(sig)
                        break
            except Exception:
                pass
        s["wifi"] = getattr(self, "_sig", None)

        for k in ("cpu", "ram", "gpu", "vram"):
            self.hist[k].append(s.get(k, 0))
        self.hist["down"].append(down)
        self.hist["up"].append(up)
        self.update()

    # ---- drawing ----
    @staticmethod
    def color(pct):
        if pct < 60:
            return QColor("#5fe3a1")
        if pct < 85:
            return QColor("#ffcf5c")
        return QColor("#ff6b6b")

    @staticmethod
    def temp_color(t):
        if t is None:
            return QColor("#aaaaaa")
        if t < 60:
            return QColor("#5fe3a1")
        if t < 80:
            return QColor("#ffcf5c")
        return QColor("#ff6b6b")

    def text(self, p, x, y, s, color=QColor("white"), bold=False, size=10,
             align=Qt.AlignLeft, width=0):
        f = QFont("Noto Sans Mono", size)
        f.setBold(bold)
        p.setFont(f)
        r = QRectF(x, y - 14, width or 300, 18)
        p.setPen(QColor(0, 0, 0, 200))
        p.drawText(r.translated(1, 1), align | Qt.AlignVCenter, s)
        p.setPen(color)
        p.drawText(r, align | Qt.AlignVCenter, s)

    def bar(self, p, x, y, w, pct, color):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(QRectF(x, y, w, 6), 3, 3)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(x, y, w * min(max(pct, 0), 100) / 100, 6), 3, 3)

    def spark(self, p, x, y, w, h, data, color, maxv=100.0):
        data = list(data)
        maxv = max(maxv, 1e-9)
        path = QPainterPath()
        n = len(data)
        for i, v in enumerate(data):
            px = x + w * i / (n - 1)
            py = y + h - h * min(v / maxv, 1.0)
            path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
        fill = QPainterPath(path)
        fill.lineTo(x + w, y + h)
        fill.lineTo(x, y + h)
        fill.closeSubpath()
        c = QColor(color)
        c.setAlpha(50)
        p.setPen(Qt.NoPen)
        p.fillPath(fill, c)
        p.setPen(QPen(color, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def section(self, p, y, title):
        self.text(p, 14, y, title, QColor("#9ad1ff"), bold=True, size=9)
        p.setPen(QPen(QColor(255, 255, 255, 50), 1))
        p.drawLine(80, int(y - 5), W - 14, int(y - 5))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        s = self.s
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 12, 18, self.bg_alpha))
        p.drawRoundedRect(QRectF(0, 0, W, self.height()), 14, 14)

        iw = W - 28
        y = 26
        self.text(p, 14, y, time.strftime("%H:%M:%S"), size=16, bold=True)
        self.text(p, 0, y, time.strftime("%a %d %b"), QColor("#cccccc"),
                  align=Qt.AlignRight, width=W - 14)

        # LLM
        y += 30
        self.section(p, y, "LOCAL LLM")
        y += 24
        st = self.llm_state
        col = {"running": QColor("#5fe3a1"), "loading": QColor("#ffcf5c"),
               "stopping": QColor("#ffcf5c"), "failed": QColor("#ff6b6b"),
               "off": QColor("#8a8f98")}[st]
        label = {"running": "RUNNING", "loading": "LOADING", "stopping": "STOPPING",
                 "failed": "FAILED", "off": "OFF"}[st]
        self.text(p, 14, y, f"● {label}", col, bold=True, size=11)
        if self.llm_ram > 0.05:
            self.text(p, 110, y, f"{self.llm_ram:.1f}G RAM", QColor("#cccccc"))
        self.btn_rect = QRectF(W - 14 - 64, y - 14, 64, 20)
        on = st in ("running", "loading")
        bc = QColor("#ff6b6b") if on else QColor("#5fe3a1")
        p.setPen(QPen(bc, 1.2))
        fc = QColor(bc)
        fc.setAlpha(45)
        p.setBrush(fc)
        p.drawRoundedRect(self.btn_rect, 6, 6)
        self.text(p, self.btn_rect.x(), y, "STOP" if on else "START", bc, bold=True,
                  size=9, align=Qt.AlignCenter, width=64)
        y += 4

        # CPU
        y += 30
        self.section(p, y, "CPU")
        y += 20
        c = self.color(s["cpu"])
        self.text(p, 14, y, f"{s['cpu']:5.1f}%", c, bold=True, size=13)
        t = s.get("cpu_t")
        self.text(p, 0, y, f"{t:.0f}°C  {s['cpu_mhz']/1000:.2f}GHz" if t else "",
                  self.temp_color(t), align=Qt.AlignRight, width=W - 14)
        y += 8
        self.spark(p, 14, y, iw, 30, self.hist["cpu"], c)
        y += 38
        bw = (iw - 11 * 3) / 12
        for i, v in enumerate(self.cores):
            bx = 14 + i * (bw + 3)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 40))
            p.drawRoundedRect(QRectF(bx, y, bw, 22), 2, 2)
            p.setBrush(self.color(v))
            hh = 22 * v / 100
            p.drawRoundedRect(QRectF(bx, y + 22 - hh, bw, hh), 2, 2)
        y += 38

        # RAM
        self.section(p, y, "RAM")
        y += 20
        c = self.color(s["ram"])
        self.text(p, 14, y, f"{s['ram_used']:4.1f}/{s['ram_total']:.0f} GB", c, bold=True)
        self.text(p, 0, y, f"swap {s['swap_used']:.1f}G", QColor("#cccccc"),
                  align=Qt.AlignRight, width=W - 14)
        y += 8
        self.bar(p, 14, y, iw, s["ram"], c)
        y += 26

        # GPU
        self.section(p, y, "GPU")
        y += 20
        if "gpu" in s:
            c = self.color(s["gpu"])
            self.text(p, 14, y, f"{s['gpu']:5.1f}%", c, bold=True, size=13)
            parts = []
            if s.get("gpu_t") is not None:
                parts.append(f"{s['gpu_t']:.0f}°C")
            if s.get("gpu_mhz"):
                parts.append(f"{s['gpu_mhz']:.0f}MHz")
            self.text(p, 0, y, "  ".join(parts), self.temp_color(s.get("gpu_t")),
                      align=Qt.AlignRight, width=W - 14)
            y += 8
            self.spark(p, 14, y, iw, 30, self.hist["gpu"], c)
            y += 44
            self.text(p, 14, y, f"VRAM {s['vram_used']:.1f}/{s['vram_total']:.0f} GB",
                      self.color(s["vram"]))
            extra = []
            if s.get("gpu_w") is not None:
                extra.append(f"{s['gpu_w']:.0f}W")
            if s.get("gpu_fan") is not None:
                extra.append(f"{s['gpu_fan']:.0f}rpm")
            self.text(p, 0, y, "  ".join(extra), QColor("#cccccc"),
                      align=Qt.AlignRight, width=W - 14)
            y += 8
            self.bar(p, 14, y, iw, s["vram"], self.color(s["vram"]))
            y += 26
        else:
            self.text(p, 14, y, "no GPU sensor", QColor("#aaaaaa"))
            y += 30

        # TEMPS / DISK
        self.section(p, y, "TEMPS / STORAGE")
        y += 20
        col = [("NVMe", s.get("nvme_t")), ("Board", s.get("mb_t"))]
        for i, (n, t) in enumerate(col):
            self.text(p, 14 + i * 140, y, f"{n} " + (f"{t:.0f}°C" if t else "-"),
                      self.temp_color(t))
        y += 6
        for d in self.disks:
            y += 20
            model = f" {d['model'][:14]}" if d["model"] else ""
            self.text(p, 14, y, f"{d['name']}{model}", QColor("#cfd6e0"), bold=True,
                      size=9)
            self.text(p, 0, y, fmt_size(d["size"]), QColor("#cfd6e0"), size=9,
                      align=Qt.AlignRight, width=W - 14)
            for pt in d["parts"]:
                y += 22
                if pt.get("locked"):
                    self.text(p, 14, y, f"{pt['label']}", QColor("#8a8f98"), size=9)
                    self.text(p, 0, y, f"locked · {fmt_size(pt['size'])}",
                              QColor("#8a8f98"), size=9, align=Qt.AlignRight,
                              width=W - 14)
                    y += 6
                    continue
                pct = pt.get("pct")
                if pct is None:
                    self.text(p, 14, y, pt["label"], QColor("#8a8f98"), size=9)
                    y += 6
                    continue
                c = self.color(pct)
                self.text(p, 14, y, f"{pt['label'][:14]} {pct:.0f}%", c, size=9)
                self.text(p, 0, y,
                          f"{fmt_size(pt['free'])} free / {fmt_size(pt['total'])}",
                          QColor("#cccccc"), size=9, align=Qt.AlignRight, width=W - 14)
                y += 6
                self.bar(p, 14, y, iw, pct, c)
        y += 26

        # NETWORK
        self.section(p, y, "NETWORK")
        y += 20
        sig = s.get("wifi")
        if sig is not None:
            wc = self.color(100 - sig)
            self.text(p, 14, y, f"WiFi {self.ssid[:16]}", wc, bold=True)
            self.text(p, 0, y, f"{sig:.0f}%", wc, align=Qt.AlignRight, width=W - 14)
        else:
            self.text(p, 14, y, "WiFi: down", QColor("#aaaaaa"))
        y += 22
        if self.eth_if and s.get("eth") is not None:
            e = s["eth"]
            if e["up"]:
                ec = QColor("#5fe3a1")
                sp = e["speed"]
                spt = (f"{sp/1000:g} Gb/s" if sp and sp >= 1000
                       else f"{sp:.0f} Mb/s" if sp else "up")
                self.text(p, 14, y, f"Eth {self.eth_if}", ec, bold=True)
                self.text(p, 0, y, spt, ec, align=Qt.AlignRight, width=W - 14)
                y += 20
                d, u = self.rates.get(self.eth_if, (0, 0))
                self.text(p, 14, y, f"{e['ip']}", QColor("#cccccc"), size=9)
                self.text(p, 130, y, f"↓{human(d).strip()}", QColor("#7fd0ff"), size=9)
                self.text(p, 0, y, f"↑{human(u).strip()}", QColor("#ffb86b"), size=9,
                          align=Qt.AlignRight, width=W - 14)
            else:
                self.text(p, 14, y, f"Eth {self.eth_if}", QColor("#8a8f98"))
                self.text(p, 0, y, "no cable", QColor("#8a8f98"),
                          align=Qt.AlignRight, width=W - 14)
            y += 22
        peak = max(max(self.hist["down"]), max(self.hist["up"]), 100e3)
        self.text(p, 14, y, f"Σ ↓ {human(s['down'])}", QColor("#7fd0ff"))
        self.text(p, 0, y, f"Σ ↑ {human(s['up'])}", QColor("#ffb86b"),
                  align=Qt.AlignRight, width=W - 14)
        y += 8
        self.spark(p, 14, y, iw, 28, self.hist["down"], QColor("#7fd0ff"), peak)
        self.spark(p, 14, y, iw, 28, self.hist["up"], QColor("#ffb86b"), peak)
        p.end()

    # ---- interaction ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self.btn_rect.contains(e.position()):
                self.llm_toggle()
            elif self.windowHandle():
                self.windowHandle().startSystemMove()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        for label, a in (("Opacity: clear", 0), ("Opacity: light", 60),
                         ("Opacity: medium", 130), ("Opacity: dark", 200)):
            act = QAction(label, m)
            act.triggered.connect(lambda _=False, a=a: self.set_alpha(a))
            m.addAction(act)
        m.addSeparator()
        ct = QAction("Click-through (restart to undo)", m)
        ct.triggered.connect(self.enable_click_through)
        m.addAction(ct)
        q = QAction("Quit", m)
        q.triggered.connect(QApplication.quit)
        m.addAction(q)
        m.exec(e.globalPos())

    def set_alpha(self, a):
        self.bg_alpha = a
        self.update()

    def enable_click_through(self):
        self.setWindowFlag(Qt.WindowTransparentForInput, True)
        self.show()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("sysmon")
    app.setDesktopFileName("sysmon")
    w = Monitor()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
