import re
import gzip
import os
import json
import tempfile
import sqlite3
import logging
import time
import requests
import paramiko
import pymysql

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

NPM_HOST = os.environ.get('NPM_HOST', '')
NPM_USER = os.environ.get('NPM_USER', 'ubuntu')
NPM_PASS = os.environ.get('NPM_PASSWORD', '')
LOG_DIR  = '/data/nginx-proxy-manager/data/logs'
SQLITE_PATH = '/data/nginx-proxy-manager/data/database.sqlite'

MYSQL_CFG = dict(
    host=os.environ.get('MYSQL_HOST', '127.0.0.1'),
    port=int(os.environ.get('MYSQL_PORT', '3306')),
    user=os.environ.get('MYSQL_USER', 'root'),
    password=os.environ.get('MYSQL_PASSWORD', ''),
    database=os.environ.get('MYSQL_DATABASE', 'npm_stats'),
    charset='utf8mb4',
    cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=10,
)

MONTH_MAP = {
    'Jan':'01','Feb':'02','Mar':'03','Apr':'04','May':'05','Jun':'06',
    'Jul':'07','Aug':'08','Sep':'09','Oct':'10','Nov':'11','Dec':'12',
}

# Matches both NPM log formats:
# proxy-host: [ts] - UPSTREAM RESP - GET https host.com "/" [Client ip]
# fallback:   [ts] STATUS - GET http host.com "/path" [Client ip]
LOG_RE = re.compile(
    r'\[(\d{2}/\w{3}/\d{4}):\d{2}:\d{2}:\d{2} [^\]]+\]'
    r'[^G]+'
    r'(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) \w+ (\S+)'
    r' "[^"]*" \[Client ([\d.]+)\]'
)


def parse_date(s):
    d, m, y = s.split('/')
    return f"{y}-{MONTH_MAP.get(m,'01')}-{d.zfill(2)}"


def get_domain_map(sftp):
    with tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False) as f:
        tmp = f.name
    try:
        sftp.get(SQLITE_PATH, tmp)
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("SELECT id, domain_names FROM proxy_host WHERE is_deleted=0")
        result = {}
        for host_id, domains in cur.fetchall():
            try:
                lst = json.loads(domains)
                result[host_id] = lst[0] if lst else f'proxy-host-{host_id}'
            except Exception:
                result[host_id] = str(domains)
        conn.close()
        return result
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def parse_lines(content, domain, stats):
    for line in content.splitlines():
        m = LOG_RE.search(line)
        if not m:
            continue
        try:
            access_date = parse_date(m.group(1))
        except Exception:
            continue
        host_in_log = m.group(3)
        client_ip   = m.group(4)
        d = domain if domain else host_in_log
        key = (d, client_ip, access_date)
        stats[key] = stats.get(key, 0) + 1


def ensure_ip_locations_table(db):
    with db.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ip_locations (
                ip VARCHAR(50) PRIMARY KEY,
                country VARCHAR(100),
                region VARCHAR(100),
                city VARCHAR(100),
                is_zhejiang TINYINT DEFAULT 0,
                queried_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB
        """)
    db.commit()


def lookup_new_ips(db):
    """Query ip-api.com for IPs not yet geolocated. Runs after main sync."""
    ensure_ip_locations_table(db)

    with db.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT client_ip FROM access_stats
            WHERE client_ip NOT IN (SELECT ip FROM ip_locations)
            LIMIT 500
        """)
        ips = [r['client_ip'] for r in cur.fetchall()]

    if not ips:
        logger.info("IP geo: no new IPs to look up")
        return

    logger.info(f"IP geo: looking up {len(ips)} new IPs via ip-api.com...")
    batch_size = 100
    results = []

    for i in range(0, len(ips), batch_size):
        batch = ips[i:i + batch_size]
        try:
            resp = requests.post(
                'http://ip-api.com/batch?fields=query,status,country,regionName,city',
                json=[{'query': ip} for ip in batch],
                timeout=30
            )
            if resp.status_code == 200:
                results.extend(resp.json())
        except Exception as e:
            logger.warning(f"IP geo batch failed: {e}")
        time.sleep(1.5)  # ~40 req/min, stay under free limit of 45

    if results:
        batch_data = []
        for r in results:
            if r.get('status') == 'success':
                is_zj = 1 if r.get('country') == 'China' and \
                             r.get('regionName') == 'Zhejiang' else 0
                batch_data.append((
                    r['query'],
                    r.get('country', ''),
                    r.get('regionName', ''),
                    r.get('city', ''),
                    is_zj
                ))
        if batch_data:
            with db.cursor() as cur:
                cur.executemany(
                    "INSERT IGNORE INTO ip_locations "
                    "(ip, country, region, city, is_zhejiang) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    batch_data
                )
            db.commit()
            logger.info(f"IP geo: stored {len(batch_data)} locations")


