# Native release checks and local bench evidence

Windows x64 and Linux x64 are the current required targets. Push/PR preview CI
builds those two targets. `include_macos` on manual workflow dispatch adds the
existing experimental arm64/x64 macOS jobs; it does not establish a macOS bench
or supported minimum OS. No signing identity is needed to build or privately
test the unsigned preview. Publisher signing is a separate distribution gate.

## Actual installed WebView qualification

`scripts/verify-native-bundle.py --require-gui` retains the existing frozen-engine,
payload, notice, relocation and native-supervisor checks, then launches the actual
installed/extracted desktop executable and bundled production frontend in the
platform WebView. Windows uses the per-user NSIS installation on a clean runner;
Linux checks both Debian extraction and AppImage layout/launcher. It does not use
the Playwright bridge to stand in for this check.

The opt-in native CLI is:

```
q22-flight-lab --verify-gui --data-dir EMPTY_ABSOLUTE_DIRECTORY --report NEW_FILE_IN_THAT_DIRECTORY --nonce HEX64
```

Only a release build with bundled assets accepts this mode. Normal user launch
is unchanged. It creates isolated engine storage and an ephemeral WebView profile
(with a separate Windows/Linux data directory), accepts only a local production
main-WebView load, waits for the rendered app navigation and “Engine connected,”
observes positive layout dimensions/two animation frames, then calls the normal
Tauri IPC handshake/status/restart path. A nonce-bound completion command exists
only for this invocation; JavaScript never provides a filesystem path. Native
code owns an exclusively created report file and waits for its engine shutdown.
Existing application localStorage and caches are not reused or changed.
There is a 60-second native deadline and 90-second verification-process deadline.

Linux CI supplies Xvfb and a session D-Bus using the runner's installed native
libraries. It does not disable the WebKit sandbox. Windows uses the installed
WebView2 runtime. Publisher signatures, Gatekeeper and quarantine are never
bypassed. A passing basic rendering/IPC check is not visual pixel comparison,
permission-dialog, accessibility, physical stream, GCS coexistence, or minimum-OS
qualification. Those limits remain in the generated JSON report.

The managed Debian cloud sysroot can compile/link the application. Its extracted
WebKit libraries do not establish usable GUI subprocess paths or sandbox support;
see `cloud-native-prerequisites.md`. A local compile pass is not reported as a
native GUI pass. The normal-host Windows/Linux CI jobs must execute the new check.

## Runnable offline Windows/Linux bench package

The preview artifacts include both scripts below. They require the new installed
application binary, not a Python/Node development environment. Run as the normal
desktop user; do not elevate. Choose a new output directory each time.

Windows PowerShell:

```powershell
.\qualify-installed-windows.ps1 -ApplicationPath 'C:\path\Q22 Flight Lab\q22-flight-lab.exe' -OutputDirectory 'C:\path\new-q22-bench' -MissionPlannerVersion 'actual version' -QGroundControlVersion 'actual version'
```

Linux:

```bash
bash qualify-installed-linux.sh /usr/bin/q22-flight-lab /absolute/new-q22-bench 'actual Mission Planner version' 'actual QGroundControl version'
```

The commands collect an exact executable SHA-256, OS version, local interface and
route observations, declared GCS versions, and the native GUI/IPC result. They
create a local ZIP/tar.gz package and do not upload, change routes/firewalls,
request camera streams, send aircraft commands, or configure DigiQ/SIYI devices.
Network addresses, interface names and local paths can be included: review the
package before sharing. DigiQ SkyPulse and SIYI A8 Mini are operator-declared bench
context, not detected/validated hardware. Missing exact GCS versions remain
“not supplied.” A failed GUI launch, timeout or mismatched report is recorded and
the local evidence package is retained; the script exits with failure. Follow the
physical cases in `release-qualification.md` separately.

## Signing readiness and exact prerequisites

