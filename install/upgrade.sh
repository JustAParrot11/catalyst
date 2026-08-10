#!/usr/bin/env bash
#
# Catalyst - one-command upgrade.
#
#   sudo bash install/upgrade.sh
#
# What it does, in order:
#
#   1. Checks the machine is in a fit state to be upgraded at all.
#   2. Backs up the database and your saved keys, stamped with today's
#      date and time. Backups are never overwritten.
#   3. Fetches the new version and installs it.
#   4. Runs the entire test suite against the new version. The tests
#      never touch the internet and never touch your real account.
#   5. Restarts the bot and checks it came back.
#
# If step 4 or step 5 fails, it puts everything back the way it was -
# old code, old database, old keys - restarts the bot, and tells you.
# You are never left on a half-upgraded machine.

set -euo pipefail

CATALYST_HOME="${CATALYST_HOME:-/opt/catalyst}"
CATALYST_VENV="${CATALYST_VENV:-${CATALYST_HOME}/venv}"
CATALYST_STATE_DIR="${CATALYST_STATE_DIR:-/var/lib/catalyst}"
CATALYST_DB="${CATALYST_DB:-${CATALYST_STATE_DIR}/catalyst.db}"
CATALYST_ETC="${CATALYST_ETC:-/etc/catalyst}"
CATALYST_CREDENTIALS="${CATALYST_CREDENTIALS:-${CATALYST_ETC}/credentials.json}"
CATALYST_SERVICE_USER="${CATALYST_SERVICE_USER:-catalyst}"
CATALYST_SERVICE_NAME="${CATALYST_SERVICE_NAME:-catalyst}"
CATALYST_BACKUP_DIR="${CATALYST_BACKUP_DIR:-/var/backups/catalyst}"
CATALYST_PORT="${CATALYST_PORT:-8000}"
CATALYST_SKIP_PULL="${CATALYST_SKIP_PULL:-0}"
CATALYST_MANAGE_SERVICE="${CATALYST_MANAGE_SERVICE:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PY="${CATALYST_VENV}/bin/python"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_PATH="${CATALYST_BACKUP_DIR}/${STAMP}"
LOG_FILE="${CATALYST_UPGRADE_LOG:-$(mktemp -t catalyst-upgrade-XXXXXX.log)}"
TEST_LOG="${BACKUP_PATH}/test-output.txt"

TOTAL_PHASES=6
PHASE_NUM=0
PHASE_NAME="starting up"
OLD_COMMIT=""
NEW_COMMIT=""
BACKUP_MADE=0

# --------------------------------------------------------------------------
# Output helpers - same contract as install.sh: never just "it failed"
# --------------------------------------------------------------------------

indent() { sed 's/^/     /'; }

phase() {
  PHASE_NUM=$((PHASE_NUM + 1))
  PHASE_NAME="$1"
  printf '\n----------------------------------------------------------------\n'
  printf ' PHASE %d of %d: %s\n' "${PHASE_NUM}" "${TOTAL_PHASES}" "${PHASE_NAME}"
  printf -- '----------------------------------------------------------------\n'
}

ok()   { printf '      ok   %s\n' "$1"; }
note() { printf '      ..   %s\n' "$1"; }

# fail <what went wrong> <the exact error> <what to do> [<and then this> ...]
fail() {
  local what="$1"
  local details="$2"
  shift 2
  printf '\n'
  printf '================================================================\n'
  printf ' UPGRADE STOPPED - phase %d of %d: %s\n' "${PHASE_NUM}" "${TOTAL_PHASES}" "${PHASE_NAME}"
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
  if [ "${BACKUP_MADE}" -eq 1 ]; then
    printf '\n Your database and keys were backed up before anything changed:\n'
    printf '   %s\n' "${BACKUP_PATH}"
  fi
  printf '\n The full log of this run is at: %s\n' "${LOG_FILE}"
  printf '================================================================\n'
  exit 1
}

