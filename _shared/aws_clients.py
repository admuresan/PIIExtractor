# See architectural_instructions before making changes
"""
Shared AWS client helper for all toolkits.

This is the ONLY supported way for a toolkit to obtain a boto3 client.

Entry points:

* ``read_client(service, region, **kwargs)`` returns a boto3 client that uses
  the standard credential chain (env vars first, then ``[default]`` in
  ``~/.aws/credentials``). Per org policy the developer's ``[default]``
  profile is read-only, so this is safe by default and never prompts for MFA.

* ``write_client(service, region, **kwargs)`` returns a boto3 client whose
  credentials are fetched lazily from ``aws-vault`` (using the existing
  ``genai-write`` profile from the AWS MFA setup doc). The first API call on
  the client triggers a ``ykman`` invocation, which makes the YubiKey blink;
  the developer taps the key and ``aws-vault`` returns short-lived STS
  credentials. The credentials are cached in process and shared with
  ``aws_vault_env`` so a single touch warms both code paths.

* ``aws_vault_env(profile=..., on_touch_required=..., on_token_received=...)``
  returns AWS_* env vars suitable for injecting into a non-Python
  subprocess (Node/tsx, shell, etc.). Same caching as ``write_client``.

* ``ensure_credentials(profile=..., on_touch_required=..., on_token_received=...)``
  is the recommended priming entry point. Call it *immediately before* any
  AWS write so the YubiKey-touch UI hint is shown at a controlled moment
  rather than the request hanging silently inside botocore. Returns True if
  a fresh tap happened, False if the cache was warm. Use with
  ``make_progress_callbacks(push_line)`` to get the standard wording.

* ``make_progress_callbacks(push_line)`` builds the standard MFA hint /
  success callback pair (tap gold sensor when LED blinks / STS acquired).
  Use this instead of inlining wording in each toolkit so every UI surfaces
  the same language.

* ``mfa_process`` (ykman) touch timeouts are retried in a loop with backoff
  unless ``MFA_PROCESS_MAX_WAIT_SEC`` is set to a positive cap (``0``/unset =
  no limit for a single HTTP handler).

* ``diagnose(profile=...)`` runs a no-touch health check (binary, backend,
  profile config, mfa_process, source-profile credentials present) and
  returns a structured dict suitable for rendering as a "Test AWS setup"
  panel. Use this to back a toolkit UI button so users can self-diagnose
  without touching the CLI.

The helper deliberately avoids any ``~/.aws/config`` edits beyond what the
MFA setup doc already requires. It also pins ``aws-vault``'s keychain
backend explicitly to the ``login`` keychain (overridable via the
``AWS_VAULT_BACKEND`` / ``AWS_VAULT_KEYCHAIN_NAME`` env vars at launcher
level), so Flask subprocesses behave the same as the developer's
interactive shell regardless of which shell-rc env vars happen to leak in.

Per-toolkit non-secret config (account name -> region) is loaded from a
per-toolkit ``accounts.json`` via ``load_accounts(toolkit_dir)``.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

import boto3
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session as _botocore_session

logger = logging.getLogger(__name__)

# The aws-vault profile created during the AWS MFA setup. We never
# parameterize this here; if a future toolkit needs a different profile
# (e.g. PROD), extend ``write_client`` with an explicit argument rather
# than adding more module-level state.
_AWS_VAULT_PROFILE = "genai-write"

# Resolve aws-vault once at import time. We fall back to the Apple Silicon
# Homebrew default path because Flask processes spawned by the launcher do
# not always inherit the developer's full PATH.
_AWS_VAULT_BIN = shutil.which("aws-vault") or "/opt/homebrew/bin/aws-vault"

# aws-vault 7.x defaults its keychain *name* to "aws-vault" (a separate
# macOS keychain), but the AWS MFA setup doc has developers store their
# IAM credentials in the standard "login" keychain. Without an explicit
# --keychain flag, aws-vault would look in the empty "aws-vault" keychain
# and report "credentials missing" — even though `aws-vault list` from a
# shell that has AWS_VAULT_KEYCHAIN_NAME=login set in .zshrc shows them.
# Flask subprocesses spawned by the launcher don't inherit those shell
# rc env vars, so we must be explicit. Override via env var if a
# developer keeps creds in a different keychain.
_AWS_VAULT_BACKEND = os.environ.get("AWS_VAULT_BACKEND", "keychain")
_AWS_VAULT_KEYCHAIN_NAME = os.environ.get("AWS_VAULT_KEYCHAIN_NAME", "login")


def _aws_vault_backend_args() -> List[str]:
    """Return the explicit --backend / --keychain args for every aws-vault call.

    Always passing these makes our subprocess invocations reproducible
    regardless of what env vars happen (or fail) to leak through from
    the developer's shell. Only the keychain backend honors --keychain;
    other backends ignore it.
    """
    args = ["--backend", _AWS_VAULT_BACKEND]
    if _AWS_VAULT_BACKEND == "keychain":
        args.extend(["--keychain", _AWS_VAULT_KEYCHAIN_NAME])
    return args


# ---------------------------------------------------------------------------
# Account config (region-only, no secrets)
# ---------------------------------------------------------------------------


def load_accounts(toolkit_dir: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Load the per-toolkit ``accounts.json`` mapping name -> {region, ...}.

    The file is expected at ``<toolkit_dir>/credentials/accounts.json`` if a
    ``credentials`` directory exists, otherwise ``<toolkit_dir>/accounts.json``.
    Returns an empty mapping (with a warning) if no file is found, so toolkits
    can show a helpful "no accounts configured" message instead of crashing.
    """
    toolkit_path = Path(toolkit_dir)
    candidates = [
        toolkit_path / "credentials" / "accounts.json",
        toolkit_path / "accounts.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Failed to load accounts file %s: %s", path, exc)
                return {}
            accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
            if not isinstance(accounts, dict):
                logger.error("accounts.json at %s has malformed 'accounts' field", path)
                return {}
            return accounts

    logger.warning(
        "No accounts.json found under %s (looked for %s); returning empty mapping. "
        "Copy accounts_template.json to accounts.json to configure regions.",
        toolkit_path,
        ", ".join(str(c) for c in candidates),
    )
    return {}


