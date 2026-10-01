"""tests/test_wifi_manager.py

Unit tests for wifi_manager.py using standard library unittest.
"""

from __future__ import annotations

import http.server
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


class TestWifiManager(unittest.TestCase):
    def test_generate_hostapd_conf(self):
        conf = generate_hostapd_conf("wlp108s0", "Dragonfly", "tonybucket0297", channel=6)
        self.assertIn("interface=wlp108s0", conf)
        self.assertIn("ssid=Dragonfly", conf)
        self.assertIn("channel=6", conf)
        self.assertIn("wpa=2", conf)
        self.assertIn("wpa_passphrase=tonybucket0297", conf)
        self.assertIn("rsn_pairwise=CCMP", conf)

    def test_generate_dnsmasq_conf(self):
        conf = generate_dnsmasq_conf("wlp108s0", "192.168.4.1", "192.168.4.10", "192.168.4.50")
        self.assertIn("interface=wlp108s0", conf)
        self.assertIn("dhcp-range=192.168.4.10,192.168.4.50", conf)
        self.assertIn("address=/#/192.168.4.1", conf)
        self.assertIn("bind-interfaces", conf)

    def test_parse_iw_scan(self):
        nets = parse_iw_scan(SAMPLE_IW_SCAN)
        self.assertEqual(len(nets), 3)  # Si Tha_5G (deduped), Free_Coffee_Shop, Modern_Home

        # Sorted by signal descending
        self.assertEqual(nets[0].ssid, "Si Tha_5G")
        self.assertEqual(nets[0].signal_dbm, -34.00)
        self.assertEqual(nets[0].security, "WPA2")
        self.assertEqual(nets[0].freq, 5300)
        self.assertGreater(nets[0].signal_percent, 80)

        # Modern_Home has SAE (WPA3)
        modern = next(n for n in nets if n.ssid == "Modern_Home")
        self.assertEqual(modern.security, "WPA3")
        self.assertEqual(modern.signal_dbm, -45.00)

        # Free_Coffee_Shop has no privacy/RSN -> Open
        open_net = next(n for n in nets if n.ssid == "Free_Coffee_Shop")
        self.assertEqual(open_net.security, "Open")

    def test_update_interfaces_content_existing(self):
        updated = update_interfaces_content(
            SAMPLE_INTERFACES,
            "wlp108s0",
            "NewOffice_5G",
            "newsecret999",
        )
        self.assertIn("wpa-ssid NewOffice_5G", updated)
        self.assertIn("wpa-psk  newsecret999", updated)
        self.assertNotIn("OldNetwork", updated)
        self.assertIn("auto lo", updated)

    def test_update_interfaces_content_new_iface(self):
        minimal = "auto lo\niface lo inet loopback\n"
        updated = update_interfaces_content(
            minimal,
            "wlan0",
            "MyWifi",
            "pass12345",
        )
        self.assertIn("allow-hotplug wlan0", updated)
        self.assertIn("iface wlan0 inet dhcp", updated)
        self.assertIn("wpa-ssid MyWifi", updated)
        self.assertIn("wpa-psk  pass12345", updated)

    def test_portal_http_server(self):
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
                self.assertEqual(resp.status, 200)
                content = resp.read().decode("utf-8")
                self.assertIn("Dragonfly Setup", content)

            # 2. Captive portal probe: GET /generate_204 -> should redirect 302
            class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                def http_error_302(self, req, fp, code, msg, headers):
                    return fp

            custom_opener = urllib.request.build_opener(NoRedirectHandler)
            res_redir = custom_opener.open(f"{base_url}/generate_204")
            self.assertEqual(res_redir.getcode(), 302)
            self.assertEqual(res_redir.headers.get("Location"), f"http://{DEFAULT_AP_IP}/")

            # 3. GET /api/status
            with urllib.request.urlopen(f"{base_url}/api/status") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("mode", data)

            # 4. GET /api/scan
            with urllib.request.urlopen(f"{base_url}/api/scan") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("networks", data)
                self.assertGreater(len(data["networks"]), 0)

            # 5. POST /api/connect with valid payload
            post_data = json.dumps({"ssid": "TestAP", "password": "pass"}).encode("utf-8")
            req_post = urllib.request.Request(
                f"{base_url}/api/connect",
                data=post_data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req_post) as resp:
                self.assertEqual(resp.status, 200)
                res_json = json.loads(resp.read().decode("utf-8"))
                self.assertIn(res_json.get("status"), ["ok", "connecting"])

            # 6. POST /api/connect with missing SSID -> 400
            bad_post = json.dumps({"ssid": "", "password": ""}).encode("utf-8")
            req_bad = urllib.request.Request(
                f"{base_url}/api/connect",
                data=bad_post,
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_bad)
            self.assertEqual(ctx.exception.code, 400)

        finally:
            server.shutdown()
            server.server_close()

    def test_fsm_backup_and_interfaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_interfaces = Path(tmpdir) / "interfaces"
            tmp_interfaces.write_text(SAMPLE_INTERFACES)

            fsm = WifiManagerFSM(
                interface="wlp108s0",
                interfaces_file=str(tmp_interfaces),
                runtime_dir=tmpdir,
            )

            bak = fsm.backup_interfaces()
            self.assertIsNotNone(bak)
            self.assertTrue(bak.exists())
            self.assertIn("OldNetwork", bak.read_text())

            symlink_bak = tmp_interfaces.with_name("interfaces.bak")
            self.assertTrue(symlink_bak.exists())


if __name__ == "__main__":
    unittest.main()
