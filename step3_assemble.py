import json, re, warnings
from collections import OrderedDict, defaultdict
import dns.resolver

warnings.filterwarnings("ignore")

rows = json.load(open('data_extracted.json'))
scrape = {int(k): v for k, v in json.load(open('scrape_results.json')).items()}

# ---------- DNS verification ----------
resolver = dns.resolver.Resolver()
resolver.lifetime = 6.0
resolver.timeout = 6.0
dns_cache = {}

def dns_present(domain):
    domain = (domain or '').lower().strip().strip('.')
    if not domain:
        return False
    if domain in dns_cache:
        return dns_cache[domain]
    ok = False
    for rtype in ('MX', 'A', 'AAAA'):
        try:
            if resolver.resolve(domain, rtype):
                ok = True
                break
        except Exception:
            continue
    dns_cache[domain] = ok
    return ok

# ---------- email cleaning ----------
BAD_LOCAL = ('example', 'your', 'youremail', 'user@', 'username', 'sentry', 'wixpress',
             'domain', 'test@', 'namn', 'exempel', 'ejemplo', 'muster', 'mustermann',
             'nom@', 'email@example', 'name@', 'firstname', 'lastname', 'abc@', 'xyz@',
             'exemplo', 'esempio', 'beispiel', 'sample@', 'contato@exemplo',
             'no-reply', 'noreply', 'donotreply')
BAD_DOMAIN = ('wixpress.com', 'sentry.io', 'sentry-next', 'example.com', 'example.org',
              'domain.com', 'email.com', 'yourdomain', 'test.com', '2x.png', 'godaddy.com',
              'wix.com', 'squarespace.com', 'sentry.wixpress.com')
BAD_SUFFIX = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.css', '.js', '.bmp', '.webp')

def valid_email(e):
    e = e.lower().strip().strip('.,;')
    if '@' not in e:
        return None
    if any(e.endswith(s) for s in BAD_SUFFIX):
        return None
    local, _, domain = e.partition('@')
    if not local or not domain or '.' not in domain:
        return None
    if any(b in e for b in BAD_LOCAL):
        return None
    if any(d in domain for d in BAD_DOMAIN):
        return None
    if len(e) > 60 or len(local) > 40:
        return None
    if re.fullmatch(r'[0-9a-f]{16,}', local):  # hex tracking ids
        return None
    return e

def email_domain(e):
    return e.split('@')[1]

# ---------- product description ----------
CAT_DESC = {
    'Suppliers and Manufacturers - Other Intermediaries': 'Cotton/textile supplier & manufacturer',
    'Suppliers and Manufacturers - Traders': 'Cotton/textile trader',
    'Retailers and Brands': 'Retailer / apparel brand',
    'Civil Society': 'Civil society organisation',
    'Producer Organisation': 'Cotton producer organisation',
    'Associate Member': 'Associate member',
}

def brief(text, limit=130):
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text:
        return ''
    # cut at sentence end near limit
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # try to end on a word boundary
    if ' ' in cut:
        cut = cut.rsplit(' ', 1)[0]
    return cut.rstrip(' .,;:-') + '…'

def product_for(item, sc):
    meta = sc.get('meta') if sc else ''
    title = sc.get('title') if sc else ''
    cat_desc = CAT_DESC.get(item['category'], 'Cotton/textile member')
    if meta and len(meta) > 20:
        return brief(meta)
    if title and len(title) > 15 and not re.search(r'(?i)(just a moment|access denied|error|not found|403|404|attention required|cloudflare)', title):
        return brief(f"{cat_desc} — {title}", 140)
    return cat_desc

def norm_display_website(w):
    w = w.strip()
    w = re.sub(r'^https?://', '', w, flags=re.I)
    w = w.rstrip('/')
    return w

# ---------- build per-company records ----------
records = []
for item in rows:
    sc = scrape.get(item['row'])
    website = norm_display_website(item['website']) if item['website'] else ''
    emails = []
    # source email(s) first
    if item['email']:
        for e in re.split(r'[;,]', item['email']):
            v = valid_email(e)
            if v:
                emails.append(v)
    # discovered emails
    if sc and sc.get('found_emails'):
        for e in sc['found_emails']:
            v = valid_email(e)
            if v:
                emails.append(v)
    # dedup emails preserve order
    seen = set(); emails = [e for e in emails if not (e in seen or seen.add(e))]
    # DNS verify: keep emails whose domain has DNS presence; discard only zero-DNS
    kept = []
    for e in emails:
        dom = email_domain(e)
        # reuse website dns if same domain and scrape said dns True
        if sc and sc.get('dns') and sc.get('site_domain') and sc['site_domain'] in dom:
            kept.append(e)
        elif dns_present(dom):
            kept.append(e)
    emails = kept
    product = product_for(item, sc)
    records.append({
        'company': item['company'],
        'country': item['country'],
        'category': item['category'],
        'website': website,
        'emails': emails,
        'product': product,
    })

# ---------- merge: group companies that share the SAME email ----------
# Build email -> list of record indices
email_to_recs = defaultdict(list)
for i, r in enumerate(records):
    for e in r['emails']:
        email_to_recs[e].append(i)

# Union-Find to group records sharing any email
parent = list(range(len(records)))
def find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x
def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[rb] = ra

for e, idxs in email_to_recs.items():
    if len(idxs) > 1:
        for j in idxs[1:]:
            union(idxs[0], j)

groups = OrderedDict()
for i, r in enumerate(records):
    root = find(i)
    groups.setdefault(root, []).append(i)

# ---------- assemble final entries ----------
final = []
for root, idxs in groups.items():
    comps = []
    websites = []
    emails = []
    products = []
    for i in idxs:
        r = records[i]
        name_country = r['company'] + ('\n' + r['country'] if r['country'] else '')
        if name_country not in comps:
            comps.append(name_country)
        if r['website'] and r['website'] not in websites:
            websites.append(r['website'])
        for e in r['emails']:
            if e not in emails:
                emails.append(e)
        if r['product'] and r['product'] not in products:
            products.append(r['product'])
    # sort key = first company name
    final.append({
        'companies': comps,
        'websites': websites,
        'emails': emails,
        'products': products,
        'sortkey': records[idxs[0]]['company'].lower(),
    })

final.sort(key=lambda x: x['sortkey'])

# ---------- summary counts ----------
only_web = only_email = neither = both = 0
for f in final:
    hw = bool(f['websites'])
    he = bool(f['emails'])
    if hw and he: both += 1
    elif hw and not he: only_web += 1
    elif he and not hw: only_email += 1
    else: neither += 1

summary = {
    'total_entries': len(final),
    'only_website_no_email': only_web,
    'only_email_no_website': only_email,
    'neither': neither,
    'both': both,
}

json.dump({'entries': final, 'summary': summary}, open('final_table.json', 'w'), indent=1, ensure_ascii=False)

print('final entries:', len(final), '(from', len(records), 'companies)')
print('grouped (shared-email) entries:', sum(1 for f in final if len(f['companies']) > 1))
print(json.dumps(summary, indent=1))
# show shared-email groups
print('--- shared-email groups ---')
for f in final:
    if len(f['companies']) > 1:
        print([c.split(chr(10))[0] for c in f['companies']], '->', f['emails'])
