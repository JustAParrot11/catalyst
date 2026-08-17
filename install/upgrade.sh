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
# set -u is on: these are read in the final report whether or not the
# fetch phase ran, so they must exist before it.
NOTHING_FETCHED=0
UPGRADE_BRANCH="unknown"
NEW_BUILD_HASH="unknown"
INSTALLED_DIR="unknown"
REPO_BUILD_HASH="unknown"
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

# THE STAMP THAT REACHES THE RUNNING BOT.
#
# The patch number is commits since catalyst/VERSION last changed, which
# is what turns the hand-set series 0.3 into 0.3.14. It has to be
# counted here, because the copy the service imports has no git.
#
# It is written INSIDE the package (catalyst/BUILD) and shipped as
# package data. Owner-reported: "almost it does just say 0.3.x though?"
# on a correctly upgraded machine. Every other channel is unreachable
# from where the bot actually runs - catalyst.service sets no build
# variable, a file at the repository root is not visible from
# site-packages, and site-packages has no .git - so all three fell
# through while THIS script's own printout looked right, because it
# passes the value explicitly. A number correct everywhere except in
# front of the owner is not a number.
#
# Defined up here, above rollback(), because rollback re-stamps too: a
# rolled-back machine that kept the new version's number would report
# the version it FAILED to install while running the previous code.
#
# Failure is never fatal. With no stamp the version reads 0.3.x, which
# is visibly not a digit and so cannot be mistaken for one.
stamp_build_number() {
  _base="$(git -C "${REPO_DIR}" log -1 --format=%H -- catalyst/VERSION 2>/dev/null || true)"
  if [ -n "${_base}" ]; then _span="${_base}..HEAD"; else _span="HEAD"; fi
  _n="$(git -C "${REPO_DIR}" rev-list --count "${_span}" 2>/dev/null || true)"
  _c="$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "${_n}" ]; then
    printf '%s\n' "${_n}" > "${REPO_DIR}/.build_number" 2>>"${LOG_FILE}" || true
  else
    rm -f "${REPO_DIR}/.build_number" 2>>"${LOG_FILE}" || true
  fi
  # BEFORE pip runs, or it never reaches the installed copy.
  printf '%s\n%s\n' "${_n}" "${_c}" \
    > "${REPO_DIR}/catalyst/BUILD" 2>>"${LOG_FILE}" || true
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

  # RE-STAMP TO THE OLD COMMIT. Without this a rolled-back machine
  # reports the version it FAILED to install while running the previous
  # code - a lie in exactly the number the owner checks to decide
  # whether an upgrade landed.
  if [ -n "${OLD_COMMIT:-}" ]; then
    printf '%s\n' "${OLD_COMMIT}" > "${REPO_DIR}/.build_commit" 2>>"${LOG_FILE}" || true
  fi
  # The working tree is back at OLD_COMMIT by now, so recounting gives
  # the old patch number rather than the one being abandoned.
  stamp_build_number

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
    # The REASON THIS ROLLBACK HAPPENED, not a guess at it. This
    # paragraph said "failed its own tests" for every rollback, whatever
    # caused it - so a rollback for an entirely different reason sent
    # the owner to read a test log that had nothing to do with it. A
    # message that cannot be wrong is not a message.
    printf ' The upgrade was refused and put back. Your database, your keys\n'
    printf ' and your open positions are untouched.\n\n'
    printf ' WHY IT WAS REFUSED\n'
    printf '%s\n\n' "${1}" | indent
    printf ' WHAT TO DO ABOUT IT\n'
    printf '     1. Send this whole message to whoever maintains Catalyst.\n'
    printf '        If a test log is mentioned above, send that too:  %s\n' "${TEST_LOG}"
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
  UPGRADE_BRANCH="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  if [ "${NEW_COMMIT}" = "${OLD_COMMIT}" ]; then
    NOTHING_FETCHED=1
    note "no new version on branch '${UPGRADE_BRANCH}' - already at ${NEW_COMMIT:0:12}"
  else
    ok "new version fetched (${OLD_COMMIT:0:12} -> ${NEW_COMMIT:0:12} on '${UPGRADE_BRANCH}')"
  fi
else
  note "skipped, as asked"
fi

# --------------------------------------------------------------------------
phase "Installing the new version"
# --------------------------------------------------------------------------

# STAMP THE VERSION BEFORE INSTALLING, so the installed package can
# report which code it is. The version used to be a hand-maintained
# string that nobody bumped, so every upgrade printed the same "0.2.0"
# and told the owner nothing had changed (owner-reported). It is derived
# now - but the service user on a VPS may have no git, and site-packages
# has no .git at all, so both numbers are written down here, where the
# repository still is.
#
#   .build_commit  the exact code, for when two machines disagree
#   .build_number  the patch: commits since catalyst/VERSION changed,
#                  which is what turns 0.3 into 0.3.14
if [ -n "${NEW_COMMIT:-}" ]; then
  printf '%s\n' "${NEW_COMMIT}" > "${REPO_DIR}/.build_commit" 2>>"${LOG_FILE}" || true
fi
stamp_build_number

if ! run "${VENV_PY}" -m pip install --quiet "${REPO_DIR}[dev]"; then
  rollback "The new version could not be installed." "$(log_tail)"
fi
NEW_VERSION="$(cd / && CATALYST_BUILD_COMMIT="${NEW_COMMIT:-}" "${VENV_PY}" -c 'import catalyst; print(catalyst.__version__)' 2>>"${LOG_FILE}" || echo unknown)"
NEW_BUILD="$(cd / && CATALYST_BUILD_COMMIT="${NEW_COMMIT:-}" "${VENV_PY}" -c 'import catalyst; print(catalyst.__build__)' 2>>"${LOG_FILE}" || echo unknown)"
# THE INSTALLED COPY, not the repo. `cd repo && python -m pytest` puts
# the repo first on sys.path, so the tests exercise the working tree
# while the SERVICE runs whatever pip put in site-packages. If those two
# ever diverge - a failed install, a second checkout, a venv that is not
# the one systemd uses - the tests pass, the service restarts on old
# code, and nothing says a word. That is exactly what happened
# (owner-reported 2026-08-11: repo byte-for-byte current, dashboard
# serving a build from somewhere else). Run from / so the repo cannot
# shadow the installed package.
NEW_BUILD_HASH="$(cd / && "${VENV_PY}" -c 'from catalyst.dashboard.build import BUILD_HASH; print(BUILD_HASH)' 2>>"${LOG_FILE}" || echo unknown)"
INSTALLED_DIR="$(cd / && "${VENV_PY}" -c 'from catalyst.dashboard.build import build_manifest; print(build_manifest()["directory"])' 2>>"${LOG_FILE}" || echo unknown)"
REPO_BUILD_HASH="$(cd "${REPO_DIR}" && "${VENV_PY}" -c 'from catalyst.dashboard.build import BUILD_HASH; print(BUILD_HASH)' 2>>"${LOG_FILE}" || echo unknown)"
if [ "${NEW_BUILD_HASH}" != "${REPO_BUILD_HASH}" ]; then
  rollback "The new version was installed, but the copy the bot actually runs is NOT the copy that was just tested." \
           "$(printf 'tested (this folder):   %s\ninstalled (what runs):  %s\ninstalled from:         %s\nthis folder:            %s\n\nThe bot imports its code from the installed folder, not from this\none. They have to be the same or the tests prove nothing about what\nwill actually run. Usual cause: the service uses a different virtual\nenvironment from the one this upgrade installed into.' \
              "${REPO_BUILD_HASH}" "${NEW_BUILD_HASH}" "${INSTALLED_DIR}" "${REPO_DIR}")"
fi
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

# Belt and braces: the tests run as root, so anything they touched in
# the service's own folders would be left root-owned and unusable by the
# service user. That happened once for real - a root-owned lock file
# silently disabled the duplicate-instance guard on every start
# (2026-08-10). The suite no longer writes there at all; this hands the
# folders back regardless, and repairs installs already broken by it.
if [ -d "${CATALYST_STATE_DIR}" ]; then
  run chown -R "${CATALYST_SERVICE_USER}:${CATALYST_SERVICE_USER}" \
      "${CATALYST_STATE_DIR}" || true
fi

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
printf '   build:           %s\n' "${NEW_BUILD}"
printf '   code before:     %s\n' "${OLD_COMMIT:0:12}"
printf '   code now:        %s  (branch %s)\n' "${NEW_COMMIT:0:12}" "${UPGRADE_BRANCH}"
printf '   dashboard build: %s\n' "${NEW_BUILD_HASH}"
printf '   running from:    %s\n' "${INSTALLED_DIR}"
printf '   tests:           all passed\n'
printf '   backup kept at:  %s\n\n' "${BACKUP_PATH}"

# The version's patch number is COUNTED from the repository, so it now
# moves on its own whenever code ships - but only the series is chosen
# by a person, and the count could still fail to be taken. The COMMIT
# always moves when the code does, so both are printed; when neither has
# moved, saying "Upgrade complete" is a lie the owner then spends an
# evening chasing through browser caches (owner-reported 2026-08-10).
if [ "${NOTHING_FETCHED}" -eq 1 ]; then
  printf ' NOTHING CHANGED. There was no new version to fetch on branch\n'
  printf " '%s', so the code you are running is exactly what\n" "${UPGRADE_BRANCH}"
  printf ' you were running before. That is not a failure - it means the\n'
  printf ' changes you are expecting have not been published to that\n'
  printf ' branch yet.\n\n'
  printf ' If you were told a change had shipped, ask whoever told you\n'
  printf ' which branch it went to. To see what this machine is following:\n'
  printf '   git -C %s status -sb\n\n' "${REPO_DIR}"
else
  printf ' Your database, your saved keys and any open positions carried\n'
  printf ' straight over. There is nothing else to do.\n\n'
  printf ' If the pages still look the same, compare the build above with\n'
  printf ' the one printed at the bottom of the dashboard sidebar. If they\n'
  printf ' match, you are seeing the new version and the change is\n'
  printf ' somewhere else. If they differ, the browser is showing a cached\n'
  printf ' page.\n\n'
fi
printf ' If anything looks wrong later, the backup above is a complete\n'
printf ' copy of how things stood before this upgrade, and the folder\n'
printf ' contains the exact instructions for putting it back.\n'
printf '================================================================\n'
