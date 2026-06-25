#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""IP Down - Multi-IP parallel download tool.

Usage:
  uv run ipdown.py <url> [options]

Options:
  -o, --output <file>       Output filename (default: from URL)
  -i, --interface <ip>      IPs to use, comma-separated
  -a, --adapter <name>      Adapter name (default: auto-detect via route)
  -p, --parts <n>           Number of parts (default: number of IPs)
  -t, --timeout <s>         Connect timeout in seconds (default: 30)
  -T, --total-timeout <s>   Total timeout per part in seconds (default: 600)
  -r, --retries <n>         Max retries per part (default: 3)
  --proxy <host:port>       HTTP proxy
  --insecure, -k            Skip certificate verification
  -h, --help                Show this help

Features:
  - Pure Python, no external dependencies (no curl)
  - Bind download to specific source IPs via socket
  - Adapter-aware: auto-detect default adapter or specify by name
  - Resume interrupted downloads (manifest-based)
  - Auto-retry on failure
  - Progress bar with speed display
"""

import argparse
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse


class SourceIPTransport:
    """HTTP transport that binds to a specific source IP, with optional proxy."""

    def __init__(self, source_ip: str, timeout: int = 30, proxy: str = None, insecure: bool = False):
        self.source_ip = source_ip
        self.timeout = timeout
        self.proxy = proxy
        if insecure:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE
        else:
            self._ssl_ctx = ssl.create_default_context()

    def _connect(self, host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        if self.proxy:
            proxy_host, proxy_port = self.proxy.rsplit(":", 1)
            sock.connect((proxy_host, int(proxy_port)))
        else:
            sock.bind((self.source_ip, 0))
            sock.connect((host, port))
        return sock

    def _ssl_wrap(self, sock: socket.socket, target_host: str) -> socket.socket:
        if self.proxy:
            connect_req = f"CONNECT {target_host}:443 HTTP/1.1\r\nHost: {target_host}:443\r\nProxy-Connection: close\r\n\r\n"
            sock.sendall(connect_req.encode())
            response = b""
            while b"\r\n\r\n" not in response:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("Proxy CONNECT failed: closed")
                response += data
            if "200" not in response.split(b"\r\n")[0].decode():
                raise ConnectionError(f"Proxy CONNECT failed: {response.split(b'\r\n')[0].decode()}")
        return self._ssl_ctx.wrap_socket(sock, server_hostname=target_host)

    def _make_request(self, method: str, url: str, extra_headers: dict = None) -> tuple[socket.socket, http.client.HTTPResponse]:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query

        sock = self._connect(host, port)
        if parsed.scheme == 'https':
            sock = self._ssl_wrap(sock, host)

        request_target = url if self.proxy else path
        headers = {
            'Host': host if not self.proxy else f"{host}:{port}",
            'User-Agent': 'IPDown/1.0',
            'Connection': 'close',
        }
        if extra_headers:
            headers.update(extra_headers)

        request_line = f"{method} {request_target} HTTP/1.1\r\n"
        header_lines = ''.join(f"{k}: {v}\r\n" for k, v in headers.items())
        sock.sendall((request_line + header_lines + '\r\n').encode())

        resp = http.client.HTTPResponse(sock, method=method)
        resp.begin()
        return sock, resp

    def head(self, url: str, follow_redirects: bool = True, max_redirects: int = 10) -> tuple[dict, str]:
        current_url = url
        for _ in range(max_redirects):
            sock, resp = self._make_request('HEAD', current_url)
            status = resp.status
            headers = dict(resp.getheaders())
            resp.close()
            sock.close()

            if status in (301, 302, 303, 307, 308) and 'Location' in headers:
                location = headers['Location']
                if location.startswith('http://') or location.startswith('https://'):
                    current_url = location
                elif location.startswith('/'):
                    parsed = urllib.parse.urlparse(current_url)
                    port = parsed.port
                    host_part = parsed.hostname if not port else f"{parsed.hostname}:{port}"
                    current_url = f"{parsed.scheme}://{host_part}{location}"
                else:
                    break
                if not follow_redirects:
                    break
                continue
            return headers, current_url
        raise ConnectionError(f"Too many redirects for {url}")

    def get_range(self, url: str, start: int, end: int) -> tuple[int, http.client.HTTPResponse, socket.socket]:
        sock, resp = self._make_request('GET', url, {'Range': f'bytes={start}-{end}'})
        return resp.status, resp, sock

    def get_stream(self, url: str) -> tuple[int, http.client.HTTPResponse, socket.socket, int]:
        """GET without Range. Returns (status, resp, sock, content_length)."""
        sock, resp = self._make_request('GET', url)
        cl = 0
        for k, v in resp.getheaders():
            if k.lower() == 'content-length':
                cl = int(v)
        return resp.status, resp, sock, cl

    def check_range_support(self, url: str) -> bool:
        try:
            sock, resp = self._make_request('GET', url, {'Range': 'bytes=0-0'})
            status = resp.status
            resp.close()
            sock.close()
            return status == 206
        except Exception:
            return False

    def download_range(self, url: str, range_start: int, range_end: int,
                       dest: str, lock: threading.Lock, progress: dict, part_index: int,
                       total_timeout: int = 600):
        expected = range_end - range_start + 1
        max_retries = progress['max_retries']
        part_start = time.time()
        full_file = progress.get('content_length', 0)
        requesting_full = (range_start == 0 and range_end == full_file - 1)

        for attempt in range(max_retries):
            if time.time() - part_start > total_timeout:
                with lock:
                    progress['failed_parts'].append(part_index)
                return

            status = 0
            resp = None
            sock = None
            try:
                status, resp, sock = self.get_range(url, range_start, range_end)
                if status == 200 and requesting_full:
                    pass
                elif status == 206:
                    pass
                else:
                    resp.close()
                    sock.close()
                    raise RuntimeError(f"HTTP {status}")

                downloaded = 0
                with open(dest, 'wb') as f:
                    while True:
                        if time.time() - part_start > total_timeout:
                            resp.close()
                            sock.close()
                            f.close()
                            if os.path.exists(dest):
                                os.remove(dest)
                            with lock:
                                progress['failed_parts'].append(part_index)
                            return
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        with lock:
                            progress['bytes'] += len(chunk)

                resp.close()
                sock.close()

                actual = os.path.getsize(dest)
                if requesting_full:
                    if actual == full_file:
                        return
                else:
                    if actual == expected:
                        return
                os.remove(dest)
                with lock:
                    progress['bytes'] -= downloaded
                raise RuntimeError("Size mismatch")

            except Exception:
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                try:
                    if resp:
                        resp.close()
                    if sock:
                        sock.close()
                except Exception:
                    pass
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue

        with lock:
            progress['failed_parts'].append(part_index)

    def download_stream(self, url: str, dest: str, lock: threading.Lock, progress: dict,
                        total_timeout: int = 600):
        """Fallback: single-threaded streaming download (unknown content length)."""
        max_retries = progress['max_retries']
        part_start = time.time()

        for attempt in range(max_retries):
            if time.time() - part_start > total_timeout:
                with lock:
                    progress['failed_parts'].append(0)
                return

            resp = None
            sock = None
            downloaded = 0
            try:
                status, resp, sock, _ = self.get_stream(url)
                if status not in (200, 206):
                    raise RuntimeError(f"HTTP {status}")

                with open(dest, 'wb') as f:
                    while True:
                        if time.time() - part_start > total_timeout:
                            resp.close()
                            sock.close()
                            f.close()
                            if os.path.exists(dest):
                                os.remove(dest)
                            with lock:
                                progress['failed_parts'].append(0)
                            return
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        with lock:
                            progress['bytes'] += len(chunk)

                resp.close()
                sock.close()

                actual = os.path.getsize(dest)
                if actual > 0:
                    return
                os.remove(dest)
                with lock:
                    progress['bytes'] -= downloaded
                raise RuntimeError("Empty file")

            except Exception:
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                try:
                    if resp:
                        resp.close()
                    if sock:
                        sock.close()
                except Exception:
                    pass
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue

        with lock:
            progress['failed_parts'].append(0)


def get_adapter_ips_windows(adapter_name: str = None) -> tuple[str, list[str]]:
    try:
        if adapter_name:
            cmd = (
                f"Get-NetAdapter -Name '{adapter_name}' -ErrorAction Stop | "
                f"Select-Object -ExpandProperty InterfaceIndex"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return adapter_name, []
            idx = result.stdout.strip()
            cmd = (
                f"Get-NetIPAddress -InterfaceIndex {idx} -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
                f"Select-Object -ExpandProperty IPAddress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                ips = [l.strip() for l in result.stdout.strip().split('\n')
                       if re.match(r'^\d+\.\d+\.\d+\.\d+$', l.strip())]
                return adapter_name, ips
        else:
            cmd = (
                "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
                "Where-Object NextHop -ne '0.0.0.0' | "
                "Sort-Object RouteMetric | Select-Object -First 1 -ExpandProperty InterfaceIndex"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 or not result.stdout.strip():
                return _get_all_ips_fallback()
            idx = result.stdout.strip()
            cmd_name = (
                f"Get-NetAdapter -InterfaceIndex {idx} -ErrorAction SilentlyContinue | "
                f"Select-Object -ExpandProperty Name"
            )
            result_name = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd_name],
                capture_output=True, text=True, timeout=10
            )
            adapter_name = result_name.stdout.strip() if result_name.returncode == 0 else f"Index:{idx}"
            cmd = (
                f"Get-NetIPAddress -InterfaceIndex {idx} -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
                f"Select-Object -ExpandProperty IPAddress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                ips = [l.strip() for l in result.stdout.strip().split('\n')
                       if re.match(r'^\d+\.\d+\.\d+\.\d+$', l.strip())]
                return adapter_name, ips
    except Exception:
        pass
    return _get_all_ips_fallback()


def _get_all_ips_fallback() -> tuple[str, list[str]]:
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        ips = list(set(addr[4][0] for addr in addrs))
        ips = [ip for ip in ips if not ip.startswith('127.')]
        return "(all)", ips
    except Exception:
        return "(unknown)", []


def get_adapter_ips_non_windows() -> tuple[str, list[str]]:
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        ips = list(set(addr[4][0] for addr in addrs))
        ips = [ip for ip in ips if not ip.startswith('127.')]
        return "(auto)", ips
    except Exception:
        return "(unknown)", []


def check_ip_reachable(ip: str, url: str, timeout: int = 10, proxy: str = None, insecure: bool = False) -> bool:
    try:
        t = SourceIPTransport(ip, timeout=timeout, proxy=proxy, insecure=insecure)
        t.head(url, follow_redirects=True)
        return True
    except ssl.SSLCertVerificationError:
        print(f"  {ip} SSL error - use -k to skip", flush=True)
        return False
    except Exception as e:
        return False


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    else:
        return f"{n / (1024 * 1024 * 1024):.2f} GB"


def print_progress(current: int, total: int, speed: float, width: int = 40):
    pct = min(current / total, 1.0) if total > 0 else 0
    filled = int(width * pct)
    bar = '#' * filled + '-' * (width - filled)
    print(f"\r  [{bar}] {pct * 100:5.1f}%  {format_size(current)}/{format_size(total)}  {format_size(int(speed))}/s    ", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description="IP Down - Multi-IP parallel download", add_help=False)
    parser.add_argument("url", nargs="?", help="Download URL")
    parser.add_argument("-o", "--output", help="Output filename")
    parser.add_argument("-i", "--interface", help="IPs, comma-separated")
    parser.add_argument("-a", "--adapter", help="Adapter name (default: auto-detect)")
    parser.add_argument("-p", "--parts", type=int, default=0, help="Number of parts")
    parser.add_argument("-t", "--timeout", type=int, default=30, help="Connect timeout (seconds)")
    parser.add_argument("-T", "--total-timeout", type=int, default=600, help="Total timeout per part (seconds)")
    parser.add_argument("-r", "--retries", type=int, default=3, help="Max retries per part")
    parser.add_argument("--proxy", help="HTTP proxy host:port")
    parser.add_argument("-k", "--insecure", action="store_true", help="Skip certificate verification")
    parser.add_argument("-h", "--help", action="store_true", help="Show help")

    args = parser.parse_args()

    if args.help or not args.url:
        print(__doc__)
        return

    # --- Proxy: --proxy > direct (default) ---
    proxy = args.proxy

    # --- Resolve IPs ---
    ips = []
    adapter_label = ""

    if args.interface:
        ips = [ip.strip() for ip in args.interface.split(',') if ip.strip()]
        adapter_label = "(manual)"
    else:
        if platform.system() == "Windows":
            adapter_label, ips = get_adapter_ips_windows(args.adapter)
        else:
            adapter_label, ips = get_adapter_ips_non_windows()

    if not ips:
        print("[ERROR] No IPs found.", file=sys.stderr)
        sys.exit(1)

    if not args.output:
        parsed = urllib.parse.urlparse(args.url)
        args.output = os.path.basename(parsed.path) or "download"

    print("\n=== IP Down ===")
    print(f"URL:    {args.url}")
    print(f"Output: {args.output}")
    print(f"Adapter: {adapter_label}")
    print(f"IPs:    {', '.join(ips)}")
    if proxy:
        print(f"Proxy:  {proxy}")
    else:
        print("Proxy:  (direct)")

    # --- Check IP reachability ---
    print("\nChecking IPs...")
    alive_ips = []
    for ip in ips:
        if check_ip_reachable(ip, args.url, timeout=args.timeout, proxy=proxy, insecure=args.insecure):
            alive_ips.append(ip)
            print(f"  {ip} OK", flush=True)
        else:
            print(f"  {ip} UNREACHABLE - skipped", flush=True)

    if not alive_ips:
        print("[ERROR] No reachable IPs.", file=sys.stderr)
        sys.exit(1)
    ips = alive_ips

    # --- Get file size ---
    print("\nGetting file size...")
    t = SourceIPTransport(ips[0], timeout=args.timeout, proxy=proxy, insecure=args.insecure)
    try:
        headers, final_url = t.head(args.url, follow_redirects=True)
    except ssl.SSLCertVerificationError:
        print("[ERROR] Certificate verification failed. Re-run with -k to skip.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Failed to get file info: {e}", file=sys.stderr)
        sys.exit(1)

    if final_url != args.url:
        print(f"Redirected to: {final_url}")
        args.url = final_url

    content_length = 0
    for k, v in headers.items():
        if k.lower() == 'content-length':
            content_length = int(v)

    # Fallback: if HEAD didn't return Content-Length, try GET Range: bytes=0-0
    if content_length == 0:
        print("HEAD has no Content-Length, probing with GET...")
        t_probe2 = SourceIPTransport(ips[0], timeout=args.timeout, proxy=proxy, insecure=args.insecure)
        try:
            status2, resp2, sock2, cl2 = t_probe2.get_stream(args.url)
            resp2.close()
            sock2.close()
            if cl2 > 0:
                content_length = cl2
                print(f"Got file size from GET: {format_size(content_length)}")
        except Exception:
            pass

    streaming_mode = False
    if content_length == 0:
        print("Cannot determine file size. Falling back to streaming (single-threaded) download.")
        streaming_mode = True
    else:
        print(f"File size: {format_size(content_length)}")

    # --- Probe Range support ---
    if streaming_mode:
        parts = 1
    else:
        print("Checking Range support...")
        t_probe = SourceIPTransport(ips[0], timeout=args.timeout, proxy=proxy, insecure=args.insecure)
        range_supported = t_probe.check_range_support(args.url)

        if not range_supported:
            print("Server does NOT support Range. Falling back to single-threaded download.")
            parts = 1
        else:
            print("Server supports Range.")
            parts = args.parts if args.parts > 0 else len(ips)
            if parts > len(ips):
                parts = len(ips)
            if parts > content_length:
                parts = content_length
    print(f"Parts:   {parts}")

    # --- Temp directory ---
    final_path = os.path.abspath(args.output)
    final_dir = os.path.dirname(final_path)
    url_hash = hashlib.sha256(args.url.encode()).hexdigest()[:12]
    tmp_dir = os.path.join(final_dir, f".dl_{url_hash}")
    os.makedirs(tmp_dir, exist_ok=True)

    manifest_path = os.path.join(tmp_dir, "manifest.json")

    def read_manifest():
        if not os.path.exists(manifest_path):
            return None
        try:
            with open(manifest_path, 'r') as f:
                return json.load(f)
        except Exception:
            return None

    def write_manifest(m):
        with open(manifest_path, 'w') as f:
            json.dump(m, f, indent=2)

    # --- Chunk plan ---
    chunk_plan = []
    if not streaming_mode:
        chunk_size = (content_length + parts - 1) // parts
        for i in range(parts):
            start = i * chunk_size
            end = min((i + 1) * chunk_size - 1, content_length - 1)
            chunk_plan.append({
                "index": i, "start": start, "end": end,
                "size": end - start + 1, "ip": ips[i % len(ips)]
            })

    # --- Resume check ---
    existing = read_manifest()
    resume_all = False
    if (existing and existing.get("url") == args.url and
            existing.get("totalSize") == content_length and
            existing.get("parts") == parts):
        all_valid = True
        for p in existing.get("partList", []):
            pf = os.path.join(tmp_dir, f"part_{p['index']}")
            if not os.path.exists(pf) or os.path.getsize(pf) != p["size"]:
                all_valid = False
                break
        if all_valid:
            resume_all = True
            print(f"\nAll {parts} parts validated from cache, skipping download...")

    start_time = time.time()

    if streaming_mode:
        # --- Streaming download (unknown content length) ---
        if not resume_all:
            print("\nStarting streaming download (size unknown)...")
            print(f"Connect timeout: {args.timeout}s | Total timeout: {args.total_timeout}s/part")

            lock = threading.Lock()
            progress = {'bytes': 0, 'failed_parts': [], 'max_retries': args.retries}

            pf = os.path.join(tmp_dir, "part_0")
            if os.path.exists(pf) and os.path.getsize(pf) > 0:
                progress['bytes'] = os.path.getsize(pf)
                print(f"  Resuming from cached part: {format_size(os.path.getsize(pf))}")
            else:
                if os.path.exists(pf):
                    os.remove(pf)
                print(f"  Downloading via {ips[0]}...")
                t_obj = SourceIPTransport(ips[0], timeout=args.timeout, proxy=proxy, insecure=args.insecure)
                t_obj.download_stream(args.url, pf, lock, progress, args.total_timeout)

            elapsed = time.time() - start_time
            if progress['failed_parts']:
                hint = " If certificate error, re-run with -k." if not args.insecure else ""
                print(f"\n[ERROR] Download failed.{hint}", file=sys.stderr)
                sys.exit(1)

            pf_size = os.path.getsize(pf) if os.path.exists(pf) else 0
            speed = pf_size / elapsed if elapsed > 0 else 0
            print(f"\n  Downloaded: {format_size(pf_size)} in {round(elapsed, 1)}s ({format_size(int(speed))}/s)")

        # Verify & move
        pf = os.path.join(tmp_dir, "part_0")
        if not os.path.exists(pf) or os.path.getsize(pf) == 0:
            print("\n[ERROR] Download produced empty file.", file=sys.stderr)
            sys.exit(1)

        tmp_final = final_path + ".tmp"
        shutil.copy2(pf, tmp_final)
        if os.path.exists(final_path):
            os.remove(final_path)
        os.rename(tmp_final, final_path)

    else:
        # --- Parallel chunk download ---
        if not resume_all:
            print(f"\nStarting {parts} parallel downloads...")
            print(f"Connect timeout: {args.timeout}s | Total timeout: {args.total_timeout}s/part")

            lock = threading.Lock()
            progress = {
                'bytes': 0,
                'failed_parts': [],
                'max_retries': args.retries,
                'content_length': content_length
            }

            threads = []
            for i in range(parts):
                pf = os.path.join(tmp_dir, f"part_{i}")
                if os.path.exists(pf) and os.path.getsize(pf) == chunk_plan[i]["size"]:
                    progress['bytes'] += os.path.getsize(pf)
                    print(f"  Part {i + 1}: {format_size(chunk_plan[i]['size'])} via {chunk_plan[i]['ip']} [cached]")
                    continue

                if os.path.exists(pf):
                    os.remove(pf)

                print(f"  Part {i + 1}: {chunk_plan[i]['start']}-{chunk_plan[i]['end']} ({format_size(chunk_plan[i]['size'])}) via {chunk_plan[i]['ip']}")

                t_obj = SourceIPTransport(chunk_plan[i]['ip'], timeout=args.timeout, proxy=proxy, insecure=args.insecure)
                thread = threading.Thread(
                    target=t_obj.download_range,
                    args=(args.url, chunk_plan[i]['start'], chunk_plan[i]['end'],
                          pf, lock, progress, i, args.total_timeout)
                )
                threads.append(thread)
                thread.start()

            while any(t.is_alive() for t in threads):
                elapsed = time.time() - start_time
                speed = progress['bytes'] / elapsed if elapsed > 0 else 0
                print_progress(min(progress['bytes'], content_length), content_length, speed)
                time.sleep(0.5)

            for t in threads:
                t.join()

            elapsed = time.time() - start_time
            speed = progress['bytes'] / elapsed if elapsed > 0 else 0
            print_progress(min(progress['bytes'], content_length), content_length, speed)
            print()

            if progress['failed_parts']:
                hint = " If certificate error, re-run with -k." if not args.insecure else ""
                print(f"\n[ERROR] Parts {sorted(progress['failed_parts'])} failed.{hint}", file=sys.stderr)
                sys.exit(1)

            write_manifest({
                "url": args.url, "totalSize": content_length, "parts": parts,
                "partList": chunk_plan,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            })

        # --- Verify ---
        print("\nVerifying parts...")
        verify_ok = True
        for i in range(parts):
            pf = os.path.join(tmp_dir, f"part_{i}")
            if not os.path.exists(pf):
                print(f"  Part {i + 1}: MISSING")
                verify_ok = False
                continue
            actual = os.path.getsize(pf)
            expected = chunk_plan[i]["size"]
            if actual != expected:
                print(f"  Part {i + 1}: SIZE MISMATCH ({actual} != {expected})")
                verify_ok = False
            else:
                print(f"  Part {i + 1}: {format_size(actual)} OK")

        if not verify_ok:
            print("\n[ERROR] Verification failed. Re-run to retry.", file=sys.stderr)
            sys.exit(1)

        # --- Merge ---
        print("\nMerging parts...")
        tmp_final = final_path + ".tmp"
        try:
            with open(tmp_final, 'wb') as out:
                for i in range(parts):
                    pf = os.path.join(tmp_dir, f"part_{i}")
                    with open(pf, 'rb') as f:
                        while True:
                            chunk = f.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)

            final_size = os.path.getsize(tmp_final)
            if final_size != content_length:
                raise Exception(f"Size mismatch: expected {content_length}, got {final_size}")

            if os.path.exists(final_path):
                os.remove(final_path)
            os.rename(tmp_final, final_path)

        except Exception as e:
            if os.path.exists(tmp_final):
                os.remove(tmp_final)
            print(f"\n[ERROR] Merge failed: {e}", file=sys.stderr)
            print(f"Parts preserved at: {tmp_dir}", file=sys.stderr)
            sys.exit(1)

    # --- Done ---
    total_time = time.time() - start_time
    final_size = os.path.getsize(final_path)
    avg_speed = final_size / total_time if total_time > 0 else 0

    print(f"\n=== Download Complete ===")
    print(f"File:      {final_path}")
    print(f"Size:      {format_size(final_size)}")
    print(f"Time:      {round(total_time, 1)}s")
    print(f"Avg speed: {format_size(int(avg_speed))}/s")

    shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