# ---------------------------------------------------------------------------
# Read clients (default credential chain, no MFA)
# ---------------------------------------------------------------------------


def read_client(service: str, region: str, **kwargs: Any):
    """Return a boto3 client using the default credential chain.

    Used for any AWS call that does not need write access. Honors env vars
    set by ``aws-vault exec`` / ``awsx`` if present (so launching the
    toolkit under ``awsx`` upgrades reads to the assumed-role session
    transparently), and otherwise falls back to ``[default]`` in
    ``~/.aws/credentials``.
    """
    return boto3.client(service, region_name=region, **kwargs)


# ---------------------------------------------------------------------------
# Write client (lazy aws-vault subprocess + RefreshableCredentials)
# ---------------------------------------------------------------------------


def _aws_vault_available() -> bool:
    return bool(_AWS_VAULT_BIN) and Path(_AWS_VAULT_BIN).exists()


# ---------------------------------------------------------------------------
# MFA token acquisition (we drive ykman ourselves, then hand the code to
# aws-vault). This gives us deterministic control over *when* the YubiKey
# touch is requested, so callers can show an MFA hint in their UI
# immediately before the LED actually blinks instead of hoping the
# message and the blink land in the same 500ms poll window.
# ---------------------------------------------------------------------------


def _aws_config_path() -> Path:
    return Path(os.environ.get("AWS_CONFIG_FILE") or (Path.home() / ".aws" / "config"))


def _load_aws_profile(profile: str) -> dict[str, str]:
    """Return the named profile's settings from ``~/.aws/config``.

    Returns an empty dict if the file or section is missing. AWS uses two
    different section header conventions: ``[default]`` for the default
    profile, and ``[profile NAME]`` for everything else.
    """
    cfg = _aws_config_path()
    if not cfg.exists():
        return {}
    parser = configparser.ConfigParser()
    try:
        parser.read(cfg)
    except configparser.Error as exc:
        logger.warning("Could not parse %s: %s", cfg, exc)
        return {}
    section = "default" if profile == "default" else f"profile {profile}"
    if section not in parser:
        return {}
    return {k: v for k, v in parser[section].items()}


def _resolve_mfa_process(profile: str) -> List[str]:
    """Return the parsed ``mfa_process`` argv for a profile.

    Raises ``RuntimeError`` with an actionable message if the profile or
    the ``mfa_process`` directive is missing — both are mandatory for the
    YubiKey-driven flow this helper implements.
    """
    settings = _load_aws_profile(profile)
    if not settings:
        raise RuntimeError(
            f"profile '{profile}' not found in {_aws_config_path()}. "
            "Configure it per the AWS MFA setup doc."
        )
    mfa_process = settings.get("mfa_process") or settings.get("credential_process")
    if not mfa_process:
        raise RuntimeError(
            f"profile '{profile}' has no 'mfa_process' (or 'credential_process') "
            f"directive in {_aws_config_path()}. Add a line like "
            "'mfa_process = ykman oath accounts code --single <name>-yubikey-primary'."
        )
    return shlex.split(mfa_process)


def _mfa_stderr_retriable(stderr: str) -> bool:
    """Whether to run mfa_process again (e.g. touch timeout / transient USB).

    The toolkit keeps looping on these without surfacing an error so the UI can
    appear to wait patiently for a tap.
    """
    low = (stderr or "").lower()
    return any(
        needle in low
        for needle in (
            "timeout",
            "timed out",
            "time out",
            "touch",
            "user presence",
            "user was not",
            "missing touch",
            "operation timed out",
            "aborted",
            "connection reset",
            "ccid",
            "pcsc",
            "cancelled",
            "canceled",
            "failed connecting",
            "communication error",
        )
    )


