#!/usr/bin/env python3
"""scripts/wifi_manager.py

Wi-Fi Manager with Automatic AP Fallback and Captive Web Portal.
Targets Debian 12 with wlp108s0, ifupdown, iw, hostapd, dnsmasq.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("wifi_manager")

# Ensure sbin directories are in PATH for iw, ifup, ifdown, hostapd, dnsmasq
for p in ["/usr/local/sbin", "/usr/sbin", "/sbin"]:
    if p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{p}:{os.environ.get('PATH', '')}"

# Default Constants
DEFAULT_IFACE = "wlp108s0"
DEFAULT_AP_SSID = "Dragonfly"
DEFAULT_AP_PSK = "tonybucket0297"
DEFAULT_AP_IP = "192.168.4.1"
DEFAULT_AP_NETMASK = "255.255.255.0"
DEFAULT_AP_CIDR = "24"
DEFAULT_AP_CHANNEL = 6
DEFAULT_DHCP_START = "192.168.4.10"
DEFAULT_DHCP_END = "192.168.4.50"
DEFAULT_PING_TARGET = "1.1.1.1"
DEFAULT_CHECK_INTERVAL = 15.0
DEFAULT_MAX_FAILS = 3
DEFAULT_CONNECT_TIMEOUT = 30.0

DEFAULT_INTERFACES_FILE = "/etc/network/interfaces"
DEFAULT_RUNTIME_DIR = "/run/wifi_manager" if os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0) else "/tmp/wifi_manager"

STATE_STA_CONNECTED = "STA_CONNECTED"
STATE_AP_FALLBACK = "AP_FALLBACK"
STATE_CONNECTING = "CONNECTING"
STATE_ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------
@dataclass
class WifiNetwork:
    ssid: str
    bssid: str
    signal_dbm: float
    freq: int
    security: str

    @property
    def signal_percent(self) -> int:
        # Quality estimate: -100 dBm = 0%, -50 dBm = 100%
        if self.signal_dbm <= -100:
            return 0
        if self.signal_dbm >= -50:
            return 100
        return int(2 * (self.signal_dbm + 100))


@dataclass
class NetworkStatus:
    mode: str
    interface: str
    ip: Optional[str]
    ssid: Optional[str]
    gateway: Optional[str]
    ping_ok: bool
    ping_target: str
    last_check: float


# ---------------------------------------------------------------------------
# Configuration Generators
# ---------------------------------------------------------------------------
def generate_hostapd_conf(
    interface: str,
    ssid: str,
    passphrase: str,
    channel: int = 6,
) -> str:
    """Generate hostapd.conf for AP mode."""
    return (
        f"interface={interface}\n"
        "driver=nl80211\n"
        f"ssid={ssid}\n"
        "hw_mode=g\n"
        f"channel={channel}\n"
        "wmm_enabled=0\n"
        "macaddr_acl=0\n"
        "auth_algs=1\n"
        "ignore_broadcast_ssid=0\n"
        "wpa=2\n"
        f"wpa_passphrase={passphrase}\n"
        "wpa_key_mgmt=WPA-PSK\n"
        "wpa_pairwise=TKIP\n"
        "rsn_pairwise=CCMP\n"
    )


def generate_dnsmasq_conf(
    interface: str,
    ip: str = DEFAULT_AP_IP,
    range_start: str = DEFAULT_DHCP_START,
    range_end: str = DEFAULT_DHCP_END,
) -> str:
    """Generate dnsmasq.conf with captive portal DNS redirection."""
    return (
        f"interface={interface}\n"
        "bind-interfaces\n"
        f"dhcp-range={range_start},{range_end},{DEFAULT_AP_NETMASK},12h\n"
        f"dhcp-option=3,{ip}\n"
        f"dhcp-option=6,{ip}\n"
        f"address=/#/{ip}\n"
        "no-resolv\n"
        "no-poll\n"
    )


def update_interfaces_content(
    content: str,
    interface: str,
    ssid: str,
    psk: Optional[str] = None,
) -> str:
    """Safely update or insert an interface block in /etc/network/interfaces."""
    # Pattern to match:
    # iface <interface> inet dhcp ...
    # up until the next section (auto|allow-|iface|source) or EOF
    pattern = (
        rf"(iface\s+{re.escape(interface)}\s+inet\s+\w+)"
        rf"([\s\S]*?)(?=\n\s*(?:auto|allow-|iface|source)|$)"
    )

    new_block = f"iface {interface} inet dhcp\n\twpa-ssid {ssid}\n"
    if psk:
        new_block += f"\twpa-psk  {psk}\n"

    if re.search(pattern, content):
        return re.sub(pattern, new_block.rstrip("\n"), content, count=1)

    # If block not found, check if allow-hotplug / auto exists for interface
    hotplug_pattern = rf"allow-hotplug\s+{re.escape(interface)}"
    if not re.search(hotplug_pattern, content):
        new_block = f"\nallow-hotplug {interface}\n" + new_block
    else:
        new_block = "\n" + new_block

    return content.rstrip("\n") + "\n" + new_block.strip() + "\n"


# ---------------------------------------------------------------------------
# Network & System Helpers
# ---------------------------------------------------------------------------
def run_cmd(
    cmd: List[str],
    timeout: float = 10.0,
    check: bool = False,
) -> Tuple[int, str, str]:
    """Execute a system command and return (code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, proc.stdout, proc.stderr
            )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        logger.warning("Command %s timed out after %ss", cmd, timeout)
        return 124, "", "Command timed out"
    except FileNotFoundError:
        logger.debug("Binary for command %s not found", cmd[0])
        return 127, "", f"Binary not found: {cmd[0]}"
    except Exception as exc:
        logger.debug("Failed to run %s: %s", cmd, exc)
        return 1, "", str(exc)