unexpected() {
  local line="$1"
  printf '\n'
  printf '================================================================\n'
  printf ' UPGRADE STOPPED unexpectedly - phase %d of %d: %s\n' \
    "${PHASE_NUM}" "${TOTAL_PHASES}" "${PHASE_NAME}"
  printf '================================================================\n\n'
  printf ' WHAT WENT WRONG\n'
  printf '%s\n' "A command inside the upgrade script failed at line ${line} without a specific explanation ready. That is a bug in the script, not something you did." | indent
  printf '\n THE EXACT ERROR\n'
  tail -n 20 "${LOG_FILE}" 2>/dev/null | indent || printf '     (no log output)\n'
  printf '\n WHAT TO DO ABOUT IT\n'
  printf '     1. Check the bot is still running:   sudo systemctl status %s\n' "${CATALYST_SERVICE_NAME}"
  printf '     2. If it is not, put the old version back by hand - the instructions are in the backup folder below.\n'
  printf '     3. Send this whole message to whoever set Catalyst up for you.\n'
  if [ "${BACKUP_MADE}" -eq 1 ]; then
    printf '\n Backup taken before anything changed: %s\n' "${BACKUP_PATH}"
  fi
  printf '\n The full log of this run is at: %s\n' "${LOG_FILE}"
  printf '================================================================\n'
  exit 1
}
trap 'unexpected "${LINENO}"' ERR

run() {
  printf -- '--- %s : %s\n' "$(date -Is)" "$*" >>"${LOG_FILE}"
  "$@" >>"${LOG_FILE}" 2>&1
}

log_tail() { tail -n 25 "${LOG_FILE}" 2>/dev/null || true; }

service_active() {
  if [ "${CATALYST_MANAGE_SERVICE}" -eq 0 ]; then return 0; fi
  systemctl is-active --quiet "${CATALYST_SERVICE_NAME}"
}

service_do() {
  if [ "${CATALYST_MANAGE_SERVICE}" -eq 0 ]; then return 0; fi
  run systemctl "$1" "${CATALYST_SERVICE_NAME}"
}

# --------------------------------------------------------------------------
# Rollback - the whole point of this script
# --------------------------------------------------------------------------

rollback() {
  local reason="$1"
  local details="$2"

  printf '\n'
  printf '================================================================\n'
  printf ' PUTTING THE OLD VERSION BACK\n'
  printf '================================================================\n\n'
  printf ' WHY\n'
  printf '%s\n' "${reason}" | indent
  printf '\n THE EXACT ERROR\n'
  printf '%s\n' "${details}" | indent
  printf '\n WHAT IS HAPPENING NOW\n'
  printf '%s\n' "The new version is being removed and the version you were running before is being put back, along with the database and keys exactly as they were when this upgrade started. Nothing you had is lost." | indent
  printf '\n'

  local rollback_failed=0

  service_do stop || true
  note "bot stopped while the old version goes back"

  if [ -n "${OLD_COMMIT}" ] && [ "${CATALYST_SKIP_PULL}" -eq 0 ]; then
    if run git -C "${REPO_DIR}" reset --hard "${OLD_COMMIT}"; then
      note "program files put back to the previous version"
    else
      rollback_failed=1
      note "COULD NOT put the program files back"
    fi
  fi

  if run "${VENV_PY}" -m pip install --quiet "${REPO_DIR}[dev]"; then
    note "previous version reinstalled"
  else
    rollback_failed=1
    note "COULD NOT reinstall the previous version"
  fi

  if [ -f "${BACKUP_PATH}/catalyst.db" ]; then
    if run cp -p "${BACKUP_PATH}/catalyst.db" "${CATALYST_DB}" &&
       run chown "${CATALYST_SERVICE_USER}:${CATALYST_SERVICE_USER}" "${CATALYST_DB}" &&
       run chmod 0600 "${CATALYST_DB}"; then
      note "database restored from the backup"
    else
      rollback_failed=1
      note "COULD NOT restore the database"
    fi
  fi

  if [ -f "${BACKUP_PATH}/credentials.json" ]; then
    if run cp -p "${BACKUP_PATH}/credentials.json" "${CATALYST_CREDENTIALS}" &&
       run chown "${CATALYST_SERVICE_USER}:${CATALYST_SERVICE_USER}" "${CATALYST_CREDENTIALS}" &&
       run chmod 0600 "${CATALYST_CREDENTIALS}"; then
      note "saved keys restored from the backup"
    else
      rollback_failed=1
      note "COULD NOT restore your saved keys"
    fi
  fi

  if service_do start; then
    sleep 2
    if service_active; then
      note "bot started again on the previous version"
    else
      rollback_failed=1
      note "the bot did NOT come back up"
    fi
  else
    rollback_failed=1
    note "the bot did NOT come back up"
  fi

  printf '\n'
  printf '================================================================\n'
  if [ "${rollback_failed}" -eq 0 ]; then
    printf ' DONE - you are back on the version you were running before.\n'
    printf '================================================================\n\n'
    printf ' The upgrade was refused because the new version failed its own\n'
    printf ' tests, which is exactly what should happen. Your database, your\n'
    printf ' keys and your open positions are untouched.\n\n'
    printf ' WHAT TO DO ABOUT IT\n'
    printf '     1. Send the test output to whoever maintains Catalyst:  %s\n' "${TEST_LOG}"
    printf '     2. Carry on as normal in the meantime - the bot is running.\n'
    printf '     3. Try the upgrade again once a fixed version is published.\n'
  else
    printf ' NEEDS A HUMAN - the old version could not be fully put back.\n'
    printf '================================================================\n\n'
    printf ' WHAT TO DO ABOUT IT\n'
    printf '     1. Send this whole message to whoever maintains Catalyst, now.\n'
    printf '     2. Do not run the upgrade again until they have looked.\n'
    printf '     3. Everything needed to put it back by hand is in the backup below - nothing has been deleted.\n'
  fi
  printf '\n Backup of the database and keys: %s\n' "${BACKUP_PATH}"
  printf ' Test output:                     %s\n' "${TEST_LOG}"
  printf ' Full log of this run:            %s\n' "${LOG_FILE}"
  printf '================================================================\n'
  exit 1
}