Run `python scripts/release-preflight.py --report /absolute/readiness.json` to
inspect existing GitHub metadata through its configured CLI authentication.
Only secret/variable names are requested; no secret values are read or printed.
The command does not create protections, ask for credentials, dispatch, sign,
publish or modify network policy.

On 13 October 2026 the available repository API reported no environments and no
rulesets. Secret-name, variable-name and self-hosted-runner metadata calls returned
HTTP 403. Those items are **unknown**, not proof that credentials/runners are
absent. The user separately reported that no Windows Authenticode identity is set
up. These observations do not prevent private unsigned application testing.

The protected candidate workflow requires a reviewed protected `vMAJOR.MINOR.PATCH`
tag, a `release-signing` environment with reviewer/deployment protections, and
dedicated clean provisioned signing runners. Its preflight checks pinned tools,
the exact clean commit, and OS key-store metadata before building/signing. No key
is imported/exported by that preflight. Required configuration names are:

| Target | Non-secret configuration names | Protected OS credential prerequisite |
| --- | --- | --- |
| Windows | `Q22_WINDOWS_CERT_THUMBPRINT`, `Q22_WINDOWS_TIMESTAMP_URL` | Authorized code-signing identity/private-key binding in the runner's current-user certificate store |
| Linux | `Q22_GPG_FINGERPRINT` | Selected publisher signing key available to the runner's GPG agent/keyring |
| Optional macOS | `APPLE_SIGNING_IDENTITY`, `APPLE_TEAM_ID`, `Q22_APPLE_NOTARY_PROFILE` | Developer ID keychain identity and stored notarytool credential profile |

Configure secret values only through appropriate protected OS/CI storage, never
in source or chat. Presence checks do not prove a hardware signing key or remote
timestamp/notary service will accept the final candidate. Final signatures,
publisher identity, applicable notarization and the installed GUI check remain
mandatory in the signed-candidate workflow. It saves candidates, not a public
release; no workflow is dispatched by these inspection commands.

## Locked dependency evidence and diagnostic helper

After freezing/checking dependencies and before native bundling, run:

```
python scripts/application-dependencies.py --target x86_64-unknown-linux-gnu
```

Use `x86_64-pc-windows-msvc` on Windows. The generated resource directory contains
the frontend production dependency graph, target Rust/application/helper build
graphs, lockfile/source hashes, upstream notice texts, and exact checksum-verified
registry source archives for relevant copyleft-declared crates. Seven current
crate archives omit standalone license texts; preserved upstream texts are pinned
to their `.cargo_vcs_info.json` revisions with blob/SHA-256 provenance under
`scripts/dependency-notices/`. The selectors MPL text is the standard text from
Debian base-files, tied to the crate's MPL metadata/source headers; its exact
registry source archive is preserved too. No upstream source is modified.

Missing texts remain explicit `unresolved_notice_texts`; they are not silently
replaced by a guessed license. Native bundle verification compares every generated
inventory/notice/source file with the packaged copy and checks the current target,
source commit and lock hashes. Rust graph entries include build scripts and proc
macros; this is not a claim that every listed crate is linked. Frozen Python/native
entries retain their separate inventory. Application distribution terms remain
undeclared; AppImage/native-system/WebView2 payload redistribution review and legal
sign-off are separate from preserving these concrete notices/source artifacts.

The separate `q22-network-helper` artifact's normal `--capabilities` / `--stdio`
invocations are unprivileged diagnostics. CI runs injected-host-state tests and
verifies this protocol advertises `diagnosis_only`, `automatic_apply=false`, and
refuses unqualified recovery. Preview artifacts also preserve explicit private
bench installers; CI never runs them, installs/elevates a service, or alters runner
networking. Administrator-enrolled service modes authenticate the exact frozen
engine and apply a separate bounded experimental policy, with app session opt-in
and per-plan approval. Linux currently enables only versioned NetworkManager host
routes; see [linux-private-bench-helper.md](linux-private-bench-helper.md). This
implemented service path does not establish publisher trust, real privilege-denial,
hardware safety-channel, manager-version or native rollback qualification.
