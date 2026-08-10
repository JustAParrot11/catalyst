#!/usr/bin/env bash
#
# Catalyst - one-command install for a fresh Ubuntu server.
#
#   sudo bash install/install.sh
#
# It is safe to run this twice, or twenty times. Re-running never
# deletes a database and never overwrites credentials that are already
# saved: every step checks what is already there before it changes
# anything.
#
# Every step checks its own work. If one fails, the installer stops and
# prints what failed, the exact error, and what to do about it. It never
# just says "failed".
#
# Everything below can be pointed somewhere else with an environment
# variable, but nobody needs to: the defaults are the answer for a
# normal VPS.

set -euo pipefail

CATALYST_HOME="${CATALYST_HOME:-/opt/catalyst}"
CATALYST_VENV="${CATALYST_VENV:-${CATALYST_HOME}/venv}"
CATALYST_STATE_DIR="${CATALYST_STATE_DIR:-/var/lib/catalyst}"
CATALYST_DB="${CATALYST_DB:-${CATALYST_STATE_DIR}/catalyst.db}"
CATALYST_ETC="${CATALYST_ETC:-/etc/catalyst}"
CATALYST_CREDENTIALS="${CATALYST_CREDENTIALS:-${CATALYST_ETC}/credentials.json}"
CATALYST_SERVICE_USER="${CATALYST_SERVICE_USER:-catalyst}"
CATALYST_PORT="${CATALYST_PORT:-8000}"
CATALYST_UNIT_DIR="${CATALYST_UNIT_DIR:-/etc/systemd/system}"
CATALYST_SERVICE_NAME="${CATALYST_SERVICE_NAME:-catalyst}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_TEMPLATE="${SCRIPT_DIR}/catalyst.service"
UNIT_TARGET="${CATALYST_UNIT_DIR}/${CATALYST_SERVICE_NAME}.service"
VENV_PY="${CATALYST_VENV}/bin/python"

LOG_FILE="${CATALYST_INSTALL_LOG:-$(mktemp -t catalyst-install-XXXXXX.log)}"
TOTAL_STEPS=12
STEP_NUM=0
STEP_NAME="starting up"
MANAGE_SERVICE=1

for arg in "$@"; do
  case "$arg" in
    --no-service)
      MANAGE_SERVICE=0
      ;;
    -h|--help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "This installer does not understand the option '${arg}'."
      echo "Run it with no options at all:   sudo bash install/install.sh"
      exit 2
      ;;
  esac
done

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

indent() { sed 's/^/     /'; }

step() {
  STEP_NUM=$((STEP_NUM + 1))
  STEP_NAME="$1"
  printf '\n[%d/%d] %s\n' "${STEP_NUM}" "${TOTAL_STEPS}" "${STEP_NAME}"
}

ok()   { printf '      ok   %s\n' "$1"; }
note() { printf '      ..   %s\n' "$1"; }

# fail <what went wrong> <the exact error> <what to do> [<and then this> ...]
#
# Three arguments minimum, always. "Setup failed" on its own is not an
# acceptable thing to print at somebody who has never used a terminal.
fail() {
  local what="$1"
  local details="$2"
  shift 2
  printf '\n'
  printf '================================================================\n'
  printf ' INSTALL STOPPED - step %d of %d: %s\n' "${STEP_NUM}" "${TOTAL_STEPS}" "${STEP_NAME}"
  printf '================================================================\n\n'
  printf ' WHAT WENT WRONG\n'
  printf '%s\n' "${what}" | indent
  printf '\n THE EXACT ERROR\n'
  if [ -n "${details}" ]; then
    printf '%s\n' "${details}" | indent
  else
    printf '%s\n' "(the command produced no output at all)" | indent
  fi
  printf '\n WHAT TO DO ABOUT IT\n'
  local n=1
  local todo
  for todo in "$@"; do
    printf '     %d. %s\n' "${n}" "${todo}"
    n=$((n + 1))
  done
  printf '\n'
  printf ' Nothing has been damaged. This installer is safe to run again\n'
  printf ' once the above is dealt with - re-running keeps any database\n'
  printf ' and any saved keys exactly as they are.\n\n'
  printf ' The full log of this run is at: %s\n' "${LOG_FILE}"
  printf '================================================================\n'
  exit 1
}