printf 'Catalyst upgrade\n'
printf '================\n'
printf 'Nothing is changed until a backup has been taken, and everything is\n'
printf 'put back automatically if the new version fails its tests.\n'
printf 'Detailed log: %s\n' "${LOG_FILE}"

# --------------------------------------------------------------------------
phase "Checking this machine is ready to be upgraded"
# --------------------------------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then
  fail "The upgrade needs administrator rights, and it was not run with them." \
       "Running as user '$(id -un)', not as root." \
       "Run exactly this instead, including the word sudo:   sudo bash ${BASH_SOURCE[0]}"
fi
ok "running with administrator rights"

if [ ! -x "${VENV_PY}" ]; then
  fail "Catalyst does not look installed on this machine, so there is nothing to upgrade." \
       "Expected to find ${VENV_PY} and it is not there." \
       "Install it first:   sudo bash ${SCRIPT_DIR}/install.sh" \
       "If Catalyst is installed somewhere unusual, tell the upgrade where:   sudo CATALYST_HOME=/your/path bash ${BASH_SOURCE[0]}"
fi
ok "found Catalyst's installation"

if [ "${CATALYST_SKIP_PULL}" -eq 0 ]; then
  if ! git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; then
    fail "The upgrade cannot tell which version you are on, because the program folder is not a tracked copy." \
         "${REPO_DIR} is not a git working copy." \
         "Ask whoever set Catalyst up to re-install it from the published repository, which is what makes upgrades possible." \
         "Your database and keys are safe either way - they are kept outside this folder."
  fi
  # --untracked-files=no on purpose: installing the package leaves build
  # artifacts in the folder, and those must not read as "somebody edited
  # this by hand" and block every future upgrade. Edits to files that are
  # actually tracked still stop the upgrade, which is the case that matters.
  if [ -n "$(git -C "${REPO_DIR}" status --porcelain --untracked-files=no)" ]; then
    fail "The program files on this machine have been edited by hand, and upgrading would throw those edits away." \
         "$(git -C "${REPO_DIR}" status --short --untracked-files=no | head -n 10)" \
         "If those edits matter, save a copy of them somewhere else first." \
         "Then discard them here so the upgrade can proceed:   sudo git -C ${REPO_DIR} checkout ." \
         "Then run the upgrade again."
  fi
  OLD_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD)"
  ok "current version recorded (${OLD_COMMIT:0:12})"
else
  note "not fetching a new version (asked to skip that), upgrading from the files already here"
fi