def _run_mfa_process(profile: str) -> str:
    """Run the profile's mfa_process and return its TOTP code.

    Blocks until the developer touches the YubiKey. On stderr that suggests a
    missed or late touch / transient USB issue, this function **retries in a
    loop** with backoff (capped) so callers see a single patient wait instead
    of an immediate failure.

    Optional env ``MFA_PROCESS_MAX_WAIT_SEC``: if set to a positive number,
    stop retrying after that many seconds and raise ``RuntimeError``. If unset
    or ``0``, there is no wall-clock limit (HTTP client may still reconnect).

    Raises ``RuntimeError`` for non-retriable failures or malformed output.
    """
    raw_cap = (os.environ.get("MFA_PROCESS_MAX_WAIT_SEC") or "").strip()
    max_wait_sec: float | None
    if not raw_cap or raw_cap == "0":
        max_wait_sec = None
    else:
        try:
            max_wait_sec = float(raw_cap)
        except ValueError:
            max_wait_sec = None

    cmd = _resolve_mfa_process(profile)
    started = time.monotonic()
    backoff = 0.5
    max_backoff = 15.0
    attempt = 0

    while True:
        if max_wait_sec is not None and (time.monotonic() - started) >= max_wait_sec:
            raise RuntimeError(
                f"mfa_process for profile '{profile}' stopped after "
                f"MFA_PROCESS_MAX_WAIT_SEC={max_wait_sec}s without a successful touch."
            )

        attempt += 1
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode == 0:
            code = (completed.stdout or "").strip()
            if not (code.isdigit() and 6 <= len(code) <= 8):
                raise RuntimeError(
                    f"mfa_process for profile '{profile}' returned unexpected output "
                    f"(expected a 6-8 digit TOTP, got {code!r})."
                )
            if attempt > 1:
                logger.info(
                    "mfa_process succeeded for profile %s after %s attempt(s)",
                    profile,
                    attempt,
                )
            return code

        last_stderr = (completed.stderr or "").strip() or "(no stderr output)"
        if _mfa_stderr_retriable(last_stderr):
            if attempt == 1 or attempt % 10 == 0:
                logger.info(
                    "mfa_process waiting for YubiKey (attempt %s); retrying. stderr: %s",
                    attempt,
                    last_stderr[:220],
                )
            time.sleep(min(backoff, max_backoff))
            backoff = min(backoff * 1.15, max_backoff)
            continue

        raise RuntimeError(
            f"mfa_process for profile '{profile}' failed (exit "
            f"{completed.returncode}): {last_stderr}. Check that the YubiKey is "
            "plugged in and the OATH account name in mfa_process matches "
            "what 'ykman oath accounts list' reports."
        )


# ---------------------------------------------------------------------------
# aws-vault invocation (with a pre-computed MFA token; no aws-vault prompts)
# ---------------------------------------------------------------------------


def _classify_aws_vault_error(stderr: str, profile: str) -> str:
    """Translate raw aws-vault stderr into actionable guidance for the UI.

    Each branch matches a known failure mode and tacks a concrete next
    step onto the original message so a developer hitting the toolkit
    for the first time can self-serve without context-switching into
    CLI debugging. Unknown errors get the raw message + a generic hint
    so we never hide signal.
    """
    msg = stderr.strip() or "(no stderr output)"
    lower = msg.lower()

    # Most common: aws-vault looked in the wrong keychain (or the entry
    # was never added). Tell the user exactly which keychain we're using
    # and what to do.
    if "credentials missing" in lower or "no credentials found" in lower:
        return (
            f"{msg}\n\n"
            f"Hint: aws-vault could not find IAM credentials for the source "
            f"profile of '{profile}' in the {_AWS_VAULT_BACKEND} backend "
            f"(keychain '{_AWS_VAULT_KEYCHAIN_NAME}'). Run once: "
            f"`aws-vault add --backend={_AWS_VAULT_BACKEND} "
            f"--keychain={_AWS_VAULT_KEYCHAIN_NAME} <source-profile>` "
            "with your IAM access key id and secret. If you store creds "
            "in a different backend or keychain, set AWS_VAULT_BACKEND / "
            "AWS_VAULT_KEYCHAIN_NAME in the launcher env."
        )

    if "incorrect mfa" in lower or "multifactorauthentication failed" in lower or "invalid mfa" in lower:
        return (
            f"{msg}\n\nHint: the YubiKey TOTP we generated was rejected by "
            "STS. This usually means the YubiKey's clock has drifted, the "
            "OATH credential name in mfa_process doesn't match the device "
            "actually serving the code, or the mfa_serial in ~/.aws/config "
            "points to a different MFA device than the YubiKey. Try again; "
            "if it persists, re-run the AWS MFA setup."
        )

    if "user is not authorized" in lower or "accessdenied" in lower:
        return (
            f"{msg}\n\nHint: STS accepted the MFA but the IAM user is not "
            f"allowed to assume the role configured in profile '{profile}'. "
            "Verify role_arn in ~/.aws/config and the IAM policy attached "
            "to the source user."
        )

    if "keychain" in lower and ("locked" in lower or "denied" in lower or "user interaction" in lower):
        return (
            f"{msg}\n\nHint: macOS denied access to the "
            f"'{_AWS_VAULT_KEYCHAIN_NAME}' keychain. Unlock it (Keychain "
            "Access app), or when the password prompt appears, click "
            "'Always Allow' so future toolkit runs don't re-prompt."
        )

    return f"{msg}\n\n(unknown aws-vault failure; see message above)"