unexpected() {
  local line="$1"
  printf '\n'
  printf '================================================================\n'
  printf ' INSTALL STOPPED unexpectedly - step %d of %d: %s\n' \
    "${STEP_NUM}" "${TOTAL_STEPS}" "${STEP_NAME}"
  printf '================================================================\n\n'
  printf ' WHAT WENT WRONG\n'
  printf '%s\n' "A command inside the installer failed at line ${line}, and the installer did not have a specific explanation ready for it. This is a bug in the installer itself, not something you did wrong." | indent
  printf '\n THE EXACT ERROR\n'
  tail -n 20 "${LOG_FILE}" 2>/dev/null | indent || printf '     (no log output)\n'
  printf '\n WHAT TO DO ABOUT IT\n'
  printf '     1. Send the whole of this message, and the log file below, to whoever set Catalyst up for you.\n'
  printf '     2. Nothing has been damaged - any database and saved keys are untouched.\n'
  printf '     3. It is safe to run the installer again after the cause is fixed.\n\n'
  printf ' The full log of this run is at: %s\n' "${LOG_FILE}"
  printf '================================================================\n'
  exit 1
}
trap 'unexpected "${LINENO}"' ERR

# run <cmd...>  - runs quietly, everything goes to the log
run() {
  printf -- '--- %s : %s\n' "$(date -Is)" "$*" >>"${LOG_FILE}"
  "$@" >>"${LOG_FILE}" 2>&1
}

log_tail() { tail -n 20 "${LOG_FILE}" 2>/dev/null || true; }

as_service_user() {
  runuser -u "${CATALYST_SERVICE_USER}" -- "$@"
}

printf 'Catalyst installer\n'
printf '==================\n'
printf 'This takes a couple of minutes. You do not need to type anything.\n'
printf 'Progress is printed below; the detailed log is at %s\n' "${LOG_FILE}"

# --------------------------------------------------------------------------
step "Checking this machine can run Catalyst"
# --------------------------------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then
  fail "The installer needs administrator rights to create a service, and it was not run with them." \
       "Running as user '$(id -un)' (user number $(id -u)), not as root." \
       "Run exactly this instead, including the word sudo:   sudo bash ${BASH_SOURCE[0]}" \
       "If the machine says 'sudo: command not found', you are probably already logged in as a different user than intended - log in as root and run:   bash ${BASH_SOURCE[0]}"
fi
ok "running with administrator rights"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  note "this machine is ${PRETTY_NAME:-an unrecognised system}"
  case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) : ;;
    *) note "Catalyst is tested on Ubuntu. Carrying on, but if a step below fails, that is the likely reason." ;;
  esac
fi

if ! command -v runuser >/dev/null 2>&1; then
  fail "A standard system tool called 'runuser' is missing, and the installer needs it to run Catalyst as its own restricted user." \
       "'runuser' was not found anywhere on this machine." \
       "Install it with:   sudo apt-get update && sudo apt-get install -y util-linux" \
       "Then run this installer again."
fi

if [ "${MANAGE_SERVICE}" -eq 1 ] && [ ! -d /run/systemd/system ]; then
  fail "This machine is not running systemd, which is what keeps Catalyst running in the background and restarts it if it stops." \
       "The directory /run/systemd/system does not exist, which is how systemd announces itself." \
       "If this is a normal Ubuntu server, reboot it and run the installer again." \
       "If this is a container (Docker, LXC without systemd), install Catalyst on a proper virtual machine instead - an unattended trading bot needs something that will restart it." \
       "To install everything except the background service (for testing only), run:   bash ${BASH_SOURCE[0]} --no-service"
fi
if [ "${MANAGE_SERVICE}" -eq 1 ]; then
  ok "systemd is running and can look after Catalyst"
fi

if [ ! -f "${REPO_DIR}/pyproject.toml" ]; then
  fail "The installer cannot find the Catalyst program files next to itself." \
       "Expected to find ${REPO_DIR}/pyproject.toml, and there is nothing there." \
       "Run the installer from inside the folder you downloaded, like this:   cd catalyst && sudo bash install/install.sh" \
       "If you moved install.sh somewhere on its own, put it back beside the rest of the files."
