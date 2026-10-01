import os
import sys
import json
import time
import shlex
import urllib.request
import urllib.error
import subprocess
import socket
from pathlib import Path
from datetime import datetime, timezone
import hashlib

# Configuration
ZONE_ID = "29a479ffa2bf2989de6137806bd78a7d"
DOMAIN = "limbus-cn.deadfish.win"
VPS_HOST = "152.42.184.69"
SSH_TARGET = f"root@{VPS_HOST}"
VPS_WWW_ROOT = "/var/www/limbus-cn"
VPS_RELEASES_DIR = f"{VPS_WWW_ROOT}/releases"
VPS_CURRENT_LINK = f"{VPS_WWW_ROOT}/current"
VPS_LETSENCRYPT_DIR = "/var/www/letsencrypt"
TOKEN_FILE = Path.home() / ".claude" / "secrets" / "cloudflare_token"
LOCAL_SITE_DIR = Path("site")
RECEIPT_DIR = Path(".workflow/evidence")

def log(msg):
    print(f"[*] {msg}", file=sys.stderr)

def error(msg):
    print(f"[!] {msg}", file=sys.stderr)
    sys.exit(1)

def run_ssh(cmd, check=True, timeout=180):
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH_TARGET, cmd]
    log(f"Running remote: {cmd}")
    res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        error(f"Remote command failed (exit {res.returncode}):\n{res.stderr}")
    return res

def run_local(cmd, check=True, timeout=180):
    log(f"Running local: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        error(f"Local command failed (exit {res.returncode}):\n{res.stderr}")
    return res

def get_cf_token():
    if not TOKEN_FILE.exists():
        error(f"Cloudflare token file not found at {TOKEN_FILE}")
    with open(TOKEN_FILE, "r") as f:
        return f.read().strip()

def cf_api_call(method, endpoint, data=None):
    url = f"https://api.cloudflare.com/client/v4/{endpoint}"
    token = get_cf_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    req_data = None
    if data:
        req_data = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)
            if not res_json.get("success"):
                error(f"Cloudflare API failed: {res_json.get('errors')}")
            return res_json
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        error(f"Cloudflare API HTTP Error {e.code}: {err_body}")
    except Exception as e:
        error(f"Cloudflare API Request Exception: {str(e)}")

def check_local_site():
    log("Checking local site files...")
    if not LOCAL_SITE_DIR.is_dir():
        error(f"Local site directory {LOCAL_SITE_DIR} not found.")

    required_files = ["index.html", "styles.css", "script.js"]
    for f in required_files:
        if not (LOCAL_SITE_DIR / f).is_file():
            error(f"Required file {f} not found in {LOCAL_SITE_DIR}")

    latest_json = LOCAL_SITE_DIR / "latest.json"
    if not latest_json.is_file():
        log("latest.json not found, downloads will be disabled.")
    else:
        log("latest.json found.")

    return [LOCAL_SITE_DIR / f for f in required_files if (LOCAL_SITE_DIR / f).is_file()]

def check_and_create_dns():
    log("Checking DNS record in Cloudflare...")
    res = cf_api_call("GET", f"zones/{ZONE_ID}/dns_records?name={DOMAIN}&type=A")
    records = res.get("result", [])

    if len(records) > 0:
        record = records[0]
        if record.get("content") != VPS_HOST:
            error(f"DNS record exists but points to {record.get('content')} instead of {VPS_HOST}")
        log(f"DNS record already exists and points to {VPS_HOST}")
    else:
        log("DNS record not found. Creating...")
        data = {
            "type": "A",
            "name": DOMAIN,
            "content": VPS_HOST,
            "proxied": True
        }
        cf_api_call("POST", f"zones/{ZONE_ID}/dns_records", data=data)
        log("DNS record created successfully.")

