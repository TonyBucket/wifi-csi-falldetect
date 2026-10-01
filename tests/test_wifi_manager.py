"""tests/test_wifi_manager.py

Unit tests for wifi_manager.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scripts.wifi_manager import (
    DEFAULT_AP_IP,
    PORTAL_HTML,
    PortalRequestHandler,
    WifiManagerFSM,
    WifiNetwork,
    generate_dnsmasq_conf,
    generate_hostapd_conf,
    parse_iw_scan,
    update_interfaces_content,
)

SAMPLE_IW_SCAN = """
BSS 26:0b:2a:db:2c:31(on wlp108s0) -- associated
\tTSF: 44256653384 usec (0d, 12:17:36)
\tfreq: 5300
\tbeacon interval: 100 TUs
\tcapability: ESS Privacy ShortPreamble (0x1931)
\tsignal: -34.00 dBm
\tlast seen: 840 ms ago
\tInformation elements from Probe Response frame:
\tSSID: Si Tha_5G
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK
BSS 26:0b:2a:db:2c:32(on wlp108s0)
\tfreq: 2412
\tsignal: -60.00 dBm
\tInformation elements from Probe Response frame:
\tSSID: Si Tha_5G
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK
BSS 00:11:22:33:44:55(on wlp108s0)
\tfreq: 2437
\tsignal: -75.00 dBm
\tInformation elements from Probe Response frame:
\tSSID: Free_Coffee_Shop
BSS 66:77:88:99:aa:bb(on wlp108s0)
\tfreq: 5745
\tsignal: -80.00 dBm
\tInformation elements from Probe Response frame:
\tSSID: 
BSS cc:dd:ee:ff:00:11(on wlp108s0)
\tfreq: 2462
\tsignal: -45.00 dBm
\tcapability: ESS Privacy (0x0411)
\tInformation elements from Probe Response frame:
\tSSID: Modern_Home
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: SAE
"""

SAMPLE_INTERFACES = """# Network interfaces
source /etc/network/interfaces.d/*

auto lo
iface lo inet loopback

allow-hotplug wlp108s0
iface wlp108s0 inet dhcp
\twpa-ssid OldNetwork
\twpa-psk  oldpassword123
"""


def test_generate_hostapd_conf():
    conf = generate_hostapd_conf("wlp108s0", "Dragonfly", "tonybucket0297", channel=6)
    assert "interface=wlp108s0" in conf
    assert "ssid=Dragonfly" in conf
    assert "channel=6" in conf
    assert "wpa=2" in conf
    assert "wpa_passphrase=tonybucket0297" in conf
    assert "rsn_pairwise=CCMP" in conf


def test_generate_dnsmasq_conf():
    conf = generate_dnsmasq_conf("wlp108s0", "192.168.4.1", "192.168.4.10", "192.168.4.50")
    assert "interface=wlp108s0" in conf
    assert "dhcp-range=192.168.4.10,192.168.4.50" in conf
    assert "address=/#/192.168.4.1" in conf
    assert "bind-interfaces" in conf


def test_parse_iw_scan():
    nets = parse_iw_scan(SAMPLE_IW_SCAN)
    assert len(nets) == 3  # Si Tha_5G (deduped), Free_Coffee_Shop, Modern_Home. Empty SSID ignored.

    # Sorted by signal descending
    assert nets[0].ssid == "Si Tha_5G"
    assert nets[0].signal_dbm == -34.00
    assert nets[0].security == "WPA2"
    assert nets[0].freq == 5300
    assert nets[0].signal_percent > 80

    # Modern_Home has SAE (WPA3)
    modern = next(n for n in nets if n.ssid == "Modern_Home")
    assert modern.security == "WPA3"
    assert modern.signal_dbm == -45.00

    # Free_Coffee_Shop has no privacy/RSN -> Open
    open_net = next(n for n in nets if n.ssid == "Free_Coffee_Shop")
    assert open_net.security == "Open"


def test_update_interfaces_content_existing():
    updated = update_interfaces_content(
        SAMPLE_INTERFACES,
        "wlp108s0",
        "NewOffice_5G",
        "newsecret999",
    )
    assert "wpa-ssid NewOffice_5G" in updated
    assert "wpa-psk  newsecret999" in updated
    assert "OldNetwork" not in updated
    assert "auto lo" in updated


def test_update_interfaces_content_new_iface():
    minimal = "auto lo\niface lo inet loopback\n"
    updated = update_interfaces_content(
        minimal,
        "wlan0",
        "MyWifi",
        "pass12345",
    )
    assert "allow-hotplug wlan0" in updated
    assert "iface wlan0 inet dhcp" in updated
    assert "wpa-ssid MyWifi" in updated
    assert "wpa-psk  pass12345" in updated


def test_portal_http_server():
    """Test Web Portal HTTP endpoints on a temporary port."""
    import http.server

    PortalRequestHandler.dry_run = True
    PortalRequestHandler.fsm = None
    PortalRequestHandler.cached_networks = [
        WifiNetwork("MockNet", "11:22:33:44:55:66", -50.0, 2412, "WPA2")
    ]

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PortalRequestHandler)
    port = server.server_port

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{port}"

    try:
        # 1. GET /
        req = urllib.request.Request(f"{base_url}/")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            content = resp.read().decode("utf-8")
            assert "Dragonfly Setup" in content

        # 2. Captive portal probe: GET /generate_204 -> should redirect 302
        req_captive = urllib.request.Request(f"{base_url}/generate_204")
        # Disallow auto-redirect to inspect 302
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())

        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                return fp

        custom_opener = urllib.request.build_opener(NoRedirectHandler)
        res_redir = custom_opener.open(f"{base_url}/generate_204")
        assert res_redir.getcode() == 302
        assert res_redir.headers.get("Location") == f"http://{DEFAULT_AP_IP}/"

        # 3. GET /api/status
        with urllib.request.urlopen(f"{base_url}/api/status") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert "mode" in data

        # 4. GET /api/scan
        with urllib.request.urlopen(f"{base_url}/api/scan") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert "networks" in data
            assert len(data["networks"]) > 0

        # 5. POST /api/connect with valid payload
        post_data = json.dumps({"ssid": "TestAP", "password": "pass"}).encode("utf-8")
        req_post = urllib.request.Request(
            f"{base_url}/api/connect",
            data=post_data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req_post) as resp:
            assert resp.status == 200
            res_json = json.loads(resp.read().decode("utf-8"))
            assert res_json.get("status") in ["ok", "connecting"]

        # 6. POST /api/connect with missing SSID -> 400
        bad_post = json.dumps({"ssid": "", "password": ""}).encode("utf-8")
        req_bad = urllib.request.Request(
            f"{base_url}/api/connect",
            data=bad_post,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req_bad)
            assert False, "Should have failed with 400"
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    finally:
        server.shutdown()
        server.server_close()


def test_fsm_backup_and_interfaces():
    """Verify backup and update interface file handling."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_interfaces = Path(tmpdir) / "interfaces"
        tmp_interfaces.write_text(SAMPLE_INTERFACES)

        fsm = WifiManagerFSM(
            interface="wlp108s0",
            interfaces_file=str(tmp_interfaces),
        )

        bak = fsm.backup_interfaces()
        assert bak is not None
        assert bak.exists()
        assert "OldNetwork" in bak.read_text()

        # Symlink .bak should point to bak file
        symlink_bak = tmp_interfaces.with_name("interfaces.bak")
        assert symlink_bak.exists()