fi
ok "found the Catalyst program files at ${REPO_DIR}"

# --------------------------------------------------------------------------
step "Finding a suitable Python"
# --------------------------------------------------------------------------

PYTHON=""
find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "${candidate}" >/dev/null 2>&1 &&
       "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PYTHON="$(command -v "${candidate}")"
      return 0
    fi
  done
  return 1
}

if ! find_python; then
  note "no Python 3.11 or newer found - installing it now (this needs internet)"
  if ! run env DEBIAN_FRONTEND=noninteractive apt-get update; then
    fail "Catalyst needs Python 3.11 or newer, and the attempt to fetch it could not reach Ubuntu's software servers." \
         "$(log_tail)" \
         "Check the server has a working internet connection:   ping -c2 archive.ubuntu.com" \
         "If your provider uses a proxy or a firewall, that is the most likely cause - ask them to allow outbound connections on ports 80 and 443." \
         "Then run this installer again."
  fi
  if ! run env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip; then
    fail "Ubuntu refused to install Python for us." \
         "$(log_tail)" \
         "Run this by hand to see the full message:   sudo apt-get install -y python3 python3-venv python3-pip" \
         "If it complains about locked files, another update is already running - wait a minute and try again." \
         "Then run this installer again."
  fi
  if ! find_python; then
    fail "Python was installed, but the version that arrived is older than 3.11, which Catalyst needs." \
         "Installed version: $(python3 --version 2>&1 || echo 'python3 not found at all')" \
         "This happens on older Ubuntu releases. Upgrade the server to Ubuntu 22.04 or newer, or ask your provider for an image with Python 3.11+." \
         "Then run this installer again."
  fi
fi
ok "using $("${PYTHON}" --version 2>&1) at ${PYTHON}"

# --------------------------------------------------------------------------
step "Creating the account Catalyst runs as"
# --------------------------------------------------------------------------

if getent group "${CATALYST_SERVICE_USER}" >/dev/null 2>&1; then
  ok "group '${CATALYST_SERVICE_USER}' already exists - leaving it alone"
else
  if ! run groupadd --system "${CATALYST_SERVICE_USER}"; then
    fail "Could not create the group that Catalyst's files belong to." \
         "$(log_tail)" \
         "Run by hand to see why:   sudo groupadd --system ${CATALYST_SERVICE_USER}" \
         "Then run this installer again."
  fi
  ok "created group '${CATALYST_SERVICE_USER}'"
fi

if id -u "${CATALYST_SERVICE_USER}" >/dev/null 2>&1; then
  ok "account '${CATALYST_SERVICE_USER}' already exists - leaving it alone"
else
  if ! run useradd --system --gid "${CATALYST_SERVICE_USER}" \
        --home-dir "${CATALYST_STATE_DIR}" --no-create-home \
        --shell /usr/sbin/nologin \
        --comment "Catalyst trading bot" "${CATALYST_SERVICE_USER}"; then
    fail "Could not create the restricted account that Catalyst runs as." \
         "$(log_tail)" \
         "Run by hand to see why:   sudo useradd --system --gid ${CATALYST_SERVICE_USER} --home-dir ${CATALYST_STATE_DIR} --no-create-home --shell /usr/sbin/nologin ${CATALYST_SERVICE_USER}" \
         "Then run this installer again."
  fi
  ok "created account '${CATALYST_SERVICE_USER}' (it cannot log in - that is deliberate)"
fi

# --------------------------------------------------------------------------
step "Creating Catalyst's folders"
# --------------------------------------------------------------------------

if ! run install -d -m 0755 -o root -g root "${CATALYST_HOME}"; then
  fail "Could not create the folder Catalyst's program lives in." \
       "$(log_tail)" \
       "Check there is free space on the disk:   df -h ${CATALYST_HOME}" \
       "If the disk is full, free some space and run the installer again."
fi
ok "program folder ready"

