import json, re, sys, warnings, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import dns.resolver

warnings.filterwarnings("ignore")
requests.packages.urllib3.disable_warnings()

with open('data_extracted.json') as f:
    rows = json.load(f)

# domains -> DNS cache
dns_cache = {}
resolver = dns.resolver.Resolver()
resolver.lifetime = 6.0
resolver.timeout = 6.0

def dns_present(domain):
    domain = domain.lower().strip().strip('.')
    if not domain:
        return False
    if domain in dns_cache:
        return dns_cache[domain]
    ok = False
    for rtype in ('MX', 'A', 'AAAA'):
        try:
            ans = resolver.resolve(domain, rtype)
            if ans:
                ok = True
                break
        except Exception:
            continue
    dns_cache[domain] = ok
    return ok

EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
# obfuscated patterns like "info [at] domain [dot] com"
OBF_RE = re.compile(r'([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\s+at\s+)\s*([A-Za-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*([A-Za-z]{2,})', re.I)

BAD_EMAIL_SUFFIX = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.css', '.js', '.bmp')
BAD_LOCAL = ('example', 'your', 'email', 'user', 'name', 'sentry', 'wixpress', 'domain', 'test')

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; MemberDirectoryBot/1.0)'}

def normalize_domain(url):
    u = url.strip()
    u = re.sub(r'^https?://', '', u, flags=re.I)
    u = u.split('/')[0].split('?')[0]
    u = u.strip().strip('.')
    if u.startswith('www.'):
        u = u[4:]
    return u.lower()

def candidate_urls(website):
    u = website.strip()
    if not re.match(r'^https?://', u, re.I):
        base = 'https://' + u
    else:
        base = u
    base = base.rstrip('/')
    return [base, base + '/contact', base + '/contact-us']

def clean_emails(found, site_domain):
    out = []
    for e in found:
        e = e.strip().strip('.').lower()
        if any(e.endswith(s) for s in BAD_EMAIL_SUFFIX):
            continue
        local = e.split('@')[0]
        if any(b in local for b in BAD_LOCAL):
            continue
        if len(e) > 60:
            continue
        out.append(e)
    # dedup preserve order, prefer same-domain
    seen = []
    for e in out:
        if e not in seen:
            seen.append(e)
    same = [e for e in seen if site_domain and site_domain in e.split('@')[1]]
    other = [e for e in seen if not (site_domain and site_domain in e.split('@')[1])]
    return same + other

TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
META_RE = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', re.I | re.S)
META_RE2 = re.compile(r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']', re.I | re.S)

def scrape(item):
    website = item['website']
    site_domain = normalize_domain(website)
    result = {
        'row': item['row'],
        'site_domain': site_domain,
        'found_emails': [],
        'title': '',
        'meta': '',
        'reachable': False,
        'dns': None,
    }
    result['dns'] = dns_present(site_domain)
    emails = []
    got_page = False
    if not result['dns']:
        # domain does not resolve at all -> no point fetching
        return result
    for url in candidate_urls(website)[:3]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=(5, 7), verify=False, allow_redirects=True, stream=True)
            if resp.status_code >= 400:
                resp.close()
                continue
            raw = resp.raw.read(600000, decode_content=True) or b''
            resp.close()
            html = raw.decode(resp.encoding or 'utf-8', errors='ignore')
            result['reachable'] = True
            got_page = True
            if not result['title']:
                m = TITLE_RE.search(html)
                if m:
                    result['title'] = re.sub(r'\s+', ' ', m.group(1)).strip()[:200]
            if not result['meta']:
                m = META_RE.search(html) or META_RE2.search(html)
                if m:
                    result['meta'] = re.sub(r'\s+', ' ', m.group(1)).strip()[:300]
            emails += EMAIL_RE.findall(html)
            for lp, dom, tld in OBF_RE.findall(html):
                emails.append(f'{lp}@{dom}.{tld}')
            ce = clean_emails(emails, site_domain)
            if ce:
                result['found_emails'] = ce[:3]
                break  # found emails, stop crawling further pages
        except Exception:
            continue
    result['found_emails'] = clean_emails(emails, site_domain)[:3]
    return result

import os
targets = [x for x in rows if x['website']]

# resume: load existing results
results = {}
if os.path.exists('scrape_results.json'):
    try:
        with open('scrape_results.json') as f:
            results = {int(k): v for k, v in json.load(f).items()}
    except Exception:
        results = {}

remaining = [t for t in targets if t['row'] not in results]
print(f'scraping {len(remaining)} sites (already have {len(results)})', flush=True)

def save():
    with open('scrape_results.json', 'w') as f:
        json.dump(results, f, indent=1, ensure_ascii=False)

done = 0
with ThreadPoolExecutor(max_workers=40) as ex:
    futs = {ex.submit(scrape, t): t for t in remaining}
    for fut in as_completed(futs):
        try:
            r = fut.result()
        except Exception as e:
            t = futs[fut]
            r = {'row': t['row'], 'site_domain': normalize_domain(t['website']), 'found_emails': [], 'title':'', 'meta':'', 'reachable': False, 'dns': None}
        results[r['row']] = r
        done += 1
        if done % 50 == 0:
            save()
            print(f'{done}/{len(remaining)} done', flush=True)

save()

reachable = sum(1 for r in results.values() if r['reachable'])
with_email = sum(1 for r in results.values() if r['found_emails'])
dns_ok = sum(1 for r in results.values() if r['dns'])
print('DONE. reachable:', reachable, 'found_email:', with_email, 'dns_ok:', dns_ok)