def get_interface_ip(interface: str) -> Optional[str]:
    """Retrieve IPv4 address of interface."""
    code, out, _ = run_cmd(["ip", "-4", "-br", "addr", "show", "dev", interface])
    if code == 0 and out:
        parts = out.split()
        if len(parts) >= 3:
            return parts[2].split("/")[0]
    return None


def get_default_gateway(interface: Optional[str] = None) -> Optional[str]:
    """Retrieve default gateway IP."""
    cmd = ["ip", "route", "show", "default"]
    if interface:
        cmd.extend(["dev", interface])
    code, out, _ = run_cmd(cmd)
    if code == 0 and out:
        # e.g. "default via 192.168.1.1 dev wlp108s0"
        match = re.search(r"via\s+([0-9\.]+)", out)
        if match:
            return match.group(1)
    return None


def get_connected_ssid(interface: str) -> Optional[str]:
    """Retrieve current SSID via iw or wpa_cli."""
    code, out, _ = run_cmd(["iw", "dev", interface, "link"])
    if code == 0 and "Connected to" in out:
        match = re.search(r"SSID:\s*(.+)", out)
        if match:
            return match.group(1).strip()

    # Fallback to wpa_cli
    code, out, _ = run_cmd(["wpa_cli", "-i", interface, "status"])
    if code == 0:
        match = re.search(r"^ssid=(.+)$", out, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


def check_ping(target: str = DEFAULT_PING_TARGET, timeout: float = 2.0) -> bool:
    """Send a single ICMP ping to target."""
    code, _, _ = run_cmd(
        ["ping", "-c", "1", "-W", str(int(timeout)), target],
        timeout=timeout + 1.0,
    )
    return code == 0


def parse_iw_scan(scan_output: str) -> List[WifiNetwork]:
    """Parse output from `iw dev <iface> scan` into a deduplicated list."""
    networks_by_ssid: Dict[str, WifiNetwork] = {}
    current_bss: Optional[str] = None
    current_freq = 2412
    current_signal = -100.0
    current_ssid = ""
    is_privacy = False
    is_rsn = False
    is_wpa = False
    is_sae = False

    def commit_current():
        nonlocal current_bss, current_ssid, current_signal, current_freq
        nonlocal is_privacy, is_rsn, is_wpa, is_sae
        if not current_bss or not current_ssid:
            return

        # Sanitize ssid (ignore null bytes or non-printable)
        clean_ssid = current_ssid.replace("\x00", "").strip()
        if not clean_ssid:
            return

        if is_sae:
            sec = "WPA3"
        elif is_rsn:
            sec = "WPA2"
        elif is_wpa:
            sec = "WPA"
        elif is_privacy:
            sec = "WEP"
        else:
            sec = "Open"

        net = WifiNetwork(
            ssid=clean_ssid,
            bssid=current_bss,
            signal_dbm=current_signal,
            freq=current_freq,
            security=sec,
        )

        # Keep network with strongest signal
        if (
            clean_ssid not in networks_by_ssid
            or current_signal > networks_by_ssid[clean_ssid].signal_dbm
        ):
            networks_by_ssid[clean_ssid] = net

    for line in scan_output.splitlines():
        line_s = line.strip()
        bss_match = re.match(r"^BSS\s+([0-9a-fA-F:]{17})", line_s)
        if bss_match:
            commit_current()
            current_bss = bss_match.group(1).lower()
            current_freq = 2412
            current_signal = -100.0
            current_ssid = ""
            is_privacy = False
            is_rsn = False
            is_wpa = False
            is_sae = False
            continue

        if not current_bss:
            continue

        if line_s.startswith("freq:"):
            try:
                current_freq = int(line_s.split(":")[1].strip())
            except ValueError:
                pass
        elif line_s.startswith("signal:"):
            try:
                sig_part = line_s.split(":")[1].replace("dBm", "").strip()
                current_signal = float(sig_part)
            except ValueError:
                pass
        elif line_s.startswith("SSID:"):
            current_ssid = line_s[5:].strip()
        elif "capability:" in line_s and "Priv" in line_s:
            is_privacy = True
        elif line_s.startswith("RSN:"):
            is_rsn = True
        elif line_s.startswith("WPA:"):
            is_wpa = True
        elif "SAE" in line_s:
            is_sae = True

    commit_current()

    # Sort descending by signal strength
    sorted_nets = sorted(
        networks_by_ssid.values(),
        key=lambda n: n.signal_dbm,
        reverse=True,
    )
    return sorted_nets


def scan_wifi(interface: str) -> List[WifiNetwork]:
    """Trigger iw scan on interface."""
    cmd = ["iw", "dev", interface, "scan"]
    code, out, err = run_cmd(cmd, timeout=8.0)

    # If unprivileged and permission denied, try sudo -n if available
    if code != 0 and ("Operation not permitted" in err or "Permission denied" in err):
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            sudo_cmd = ["sudo", "-n", "iw", "dev", interface, "scan"]
            s_code, s_out, s_err = run_cmd(sudo_cmd, timeout=8.0)
            if s_code == 0:
                return parse_iw_scan(s_out)

    if code != 0:
        logger.warning("iw scan failed (code %d): %s", code, err)
        return []
    return parse_iw_scan(out)


# ---------------------------------------------------------------------------
# Web Portal HTML Template
# ---------------------------------------------------------------------------
PORTAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Dragonfly Wi-Fi Setup</title>
<style>
  :root {
    --bg: #0f172a;
    --card: #1e293b;
    --card-border: #334155;
    --text: #f8fafc;
    --text-muted: #94a3b8;
    --primary: #38bdf8;
    --primary-hover: #0ea5e9;
    --success: #34d399;
    --error: #f87171;
    --radius: 12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 16px;
    display: flex;
    justify-content: center;
    min-height: 100vh;
  }
  .container {
    width: 100%;
    max-width: 440px;
    display: flex;
    flex-direction: column;
    gap: 18px;
  }
  .header {
    text-align: center;
    padding: 12px 0 6px 0;
  }
  .header h1 {
    font-size: 1.6rem;
    font-weight: 700;
    letter-spacing: -0.5px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }
  .header p {
    color: var(--text-muted);
    font-size: 0.88rem;
    margin-top: 4px;
  }
  .badge {
    display: inline-block;
    padding: 4px 10px;
    font-size: 0.75rem;
    font-weight: 600;
    border-radius: 9999px;
    background: rgba(56, 189, 248, 0.15);
    color: var(--primary);
    margin-top: 8px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: var(--radius);
    padding: 18px;
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.4);
  }
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
  }
  .card-title {
    font-size: 1rem;
    font-weight: 600;
  }
  .btn-refresh {
    background: transparent;
    border: 1px solid var(--card-border);
    color: var(--primary);
    padding: 6px 12px;
    border-radius: 8px;
    font-size: 0.8rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .btn-refresh:active {
    background: rgba(56, 189, 248, 0.1);
  }
  .network-list {
    max-height: 240px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-right: 2px;
  }
  .network-item {
    background: #0f172a;
    border: 1px solid var(--card-border);
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .network-item:active, .network-item.selected {
    border-color: var(--primary);
    background: rgba(56, 189, 248, 0.08);
  }
  .net-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    overflow: hidden;
  }
  .net-ssid {
    font-size: 0.95rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .net-meta {
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .net-signal {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.8rem;
    color: var(--primary);
    font-weight: 600;
  }
  .form-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 14px;
  }
  label {
    font-size: 0.85rem;
    color: var(--text-muted);
    font-weight: 500;
  }
  .input-wrapper {
    position: relative;
    display: flex;
    align-items: center;
  }
  input[type="text"], input[type="password"] {
    width: 100%;
    background: #0f172a;
    border: 1px solid var(--card-border);
    border-radius: 8px;
    padding: 12px 14px;
    font-size: 0.95rem;
    color: var(--text);
    outline: none;
    transition: border-color 0.15s ease;
  }
  input:focus {
    border-color: var(--primary);
  }
  .toggle-pwd {
    position: absolute;
    right: 12px;
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 0.8rem;
    cursor: pointer;
    padding: 4px;
  }
  .btn-submit {
    width: 100%;
    background: var(--primary);
    color: #0f172a;
    border: none;
    border-radius: 8px;
    padding: 14px;
    font-size: 1rem;
    font-weight: 700;
    cursor: pointer;
    transition: background 0.15s ease;
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 8px;
  }
  .btn-submit:active {
    background: var(--primary-hover);
  }
  .spinner {
    width: 18px;
    height: 18px;
    border: 2px solid #0f172a;
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .empty-state {
    text-align: center;
    padding: 24px 0;
    color: var(--text-muted);
    font-size: 0.9rem;
  }
  .alert {
    padding: 12px;
    border-radius: 8px;
    font-size: 0.85rem;
    line-height: 1.4;
    display: none;
    margin-bottom: 12px;
  }
  .alert.info { background: rgba(56, 189, 248, 0.15); color: var(--primary); border: 1px solid var(--primary); display: block; }
  .alert.error { background: rgba(248, 113, 113, 0.15); color: var(--error); border: 1px solid var(--error); display: block; }
  .alert.success { background: rgba(52, 211, 153, 0.15); color: var(--success); border: 1px solid var(--success); display: block; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1><span>📶</span> Dragonfly Setup</h1>
    <p>Configure Wi-Fi connection for dragonfly node</p>
    <div class="badge" id="modeBadge">AP Mode (Fallback)</div>
  </div>

  <div id="statusAlert" class="alert"></div>

  <div class="card">
    <div class="card-header">
      <span class="card-title">Available Networks</span>
      <button class="btn-refresh" id="refreshBtn" onclick="fetchScan()">
        <span id="refreshIcon">↻</span> Refresh
      </button>
    </div>
    <div class="network-list" id="networkList">
      <div class="empty-state">Scanning for Wi-Fi networks...</div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">
      <span class="card-title">Connect to Wi-Fi</span>
    </div>
    <form id="wifiForm" onsubmit="handleConnect(event)">
      <div class="form-group">
        <label for="ssid">Network Name (SSID)</label>
        <input type="text" id="ssid" name="ssid" placeholder="Select above or type SSID" required autocomplete="off">
      </div>
      <div class="form-group">
        <label for="password">Password (PSK)</label>
        <div class="input-wrapper">
          <input type="password" id="password" name="password" placeholder="Enter network password" autocomplete="off">
          <button type="button" class="toggle-pwd" onclick="togglePassword()">SHOW</button>
        </div>
      </div>
      <button type="submit" class="btn-submit" id="submitBtn">
        Connect to Wi-Fi
      </button>
    </form>
  </div>
</div>

<script>
let networks = [];

function togglePassword() {
  const pwd = document.getElementById('password');
  const btn = event.target;
  if (pwd.type === 'password') {
    pwd.type = 'text';
    btn.innerText = 'HIDE';
  } else {
    pwd.type = 'password';
    btn.innerText = 'SHOW';
  }
}

function selectNetwork(ssid) {
  document.getElementById('ssid').value = ssid;
  document.getElementById('password').focus();
  document.querySelectorAll('.network-item').forEach(el => {
    el.classList.toggle('selected', el.getAttribute('data-ssid') === ssid);
  });
}

function renderNetworks(list) {
  const container = document.getElementById('networkList');
  if (!list || list.length === 0) {
    container.innerHTML = '<div class="empty-state">No networks found. Try refresh.</div>';
    return;
  }
  container.innerHTML = list.map(n => `
    <div class="network-item" data-ssid="${escapeHtml(n.ssid)}" onclick="selectNetwork('${escapeHtml(n.ssid)}')">
      <div class="net-info">
        <span class="net-ssid">${escapeHtml(n.ssid)}</span>
        <span class="net-meta">${n.security} • ${n.freq > 5000 ? '5GHz' : '2.4GHz'}</span>
      </div>
      <div class="net-signal">
        <span>${n.signal_percent}%</span>
        <span>${n.security === 'Open' ? '🔓' : '🔒'}</span>
      </div>
    </div>
  `).join('');
}

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

async function fetchScan() {
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true;
  document.getElementById('refreshIcon').innerText = '⏳';
  try {
    const res = await fetch('/api/scan');
    const data = await res.json();
    networks = data.networks || [];
    renderNetworks(networks);
  } catch (err) {
    console.error('Scan error:', err);
  } finally {
    btn.disabled = false;
    document.getElementById('refreshIcon').innerText = '↻';
  }
}

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (data.mode) {
      document.getElementById('modeBadge').innerText = data.mode === 'STA_CONNECTED' ? 'STA Connected' : 'AP Fallback';
    }
  } catch (err) {}
}

async function handleConnect(e) {
  e.preventDefault();
  const ssid = document.getElementById('ssid').value.trim();
  const password = document.getElementById('password').value;
  const alert = document.getElementById('statusAlert');
  const btn = document.getElementById('submitBtn');

  if (!ssid) return;

  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Applying changes...';
  alert.className = 'alert info';
  alert.innerText = 'Applying network configuration and switching to station mode. AP Dragonfly will terminate. Please verify connection.';

  try {
    const res = await fetch('/api/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid, password })
    });
    const data = await res.json();
    if (data.status === 'ok' || data.status === 'connecting') {
      alert.className = 'alert success';
      alert.innerText = 'Configuration applied. Device is connecting to ' + ssid + '. If successful, connection will remain active.';
    } else {
      alert.className = 'alert error';
      alert.innerText = data.message || 'Connection request failed.';
      btn.disabled = false;
      btn.innerText = 'Connect to Wi-Fi';
    }
  } catch (err) {
    alert.className = 'alert info';
    alert.innerText = 'Command sent. AP is shutting down to switch to Wi-Fi ' + ssid + '.';
  }
}

// Initial fetch
fetchScan();
fetchStatus();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Web Portal HTTP Handler
# ---------------------------------------------------------------------------
class PortalRequestHandler(http.server.BaseHTTPRequestHandler):
    fsm: Optional[WifiManagerFSM] = None
    dry_run: bool = False
    cached_networks: List[WifiNetwork] = []

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - " + format, self.address_string(), *args)

    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, html: str, status: int = 200) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path

        # Captive Portal Probes Redirection
        captive_paths = {
            "/generate_204",
            "/gen_204",
            "/mobile/status.php",
            "/hotspot-detect.html",
            "/library/test/success.html",
            "/captiveportal.html",
            "/connecttest.txt",
            "/ncsi.txt",
        }
        if path in captive_paths:
            host_header = self.headers.get("Host", "")
            # If requesting from another host or captive path, redirect to root
            self._send_redirect(f"http://{DEFAULT_AP_IP}/")
            return

        if path == "/" or path == "/index.html":
            self._send_html(PORTAL_HTML)
            return

        if path == "/api/status":
            if self.fsm:
                st = self.fsm.get_status()
                res = asdict(st)
            else:
                res = {
                    "mode": "TEST_PORTAL",
                    "interface": DEFAULT_IFACE,
                    "ip": "127.0.0.1",
                    "ssid": "TestSSID",
                    "gateway": "127.0.0.1",
                    "ping_ok": True,
                    "ping_target": DEFAULT_PING_TARGET,
                    "last_check": time.time(),
                }
            self._send_json(res)
            return

        if path == "/api/scan":
            nets: List[WifiNetwork] = []
            if self.dry_run or not self.fsm:
                # Try real scan if possible, else fallback to mock/cache
                nets = scan_wifi(DEFAULT_IFACE)
                if not nets and PortalRequestHandler.cached_networks:
                    nets = PortalRequestHandler.cached_networks
                elif not nets:
                    nets = [
                        WifiNetwork(
                            ssid="Si Tha_5G",
                            bssid="26:0b:2a:db:2c:31",
                            signal_dbm=-35.0,
                            freq=5300,
                            security="WPA2",
                        ),
                        WifiNetwork(
                            ssid="Si Tha",
                            bssid="26:0b:2a:db:2c:30",
                            signal_dbm=-40.0,
                            freq=2412,
                            security="WPA2",
                        ),
                        WifiNetwork(
                            ssid="Guest_Open",
                            bssid="aa:bb:cc:dd:ee:ff",
                            signal_dbm=-75.0,
                            freq=2437,
                            security="Open",
                        ),
                    ]
            else:
                nets = self.fsm.get_scan_networks()

            PortalRequestHandler.cached_networks = nets
            self._send_json({"networks": [asdict(n) for n in nets]})
            return

        # Fallback for captive requests
        if not path.startswith("/api/"):
            self._send_redirect(f"http://{DEFAULT_AP_IP}/")
            return

        self._send_json({"error": "Not Found"}, status=404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/connect":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body)
            except Exception as exc:
                self._send_json(
                    {"status": "error", "message": f"Invalid body: {exc}"}, status=400
                )
                return

            ssid = data.get("ssid", "").strip()
            password = data.get("password", "")

            if not ssid:
                self._send_json(
                    {"status": "error", "message": "SSID cannot be empty"}, status=400
                )
                return

            if self.dry_run or not self.fsm:
                logger.info(
                    "[DRY-RUN] Received connect request for SSID=%s (psk len=%d)",
                    ssid,
                    len(password),
                )
                self._send_json(
                    {
                        "status": "ok",
                        "message": f"[DRY-RUN] Config simulated for {ssid}",
                    }
                )
                return

            # In daemon mode: trigger asynchronous connect via FSM
            logger.info("Triggering connection to SSID=%s", ssid)
            self._send_json(
                {
                    "status": "connecting",
                    "message": f"Switching to STA mode for {ssid}...",
                }
            )

            # Spawn transition in separate thread so HTTP response completes cleanly
            def trigger_connect():
                time.sleep(1.0)
                if self.fsm:
                    self.fsm.connect_new_wifi(ssid, password)

            threading.Thread(target=trigger_connect, daemon=True).start()
            return

        self._send_json({"error": "Not Found"}, status=404)


# ---------------------------------------------------------------------------
# FSM & Orchestrator
# ---------------------------------------------------------------------------
class WifiManagerFSM:
    def __init__(
        self,
        interface: str = DEFAULT_IFACE,
        ping_target: str = DEFAULT_PING_TARGET,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        max_fails: int = DEFAULT_MAX_FAILS,
        interfaces_file: str = DEFAULT_INTERFACES_FILE,
        ap_ssid: str = DEFAULT_AP_SSID,
        ap_psk: str = DEFAULT_AP_PSK,
        portal_port: int = 80,
        runtime_dir: Optional[str] = None,
    ) -> None:
        self.interface = interface
        self.ping_target = ping_target
        self.check_interval = check_interval
        self.max_fails = max_fails
        self.interfaces_file = Path(interfaces_file)
        self.ap_ssid = ap_ssid
        self.ap_psk = ap_psk
        self.portal_port = portal_port
        self.runtime_dir = Path(runtime_dir or DEFAULT_RUNTIME_DIR)

        self.current_state = STATE_STA_CONNECTED
        self.fail_count = 0
        self.running = False
        self.lock = threading.Lock()

        self.http_server: Optional[http.server.HTTPServer] = None
        self.http_thread: Optional[threading.Thread] = None
        self.hostapd_proc: Optional[subprocess.Popen] = None
        self.dnsmasq_proc: Optional[subprocess.Popen] = None

        self.cached_networks: List[WifiNetwork] = []
        self.last_status = NetworkStatus(
            mode=STATE_STA_CONNECTED,
            interface=interface,
            ip=None,
            ssid=None,
            gateway=None,
            ping_ok=True,
            ping_target=ping_target,
            last_check=time.time(),
        )

        # Prepare runtime directory
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def get_status(self) -> NetworkStatus:
        with self.lock:
            return self.last_status

    def get_scan_networks(self) -> List[WifiNetwork]:
        # If in AP mode, iw scan might return busy; use cached scan if scan fails
        nets = scan_wifi(self.interface)
        if nets:
            self.cached_networks = nets
            return nets
        return self.cached_networks

    def update_status_probe(self) -> bool:
        """Perform connectivity check in STA mode."""
        ip = get_interface_ip(self.interface)
        ssid = get_connected_ssid(self.interface)
        gw = get_default_gateway(self.interface)

        # Probe ping: try gateway first, then ping_target
        ping_ok = False
        if gw and check_ping(gw, timeout=2.0):
            ping_ok = True
        elif check_ping(self.ping_target, timeout=2.0):
            ping_ok = True

        with self.lock:
            self.last_status = NetworkStatus(
                mode=self.current_state,
                interface=self.interface,
                ip=ip,
                ssid=ssid,
                gateway=gw,
                ping_ok=ping_ok,
                ping_target=self.ping_target,
                last_check=time.time(),
            )
        return ping_ok

    def start_ap_services(self) -> bool:
        """Transition interface to AP mode, start hostapd, dnsmasq, and portal."""
        logger.warning("Initiating AP mode fallback on %s", self.interface)
        with self.lock:
            self.current_state = STATE_AP_FALLBACK

        # 1. Cache available networks before bringing down STA
        logger.info("Scanning nearby Wi-Fi networks before stopping STA...")
        nets = scan_wifi(self.interface)
        if nets:
            self.cached_networks = nets

        # 2. Lower STA mode safely
        logger.info("Stopping STA on %s...", self.interface)
        run_cmd(["ifdown", "--force", self.interface])
        # Kill lingering wpa_supplicant or dhclient on this interface
        run_cmd(["killall", "-q", f"wpa_supplicant.{self.interface}"])
        run_cmd(["ip", "addr", "flush", "dev", self.interface])

        # 3. Configure static IP for AP
        logger.info("Assigning static IP %s/24 to %s...", DEFAULT_AP_IP, self.interface)
        run_cmd(["ip", "link", "set", "dev", self.interface, "up"])
        run_cmd(
            [
                "ip",
                "addr",
                "add",
                f"{DEFAULT_AP_IP}/{DEFAULT_AP_CIDR}",
                "dev",
                self.interface,
            ]
        )

        # 4. Generate configs in runtime_dir
        hostapd_conf = self.runtime_dir / "hostapd.conf"
        dnsmasq_conf = self.runtime_dir / "dnsmasq.conf"

        hostapd_conf.write_text(
            generate_hostapd_conf(
                self.interface,
                self.ap_ssid,
                self.ap_psk,
                channel=DEFAULT_AP_CHANNEL,
            )
        )
        dnsmasq_conf.write_text(
            generate_dnsmasq_conf(
                self.interface,
                ip=DEFAULT_AP_IP,
                range_start=DEFAULT_DHCP_START,
                range_end=DEFAULT_DHCP_END,
            )
        )

        # 5. Start dnsmasq
        self.stop_dnsmasq()
        logger.info("Starting dnsmasq...")
        try:
            self.dnsmasq_proc = subprocess.Popen(
                [
                    "dnsmasq",
                    "-C",
                    str(dnsmasq_conf),
                    "-x",
                    str(self.runtime_dir / "dnsmasq.pid"),
                    "-d",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.error("Failed to start dnsmasq: %s", exc)

        # 6. Start hostapd
        self.stop_hostapd()
        logger.info("Starting hostapd on %s (SSID=%s)...", self.interface, self.ap_ssid)
        try:
            self.hostapd_proc = subprocess.Popen(
                ["hostapd", str(hostapd_conf)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.error("Failed to start hostapd: %s", exc)

        # 7. Start Web Portal HTTP server
        self.start_portal_server()
        logger.info("AP Fallback active on http://%s", DEFAULT_AP_IP)
        return True

    def stop_dnsmasq(self) -> None:
        if self.dnsmasq_proc and self.dnsmasq_proc.poll() is None:
            self.dnsmasq_proc.terminate()
            try:
                self.dnsmasq_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.dnsmasq_proc.kill()
        self.dnsmasq_proc = None
        run_cmd(["killall", "-q", "dnsmasq"])

    def stop_hostapd(self) -> None:
        if self.hostapd_proc and self.hostapd_proc.poll() is None:
            self.hostapd_proc.terminate()
            try:
                self.hostapd_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.hostapd_proc.kill()
        self.hostapd_proc = None
        run_cmd(["killall", "-q", "hostapd"])

    def start_portal_server(self) -> None:
        if self.http_server:
            return

        PortalRequestHandler.fsm = self
        PortalRequestHandler.dry_run = False
        PortalRequestHandler.cached_networks = self.cached_networks

        try:
            # Bind to 0.0.0.0 so both AP IP (192.168.4.1) and any interface can access
            server_addr = ("0.0.0.0", self.portal_port)
            self.http_server = http.server.ThreadingHTTPServer(
                server_addr, PortalRequestHandler
            )
            self.http_thread = threading.Thread(
                target=self.http_server.serve_forever, daemon=True
            )
            self.http_thread.start()
            logger.info("Captive Web Portal listening on port %d", self.portal_port)
        except Exception as exc:
            logger.error(
                "Failed to bind Web Portal on port %d: %s", self.portal_port, exc
            )

    def stop_portal_server(self) -> None:
        if self.http_server:
            logger.info("Stopping Web Portal server...")
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None
            self.http_thread = None

    def stop_ap_services(self) -> None:
        """Tear down AP services."""
        logger.info("Stopping AP services...")
        self.stop_portal_server()
        self.stop_hostapd()
        self.stop_dnsmasq()
        run_cmd(["ip", "addr", "flush", "dev", self.interface])

    def backup_interfaces(self) -> Optional[Path]:
        """Create timestamped backup of /etc/network/interfaces."""
        if not self.interfaces_file.exists():
            return None
        ts = int(time.time())
        bak_file = self.interfaces_file.with_name(f"{self.interfaces_file.name}.bak.{ts}")
        try:
            shutil.copy2(self.interfaces_file, bak_file)
            symlink_bak = self.interfaces_file.with_name(
                f"{self.interfaces_file.name}.bak"
            )
            if symlink_bak.exists() or symlink_bak.is_symlink():
                symlink_bak.unlink()
            symlink_bak.symlink_to(bak_file.name)
            logger.info("Network interfaces backed up to %s", bak_file)
            return bak_file
        except Exception as exc:
            logger.error("Failed to backup interfaces: %s", exc)
            return None

    def connect_new_wifi(self, ssid: str, password: str) -> bool:
        """Apply new Wi-Fi credentials, tear down AP, test STA for 30s with rollback."""
        logger.info("Connecting to Wi-Fi SSID '%s'...", ssid)
        with self.lock:
            self.current_state = STATE_CONNECTING

        # 1. Backup current config
        backup_file = self.backup_interfaces()

        # 2. Write new /etc/network/interfaces config
        try:
            curr_content = (
                self.interfaces_file.read_text()
                if self.interfaces_file.exists()
                else ""
            )
            updated_content = update_interfaces_content(
                curr_content,
                self.interface,
                ssid,
                password if password else None,
            )
            self.interfaces_file.write_text(updated_content)
            logger.info("Updated %s with SSID %s", self.interfaces_file, ssid)
        except Exception as exc:
            logger.error("Failed to write %s: %s", self.interfaces_file, exc)
            self.start_ap_services()
            return False

        # 3. Stop AP services
        self.stop_ap_services()

        # 4. Bring up STA mode
        logger.info("Bringing up STA on %s...", self.interface)
        run_cmd(["ifup", "--force", self.interface])

        # 5. Monitor connection for up to DEFAULT_CONNECT_TIMEOUT seconds
        logger.info(
            "Verifying STA connection (timeout=%ss)...", DEFAULT_CONNECT_TIMEOUT
        )
        start_time = time.time()
        success = False

        while time.time() - start_time < DEFAULT_CONNECT_TIMEOUT:
            time.sleep(2.0)
            ip = get_interface_ip(self.interface)
            if ip and check_ping(self.ping_target, timeout=2.0):
                logger.info("STA connection verified! IP=%s, ping OK", ip)
                success = True
                break

        if success:
            with self.lock:
                self.current_state = STATE_STA_CONNECTED
                self.fail_count = 0
            # Update known-good config
            known_good = self.interfaces_file.with_name(
                f"{self.interfaces_file.name}.known_good"
            )
            try:
                shutil.copy2(self.interfaces_file, known_good)
            except Exception:
                pass
            return True

        # Failed: Rollback to AP fallback
        logger.warning(
            "Failed to connect to %s within %ss! Rolling back to AP fallback...",
            ssid,
            DEFAULT_CONNECT_TIMEOUT,
        )
        if backup_file and backup_file.exists():
            try:
                shutil.copy2(backup_file, self.interfaces_file)
                logger.info("Restored interfaces from %s", backup_file)
            except Exception as exc:
                logger.error("Failed to restore backup interfaces: %s", exc)

        self.start_ap_services()
        return False

    def run_loop(self) -> None:
        """Main FSM loop for daemon service."""
        self.running = True
        logger.info(
            "Wi-Fi Manager daemon started on %s (interval=%ss, max_fails=%d)",
            self.interface,
            self.check_interval,
            self.max_fails,
        )

        # Initial check
        ping_ok = self.update_status_probe()
        if ping_ok:
            logger.info(
                "Initial connectivity OK. Operating in %s", STATE_STA_CONNECTED
            )
            with self.lock:
                self.current_state = STATE_STA_CONNECTED
                self.fail_count = 0
        else:
            logger.warning(
                "Initial connectivity check failed (fails: %d/%d)",
                self.fail_count + 1,
                self.max_fails,
            )
            self.fail_count += 1
            if self.fail_count >= self.max_fails:
                self.start_ap_services()

        while self.running:
            time.sleep(self.check_interval)
            if not self.running:
                break

            with self.lock:
                state = self.current_state

            if state == STATE_STA_CONNECTED:
                ping_ok = self.update_status_probe()
                if ping_ok:
                    self.fail_count = 0
                else:
                    self.fail_count += 1
                    logger.warning(
                        "Ping check failed (%d/%d)",
                        self.fail_count,
                        self.max_fails,
                    )
                    if self.fail_count >= self.max_fails:
                        logger.error(
                            "Lost connectivity %d consecutive times! Entering AP fallback.",
                            self.max_fails,
                        )
                        self.start_ap_services()

            elif state == STATE_AP_FALLBACK:
                # In AP mode: keep services alive, update status
                with self.lock:
                    self.last_status = NetworkStatus(
                        mode=STATE_AP_FALLBACK,
                        interface=self.interface,
                        ip=DEFAULT_AP_IP,
                        ssid=self.ap_ssid,
                        gateway=None,
                        ping_ok=False,
                        ping_target=self.ping_target,
                        last_check=time.time(),
                    )

    def shutdown(self) -> None:
        """Orderly shutdown of all services."""
        logger.info("Shutting down Wi-Fi Manager...")
        self.running = False
        with self.lock:
            state = self.current_state
        if state == STATE_AP_FALLBACK:
            self.stop_ap_services()
        elif self.http_server:
            self.stop_portal_server()


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> int:
    """Print current network status."""
    iface = args.interface
    ip = get_interface_ip(iface)
    ssid = get_connected_ssid(iface)
    gw = get_default_gateway(iface)
    ping_ok = check_ping(args.ping_target, timeout=2.0)

    if args.json:
        data = {
            "interface": iface,
            "ip": ip,
            "ssid": ssid,
            "gateway": gw,
            "ping_target": args.ping_target,
            "ping_ok": ping_ok,
            "status": "ONLINE" if ping_ok else "OFFLINE",
        }
        print(json.dumps(data, indent=2))
        return 0

    print("========================================")
    print("      DRAGONFLY NETWORK STATUS          ")
    print("========================================")
    print(f"Interface:   {iface}")
    print(f"IP Address:  {ip or 'None'}")
    print(f"SSID:        {ssid or 'Not connected'}")
    print(f"Gateway:     {gw or 'None'}")
    print(f"Ping ({args.ping_target}): {'OK (Online)' if ping_ok else 'FAILED (Offline)'}")
    print("========================================")
    return 0 if ping_ok else 1


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan and list surrounding Wi-Fi networks."""
    print(f"Scanning Wi-Fi networks on {args.interface}...")
    networks = scan_wifi(args.interface)
    if args.json:
        print(json.dumps([asdict(n) for n in networks], indent=2))
        return 0

    if not networks:
        print("No networks found or interface busy.")
        return 1

    print(
        f"{'SSID':<30} {'SIGNAL':<10} {'PERCENT':<10} {'FREQ':<8} {'SECURITY':<10} {'BSSID'}"
    )
    print("-" * 84)
    for n in networks:
        print(
            f"{n.ssid:<30} {f'{n.signal_dbm:.1f} dBm':<10} {f'{n.signal_percent}%':<10} {n.freq:<8} {n.security:<10} {n.bssid}"
        )
    return 0


def cmd_test_portal(args: argparse.Namespace) -> int:
    """Run standalone Web Portal on test port for dry-run testing."""
    port = args.port
    host = args.host
    print(f"Starting standalone test Web Portal on http://{host}:{port}...")
    print("Dry-run mode: does NOT modify interface or disconnect SSH.")

    PortalRequestHandler.dry_run = True
    PortalRequestHandler.fsm = None

    server = http.server.ThreadingHTTPServer((host, port), PortalRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping test Web Portal...")
    finally:
        server.server_close()
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Run Wi-Fi Manager background daemon."""
    fsm = WifiManagerFSM(
        interface=args.interface,
        ping_target=args.ping_target,
        check_interval=args.check_interval,
        max_fails=args.max_fails,
        interfaces_file=args.interfaces_file,
        ap_ssid=args.ap_ssid,
        ap_psk=args.ap_psk,
        portal_port=args.portal_port,
    )

    def sig_handler(sig, frame):
        logger.info("Received signal %s, initiating shutdown...", sig)
        fsm.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    fsm.run_loop()
    return 0


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dragonfly Wi-Fi Manager & Captive AP Fallback"
    )
    parser.add_argument(
        "-i",
        "--interface",
        default=DEFAULT_IFACE,
        help=f"Wi-Fi interface (default: {DEFAULT_IFACE})",
    )
    parser.add_argument(
        "--ping-target",
        default=DEFAULT_PING_TARGET,
        help=f"Ping target for connectivity check (default: {DEFAULT_PING_TARGET})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Show current Wi-Fi status")
    p_status.add_argument("--json", action="store_true", help="Output as JSON")

    # scan
    p_scan = subparsers.add_parser("scan", help="Scan surrounding Wi-Fi networks")
    p_scan.add_argument("--json", action="store_true", help="Output as JSON")

    # test-portal
    p_portal = subparsers.add_parser(
        "test-portal", help="Run standalone Web Portal on test port"
    )
    p_portal.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to bind portal (default: 8080)",
    )
    p_portal.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host address to bind (default: 0.0.0.0)",
    )

    # daemon
    p_daemon = subparsers.add_parser("daemon", help="Run background manager daemon")
    p_daemon.add_argument(
        "--check-interval",
        type=float,
        default=DEFAULT_CHECK_INTERVAL,
        help=f"Seconds between ping checks (default: {DEFAULT_CHECK_INTERVAL})",
    )
    p_daemon.add_argument(
        "--max-fails",
        type=int,
        default=DEFAULT_MAX_FAILS,
        help=f"Consecutive ping failures to trigger AP (default: {DEFAULT_MAX_FAILS})",
    )
    p_daemon.add_argument(
        "--interfaces-file",
        default=DEFAULT_INTERFACES_FILE,
        help=f"Network interfaces file (default: {DEFAULT_INTERFACES_FILE})",
    )
    p_daemon.add_argument(
        "--ap-ssid",
        default=DEFAULT_AP_SSID,
        help=f"AP SSID (default: {DEFAULT_AP_SSID})",
    )
    p_daemon.add_argument(
        "--ap-psk",
        default=DEFAULT_AP_PSK,
        help="AP Passphrase (default: tonybucket0297)",
    )
    p_daemon.add_argument(
        "--portal-port",
        type=int,
        default=80,
        help="Captive portal port in AP mode (default: 80)",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )

    if args.command == "status":
        return cmd_status(args)
    elif args.command == "scan":
        return cmd_scan(args)
    elif args.command == "test-portal":
        return cmd_test_portal(args)
    elif args.command == "daemon":
        return cmd_daemon(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