# 0700, owned by the service user: the trading record and every decision
# behind it live here, and nothing else on the machine needs to read them.
if ! run install -d -m 0700 -o "${CATALYST_SERVICE_USER}" -g "${CATALYST_SERVICE_USER}" "${CATALYST_STATE_DIR}"; then
  fail "Could not create the folder Catalyst keeps its trading records in." \
       "$(log_tail)" \
       "Check there is free space on the disk:   df -h ${CATALYST_STATE_DIR}" \
       "Then run this installer again."
fi
run chmod 0700 "${CATALYST_STATE_DIR}"
run chown "${CATALYST_SERVICE_USER}:${CATALYST_SERVICE_USER}" "${CATALYST_STATE_DIR}"
ok "records folder ready, readable only by Catalyst itself"

if ! run install -d -m 0750 -o "${CATALYST_SERVICE_USER}" -g "${CATALYST_SERVICE_USER}" "${CATALYST_ETC}"; then
  fail "Could not create the folder Catalyst keeps your saved keys in." \
       "$(log_tail)" \
       "Check there is free space on the disk:   df -h ${CATALYST_ETC}" \
       "Then run this installer again."
fi
ok "settings folder ready, readable only by Catalyst itself"

# --------------------------------------------------------------------------
step "Building Catalyst's private Python environment"
# --------------------------------------------------------------------------

