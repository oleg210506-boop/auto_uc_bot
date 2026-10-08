"""Local HTTPS -> HTTP -> autoUCbot acceptance test; no external services.

Run from the repository root: python scripts/smoke_https_login.py --output result.json
Uses an ephemeral database, locally generated certificate, two loopback ports,
and temporary credentials. This is a real socket/TLS test, NOT a browser test.
The script never reads your production secrets or opens an external URL.
"""
from __future__ import annotations
import argparse
import datetime
import http.client
import http.server
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
import uvicorn
from bs4 import BeautifulSoup
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from autoucbot import __version__
from autoucbot.config import Config
from autoucbot.web import create_app


def token(response):
    element = BeautifulSoup(response.text, "html.parser").find("input", {"name": "csrf"})
    assert element and element.get("value"), "Expected a usable form"
    return element["value"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("https-login-result.json"))
    args = parser.parse_args()
    results = {"version": __version__, "transport": "real loopback HTTPS -> HTTP -> Uvicorn",
               "client": "requests (not a browser)", "external_services_called": False, "cases": []}
    with tempfile.TemporaryDirectory(prefix="auc-login-test-") as directory:
        root = Path(directory)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                       .sign(key, hashes.SHA256()))
        (root / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (root / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        # Keep the listener reserved until Uvicorn takes ownership (no port race).
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        upstream_port = listener.getsockname()[1]
        trace = []

        class HTTPSProxy(http.server.BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("connection", "host")}
                # Deliberately use the INTERNAL host to prove PUBLIC_URL wins.
                headers.update({"Host": f"127.0.0.1:{upstream_port}", "X-Forwarded-Proto": "https",
                                "X-Forwarded-For": "192.0.2.42", "X-Forwarded-Host": self.headers["Host"]})
                conn = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=15)
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    response = conn.getresponse()
                    payload = response.read()
                    trace.append({"method": self.command, "path": self.path, "status": response.status})
                    self.send_response(response.status)
                    for k, v in response.getheaders():
                        if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                            self.send_header(k, v)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    conn.close()

        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HTTPSProxy)
        public = f"https://localhost:{proxy.server_port}"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root / "cert.pem", root / "key.pem")
        proxy.socket = context.wrap_socket(proxy.socket, server_side=True)
        password = "Ephemeral-test-password-001!"
        cfg = Config(root / "data", "ephemeral-test-secret-" * 3, bootstrap_password=password,
                     public_url=public, secure_cookie=True, start_worker=False,
                     forwarded_allow_ips="127.0.0.1,::1")
        app = create_app(cfg)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", proxy_headers=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            assert server.started, "Local server failed to start"

            def session():
                s = requests.Session()
                s.trust_env = False
                s.verify = str(root / "cert.pem")
                s.headers["Origin"] = public
                return s

            def get(s, path):
                return s.get(public + path, timeout=15, allow_redirects=False)

            def post(s, path, data, headers=None):
                return s.post(public + path, data=data, headers=headers, timeout=15, allow_redirects=False)

            def login(s, csrf=None, pw=password, headers=None):
                return post(s, "/login", {"csrf": csrf or token(get(s, "/login")), "username": "owner", "password": pw}, headers)

            def passed(name, **details):
                results["cases"].append({"case": name, "passed": True, **details})

            with session() as s:
                initial = get(s, "/login")
                assert initial.headers["Referrer-Policy"] == "same-origin"
                assert "SameSite=lax" in initial.headers["Set-Cookie"] and "Secure" in initial.headers["Set-Cookie"]
                r = login(s, token(initial))
                assert r.status_code == 303 and r.headers["Location"] == "/"
                assert get(s, "/").status_code == 200
                passed("first_login_secure_cookie_through_internal_http", post_status=303, dashboard_status=200)
                dash = get(s, "/")
                assert post(s, "/control", {"csrf": token(dash), "action": "pause"}, {"Origin": "https://evil.test"}).status_code == 403
                assert post(s, "/control", {"csrf": token(dash), "action": "pause"}).status_code == 303
                passed("authenticated_form_origin_and_csrf")

            with session() as s:
                first, second = get(s, "/login"), get(s, "/login")
                assert token(first) == token(second)
                assert login(s, token(first)).status_code == 303
                passed("first_form_survives_second_tab")

            with session() as s:
                r = login(s, pw="wrong-test-password")
                assert r.status_code == 400 and "LOGIN_REJECTED" in r.text
                assert login(s, token(r)).status_code == 303
                passed("wrong_password_retry_without_reloading")

            with session() as s:
                r = get(s, "/login")
                s.cookies.clear()
                r = login(s, token(r))
                assert r.status_code == 403 and "CSRF_COOKIE_MISSING" in r.text
                assert login(s, token(r)).status_code == 303
                passed("missing_cookie_safe_recovery")

            with session() as s:
                r = login(s, headers={"Origin": "null"})
                assert r.status_code == 403 and "CSRF_ORIGIN" in r.text
                assert r.headers["Referrer-Policy"] == "same-origin"
                assert login(s, token(r)).status_code == 303
                passed("opaque_origin_rejected_then_fresh_form_recovery")

            with session() as s:
                r = login(s, headers={"Origin": "https://evil.test"})
                assert r.status_code == 403 and "CSRF_ORIGIN" in r.text
                assert password not in r.text
                passed("foreign_origin_rejected_no_password_echo")

            with session() as owner, session() as operator:
                assert login(owner).status_code == 303
                team = get(owner, "/team")
                assert post(owner, "/team", {"csrf": token(team), "action": "create", "username": "operator",
                                              "password": "Operator-local-test-001!", "role": "operator"}).status_code == 303
                f = get(operator, "/login")
                assert post(operator, "/login", {"csrf": token(f), "username": "operator", "password": "Operator-local-test-001!"}).status_code == 303
                assert get(operator, "/orders").status_code == 200
                assert get(operator, "/settings").status_code == 403
                assert get(owner, "/settings").status_code == 200
                assert owner.cookies.get("auc_session") != operator.cookies.get("auc_session")
                passed("two_users_independent_sessions_and_permissions")

            assert app.state.db.one("SELECT COUNT(*) n FROM orders")["n"] == 0
            with session() as s:
                assert get(s, "/healthz").json()["version"] == __version__
            passed("no_orders_or_real_purchases_health_version")
            results["http_trace"] = trace
            results["passed"] = len(results["cases"])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in results.items() if k != "http_trace"}, ensure_ascii=False, indent=2))
        finally:
            server.should_exit = True
            proxy.shutdown()
            proxy.server_close()
            worker.join(timeout=15)
            listener.close()


if __name__ == "__main__":
    main()