OLD_VERSION="$("${VENV_PY}" -c 'import catalyst; print(catalyst.__version__)' 2>>"${LOG_FILE}" || echo unknown)"
ok "installed version is ${OLD_VERSION}"

# --------------------------------------------------------------------------
phase "Backing up the database and your saved keys"
# --------------------------------------------------------------------------

if ! run install -d -m 0700 -o root -g root "${BACKUP_PATH}"; then
  fail "Could not create the folder to put the backup in, and the upgrade will not proceed without one." \
       "$(log_tail)" \
       "Check there is free disk space:   df -h ${CATALYST_BACKUP_DIR}" \
       "Then run the upgrade again."
fi
BACKUP_MADE=1

if [ -f "${CATALYST_DB}" ]; then
  # sqlite's own backup, not a file copy: a plain copy of a database
  # being written to at that instant can be restored as a corrupt file.
  if ! run "${VENV_PY}" -c "
import sqlite3
src = sqlite3.connect('file:${CATALYST_DB}?mode=ro', uri=True)
dst = sqlite3.connect('${BACKUP_PATH}/catalyst.db')
src.backup(dst)
dst.close(); src.close()
"; then
    fail "Could not take a safe copy of the database, so the upgrade stopped before changing anything." \
         "$(log_tail)" \
         "Check there is free disk space:   df -h ${CATALYST_BACKUP_DIR}" \
         "Then run the upgrade again. Nothing has been changed."
  fi
  INTEGRITY="$("${VENV_PY}" -c "
import sqlite3
print(sqlite3.connect('${BACKUP_PATH}/catalyst.db').execute('PRAGMA integrity_check').fetchone()[0])
" 2>>"${LOG_FILE}" || echo 'check failed')"
  if [ "${INTEGRITY}" != "ok" ]; then
    fail "The backup of the database was taken but does not read back cleanly, so it could not be trusted to restore from." \
         "The database's own integrity check said: ${INTEGRITY}" \
         "Do not upgrade yet. Send this message to whoever maintains Catalyst." \
         "The bot is still running the version it was on, untouched."
  fi
  ok "database backed up and verified readable ($(du -h "${BACKUP_PATH}/catalyst.db" | cut -f1))"
else
  note "no database yet - nothing to back up"
fi

if [ -f "${CATALYST_CREDENTIALS}" ]; then
  if ! run cp -p "${CATALYST_CREDENTIALS}" "${BACKUP_PATH}/credentials.json"; then
    fail "Could not back up your saved keys, so the upgrade stopped before changing anything." \
         "$(log_tail)" \
         "Check there is free disk space:   df -h ${CATALYST_BACKUP_DIR}" \
         "Then run the upgrade again."
  fi
  run chmod 0600 "${BACKUP_PATH}/credentials.json"
  ok "your saved keys backed up (still locked so only Catalyst can read them)"
else
  note "no saved keys yet - nothing to back up"
fi

{
  printf 'Catalyst backup taken before an upgrade\n'
  printf 'when:            %s\n' "$(date -Is)"
  printf 'version before:  %s\n' "${OLD_VERSION}"
  printf 'commit before:   %s\n' "${OLD_COMMIT:-unknown}"
  printf 'database:        %s\n' "${CATALYST_DB}"
  printf 'keys file:       %s\n' "${CATALYST_CREDENTIALS}"
  printf '\nTo put this back by hand:\n'
  printf '  sudo systemctl stop %s\n' "${CATALYST_SERVICE_NAME}"
  printf '  sudo cp -p %s/catalyst.db %s\n' "${BACKUP_PATH}" "${CATALYST_DB}"
  printf '  sudo cp -p %s/credentials.json %s\n' "${BACKUP_PATH}" "${CATALYST_CREDENTIALS}"
  printf '  sudo chown %s %s %s\n' "${CATALYST_SERVICE_USER}" "${CATALYST_DB}" "${CATALYST_CREDENTIALS}"
  printf '  sudo systemctl start %s\n' "${CATALYST_SERVICE_NAME}"
} >"${BACKUP_PATH}/README.txt"
run chmod 0600 "${BACKUP_PATH}/README.txt"
ok "backup folder ready: ${BACKUP_PATH}"

# --------------------------------------------------------------------------
phase "Fetching the new version"
# --------------------------------------------------------------------------