venv_is_good() {
  [ -x "${VENV_PY}" ] &&
    "${VENV_PY}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

if venv_is_good; then
  ok "an existing environment is already there and healthy - reusing it"
else
  if [ -e "${CATALYST_VENV}" ]; then
    note "the existing environment is broken or too old - rebuilding it"
    run rm -rf "${CATALYST_VENV}"
  fi
  if ! run "${PYTHON}" -m venv "${CATALYST_VENV}"; then
    note "that failed - trying to install the missing Python add-on first"
    if run env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv &&
       run "${PYTHON}" -m venv "${CATALYST_VENV}"; then
      :
    else
      fail "Could not build the private Python environment Catalyst runs inside." \
           "$(log_tail)" \
           "Install the missing piece by hand:   sudo apt-get install -y python3-venv python3-pip" \
           "Then run this installer again." \
           "If it mentions 'No space left on device', free some disk space first:   df -h"
    fi
  fi
  if ! venv_is_good; then
    fail "The private Python environment was created but does not work." \
         "$(log_tail)" \
         "Delete it and let the installer rebuild it:   sudo rm -rf ${CATALYST_VENV}" \
         "Then run this installer again."
  fi
  ok "environment built at ${CATALYST_VENV}"
fi

if ! run "${VENV_PY}" -m pip install --quiet --upgrade pip setuptools wheel; then
  fail "Could not update the tools Catalyst uses to install itself." \
       "$(log_tail)" \
       "This is almost always a lost internet connection. Check with:   ping -c2 pypi.org" \
       "If your provider uses a proxy, it must allow outbound connections to pypi.org on port 443." \
       "Then run this installer again."
fi
ok "install tools up to date"

# --------------------------------------------------------------------------
step "Installing Catalyst"
# --------------------------------------------------------------------------

if ! run "${VENV_PY}" -m pip install --quiet "${REPO_DIR}"; then
  fail "Catalyst itself could not be installed." \
       "$(log_tail)" \
       "The usual cause is no internet connection on the server. Check with:   ping -c2 pypi.org" \
       "If that works, run the install by hand to see the full message:   ${VENV_PY} -m pip install ${REPO_DIR}" \
       "Then run this installer again."
fi
ok "Catalyst installed into its own environment"

# --------------------------------------------------------------------------
step "Checking the installation actually works"
# --------------------------------------------------------------------------

INSTALLED_VERSION="$("${VENV_PY}" -c 'import catalyst; print(catalyst.__version__)' 2>>"${LOG_FILE}" || true)"
if [ -z "${INSTALLED_VERSION}" ]; then
  fail "Catalyst was installed, but it will not start - importing it fails." \
       "$(log_tail)" \
       "Delete the environment so it is rebuilt from scratch:   sudo rm -rf ${CATALYST_VENV}" \
       "Then run this installer again." \
       "If it fails the same way twice, send the log file named at the bottom of this message to whoever set Catalyst up."
fi
ok "Catalyst version ${INSTALLED_VERSION} imports cleanly"

if ! run "${VENV_PY}" -c 'from catalyst.setup.first_run import SetupApp; SetupApp().handle("GET", "/health")'; then
  fail "The setup page - the browser form where you type your keys - does not work in this installation." \
       "$(log_tail)" \
       "Delete the environment so it is rebuilt from scratch:   sudo rm -rf ${CATALYST_VENV}" \
       "Then run this installer again." \
       "If it fails the same way twice, send the log file named at the bottom of this message to whoever set Catalyst up."
fi
ok "the browser setup page responds"

# --------------------------------------------------------------------------
step "Preparing Catalyst's database"
# --------------------------------------------------------------------------

DB_EXISTED=0
# -s not -f: an empty file left behind by an interrupted first run is not
# an existing database, and reporting it as one is misleading.
if [ -s "${CATALYST_DB}" ]; then DB_EXISTED=1; fi

# init_db only ever creates tables that are missing, so this is safe on
# an existing database - it adds anything new and touches nothing else.
if ! run as_service_user "${VENV_PY}" -c \
      "from catalyst.storage import init_db; init_db('${CATALYST_DB}').close()"; then
  fail "Could not prepare the database Catalyst records every trade and decision in." \
       "$(log_tail)" \
       "If the error above mentions a file missing from inside ${CATALYST_HOME}, the installation is incomplete - delete it and let the installer rebuild it:   sudo rm -rf ${CATALYST_VENV}" \
       "If it mentions permission denied, give Catalyst back its own folder:   sudo chown -R ${CATALYST_SERVICE_USER} ${CATALYST_STATE_DIR}" \
       "Check there is free disk space:   df -h ${CATALYST_STATE_DIR}" \
       "Then run this installer again."
fi
run chmod 0600 "${CATALYST_DB}"
run chown "${CATALYST_SERVICE_USER}:${CATALYST_SERVICE_USER}" "${CATALYST_DB}"

TABLE_COUNT="$(as_service_user "${VENV_PY}" -c \
  "import sqlite3;print(sqlite3.connect('${CATALYST_DB}').execute(\"select count(*) from sqlite_master where type='table'\").fetchone()[0])" \
  2>>"${LOG_FILE}" || echo 0)"
if [ "${DB_EXISTED}" -eq 1 ]; then
  ok "kept the existing database (${TABLE_COUNT} tables) - nothing was erased"
else
  ok "created a new empty database (${TABLE_COUNT} tables)"
fi

# --------------------------------------------------------------------------
step "Creating your private access code"
# --------------------------------------------------------------------------

# Generated, never asked for: the owner should not have to invent a
# password, and the setup page must be protected from its very first
# request. Re-running the installer keeps the code that already exists.
ACCESS_CODE="$(as_service_user env "CATALYST_CREDENTIALS=${CATALYST_CREDENTIALS}" \
  "${VENV_PY}" -m catalyst.setup.credentials --ensure-dashboard-token \
  2>>"${LOG_FILE}" || true)"
if [ -z "${ACCESS_CODE}" ]; then
  fail "Could not create the private access code that protects Catalyst's web page." \
       "$(log_tail)" \
       "Check Catalyst owns its settings folder:   sudo chown -R ${CATALYST_SERVICE_USER} ${CATALYST_ETC}" \
       "Then run this installer again."
fi
if [ ! -f "${CATALYST_CREDENTIALS}" ]; then
  fail "The file that holds your saved keys was not created." \
       "Expected a file to appear at ${CATALYST_CREDENTIALS} and it is not there." \
       "Check Catalyst owns its settings folder:   sudo chown -R ${CATALYST_SERVICE_USER} ${CATALYST_ETC}" \
       "Then run this installer again."
fi
CRED_MODE="$(stat -c '%a' "${CATALYST_CREDENTIALS}")"
if [ "${CRED_MODE}" != "600" ]; then
  run chmod 0600 "${CATALYST_CREDENTIALS}"
  CRED_MODE="$(stat -c '%a' "${CATALYST_CREDENTIALS}")"
fi
if [ "${CRED_MODE}" != "600" ]; then
  fail "The file holding your keys is readable by other people on this machine, and the installer could not lock it down." \
       "Its permissions are ${CRED_MODE}, and they should be 600 (readable only by Catalyst)." \
       "Lock it by hand:   sudo chmod 600 ${CATALYST_CREDENTIALS}" \
       "Then run this installer again."
fi
ok "access code ready, and your keys file is locked to Catalyst only"

if [ "${MANAGE_SERVICE}" -eq 0 ]; then
  printf '\n'
  printf 'Stopped before the background service, because --no-service was given.\n'
  printf 'Everything except starting the bot has been done.\n'
  exit 0
fi

# --------------------------------------------------------------------------
step "Installing the background service"
# --------------------------------------------------------------------------

if [ ! -f "${UNIT_TEMPLATE}" ]; then
  fail "The installer cannot find its own service description file." \
       "Expected it at ${UNIT_TEMPLATE}, and there is nothing there." \
       "Download or copy the Catalyst files again, keeping the install folder intact." \
       "Then run this installer again."
fi

RENDERED="$(mktemp)"
sed -e "s|__USER__|${CATALYST_SERVICE_USER}|g" \
    -e "s|__GROUP__|${CATALYST_SERVICE_USER}|g" \
    -e "s|__STATE_DIR__|${CATALYST_STATE_DIR}|g" \
    -e "s|__ETC_DIR__|${CATALYST_ETC}|g" \
    -e "s|__DB__|${CATALYST_DB}|g" \
    -e "s|__CREDENTIALS__|${CATALYST_CREDENTIALS}|g" \
    -e "s|__PORT__|${CATALYST_PORT}|g" \
    -e "s|__VENV_PYTHON__|${VENV_PY}|g" \
    "${UNIT_TEMPLATE}" >"${RENDERED}"

if grep -q '__[A-Z_]*__' "${RENDERED}"; then
  fail "The service description still has unfilled blanks in it, so the bot would not start correctly." \
       "$(grep -n '__[A-Z_]*__' "${RENDERED}" | head -n 5)" \
       "This is a bug in the installer. Send this message to whoever set Catalyst up for you." \
       "Nothing has been changed on the machine yet."
fi

if [ -f "${UNIT_TARGET}" ] && cmp -s "${RENDERED}" "${UNIT_TARGET}"; then
  ok "the service description is already correct - left as it is"
  rm -f "${RENDERED}"
else
  if ! run install -m 0644 -o root -g root "${RENDERED}" "${UNIT_TARGET}"; then
    rm -f "${RENDERED}"
    fail "Could not install the service description that tells the machine how to run Catalyst." \
         "$(log_tail)" \
         "Check the folder exists and is writable:   ls -ld ${CATALYST_UNIT_DIR}" \
         "Then run this installer again."
  fi
  rm -f "${RENDERED}"
  ok "service description written to ${UNIT_TARGET}"
fi

if ! run systemctl daemon-reload; then
  fail "The machine would not reload its list of background services." \
       "$(log_tail)" \
       "Run by hand to see the message:   sudo systemctl daemon-reload" \
       "Then run this installer again."
fi
ok "the machine has picked up the new service"

# --------------------------------------------------------------------------
step "Starting Catalyst"
# --------------------------------------------------------------------------

if ! systemctl is-enabled --quiet "${CATALYST_SERVICE_NAME}" 2>/dev/null; then
  if ! run systemctl enable "${CATALYST_SERVICE_NAME}"; then
    fail "Catalyst could not be set to start automatically when the machine reboots." \
         "$(log_tail)" \
         "Run by hand to see the message:   sudo systemctl enable ${CATALYST_SERVICE_NAME}" \
         "Then run this installer again."
  fi
fi
ok "Catalyst will start again by itself if the machine reboots"

if ! run systemctl restart "${CATALYST_SERVICE_NAME}"; then
  fail "Catalyst would not start." \
       "$(systemctl status "${CATALYST_SERVICE_NAME}" --no-pager --lines=20 2>&1 || true)" \
       "See the bot's own account of what happened:   sudo journalctl -u ${CATALYST_SERVICE_NAME} -n 50 --no-pager" \
       "The most common causes are no internet on the server, or a disk that is full (check with:   df -h)." \
       "Fix what that reports and run this installer again."
fi

sleep 2
if ! systemctl is-active --quiet "${CATALYST_SERVICE_NAME}"; then
  fail "Catalyst started and then stopped again straight away." \
       "$(journalctl -u "${CATALYST_SERVICE_NAME}" -n 30 --no-pager 2>&1 || true)" \
       "The lines above are the bot's own account of what went wrong - the last few are the important ones." \
       "If they mention permissions, run:   sudo chown -R ${CATALYST_SERVICE_USER} ${CATALYST_STATE_DIR} ${CATALYST_ETC}" \
       "Then run this installer again."
fi
ok "Catalyst is running"

# --------------------------------------------------------------------------
step "Checking Catalyst answers in a browser"
# --------------------------------------------------------------------------

HEALTH=""
for _attempt in {1..20}; do
  HEALTH="$("${VENV_PY}" -c "
import urllib.request
try:
    print(urllib.request.urlopen('http://127.0.0.1:${CATALYST_PORT}/health', timeout=3).read().decode())
except Exception:
    print('')
" 2>>"${LOG_FILE}" || true)"
  if [ -n "${HEALTH}" ]; then break; fi
  sleep 1
done

if [ -z "${HEALTH}" ]; then
  fail "Catalyst is running, but its web page is not answering, so you would not be able to type your keys in." \
       "$(journalctl -u "${CATALYST_SERVICE_NAME}" -n 30 --no-pager 2>&1 || true)" \
       "Check nothing else is already using port ${CATALYST_PORT}:   sudo ss -lntp | grep ${CATALYST_PORT}" \
       "If something is, stop it, or run the installer again with a different port:   sudo CATALYST_PORT=8001 bash ${BASH_SOURCE[0]}" \
       "Then run this installer again."
fi
ok "the web page answered: ${HEALTH}"

# --------------------------------------------------------------------------
# What to do next
# --------------------------------------------------------------------------

SERVER_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)"
if [ -z "${SERVER_IP}" ]; then
  SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
if [ -z "${SERVER_IP}" ]; then SERVER_IP="YOUR-SERVER-ADDRESS"; fi

SETUP_STATE="$(printf '%s' "${HEALTH}" | grep -o 'awaiting_setup' || true)"

printf '\n'
printf '================================================================\n'
printf ' Catalyst is installed and running.\n'
printf '================================================================\n\n'
if [ -n "${SETUP_STATE}" ]; then
  printf ' NEXT STEP - open this link in your web browser:\n\n'
  printf '     http://%s:%s/?code=%s\n\n' "${SERVER_IP}" "${CATALYST_PORT}" "${ACCESS_CODE}"
  printf ' That page asks for three things - your Alpaca practice-account\n'
  printf ' key and secret, and your Anthropic key - and explains where to\n'
  printf ' find each one. There is a Test button beside each, so you can\n'
  printf ' check them before saving. It takes about five minutes and you\n'
  printf ' will not have to do it again.\n\n'
  printf ' Until that is done, Catalyst will not trade. It is simply\n'
  printf ' waiting for you.\n\n'
else
  printf ' Your keys were already saved, so there is nothing more to do.\n'
  printf ' The bot is trading (practice money) and its page is here:\n\n'
  printf '     http://%s:%s/?code=%s\n\n' "${SERVER_IP}" "${CATALYST_PORT}" "${ACCESS_CODE}"
fi
printf ' Keep that link private. The code at the end of it is the only\n'
printf ' thing keeping other people out of your bot.\n\n'
printf ' If the link does not open, the usual cause is your server\n'
printf ' provider blocking port %s - their control panel will have a\n' "${CATALYST_PORT}"
printf ' firewall page where you can allow it for your own address.\n\n'
printf ' Useful later, if you ever need it:\n'
printf '   is it running?   sudo systemctl status %s\n' "${CATALYST_SERVICE_NAME}"
printf '   what has it done? sudo journalctl -u %s -f\n' "${CATALYST_SERVICE_NAME}"
printf '   update it:        sudo bash %s/install/upgrade.sh\n' "${REPO_DIR}"
printf '================================================================\n'
