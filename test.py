#!/usr/bin/env python3

import argparse
import string
import threading
import time
import random
import re
from typing import List, Dict, Tuple
from queue import Queue, Empty
import socket
import concurrent.futures
import requests
from threading import Thread
import json
import os
from urllib.parse import parse_qsl
try:
    import dns.resolver
except ImportError:
    dns = None

# ---------- Config ----------
DEFAULT_HEADLESS = True
WORKER_SLEEP_ON_EMPTY = 0.5
WORKER_RETRY_DELAY = 2.0
MAX_RETRIES = 1
PROXY_RESOLVER_THREADS = 4
PROXY_TEST_TIMEOUT = 4.0
RESOLVE_RETRIES = 2
MAX_CONCURRENT_BROWSERS = 230
# ---------------------------

def human_sleep(min_s=0.0, max_s=0.0):
    time.sleep(random.uniform(min_s, max_s))

def extract_security_token(html: str) -> str:
    if not html:
        return None
    patterns = [
        r'<input[^>]*name=["\']securityToken["\'][^>]*value=["\']([^"\']+)["\']',
        r'<input[^>]*value=["\']([^"\']+)["\'][^>]*name=["\']securityToken["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def evaluate_subscription_result(html: str, config: Dict, response_url: str = "") -> Tuple[bool, str]:
    if not html:
        return False, "Empty response body"

    body = html.lower()
    url = (response_url or "").lower()
    success_markers = [m.lower() for m in config.get("success_markers", []) if isinstance(m, str)]
    failure_markers = [m.lower() for m in config.get("failure_markers", []) if isinstance(m, str)]
    success_url_markers = [m.lower() for m in config.get("success_url_markers", []) if isinstance(m, str)]
    failure_url_markers = [m.lower() for m in config.get("failure_url_markers", []) if isinstance(m, str)]

    if success_url_markers and any(marker in url for marker in success_url_markers):
        return True, "Success URL marker found"
    if failure_url_markers and any(marker in url for marker in failure_url_markers):
        return False, "Failure URL marker found"

    if success_markers:
        if any(marker in body for marker in success_markers):
            return True, "Success marker found"
        return False, "No success marker found in response"

    if failure_markers and any(marker in body for marker in failure_markers):
        return False, "Failure marker found in response"

    # Heuristic fallback: if form is shown again, it is usually not a successful subscription.
    if 'name="we_subscribe_email__"' in body and 'name="securitytoken"' in body:
        return False, "Form still present in response"

    return True, "HTTP OK (no markers configured)"

def _replace_email_placeholder(value, email: str):
    if isinstance(value, str) and "{email}" in value:
        return value.format(email=email)
    return value

def build_payload(config: Dict, email: str, security_token: str):
    payload_items = config.get("payload_items")
    payload_encoded = config.get("payload_encoded")
    payload = None

    if isinstance(payload_items, list):
        payload = []
        for entry in payload_items:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                key = _replace_email_placeholder(entry[0], email)
                value = _replace_email_placeholder(entry[1], email)
                payload.append((key, value))
    elif isinstance(payload_encoded, str):
        payload = [
            (_replace_email_placeholder(k, email), _replace_email_placeholder(v, email))
            for k, v in parse_qsl(payload_encoded, keep_blank_values=True)
        ]
    else:
        payload = {
            k: _replace_email_placeholder(v, email)
            for k, v in config.get("payload", {}).items()
        }

    if security_token:
        if isinstance(payload, list):
            replaced = False
            for idx, (key, _) in enumerate(payload):
                if key == "securityToken":
                    payload[idx] = (key, security_token)
                    replaced = True
            if not replaced:
                payload.append(("securityToken", security_token))
        elif isinstance(payload, dict):
            payload["securityToken"] = security_token

    return payload

def fetch_mullvad_socks_raw() -> List[str]:
    try:
        r = requests.get('https://api.mullvad.net/www/relays/wireguard/', timeout=15)
        r.raise_for_status()
        relays = r.json()
    except Exception as e:
        print("Error fetching Mullvad API:", e)
        return []
    socks = []
    for host in relays:
        if host.get('socks_name') and host.get('active'):
            s = host['socks_name']
            if ':' not in s:
                s = f"{s}:1080"
            socks.append(s)
    return socks

# ---------- LazyResolver (resolves only as needed) ----------
class LazyResolver:
    def __init__(self, raw_hosts: List[str], need: int, threads: int = PROXY_RESOLVER_THREADS):
        if dns is None:
            raise RuntimeError("dnspython is required for proxy resolution (pip install dnspython)")
        self.raw_hosts = raw_hosts[:]  # 'host:port'
        self.need = max(0, need)
        self.threads = threads
        # if there are many raw hosts, allow reuse/rotation instead of removing
        self.reuse_if_many = len(self.raw_hosts) > 500
        self.lock = threading.Lock()
        self.index = 0
        self.resolved_cache: Dict[str, str] = {}
        self.bad: Dict[str, int] = {}
        self.good_proxies: List[str] = []
        self.done_event = threading.Event()
        self.resolver = dns.resolver.Resolver()
        self.resolver.nameservers = ['1.1.1.1', '8.8.8.8']
        self.resolver.lifetime = 5
        self.resolver.timeout = 5
        self.stop_flag = False
        self.workers = []

    def start(self):
        if self.need <= 0:
            self.done_event.set()
            return
        for _ in range(self.threads):
            t = Thread(target=self._worker, daemon=True)
            t.start()
            self.workers.append(t)

    def stop(self):
        self.stop_flag = True
        for t in self.workers:
            t.join(timeout=0.5)

    def _pop_next_raw(self) -> str:
        with self.lock:
            while self.index < len(self.raw_hosts):
                item = self.raw_hosts[self.index]
                self.index += 1
                if item in self.resolved_cache:
                    continue
                if self.bad.get(item, 0) > RESOLVE_RETRIES:
                    continue
                return item
            return None

    def _resolve_host(self, hostport: str) -> Tuple[str, bool]:
        if ':' in hostport:
            host, port = hostport.split(':', 1)
        else:
            host, port = hostport, '1080'
        ip = None
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
            if infos:
                ip = infos[0][4][0]
        except Exception:
            ip = None
        if not ip:
            try:
                answers = self.resolver.resolve(host, 'A')
                for r in answers:
                    ip = r.to_text()
                    break
            except Exception:
                ip = None
        if ip:
            return f"{ip}:{port}", True
        return None, False

    def _test_connectivity(self, ipport: str, timeout: float = PROXY_TEST_TIMEOUT) -> bool:
        try:
            host, port = ipport.split(":", 1)
            port = int(port)
        except Exception:
            return False
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False

    def _worker(self):
        while not self.stop_flag and len(self.good_proxies) < self.need:
            raw = self._pop_next_raw()
            if raw is None:
                time.sleep(0.15)
                continue
            success = False
            for attempt in range(RESOLVE_RETRIES + 1):
                ipport, ok = self._resolve_host(raw)
                if not ok:
                    self.bad[raw] = self.bad.get(raw, 0) + 1
                    time.sleep(0.08)
                    continue
                if self._test_connectivity(ipport):
                    with self.lock:
                        # store mapping and append if not present
                        if ipport not in self.good_proxies:
                            self.resolved_cache[raw] = ipport
                            self.good_proxies.append(ipport)
                        else:
                            # ensure cache maps raw to ipport
                            self.resolved_cache.setdefault(raw, ipport)
                            if len(self.good_proxies) >= self.need:
                                self.done_event.set()
                    success = True
                    break
                else:
                    self.bad[raw] = self.bad.get(raw, 0) + 1
                    time.sleep(0.08)
            if not success:
                continue

    def get_proxy(self, timeout: float = 5.0) -> str:
        start = time.time()
        while time.time() - start < timeout:
            with self.lock:
                if self.good_proxies:
                    # if reuse is allowed, rotate instead of popping
                    if self.reuse_if_many:
                        # simple round-robin
                        proxy = self.good_proxies[self.index % len(self.good_proxies)]
                        self.index += 1
                        return proxy
                    else:
                        return self.good_proxies.pop(0)
                if self.done_event.is_set() and not self.good_proxies:
                    return None
            time.sleep(0.2)
        return None

# ---------- Parallel worker tasks using ThreadPoolExecutor ----------
def worker_task(email: str, attempt_no: int, service_name: str, resolver: LazyResolver, config: Dict):
    proxy = None
    proxies_dict = None
    if resolver:
        proxy = resolver.get_proxy(timeout=6.0)
        if proxy:
            proxies_dict = {
                "http": f"socks5://{proxy}",
                "https": f"socks5://{proxy}"
            }
            print(f"[+] Using proxy (IPv4): {proxy} for {email} (Run {attempt_no}) Service: {service_name}")
        else:
            print(f"[Task] No proxy available, fallback to direct connection for {email} (Run {attempt_no}) Service: {service_name}")
    else:
        print(f"[+] No resolver active, direct connection for {email} (Run {attempt_no}) Service: {service_name}")

    session = requests.Session()
    security_token = None
    try:
        cookie_url = config.get("cookie_url")
        if cookie_url:
            print(f"[-] Fetching cookies from {cookie_url} for {email} (Run {attempt_no}) Service: {service_name}...")
            response = session.get(cookie_url, proxies=proxies_dict, timeout=10)
            response.raise_for_status()
            security_token = extract_security_token(response.text)
            if security_token:
                print(
                    f"[+] Extracted securityToken for {email} (Run {attempt_no}) "
                    f"Service: {service_name}: {security_token}"
                )
            else:
                print(f"[Task] No securityToken found in HTML for {email} (Run {attempt_no}) Service: {service_name}")
            # Show fetched cookies
            cookies = session.cookies.get_dict()
            print(f"[+] Received cookies for {email} (Run {attempt_no}) Service: {service_name}: {cookies}")

            # Optional: check expected cookies
            expected_cookies = config.get("expected_cookies", [])
            if expected_cookies:
                missing = [c for c in expected_cookies if c not in cookies]
                if missing:
                    msg = f"Expected cookies missing: {', '.join(missing)}"
                    print(f"[Task] {msg} for Service: {service_name}")
                    return (email, attempt_no, service_name, False, msg, proxy)

    except Exception as e:
        msg = f"Error while fetching cookies: {str(e)}"
        print(f"[Task] {msg} for Service: {service_name}")
        return (email, attempt_no, service_name, False, msg, proxy)

    # Replace placeholders in payload and headers
    payload = build_payload(config, email, security_token)
    headers = {k: v.format(email=email) if isinstance(v, str) and '{email}' in v else v
               for k, v in config.get("headers", {}).items()}

    post_url = config.get("post_url")
    if not post_url:
        msg = "No post_url specified in config"
        print(f"[Task] {msg} for Service: {service_name}")
        return (email, attempt_no, service_name, False, msg, proxy)

    try:
        method = str(config.get("method", "POST")).upper()
        content_type = str(config.get("content_type", "application/json")).lower()
        print(f"[+] Sending request for {email} (Run {attempt_no}) Service: {service_name}...")
        request_kwargs = {
            "headers": headers,
            "proxies": proxies_dict,
            "timeout": 10,
        }
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            request_kwargs["data"] = payload
        else:
            request_kwargs["json"] = payload

        response = session.request(method, post_url, **request_kwargs)
        response.raise_for_status()
        success, msg = evaluate_subscription_result(response.text, config, response.url)
        print(f"[Task] POST Status: {response.status_code} for {email} (Run {attempt_no}) Service: {service_name}")
        print(f"[Task] Response URL: {response.url} for Service: {service_name}")
        print(f"[Task] Response preview: {response.text[:300]!r} for Service: {service_name}")
    except requests.RequestException as e:
        success = False
        msg = f"POST failed: {str(e)}"
        if e.response is not None:
            print(f"[Task] Server response on error: {e.response.text[:500]} for Service: {service_name}")

    return (email, attempt_no, service_name, success, msg, proxy)

def random_suffix(length: int = 4) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

# ---------- CLI Entrypoint ----------
def load_emails_from_file(path: str) -> List[str]:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and "@" in s:
                    out.append(s)
    except FileNotFoundError:
        print(f"Error: File not found: {path}")
    return out

def main():
    parser = argparse.ArgumentParser(
        description="Parallel POST requests with cookies for custom newsletters (optional proxies)."
    )
    parser.add_argument("--service", action="append", default=[], help="Service name (can be specified multiple times).")
    parser.add_argument("--all-services", action="store_true", help="Use all available services (all .json files).")
    parser.add_argument("--email", action="append", help="Email address (can be specified multiple times).")
    parser.add_argument("--file", help="File containing email addresses, one per line.")
    parser.add_argument("--repeats", type=int, default=1, help="How many times each email should be submitted (per email).")
    parser.add_argument("--refresh-proxies", action="store_true", help="Fetch fresh raw proxies from Mullvad.")
    parser.add_argument("--need", type=int, default=0, help="How many working proxies to resolve at most (lazy). 0 = use --match-jobs.")
    parser.add_argument("--match-jobs", action="store_true", help="Automatically set need to number of jobs (emails * repeats).")
    args = parser.parse_args()

    # Load configs for the selected services
    configs = []
    if args.all_services:
        for filename in os.listdir('.'):
            if filename.endswith('.json'):
                service_name = filename[:-5]
                config_path = filename
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    configs.append((service_name, config))
                except Exception as e:
                    print(f"Error loading config for {service_name}: {e}")
    elif args.service:
        for service_name in args.service:
            config_path = f"{service_name}.json"
            if not os.path.exists(config_path):
                print(f"Error: Config file not found: {config_path}")
                continue
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                configs.append((service_name, config))
            except Exception as e:
                print(f"Error loading config for {service_name}: {e}")
    else:
        print("No service specified. Use --service or --all-services.")
        return

    if not configs:
        print("No valid configs loaded.")
        return

    emails = []
    if args.email:
        emails.extend(args.email)
    if args.file:
        emails.extend(load_emails_from_file(args.file))
    if not emails:
        print("No valid emails provided. Use --email or --file.")
        return

    repeats = max(1, args.repeats)
    jobs = []
    for e in emails:
        for i in range(1, repeats + 1):
            for service_name, config in configs:
                jobs.append((e, i, service_name, config))
    total_jobs = len(jobs)
    print(f"Total jobs: {total_jobs}")

    raw_proxies = []
    if args.refresh_proxies:
        print("[+] Fetching proxies from Mullvad...")
        raw_proxies = fetch_mullvad_socks_raw()
        print(f"[+] Received {len(raw_proxies)} proxies")
    else:
        print("No proxy refresh requested. Make sure you have proxies if --need > 0.")

    if args.need and args.need > 0:
        need = args.need
    elif args.match_jobs:
        need = total_jobs
    else:
        need = 0

    if need > 0 and not raw_proxies:
        print("Warning: need > 0 set, but no raw proxies fetched. Setting need=0 and running without proxy.")
        need = 0

    resolver = None
    if need > 0 and dns is None:
        print("dnspython not installed. Proxy resolver disabled. Running without proxies.")
        need = 0

    if need > 0:
        effective_need = min(need, len(raw_proxies))
        print(f"Resolver started, searching for {effective_need} working proxies...")
        resolver = LazyResolver(raw_proxies, need=effective_need, threads=PROXY_RESOLVER_THREADS)
        resolver.start()
    else:
        print("Resolver not started (need=0). Running without proxies.")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BROWSERS) as executor:
        future_to_job = {}
        for email, attempt_no, service_name, config in jobs:
            fut = executor.submit(worker_task, email, attempt_no, service_name, resolver, config)
            future_to_job[fut] = (email, attempt_no, service_name)
        try:
            for fut in concurrent.futures.as_completed(future_to_job):
                email, attempt_no, service_name = future_to_job[fut]
                try:
                    res = fut.result()
                    results.append(res)
                    email, attempt_no, service_name, ok, msg, proxy = res
                    if ok:
                        print(f"[Main] Success: {email} (Run {attempt_no}) Service: {service_name} via {proxy if proxy else 'no-proxy'}")
                    else:
                        print(f"[Main] Failed: {email} (Run {attempt_no}) Service: {service_name} via {proxy if proxy else 'no-proxy'} → {msg[:200]}")
                except Exception as e:
                    print(f"[Main] Task exception for {email} (Run {attempt_no}) Service: {service_name}: {e}")
        except KeyboardInterrupt:
            print("Interrupted by user. Waiting for running tasks to finish...")
            executor.shutdown(wait=False)

    if resolver:
        resolver.stop()

    processed = sum(1 for r in results if r[3])
    failed = len(results) - processed
    print(f"Processed: {processed}, Failed: {failed}, Total: {len(results)}")

if __name__ == "__main__":
    main()