def run_sync():
    logger.info("=== Sync started ===")
    db = pymysql.connect(**MYSQL_CFG)
    sync_id = None
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO sync_log (status, message) VALUES ('running', 'started')")
            sync_id = db.insert_id()
        db.commit()

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(NPM_HOST, username=NPM_USER, password=NPM_PASS, timeout=30)
        sftp = ssh.open_sftp()

        domain_map = get_domain_map(sftp)
        logger.info(f"Domain map: {len(domain_map)} hosts")

        stats = {}
        log_files = sftp.listdir(LOG_DIR)

        for filename in log_files:
            m = re.match(r'proxy-host-(\d+)_access\.log(\.\d+)?$', filename)
            if m:
                host_id = int(m.group(1))
                domain  = domain_map.get(host_id, f'proxy-host-{host_id}')
                try:
                    with sftp.file(f"{LOG_DIR}/{filename}", 'r') as f:
                        content = f.read().decode('utf-8', errors='ignore')
                    parse_lines(content, domain, stats)
                except Exception as e:
                    logger.warning(f"Skip {filename}: {e}")
                continue

            m = re.match(r'proxy-host-(\d+)_access\.log\.\d+\.gz$', filename)
            if m:
                host_id = int(m.group(1))
                domain  = domain_map.get(host_id, f'proxy-host-{host_id}')
                try:
                    with sftp.file(f"{LOG_DIR}/{filename}", 'rb') as f:
                        raw = f.read()
                    content = gzip.decompress(raw).decode('utf-8', errors='ignore')
                    parse_lines(content, domain, stats)
                except Exception as e:
                    logger.warning(f"Skip {filename}: {e}")

        sftp.close()
        ssh.close()
        logger.info(f"Parsed {len(stats)} unique (domain, ip, date) combos")

        with db.cursor() as cur:
            cur.execute("DELETE FROM access_stats")
            batch = []
            for (domain, ip, date), cnt in stats.items():
                batch.append((domain, ip, date, cnt))
                if len(batch) >= 500:
                    cur.executemany(
                        "INSERT INTO access_stats (domain,client_ip,access_date,count)"
                        " VALUES (%s,%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE count=VALUES(count)",
                        batch
                    )
                    batch = []
            if batch:
                cur.executemany(
                    "INSERT INTO access_stats (domain,client_ip,access_date,count)"
                    " VALUES (%s,%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE count=VALUES(count)",
                    batch
                )
            total_hits = sum(stats.values())
            msg = f"OK: {len(stats)} records, {total_hits} total hits"
            if sync_id:
                cur.execute("UPDATE sync_log SET status='success', message=%s WHERE id=%s",
                            (msg, sync_id))
        db.commit()
        logger.info(msg)

        # Geo-lookup new IPs (non-blocking, runs after main sync)
        try:
            lookup_new_ips(db)
        except Exception as e:
            logger.warning(f"IP geo lookup failed (non-fatal): {e}")

    except Exception as e:
        logger.error(f"Sync error: {e}", exc_info=True)
        try:
            with db.cursor() as cur:
                if sync_id:
                    cur.execute(
                        "UPDATE sync_log SET status='error', message=%s WHERE id=%s",
                        (str(e)[:500], sync_id)
                    )
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


if __name__ == '__main__':
    run_sync()
