#!/usr/bin/env bash
# =============================================================================
# setup_postgres_fish.sh
# =============================================================================
# One-shot installer for FISH's PostgreSQL + TimescaleDB backend.
#
# Sets up:
#   - PostgreSQL official apt repo
#   - TimescaleDB packagecloud apt repo
#   - postgresql-${PG_VERSION} + matching timescaledb-2 extension
#   - shared_preload_libraries = 'timescaledb' in postgresql.conf
#   - `fish` database owned by `fish` user (default password: fish)
#   - timescaledb extension activated inside the fish db
#
# Idempotent — safe to re-run. Skips work already done.
#
# Usage:
#   sudo ./setup_postgres_fish.sh                  # default: PG 16, password 'fish'
#   PG_VERSION=18 sudo -E ./setup_postgres_fish.sh # use PG 18
#   PG_PASSWORD=mypass sudo -E ./setup_postgres_fish.sh
#
# After this finishes, connect via:
#   PGPASSWORD=fish psql -h localhost -U fish -d fish
# =============================================================================

set -euo pipefail

# Auto-detect an already-running PostgreSQL cluster's version (highest one)
# so we don't accidentally install a second PG version side-by-side.
auto_detect_pg() {
    if command -v pg_lsclusters &>/dev/null; then
        pg_lsclusters --no-header 2>/dev/null \
            | awk '$NF=="online" || $4=="online" {print $1}' \
            | sort -rn | head -1
    fi
}
DETECTED_PG=$(auto_detect_pg || true)
PG_VERSION=${PG_VERSION:-${DETECTED_PG:-16}}
PG_USER=${PG_USER:-fish}
PG_PASSWORD=${PG_PASSWORD:-fish}
PG_DATABASE=${PG_DATABASE:-fish}

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
blue()   { printf '\033[34m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
info()   { printf '  %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    red "ERROR: this script must be run as root. Try: sudo $0"
    exit 1
fi

if ! command -v lsb_release &>/dev/null; then
    apt-get update -qq
    apt-get install -y lsb-release ca-certificates curl gnupg
fi
UBUNTU_CODENAME=$(lsb_release -cs)
info "Ubuntu codename: ${UBUNTU_CODENAME}"
info "Target PostgreSQL version: ${PG_VERSION}"

# ─── 1. PostgreSQL apt repo ─────────────────────────────────────────────────
step "1/7 PostgreSQL apt repo"
if [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
    install -d /usr/share/postgresql-common/pgdg
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt ${UBUNTU_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
    info "added pgdg.list"
else
    info "already configured"
fi

# ─── 2. TimescaleDB apt repo ────────────────────────────────────────────────
step "2/7 TimescaleDB apt repo"
if [[ ! -f /etc/apt/sources.list.d/timescaledb.list ]]; then
    install -d /usr/share/keyrings
    curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
        | gpg --dearmor -o /usr/share/keyrings/timescale.gpg
    echo "deb [signed-by=/usr/share/keyrings/timescale.gpg] \
https://packagecloud.io/timescale/timescaledb/ubuntu/ ${UBUNTU_CODENAME} main" \
        > /etc/apt/sources.list.d/timescaledb.list
    info "added timescaledb.list"
else
    info "already configured"
fi

# ─── 3. Install packages ────────────────────────────────────────────────────
step "3/7 apt update + install"
apt-get update -qq
PACKAGES=(
    "postgresql-${PG_VERSION}"
    "postgresql-client-${PG_VERSION}"
)
# Two upstream sources ship the TimescaleDB extension; they ship the same .so
# files and can't be co-installed. Prefer whichever is already there.
TS_PGDG="postgresql-${PG_VERSION}-timescaledb"
TS_PACKAGECLOUD="timescaledb-2-postgresql-${PG_VERSION}"
if dpkg -s "$TS_PGDG" &>/dev/null; then
    info "TimescaleDB already installed via pgdg ($TS_PGDG) — using that"
elif dpkg -s "$TS_PACKAGECLOUD" &>/dev/null; then
    info "TimescaleDB already installed via packagecloud ($TS_PACKAGECLOUD) — using that"
else
    # Prefer packagecloud (latest) if available, else pgdg
    if apt-cache show "$TS_PACKAGECLOUD" &>/dev/null; then
        PACKAGES+=("$TS_PACKAGECLOUD")
    elif apt-cache show "$TS_PGDG" &>/dev/null; then
        PACKAGES+=("$TS_PGDG")
    else
        red "ERROR: no TimescaleDB package available for PG ${PG_VERSION} in any configured repo"
        exit 4
    fi
fi
if apt-cache show timescaledb-tools &>/dev/null && ! dpkg -s timescaledb-tools &>/dev/null; then
    PACKAGES+=("timescaledb-tools")
fi
apt-get install -y "${PACKAGES[@]}"
info "installed: ${PACKAGES[*]}"

# ─── 4. shared_preload_libraries = 'timescaledb' ────────────────────────────
step "4/7 configure shared_preload_libraries"
PG_CONF="/etc/postgresql/${PG_VERSION}/main/postgresql.conf"
CURRENT_SPL=$(sudo -u postgres psql -tA -c "SHOW shared_preload_libraries;" 2>/dev/null || echo '')
if [[ "$CURRENT_SPL" == *timescaledb* ]]; then
    info "already set: $CURRENT_SPL"
else
    if [[ ! -f $PG_CONF ]]; then
        red "ERROR: postgresql.conf not found at $PG_CONF — initdb may have failed."
        red "  If you have another running PG cluster on this host, set PG_VERSION to match it:"
        red "    pg_lsclusters"
        red "    sudo PG_VERSION=<N> $0"
        exit 2
    fi
    cp "$PG_CONF" "${PG_CONF}.bak.$(date +%s)"
    if grep -qE "^\s*shared_preload_libraries\s*=" "$PG_CONF"; then
        sed -i -E "s|^(\s*shared_preload_libraries\s*=\s*')([^']*)'|\1\2,timescaledb'|; \
                   s|,timescaledb,timescaledb|,timescaledb|; \
                   s|^(\s*shared_preload_libraries\s*=\s*'),timescaledb|\1timescaledb|" "$PG_CONF"
    else
        sed -i "s|^#\s*shared_preload_libraries\s*=.*$|shared_preload_libraries = 'timescaledb'|" "$PG_CONF"
    fi
    info "patched $PG_CONF (backup left as .bak.<ts>)"
fi

# ─── 5. timescaledb-tune (optional) ─────────────────────────────────────────
step "5/7 timescaledb-tune (optional)"
if command -v timescaledb-tune &>/dev/null; then
    timescaledb-tune --quiet --yes --conf-path "$PG_CONF" || \
        yellow "  timescaledb-tune ran with warnings — proceeding"
else
    info "skipped (timescaledb-tune not available — performance defaults will apply)"
fi

# ─── 6. Restart PG ──────────────────────────────────────────────────────────
step "6/7 restart PostgreSQL"
systemctl restart postgresql
sleep 2
NEW_SPL=$(sudo -u postgres psql -tA -c "SHOW shared_preload_libraries;")
if [[ "$NEW_SPL" != *timescaledb* ]]; then
    red "ERROR: shared_preload_libraries not loaded after restart: '$NEW_SPL'"
    journalctl -u postgresql --no-pager -n 30
    exit 3
fi
info "shared_preload_libraries = $NEW_SPL"

# ─── 7. Create fish role + database + extension ─────────────────────────────
step "7/7 create role + database + extension"
# Idempotent role + db creation
sudo -u postgres psql -v ON_ERROR_STOP=1 <<EOF
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${PG_USER}') THEN
        CREATE USER ${PG_USER} WITH PASSWORD '${PG_PASSWORD}';
    ELSE
        ALTER USER ${PG_USER} WITH PASSWORD '${PG_PASSWORD}';
    END IF;
END
\$\$;
SELECT 'create database if missing' AS step;
EOF

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${PG_DATABASE}'" | grep -q 1; then
    sudo -u postgres createdb -O "${PG_USER}" "${PG_DATABASE}"
    info "created database ${PG_DATABASE}"
else
    info "database ${PG_DATABASE} already exists"
fi

sudo -u postgres psql -d "${PG_DATABASE}" -v ON_ERROR_STOP=1 <<EOF
CREATE EXTENSION IF NOT EXISTS timescaledb;
GRANT ALL ON SCHEMA public TO ${PG_USER};
EOF

# ─── Verify ────────────────────────────────────────────────────────────────
step "verify"
PGPASSWORD="${PG_PASSWORD}" psql -h localhost -U "${PG_USER}" -d "${PG_DATABASE}" -c \
    "SELECT extname, extversion FROM pg_extension ORDER BY extname;"

green ""
green "✓ FISH PostgreSQL backend ready."
green ""
green "  Connection: postgresql://${PG_USER}:${PG_PASSWORD}@localhost:5432/${PG_DATABASE}"
green "  psql:       PGPASSWORD=${PG_PASSWORD} psql -h localhost -U ${PG_USER} -d ${PG_DATABASE}"
green "  Grafana datasource:"
green "    Type:     PostgreSQL"
green "    Host:     localhost:5432"
green "    Database: ${PG_DATABASE}"
green "    User:     ${PG_USER}"
green "    Password: ${PG_PASSWORD}"
green "    TLS Mode: disable (localhost)"
green "    TimescaleDB enabled: yes"
