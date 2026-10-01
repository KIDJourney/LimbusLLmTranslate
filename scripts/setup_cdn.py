import urllib.request
import urllib.error
import json
import os
import sys

ACCOUNT_ID = "986c17d0347c33645b6b4958fdb52b23"
ZONE_ID = "29a479ffa2bf2989de6137806bd78a7d"
BUCKET_NAME = "limbus-translation-releases"
CUSTOM_DOMAIN = "limbus-cdn.deadfish.win"
TOKEN_FILE = os.path.expanduser("~/.claude/secrets/meme_gen/r2_api_token")

def get_token():
    try:
        with open(TOKEN_FILE, 'r') as f:
            return f.read().strip()
    except Exception as e:
        print(f"Error reading token: {e}")
        sys.exit(1)

def request(url, method='GET', data=None, token=None):
    headers = {
        'Authorization': f'Bearer {token}'
    }
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode('utf-8')
    
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404 or e.code == 403:
            try:
                return json.loads(e.read().decode('utf-8'))
            except:
                pass
        print(f"HTTPError: {e.code} - {e.reason}")
        return {"success": False, "errors": [{"message": f"HTTPError {e.code}"}]}
    except Exception as e:
        print(f"Exception: {e}")
        return {"success": False, "errors": [{"message": str(e)}]}

def main():
    token = get_token()
    
    print(f"Checking bucket: {BUCKET_NAME}...")
    url_buckets = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/r2/buckets"
    res_buckets = request(url_buckets, method='GET', token=token)
    
    bucket_exists = False
    if res_buckets.get('success'):
        buckets = res_buckets.get('result', {}).get('buckets', [])
        if any(b.get('name') == BUCKET_NAME for b in buckets):
            bucket_exists = True
            
    if bucket_exists:
        print("Bucket already exists.")
    else:
        print("Creating bucket...")
        create_res = request(url_buckets, method='POST', data={"name": BUCKET_NAME}, token=token)
        if not create_res.get('success'):
            print(f"Failed to create bucket: {create_res.get('errors')}")
            sys.exit(1)
        print("Bucket created successfully.")
        
    print(f"Checking custom domain: {CUSTOM_DOMAIN}...")
    url_domain = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/r2/buckets/{BUCKET_NAME}/domains/custom"
    res_domains = request(url_domain, method='GET', token=token)
    
    domain_exists = False
    if res_domains.get('success'):
        domains = res_domains.get('result', {}).get('domains', [])
        if any(d.get('domain') == CUSTOM_DOMAIN for d in domains):
            domain_exists = True
            
    if domain_exists:
        print("Custom domain already bound.")
    else:
        print("Binding custom domain...")
        bind_res = request(url_domain, method='POST', data={"domain": CUSTOM_DOMAIN, "enabled": True, "zoneId": ZONE_ID}, token=token)
        if not bind_res.get('success'):
            print(f"Failed to bind custom domain: {bind_res.get('errors')}")
            sys.exit(1)
        print("Custom domain bound successfully.")

    print(json.dumps({
        "bucket": BUCKET_NAME,
        "domain": CUSTOM_DOMAIN,
        "status": "configured"
    }))

if __name__ == "__main__":
    main()