def _run_aws_vault_with_token(profile: str, mfa_token: str) -> str:
    """Invoke ``aws-vault exec --json --mfa-token=<code> <profile>``.

    Always passes explicit ``--backend`` / ``--keychain`` flags (see
    ``_aws_vault_backend_args``) so behavior is deterministic across
    interactive shells and Flask subprocesses — the latter don't
    inherit shell-rc env vars like ``AWS_VAULT_KEYCHAIN_NAME``.

    By passing ``--mfa-token`` we keep aws-vault out of the YubiKey path
    entirely — it just calls STS with the code we already obtained. This
    is what lets us guarantee the UI hint is shown *before* the YubiKey
    blinks rather than racing the blink.

    On non-zero exit, raises ``RuntimeError`` with both the raw stderr
    and an actionable hint produced by ``_classify_aws_vault_error``.
    """
    if not _aws_vault_available():
        raise RuntimeError(
            "aws-vault not found on PATH; install it and configure the "
            f"'{profile}' profile per the AWS MFA setup doc."
        )

    cmd = [
        _AWS_VAULT_BIN,
        *_aws_vault_backend_args(),
        "exec",
        "--json",
        f"--mfa-token={mfa_token}",
        profile,
    ]
    logger.debug("aws-vault command: %s", " ".join(shlex.quote(c) for c in cmd))

    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if completed.returncode != 0:
        hint = _classify_aws_vault_error(completed.stderr or "", profile)
        if completed.stderr:
            print(completed.stderr, end="")
        raise RuntimeError(
            f"aws-vault failed (exit {completed.returncode}) for profile "
            f"'{profile}': {hint}"
        )

    return completed.stdout


