#!/usr/bin/env python3
"""深信服AC Portal自动认证 - 简化版"""

import json
import os
import sys
import time
import httpx
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ["SANGFOR_USER", "SANGFOR_PASS"]:
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def rc4_encrypt(src: str, passwd: str) -> str:
    src = src.strip()
    key = [ord(passwd[i % len(passwd)]) for i in range(256)]
    sbox = list(range(256))
    j = 0
    for i in range(256):
        j = (j + sbox[i] + key[i]) % 256
        sbox[i], sbox[j] = sbox[j], sbox[i]
    a = b = 0
    output = []
    for ch in src:
        a = (a + 1) % 256
        b = (b + sbox[a]) % 256
        sbox[a], sbox[b] = sbox[b], sbox[a]
        c = (sbox[a] + sbox[b]) % 256
        x = ord(ch) ^ sbox[c]
        output.append(f"{x:02x}")
    return "".join(output)


def get_portal_url(client: httpx.Client) -> str | None:
    """检测Portal重定向"""
    for url in [
        "http://www.msftconnecttest.com/connecttest.txt",
        "http://connect.rom.miui.com/generate_204",
    ]:
        try:
            r = client.get(url, follow_redirects=False, timeout=5)
            if r.status_code == 200 and "Microsoft Connect Test" in r.text:
                return None  # 已认证
            loc = r.headers.get("location", "")
            if "1.1.1.3" in loc or "ac_portal" in loc:
                return loc
        except Exception:
            pass
    return None


def do_login(client: httpx.Client, portal_url: str, username: str, password: str) -> bool:
    """执行登录"""
    # 从portal URL提取基础地址
    base = "http://1.1.1.3"
    rckey = str(int(time.time() * 1000))
    encrypted_pwd = rc4_encrypt(password, rckey)

    params = {
        "opr": "pwdLogin",
        "userName": username,
        "pwd": encrypted_pwd,
        "auth_tag": rckey,
        "rememberPwd": "0",
    }

    r = client.post(f"{base}/ac_portal/login.php", data=params, timeout=10)
    resp = r.text
    print(f"  Response: {resp[:300]}")
    try:
        data = json.loads(resp)
        return data.get("success", False)
    except Exception:
        return False


def auth_ip(ip: str, username: str, password: str):
    print(f"\n--- IP: {ip} ---")

    transport = httpx.HTTPTransport(local_address=ip)
    with httpx.Client(verify=False, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, transport=transport) as c:
        portal = get_portal_url(c)
        if not portal:
            print("  [OK] Already authenticated")
            return True

        print(f"  Portal: {portal}")
        ok = do_login(c, portal, username, password)
        if ok:
            print("  [OK] Login success")
        else:
            print("  [FAIL] Login failed")
        return ok


def main():
    env = load_env()
    username = env.get("SANGFOR_USER", "")
    password = env.get("SANGFOR_PASS", "")

    if not username or not password:
        print("Error: SANGFOR_USER / SANGFOR_PASS not set")
        print(f"Edit {ENV_FILE} or set env vars")
        sys.exit(1)

    # 目标IP
    ips = ["192.168.132.118", "192.168.132.200"]
    for ip in ips:
        auth_ip(ip, username, password)


if __name__ == "__main__":
    main()
