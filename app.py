import os
import threading
import pymysql
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError("SECRET_KEY environment variable is required")

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS')
if not ADMIN_PASS:
    raise RuntimeError("ADMIN_PASS environment variable is required")

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


def get_db():
    return pymysql.connect(**MYSQL_CFG)


def ensure_tables():
    """Create tables that may not exist yet (e.g. ip_locations)."""
    db = get_db()
    try:
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
    finally:
        db.close()


def serialize_row(r):
    """Convert Decimal/date objects to JSON-safe types."""
    out = {}
    for k, v in r.items():
        if hasattr(v, 'year') and hasattr(v, 'month'):  # date / datetime
            out[k] = str(v)[:10]  # YYYY-MM-DD
        elif hasattr(v, '__round__'):  # Decimal
            out[k] = int(v)
        else:
            out[k] = v
    return out


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USER and \
           request.form.get('password') == ADMIN_PASS:
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = '用户名或密码错误'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html')


# ── API ───────────────────────────────────────────────────────────────────────

@app.route('/api/summary')
@login_required
def api_summary():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT domain,
                       SUM(count)                AS total,
                       COUNT(DISTINCT client_ip) AS unique_ips,
                       MAX(access_date)          AS last_seen,
                       MIN(access_date)          AS first_seen
                FROM access_stats
                GROUP BY domain
                ORDER BY total DESC
            """)
            return jsonify([serialize_row(r) for r in cur.fetchall()])
    finally:
        db.close()


@app.route('/api/detail')
@login_required
def api_detail():
    domain    = request.args.get('domain', '')
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    sort      = request.args.get('sort', 'date')   # 'date' or 'count'
    page      = max(1, int(request.args.get('page', 1)))
    per_page  = 50

    conds, params = [], []
    if domain:
        conds.append('a.domain = %s'); params.append(domain)
    if date_from:
        conds.append('a.access_date >= %s'); params.append(date_from)
    if date_to:
        conds.append('a.access_date <= %s'); params.append(date_to)

    where  = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    order  = 'a.count DESC, a.access_date DESC' if sort == 'count' \
             else 'a.access_date DESC, a.count DESC'

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                f"SELECT a.domain, a.client_ip, a.access_date, a.count, "
                f"COALESCE(l.is_zhejiang, -1) AS is_zhejiang, "
                f"COALESCE(l.region, '') AS region, "
                f"COALESCE(l.country, '') AS country "
                f"FROM access_stats a "
                f"LEFT JOIN ip_locations l ON a.client_ip = l.ip "
                f"{where} "
                f"ORDER BY {order} "
                f"LIMIT %s OFFSET %s",
                params + [per_page, (page - 1) * per_page]
            )
            rows = [serialize_row(r) for r in cur.fetchall()]

            # count without ip_locations join for performance
            count_where = where.replace('a.domain', 'domain') \
                               .replace('a.access_date', 'access_date')
            count_conds, count_params = [], []
            if domain:
                count_conds.append('domain = %s'); count_params.append(domain)
            if date_from:
                count_conds.append('access_date >= %s'); count_params.append(date_from)
            if date_to:
                count_conds.append('access_date <= %s'); count_params.append(date_to)
            count_where2 = ('WHERE ' + ' AND '.join(count_conds)) if count_conds else ''

            cur.execute(f"SELECT COUNT(*) AS total FROM access_stats {count_where2}", count_params)
            total = int(cur.fetchone()['total'])

        return jsonify({'data': rows, 'total': total, 'page': page, 'per_page': per_page})
    finally:
        db.close()


@app.route('/api/trend')
@login_required
def api_trend():
    domain = request.args.get('domain', '')
    days   = int(request.args.get('days', 30))

    db = get_db()
    try:
        with db.cursor() as cur:
            if domain:
                cur.execute("""
                    SELECT access_date, SUM(count) AS total
                    FROM access_stats
                    WHERE domain = %s
                      AND access_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                    GROUP BY access_date ORDER BY access_date
                """, [domain, days])
            else:
                cur.execute("""
                    SELECT access_date, SUM(count) AS total
                    FROM access_stats
                    WHERE access_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                    GROUP BY access_date ORDER BY access_date
                """, [days])
            return jsonify([serialize_row(r) for r in cur.fetchall()])
    finally:
        db.close()


@app.route('/api/overview')
@login_required
def api_overview():
    db = get_db()
    try:
        with db.cursor() as cur:
            # 今日 / 昨日 访问量
            cur.execute("""
                SELECT
                    SUM(CASE WHEN access_date = CURDATE()                        THEN `count` ELSE 0 END) AS today,
                    SUM(CASE WHEN access_date = DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN `count` ELSE 0 END) AS yesterday
                FROM access_stats
                WHERE access_date >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)
            """)
            day_cmp = cur.fetchone()

            # 7 天每日访问量（折线）
            cur.execute("""
                SELECT access_date, SUM(`count`) AS total
                FROM access_stats
                WHERE access_date >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
                GROUP BY access_date ORDER BY access_date
            """)
            trend7 = [serialize_row(r) for r in cur.fetchall()]

            # 今日 Top 10 域名（饼图）
            cur.execute("""
                SELECT domain, SUM(`count`) AS total
                FROM access_stats
                WHERE access_date = CURDATE()
                GROUP BY domain ORDER BY total DESC LIMIT 10
            """)
            today_domains = [serialize_row(r) for r in cur.fetchall()]

            # 7 天 IP 归属分布
            cur.execute("""
                SELECT
                    SUM(CASE WHEN l.is_zhejiang = 1  THEN a.`count` ELSE 0 END) AS zhejiang,
                    SUM(CASE WHEN l.is_zhejiang = 0  THEN a.`count` ELSE 0 END) AS outside,
                    SUM(CASE WHEN l.is_zhejiang IS NULL THEN a.`count` ELSE 0 END) AS unknown
                FROM access_stats a
                LEFT JOIN ip_locations l ON a.client_ip = l.ip
                WHERE a.access_date >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
            """)
            ip_dist = cur.fetchone()

        return jsonify({
            'today':        int(day_cmp['today']     or 0),
            'yesterday':    int(day_cmp['yesterday'] or 0),
            'trend7':       trend7,
            'today_domains': today_domains,
            'ip_dist': {
                'zhejiang': int(ip_dist['zhejiang'] or 0),
                'outside':  int(ip_dist['outside']  or 0),
                'unknown':  int(ip_dist['unknown']  or 0),
            }
        })
    finally:
        db.close()


@app.route('/api/domains')
@login_required
def api_domains():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT DISTINCT domain FROM access_stats ORDER BY domain")
            return jsonify([r['domain'] for r in cur.fetchall()])
    finally:
        db.close()


@app.route('/api/sync/status')
@login_required
def api_sync_status():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM sync_log ORDER BY synced_at DESC LIMIT 10")
            rows = cur.fetchall()
            for r in rows:
                if r.get('synced_at'):
                    r['synced_at'] = r['synced_at'].strftime('%Y-%m-%d %H:%M:%S')
            return jsonify(rows)
    finally:
        db.close()


@app.route('/api/sync/trigger', methods=['POST'])
@login_required
def api_sync_trigger():
    from sync import run_sync
    t = threading.Thread(target=run_sync, daemon=True)
    t.start()
    return jsonify({'status': 'started'})


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    from sync import run_sync
    scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
    scheduler.add_job(run_sync, 'interval', hours=1, id='hourly_sync')
    scheduler.start()
    t = threading.Thread(target=run_sync, daemon=True)
    t.start()


if __name__ == '__main__':
    ensure_tables()
    start_scheduler()
    app.run(host='0.0.0.0', port=5000, debug=False)