def _acquire_sts_credentials(
    profile: str,
    on_touch_required: Optional[Callable[[str], None]] = None,
    on_token_received: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Drive the full YubiKey -> TOTP -> aws-vault -> STS dance for ``profile``.

    Sequencing (deterministic, no timing coincidences):

      1. Resolve mfa_process from ~/.aws/config.
      2. Call ``on_touch_required(profile)`` so the UI can show an MFA /
         tap-YubiKey hint *before* anything talks to the device.
      3. Run mfa_process (ykman) — YubiKey LED starts blinking, blocks
         until the developer taps.
      4. Call ``on_token_received(profile)`` so the UI can transition to
         "Got code, assuming role".
      5. Run aws-vault with ``--mfa-token``; aws-vault never prompts.
      6. Return the parsed STS payload dict.

    Either callback may be ``None`` for non-UI callers.
    """
    _resolve_mfa_process(profile)  # validate config before we tell the user to tap

    if on_touch_required is not None:
        on_touch_required(profile)

    code = _run_mfa_process(profile)

    if on_token_received is not None:
        on_token_received(profile)

    out = _run_aws_vault_with_token(profile, code)
    payload = json.loads(out)

    # Populate the shared env-vars cache so any later aws_vault_env() OR
    # write_client refresh for this profile is silent until expiry. This
    # is what lets a UI-aware caller prime credentials once (with hints)
    # via ensure_credentials() and then have downstream AWS calls — even
    # ones that go through write_client's RefreshableCredentials inside
    # botocore — reuse the freshly-acquired STS bundle without prompting
    # again.
    env: dict[str, str] = {
        "AWS_ACCESS_KEY_ID": payload["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": payload["SecretAccessKey"],
        "AWS_SESSION_TOKEN": payload["SessionToken"],
    }
    region = payload.get("Region") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        env["AWS_DEFAULT_REGION"] = region
        env["AWS_REGION"] = region
    expiry = _parse_iso_expiry(payload["Expiration"])
    _aws_vault_env_cache[profile] = (env, expiry)

    return payload


def _refresh_via_aws_vault() -> dict[str, str]:
    """Refresh callback for ``RefreshableCredentials`` (write_client path).

    Honors the shared env-vars cache: if a non-stale STS bundle for the
    write profile is in cache (e.g. populated by a prior
    ``ensure_credentials()`` or ``aws_vault_env()`` call), we reuse it
    without prompting. Otherwise we drive the full touch flow silently —
    no UI callbacks, because this fires from inside botocore where there
    is no obvious place to surface a hint.

    UI-aware callers (json_viewer's Save handler, requirements_matcher's
    tsx runner, etc.) should call ``ensure_credentials()`` *before* the
    AWS write, so this silent fallback only ever runs for sessions that
    genuinely expired mid-use.
    """
    cached = _aws_vault_env_cache.get(_AWS_VAULT_PROFILE)
    if cached and datetime.now(timezone.utc) < cached[1] - _AWS_VAULT_ENV_REFRESH_LEEWAY:
        env, expiry = cached
        return {
            "access_key": env["AWS_ACCESS_KEY_ID"],
            "secret_key": env["AWS_SECRET_ACCESS_KEY"],
            "token": env["AWS_SESSION_TOKEN"],
            "expiry_time": expiry.isoformat(),
        }

    # Cold-cache or expired-cache path: serialise via the shared
    # _credentials_lock so botocore's RefreshableCredentials hook (which
    # can be invoked from many threads simultaneously when boto3 clients
    # are used concurrently) doesn't fork ykman against the YubiKey
    # multiple times in parallel.
    with _credentials_lock:
        cached = _aws_vault_env_cache.get(_AWS_VAULT_PROFILE)
        if cached and datetime.now(timezone.utc) < cached[1] - _AWS_VAULT_ENV_REFRESH_LEEWAY:
            env, expiry = cached
            return {
                "access_key": env["AWS_ACCESS_KEY_ID"],
                "secret_key": env["AWS_SECRET_ACCESS_KEY"],
                "token": env["AWS_SESSION_TOKEN"],
                "expiry_time": expiry.isoformat(),
            }
        payload = _acquire_sts_credentials(_AWS_VAULT_PROFILE)
        return {
            "access_key": payload["AccessKeyId"],
            "secret_key": payload["SecretAccessKey"],
            "token": payload["SessionToken"],
            "expiry_time": payload["Expiration"],
        }


# Cache the boto3.Session so RefreshableCredentials can do its job across
# multiple write_client() calls in the same process.
_write_session_cache: dict[str, boto3.Session | None] = {"session": None}


def _write_session() -> boto3.Session:
    if _write_session_cache["session"] is None:
        creds = RefreshableCredentials.create_from_metadata(
            metadata=_refresh_via_aws_vault(),
            refresh_using=_refresh_via_aws_vault,
            method="aws-vault-subprocess",
        )
        botocore_sess = _botocore_session()
        # botocore exposes credentials as a private attribute; this is the
        # documented hook for plugging in custom credential providers.
        botocore_sess._credentials = creds
        _write_session_cache["session"] = boto3.Session(botocore_session=botocore_sess)
    session = _write_session_cache["session"]
    assert session is not None
    return session


def write_client(service: str, region: str, **kwargs: Any):
    """Return a boto3 client whose credentials come from aws-vault on demand.

    The first API call on the returned client (e.g. ``put_object``) will
    invoke aws-vault, which makes the YubiKey blink and waits for a tap.
    Subsequent calls reuse the in-process credential cache until the STS
    session expires, at which point the next call triggers another tap.
    """
    return _write_session().client(service, region_name=region, **kwargs)


def reset_write_session_cache() -> None:
    """Test/diagnostic hook: drop the cached write session.

    The next ``write_client`` call will re-invoke aws-vault (and therefore
    re-prompt the YubiKey).
    """
    _write_session_cache["session"] = None


# ---------------------------------------------------------------------------
# Env var injection for non-Python subprocesses (Node, Go, shell, ...)
# ---------------------------------------------------------------------------


# Per-profile cache of (env-vars dict, expiry datetime). aws-vault keychains
# its own STS sessions, so re-invoking it within the session window is cheap
# (no prompt), but it still costs a 200-500ms subprocess fork + JSON parse.
# Caching here makes back-to-back tsx invocations fast.
_aws_vault_env_cache: dict[str, tuple[dict[str, str], datetime]] = {}

# Serialises the cache-miss path of `aws_vault_env` / `ensure_credentials`
# / `_refresh_via_aws_vault` so concurrent Flask handlers (e.g. a "Match
# all" batch fanning 25 tsx subprocesses out in parallel) only ever
# trigger ONE YubiKey-touch flow per cold cache. Without this the first
# 25 callers all observe an empty cache simultaneously, all fork ykman,
# and only one of them can actually talk to the YubiKey hardware — the
# rest time out and surface as "Touch account timed out!" errors.
#
# RLock (not Lock) so a future code path that calls a locked helper from
# inside another locked helper doesn't deadlock — the same thread can
# re-enter freely. Module-level singleton, scoped by profile inside
# the locked region (see the `with` blocks below).
_credentials_lock = threading.RLock()

# How long before STS expiry to consider a cached env "stale" and refresh.
_AWS_VAULT_ENV_REFRESH_LEEWAY = timedelta(minutes=5)


def _parse_iso_expiry(iso: str) -> datetime:
    """Parse the ISO-8601 expiry string aws-vault returns (`...Z`)."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


def is_aws_vault_env_cached(profile: str = _AWS_VAULT_PROFILE) -> bool:
    """Return True if a non-stale env-var bundle for ``profile`` is in cache.

    Useful for callers that want to decide *before* invoking
    ``aws_vault_env()`` whether the call will be silent (cache hit) or
    prompt for a YubiKey touch (cache miss). E.g. a UI can show a
    "touch your YubiKey" affordance only on the miss path.
    """
    cached = _aws_vault_env_cache.get(profile)
    if not cached:
        return False
    _env, expiry = cached
    return datetime.now(timezone.utc) < expiry - _AWS_VAULT_ENV_REFRESH_LEEWAY


def aws_vault_env(
    profile: str = _AWS_VAULT_PROFILE,
    *,
    on_touch_required: Optional[Callable[[str], None]] = None,
    on_token_received: Optional[Callable[[str], None]] = None,
) -> dict[str, str]:
    """Return AWS_* env vars from aws-vault, suitable for injecting into a subprocess.

    Used by toolkits that spawn non-Python subprocesses (Node/tsx scripts,
    shell tools, etc.) which need real STS credentials with write/decrypt
    permissions — the developer's ``[default]`` profile is read-only and
    can't, e.g., perform ``kms:Decrypt`` on tenant data keys.

    The first call (per profile) drives ykman directly to obtain a TOTP
    (which blinks the YubiKey LED and blocks on a tap), then hands the
    TOTP to aws-vault via ``--mfa-token``. The returned STS creds are
    cached in-process until shortly before expiry; subsequent calls are
    zero-cost. After expiry the next call re-runs the touch flow.

    ``on_touch_required(profile)`` is called *immediately before* ykman
    is invoked — i.e. before the YubiKey LED starts blinking. Use it to
    push an MFA / tap-YubiKey hint into a UI progress stream;
    because we drive the sequencing ourselves the hint is guaranteed to
    appear before the LED blinks, not after.

    ``on_token_received(profile)`` is called *after* the developer has
    tapped and we have the TOTP, but *before* aws-vault calls STS. Use
    it to transition the UI from "touch your YubiKey" to "calling AWS"
    so the user knows the tap registered.

    Both callbacks fire only on cache-miss paths — never on a cache hit.
    Either callback may be ``None``.

    Returns a dict containing ``AWS_ACCESS_KEY_ID``,
    ``AWS_SECRET_ACCESS_KEY``, ``AWS_SESSION_TOKEN``, and
    ``AWS_DEFAULT_REGION`` (the latter only if exported by aws-vault).

    Raises:
        RuntimeError: aws-vault not on PATH, mfa_process not configured,
            ykman/YubiKey failure, or aws-vault exited non-zero. The
            message includes the underlying tool's stderr.
    """
    if is_aws_vault_env_cached(profile):
        return dict(_aws_vault_env_cache[profile][0])

    # Cold-cache path: serialise so parallel callers don't all fork
    # ykman against the same YubiKey at once (only one of them could
    # actually tap-handshake; the rest would time out). Double-checked
    # against the cache *inside* the lock — by the time we acquire it,
    # the previous holder may have populated the cache, in which case
    # we silently reuse their work without prompting again.
    #
    # _acquire_sts_credentials populates _aws_vault_env_cache as a side
    # effect, so we just read it back to return the dict shape callers
    # expect. Keeping the cache write in one place (the acquire function)
    # ensures write_client's refresh path and aws_vault_env's env-var
    # path share state — a single touch warms both.
    with _credentials_lock:
        if is_aws_vault_env_cached(profile):
            return dict(_aws_vault_env_cache[profile][0])
        _acquire_sts_credentials(
            profile,
            on_touch_required=on_touch_required,
            on_token_received=on_token_received,
        )
        return dict(_aws_vault_env_cache[profile][0])


def reset_aws_vault_env_cache(profile: str | None = None) -> None:
    """Test/diagnostic hook: drop the cached env vars for one (or all) profiles."""
    if profile is None:
        _aws_vault_env_cache.clear()
    else:
        _aws_vault_env_cache.pop(profile, None)


# Env vars that, if present in a subprocess env alongside our injected
# static AWS credentials, will silently downgrade the credential
# resolution. The AWS SDK (notably JS v3, but also some boto3 paths)
# prefers AWS_PROFILE over static AWS_ACCESS_KEY_ID env credentials —
# so a stray AWS_PROFILE=default loaded from a project's .env file
# (e.g. genai/.env via tsx --env-file) silently routes the SDK back to
# the read-only [default] IAM user, defeating the entire MFA flow. Same
# story for AWS_DEFAULT_PROFILE.
_AWS_PROFILE_OVERRIDE_VARS = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")


def aws_vault_subprocess_env(
    base_env: Optional[dict[str, str]] = None,
    profile: str = _AWS_VAULT_PROFILE,
    *,
    on_touch_required: Optional[Callable[[str], None]] = None,
    on_token_received: Optional[Callable[[str], None]] = None,
) -> dict[str, str]:
    """Build a complete subprocess env with our STS creds *and* AWS_PROFILE neutralized.

    Use this — instead of ``base_env.update(aws_vault_env(...))`` — for
    any non-Python subprocess that talks to AWS via the SDK (Node/tsx,
    Go binaries, shell tools that wrap the AWS CLI, etc.).

    What it does:

      1. Starts from ``base_env`` (or ``os.environ``) so the child
         inherits the developer's normal shell environment.
      2. Layers our short-lived STS creds (``AWS_ACCESS_KEY_ID``,
         ``AWS_SECRET_ACCESS_KEY``, ``AWS_SESSION_TOKEN``,
         ``AWS_DEFAULT_REGION``) on top via ``aws_vault_env``.
      3. *Explicitly sets* ``AWS_PROFILE`` and ``AWS_DEFAULT_PROFILE``
         to the empty string. This neutralizes them in two ways:

           - Empty strings are *defined* in the env, so Node's
             ``--env-file`` (and most dotenv loaders) will not re-add
             them from a project ``.env`` file. Verified against
             Node 20+ ``--env-file`` semantics ("if a variable is
             already defined, the value from the .env file is
             ignored").
           - Empty strings are *falsy* to AWS SDK profile detection,
             so the SDK skips profile-based credential lookup and
             uses the static keys we just injected. Otherwise a
             stray ``AWS_PROFILE=default`` (as in genai/.env)
             silently downgrades the call to the read-only
             ``[default]`` IAM user — which can't ``kms:Decrypt``,
             which is the whole reason we did the MFA dance.

    Returns the final env dict ready to pass to
    ``subprocess.Popen(..., env=...)``. Same caching and failure
    semantics as ``aws_vault_env``.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env.update(
        aws_vault_env(
            profile,
            on_touch_required=on_touch_required,
            on_token_received=on_token_received,
        )
    )
    for k in _AWS_PROFILE_OVERRIDE_VARS:
        env[k] = ""
    return env


# ---------------------------------------------------------------------------
# Public priming API + standard UI callback factory (reusable across toolkits)
# ---------------------------------------------------------------------------


def ensure_credentials(
    profile: str = _AWS_VAULT_PROFILE,
    *,
    on_touch_required: Optional[Callable[[str], None]] = None,
    on_token_received: Optional[Callable[[str], None]] = None,
) -> bool:
    """Pre-acquire STS credentials so the next AWS call can be silent.

    This is the recommended entry point for any toolkit that performs an
    AWS write at a user-visible moment (e.g. a Save button, a
    long-running script kickoff). Call it *immediately before* the AWS
    call so the YubiKey-touch prompt appears at a controlled moment in
    the UI, instead of the request hanging silently while botocore
    refreshes credentials inside the AWS call.

    Because both ``write_client`` (RefreshableCredentials) and
    ``aws_vault_env`` (subprocess env injection) read from the same
    in-process cache, a single ``ensure_credentials()`` warms both —
    json_viewer's Save (which uses ``write_client.put_object``),
    requirements_matcher's tsx runs (which use ``aws_vault_env``), and
    any future caller all benefit from the same prime.

    Returns True if a fresh STS exchange happened (the user tapped the
    YubiKey), False if the cache was already warm and nothing was
    prompted. Callers can use this to suppress redundant "credentials
    primed" toasts on cache hits.

    Callbacks have the same semantics as in ``aws_vault_env``:
    ``on_touch_required`` fires immediately before the YubiKey LED
    blinks; ``on_token_received`` fires after the tap and before the STS
    call. Both fire only on cache-miss paths. Either may be ``None`` for
    non-UI callers (e.g. tests, scripts).

    Raises:
        RuntimeError: aws-vault not on PATH, mfa_process not configured,
            ykman/YubiKey failure, or aws-vault exited non-zero. The
            message includes the underlying tool's stderr.
    """
    if is_aws_vault_env_cached(profile):
        return False
    # Same double-checked locking dance as `aws_vault_env`. If we end
    # up taking the lock and observe a now-warm cache, return False so
    # UI callers don't show a redundant "tap registered" toast — the
    # previous lock holder already handled the user-facing prompt.
    with _credentials_lock:
        if is_aws_vault_env_cached(profile):
            return False
        _acquire_sts_credentials(
            profile,
            on_touch_required=on_touch_required,
            on_token_received=on_token_received,
        )
        return True


def make_progress_callbacks(
    push_line: Optional[Callable[[str], None]],
    *,
    log: bool = True,
) -> tuple[Callable[[str], None], Callable[[str], None]]:
    """Build the standard ``(on_touch_required, on_token_received)`` pair.

    Centralises the user-facing wording and logging so every toolkit
    reports YubiKey state with the same language (tap gold sensor when
    the LED blinks / STS acquired...). Pass any callable that knows how
    to surface a line of text
    to the user — a ``progress_store.push_line`` partial, a Flask SSE
    sink, ``print``, or ``None`` if the caller only wants the log
    statements.

    When ``log`` is True (default), each event is also logged at INFO
    level via this module's logger so the messages show up in toolkit
    server logs even when no UI sink is attached.

    Both returned callbacks are safe to pass directly to
    ``ensure_credentials`` or ``aws_vault_env``.
    """

    def _on_touch_required(profile: str) -> None:
        msg = (
            f"MFA required — when your YubiKey LED blinks, tap the gold capacitive sensor "
            f"on the key itself (not a keyboard key). Profile: {profile}. Leave the key plugged in."
        )
        if log:
            logger.info("%s", msg)
        if push_line is not None:
            try:
                push_line(msg)
            except Exception:  # never let a UI sink failure break credential acquisition
                logger.exception("on_touch_required sink raised; ignoring")

    def _on_token_received(profile: str) -> None:
        msg = (
            f"MFA succeeded — AWS STS session acquired for profile {profile}. Continuing..."
        )
        if log:
            logger.info("%s", msg)
        if push_line is not None:
            try:
                push_line(msg)
            except Exception:
                logger.exception("on_token_received sink raised; ignoring")

    return _on_touch_required, _on_token_received


# ---------------------------------------------------------------------------
# Diagnostics (for "Test AWS setup" buttons / first-run health checks)
# ---------------------------------------------------------------------------


def diagnose(profile: str = _AWS_VAULT_PROFILE) -> dict[str, Any]:
    """Run a no-touch health check of the developer's AWS setup.

    Designed to back a toolkit "Test AWS" UI affordance so a user who's
    hitting credential errors can self-diagnose without dropping into
    the CLI. Returns a structured dict the toolkit can render directly.

    Checks (all read-only, no STS call, no YubiKey touch):

      - ``aws-vault`` binary is found and reports its version.
      - The configured backend/keychain is queryable.
      - The given write profile exists in ``~/.aws/config`` and has a
        ``mfa_process`` directive.
      - The mfa_process command (e.g. ``ykman``) is on PATH.
      - The source profile referenced by the write profile has an entry
        in the configured backend (so the next exec won't fail with
        "credentials missing").

    Each check produces ``{ok: bool, detail: str}``. The top-level
    ``healthy`` flag is True only if every check passed.
    """
    results: dict[str, dict[str, Any]] = {}

    # 1. aws-vault binary
    if _aws_vault_available():
        version = subprocess.run(
            [_AWS_VAULT_BIN, "--version"], capture_output=True, text=True, check=False
        )
        results["aws_vault_binary"] = {
            "ok": True,
            "detail": (version.stdout or version.stderr or "").strip() or _AWS_VAULT_BIN,
        }
    else:
        results["aws_vault_binary"] = {
            "ok": False,
            "detail": "aws-vault not found on PATH; install via Homebrew (`brew install aws-vault`).",
        }

    # 2. Backend reachable
    backend_args = _aws_vault_backend_args()
    listing = subprocess.run(
        [_AWS_VAULT_BIN, *backend_args, "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    results["backend_reachable"] = {
        "ok": listing.returncode == 0,
        "detail": (
            f"backend={_AWS_VAULT_BACKEND} keychain={_AWS_VAULT_KEYCHAIN_NAME}: "
            + ((listing.stdout or "").strip() if listing.returncode == 0 else (listing.stderr or "").strip())
        ),
    }

    # 3. Profile exists with mfa_process
    settings = _load_aws_profile(profile)
    if not settings:
        results["profile_configured"] = {
            "ok": False,
            "detail": f"profile '{profile}' not found in {_aws_config_path()}",
        }
    elif not (settings.get("mfa_process") or settings.get("credential_process")):
        results["profile_configured"] = {
            "ok": False,
            "detail": f"profile '{profile}' has no mfa_process directive in {_aws_config_path()}",
        }
    else:
        results["profile_configured"] = {
            "ok": True,
            "detail": f"profile '{profile}' OK (source_profile={settings.get('source_profile', '?')})",
        }

    # 4. mfa_process command resolvable
    if results["profile_configured"]["ok"]:
        try:
            mfa_cmd = _resolve_mfa_process(profile)
            mfa_bin = shutil.which(mfa_cmd[0])
            if mfa_bin:
                results["mfa_process_executable"] = {
                    "ok": True,
                    "detail": f"{mfa_cmd[0]} -> {mfa_bin}",
                }
            else:
                results["mfa_process_executable"] = {
                    "ok": False,
                    "detail": f"mfa_process command '{mfa_cmd[0]}' not found on PATH",
                }
        except RuntimeError as exc:
            results["mfa_process_executable"] = {"ok": False, "detail": str(exc)}

    # 5. Source profile has credentials in the configured backend.
    # We parse `aws-vault list` output and look for the source_profile name
    # in the "Credentials" column. This avoids any read of the secret value
    # (which would prompt for keychain unlock), so diagnose() is safe to
    # run silently from a UI poll.
    source_profile = settings.get("source_profile") if settings else None
    if source_profile and listing.returncode == 0:
        has_creds = False
        for line in (listing.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == source_profile and parts[1] == source_profile:
                has_creds = True
                break
        if has_creds:
            results["source_credentials_present"] = {
                "ok": True,
                "detail": f"'{source_profile}' has credentials in keychain '{_AWS_VAULT_KEYCHAIN_NAME}'",
            }
        else:
            results["source_credentials_present"] = {
                "ok": False,
                "detail": (
                    f"'{source_profile}' has no credentials in {_AWS_VAULT_BACKEND}/"
                    f"{_AWS_VAULT_KEYCHAIN_NAME}. Run once: "
                    f"`aws-vault add --backend={_AWS_VAULT_BACKEND} "
                    f"--keychain={_AWS_VAULT_KEYCHAIN_NAME} {source_profile}`"
                ),
            }

    healthy = all(r["ok"] for r in results.values())
    return {
        "healthy": healthy,
        "profile": profile,
        "backend": _AWS_VAULT_BACKEND,
        "keychain": _AWS_VAULT_KEYCHAIN_NAME,
        "checks": results,
    }