def wait_for_dns(domain, timeout=60):
    log(f"Waiting up to {timeout}s for DNS propagation for {domain}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            socket.getaddrinfo(domain, 443)
            log("DNS resolved.")
            return
        except socket.gaierror:
            time.sleep(5)
    error(f"DNS resolution for {domain} failed after {timeout} seconds.")

def upload_site():
    log("Uploading site to VPS...")
    import random
    import string

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    rand_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    release_name = f"{timestamp}-{rand_id}"
    release_path = f"{VPS_RELEASES_DIR}/{release_name}"

    run_ssh(f"mkdir -p {VPS_RELEASES_DIR}")

    log(f"Copying files to {release_path}")
    rsync_cmd = ["rsync", "-avz", "--delete", f"{LOCAL_SITE_DIR}/", f"{SSH_TARGET}:{release_path}/"]
    run_local(rsync_cmd)

    return release_path

def configure_nginx_http(release_path):
    log("Configuring Nginx for HTTP (ACME challenge)...")
    run_ssh(f"mkdir -p {VPS_LETSENCRYPT_DIR}")

    nginx_conf = f"""
server {{
    listen 80;
    listen [::]:80;
    server_name {DOMAIN};

    location /.well-known/acme-challenge/ {{
        root {VPS_LETSENCRYPT_DIR};
    }}

    location / {{
        root {release_path};
        index index.html;
    }}
}}
"""
    conf_path = f"/etc/nginx/sites-available/{DOMAIN}"
    link_path = f"/etc/nginx/sites-enabled/{DOMAIN}"

    # Backup existing if any
    backup_cmd = f"if [ -f {conf_path} ]; then cp {conf_path} {conf_path}.bak; echo exists; else echo not_exists; fi"
    res = run_ssh(backup_cmd, check=False)
    existed = "exists" in res.stdout

    write_cmd = f"cat << 'EOF' > {conf_path}\n{nginx_conf}\nEOF"
    run_ssh(write_cmd)
    run_ssh(f"ln -sf {conf_path} {link_path}")

    res_test = run_ssh("nginx -t", check=False)
    if res_test.returncode != 0:
        log("nginx -t failed for HTTP config, restoring...")
        if existed:
            run_ssh(f"mv {conf_path}.bak {conf_path}")
        else:
            run_ssh(f"rm -f {conf_path} {link_path}")
        run_ssh("nginx -t")
        run_ssh("systemctl reload nginx")
        error(f"Nginx test failed for HTTP:\n{res_test.stderr}")

    run_ssh("systemctl reload nginx")
    return existed

def obtain_cert():
    log("Obtaining SSL certificate via certbot...")
    certbot_cmd = f"certbot certonly --webroot -w {VPS_LETSENCRYPT_DIR} -d {DOMAIN} --non-interactive"
    res = run_ssh(certbot_cmd, check=False, timeout=300)
    if res.returncode != 0:
        error(f"Certbot failed:\n{res.stderr}\n{res.stdout}")
    log("Certbot succeeded.")

def configure_nginx_https(release_path, existed):
    log("Configuring Nginx for HTTPS...")

    conf_path = f"/etc/nginx/sites-available/{DOMAIN}"

    nginx_conf = f"""
server {{
    listen 80;
    listen [::]:80;
    server_name {DOMAIN};

    location /.well-known/acme-challenge/ {{
        root {VPS_LETSENCRYPT_DIR};
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}

server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {DOMAIN};

    ssl_certificate /etc/letsencrypt/live/{DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{DOMAIN}/privkey.pem;

    root {VPS_CURRENT_LINK};
    index index.html;

    location / {{
        try_files $uri $uri/ =404;
    }}

    location = /latest.json {{
        add_header Cache-Control "no-store";
    }}

    location ~* \.(css|js|png|jpg|jpeg|gif|ico|svg)$ {{
        expires 1h;
        add_header Cache-Control "public";
    }}
}}
"""
    # read old symlink target
    res_link = run_ssh(f"readlink {VPS_CURRENT_LINK} || echo no_link", check=False)
    old_target = res_link.stdout.strip()
    if old_target == "no_link":
        old_target = None

    write_cmd = f"cat << 'EOF' > {conf_path}.new\n{nginx_conf}\nEOF"
    run_ssh(write_cmd)

    # test new conf
    test_cmd = f"mv {conf_path} {conf_path}.tmp && mv {conf_path}.new {conf_path} && nginx -t"
    res = run_ssh(test_cmd, check=False)

    if res.returncode != 0:
        # Restore old conf
        run_ssh(f"mv {conf_path}.tmp {conf_path}")
        error(f"Nginx test failed for HTTPS:\n{res.stderr}")

    log("Swapping symlink to new release...")
    tmp_link = f"{VPS_CURRENT_LINK}_tmp"
    run_ssh(f"ln -s {release_path} {tmp_link} && mv -Tf {tmp_link} {VPS_CURRENT_LINK}")

    res_reload = run_ssh("systemctl reload nginx", check=False)
    if res_reload.returncode != 0:
        log("HTTPS reload failed, rolling back...")
        rollback(release_path, old_target, conf_path, existed)
        error(f"HTTPS reload failed:\n{res_reload.stderr}")

    return old_target, conf_path, existed

def rollback(release_path, old_target, conf_path, existed):
    log("Rolling back to previous state...")
    if old_target:
        tmp_link = f"{VPS_CURRENT_LINK}_tmp"
        run_ssh(f"ln -s {old_target} {tmp_link} && mv -Tf {tmp_link} {VPS_CURRENT_LINK}", check=False)
    # Restore config
    if existed:
        run_ssh(f"mv {conf_path}.bak {conf_path}", check=False)
    else:
        run_ssh(f"rm -f {conf_path} /etc/nginx/sites-enabled/{DOMAIN}", check=False)

    run_ssh("nginx -t", check=False)
    run_ssh("systemctl reload nginx", check=False)

def get_file_hash(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        h.update(f.read())
    return h.hexdigest()

def verify_deployment():
    log("Verifying deployment via HTTPS...")
    time.sleep(3)

    results = {}
    errors = []

    def check_url(path, local_file, expect_404=False):
        url = f"https://{DOMAIN}{path}"
        log(f"Checking {url}...")

        req = urllib.request.Request(url, headers={'User-Agent': 'LimbusTranslationUpdater/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if expect_404:
                    errors.append(f"Expected 404 for {url} but got {response.status}")
                else:
                    body = response.read()
                    remote_hash = hashlib.sha256(body).hexdigest()

                    cf_ray = response.headers.get("CF-RAY", "unknown")
                    cf_cache = response.headers.get("CF-Cache-Status", "unknown")
                    results[path] = {"status": response.status, "cf_ray": cf_ray, "cf_cache": cf_cache, "hash_match": False}

                    if local_file:
                        local_hash = get_file_hash(local_file)
                        if remote_hash != local_hash:
                            errors.append(f"Hash mismatch for {path}: local {local_hash} != remote {remote_hash}")
                        else:
                            results[path]["hash_match"] = True
                            log(f"Hash matched for {path}")

        except urllib.error.HTTPError as e:
            if expect_404 and e.code == 404:
                log(f"Got expected 404 for {url}")
                results[path] = {"status": 404}
            else:
                errors.append(f"HTTP error {e.code} for {url}")
        except Exception as e:
            errors.append(f"Failed to fetch {url}: {e}")

    check_url("/", LOCAL_SITE_DIR / "index.html")
    check_url("/styles.css", LOCAL_SITE_DIR / "styles.css")
    check_url("/script.js", LOCAL_SITE_DIR / "script.js")

    if not (LOCAL_SITE_DIR / "latest.json").is_file():
        check_url("/latest.json", None, expect_404=True)
    else:
        check_url("/latest.json", LOCAL_SITE_DIR / "latest.json")

    is_success = len(errors) == 0
    return is_success, results, errors

def write_receipt(release_path, verify_results, is_success):
    log("Writing receipt...")
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)

    receipt_id = release_path.split('/')[-1]
    receipt_path = RECEIPT_DIR / f"site-deploy-{receipt_id}.json"

    receipt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "domain": DOMAIN,
        "release_path": release_path,
        "verify_results": verify_results,
        "success": is_success
    }

    with open(receipt_path, "w") as f:
        json.dump(receipt, f, indent=2)

    log(f"Receipt written to {receipt_path}")

def main():
    check_local_site()
    check_and_create_dns()

    release_path = upload_site()

    existed = configure_nginx_http(release_path)
    wait_for_dns(DOMAIN)
    obtain_cert()

    old_target, conf_path, existed = configure_nginx_https(release_path, existed)

    is_success, verify_results, verify_errors = verify_deployment()
    write_receipt(release_path, verify_results, is_success)

    if not is_success:
        rollback(release_path, old_target, conf_path, existed)
        error(f"Deployment verification failed:\n" + "\n".join(verify_errors))

    log("Deployment completed successfully.")

if __name__ == "__main__":
    main()