if [ "${CATALYST_SKIP_PULL}" -eq 0 ]; then
  if ! run git -C "${REPO_DIR}" pull --ff-only; then
    fail "Could not fetch the new version." \
         "$(log_tail)" \
         "The usual cause is no internet connection on the server. Check with:   ping -c2 github.com" \
         "Nothing has been changed - the bot is still running the version it was on." \
         "Fix the connection and run the upgrade again."
  fi
  NEW_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD)"
  if [ "${NEW_COMMIT}" = "${OLD_COMMIT}" ]; then
    note "there is no new version - you already have the latest"
  else
    ok "new version fetched (${NEW_COMMIT:0:12})"
  fi
else
  note "skipped, as asked"
fi

# --------------------------------------------------------------------------
phase "Installing the new version"
# --------------------------------------------------------------------------

if ! run "${VENV_PY}" -m pip install --quiet "${REPO_DIR}[dev]"; then
  rollback "The new version could not be installed." "$(log_tail)"
fi
NEW_VERSION="$("${VENV_PY}" -c 'import catalyst; print(catalyst.__version__)' 2>>"${LOG_FILE}" || echo unknown)"
if [ "${NEW_VERSION}" = "unknown" ]; then
  rollback "The new version installed, but Catalyst will not even start up." "$(log_tail)"
fi
ok "version ${NEW_VERSION} installed (was ${OLD_VERSION})"

# --------------------------------------------------------------------------
phase "Testing the new version"
# --------------------------------------------------------------------------

note "running the full test suite - this takes a minute and touches nothing real"
# Deliberately an `if`, not `set +e` around a `$?` capture: the ERR trap
# above fires on a failing command even when errexit is off, so capturing
# the status that way sent a failing test run to the "unexpected error"
# handler instead of to rollback() - which is the one path this whole
# script exists for. Inside an `if` condition the ERR trap does not fire.
TEST_RC=0
if ! (cd "${REPO_DIR}" && "${VENV_PY}" -m pytest) >"${TEST_LOG}" 2>&1; then
  TEST_RC=1
fi
run chmod 0600 "${TEST_LOG}"

if [ "${TEST_RC}" -ne 0 ]; then
  rollback "The new version failed its own tests, so it is not safe to run your money through it." \
           "$(tail -n 30 "${TEST_LOG}")"
fi
ok "every test passed ($(grep -Eo '[0-9]+ passed' "${TEST_LOG}" | tail -n 1 || echo 'see test log'))"

# --------------------------------------------------------------------------
phase "Restarting Catalyst and checking it came back"
# --------------------------------------------------------------------------

if [ "${CATALYST_MANAGE_SERVICE}" -eq 0 ]; then
  note "not touching the background service, as asked"
else
  if ! run systemctl restart "${CATALYST_SERVICE_NAME}"; then
    rollback "The new version passed its tests but would not start." \
             "$(journalctl -u "${CATALYST_SERVICE_NAME}" -n 30 --no-pager 2>&1 || true)"
  fi
  sleep 3
  if ! service_active; then
    rollback "The new version started and then stopped again straight away." \
             "$(journalctl -u "${CATALYST_SERVICE_NAME}" -n 30 --no-pager 2>&1 || true)"
  fi

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
    rollback "The new version is running but its web page never answered, so you would not be able to see or control it." \
             "$(journalctl -u "${CATALYST_SERVICE_NAME}" -n 30 --no-pager 2>&1 || true)"
  fi
  ok "the bot is running and its web page answered: ${HEALTH}"
fi

printf '\n'
printf '================================================================\n'
printf ' Upgrade complete.\n'
printf '================================================================\n\n'
printf '   version before:  %s\n' "${OLD_VERSION}"
printf '   version now:     %s\n' "${NEW_VERSION}"
printf '   tests:           all passed\n'
printf '   backup kept at:  %s\n\n' "${BACKUP_PATH}"
printf ' Your database, your saved keys and any open positions carried\n'
printf ' straight over. There is nothing else to do.\n\n'
printf ' If anything looks wrong later, the backup above is a complete\n'
printf ' copy of how things stood before this upgrade, and the folder\n'
printf ' contains the exact instructions for putting it back.\n'
printf '================================================================\n'
