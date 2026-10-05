# Native foundation preview

This is the initial Tauri 2 / packaged Python integration, not a qualified flight application. The desktop process starts one frozen Python engine, verifies protocol version 1 and the product identity, and keeps that process alive for serialized JSON-RPC requests. The end user does not need Python or Node. Development and packaging need the tools below.

## Build on the target operating system

Use Node 24.19.0, Python 3.12.14, uv 0.12.19 and Rust 1.99.0. JavaScript, Python and Rust dependency resolutions are in `package-lock.json`, `uv.lock`, and `src-tauri/Cargo.lock`. Use each target's native CPU architecture; PyInstaller does not cross-compile. Windows x64, Linux x64, macOS Apple Silicon and macOS Intel are equally required release targets.

Linux requires a C compiler, `pkg-config`, WebKitGTK 4.1 development files, GTK 3 development files, OpenSSL development files, Ayatana AppIndicator, librsvg, and patchelf. Ubuntu 22.04 is the CI build baseline; installed `.deb` packages retain their native WebKit dependencies. Windows requires the Visual Studio C++ build tools and WebView2. The Windows installer embeds the offline WebView2 installer. macOS requires Xcode command-line tools. The declared macOS deployment floor is 13.0: the packager inspects the frozen bootloader and every embedded Mach-O library and rejects a dependency whose minimum exceeds that floor. The CI job also checks the native executable and application metadata. Running the CI job on macOS 15 does not establish testing on macOS 13.

From the repository root:

```sh
npm ci
uv sync --frozen --python 3.12.14
uv run --frozen pytest
uv run --frozen python scripts/package-engine.py
npm run build
cargo test --manifest-path src-tauri/Cargo.toml --locked
npm run tauri -- dev
```

The packager freezes `q22_engine` with its Python runtime and dependencies, writes a host-specific executable to `src-tauri/binaries/q22-engine-<target>[.exe]`, and records its SHA-256. It relocates the executable into a directory containing spaces and Unicode characters, removes Python import environment variables, and checks handshake, status, capabilities, a synthetic Unicode parameter import and persistence, actual native interface inspection, and clean EOF shutdown. Synthetic BIN, LOG and TLOG imports exercise background jobs, MAVLink CRC decoding, Parquet writes/reads and parameter-scoped interval metrics in the frozen runtime. It sends no network probes or repair commands. PyInstaller caches stay in the ignored project build directory. These checks do not substitute for launching the installed GUI.

The custom PyArrow hook collects the local Parquet/dataset runtime and its required transitive adapters/native libraries. Packaging rejects `pymavlink.DFReader`, `pymavlink.mavutil`, the MAVLink generator, lxml, iconv and optional fastcrc in the actual archive. Generated dialect code supplies the TLOG decoder and its pure-Python CRC fallback; the optional native accelerator is excluded from distributed binaries. Each build records its source Git commit/dirty state, Python modules and every frozen native/data entry with its hash in `*.inventory.json`, and installs the inventory plus dependency license/NOTICE texts under its `engine` resources. Linux additionally copies available distro copyright texts for collected system libraries. The artifact verifier checks that these resources survived bundling unchanged. Linux system notices are outer resources, so the standalone sidecar is not an independent distribution. Review the complete per-target inventory and [dependency review](dependency-review.md) before distribution; an inventory and selected license files are not a blanket legal approval for every native library, nor an application distribution license.

Linux engine preparation sets the AppImage deployment RPATH (`$ORIGIN/../lib`) before hashing and smoke testing, and checks that applying it again preserves every byte. Tauri's pinned linuxdeploy otherwise rewrites that ELF metadata during AppImage creation. Preparing the expected value preserves strict whole-engine equality, including the bootloader; the verifier still rejects any packaged byte mismatch. Linux packaging therefore requires `patchelf` on `PATH`.

For installers, run `npm run desktop:build`. Bundles are written to `src-tauri/target/release/bundle`. Keep sidecar and shell from the same build together. Packaged binaries and intermediate outputs are ignored by Git. Icons are generated from `src-tauri/icons/app-icon.svg` with `npm exec -- tauri icon src-tauri/icons/app-icon.svg`.

`.github/workflows/native-build.yml` requires engine tests, Rust supervisor tests, browser integration tests, and native artifact verification on every configured OS/CPU target before uploading its artifacts. Runner labels `macos-15` (arm64) and `macos-15-intel` (x64) match the [official runner image table](https://github.com/actions/runner-images/blob/main/README.md); the packager also rejects a host/target mismatch. These remain **foundation previews**, without qualified publisher signatures or notarization. Tauri uses the supported ad-hoc signing identity `-` to seal the complete macOS preview bundle, including its resources; strict verification checks that seal and the engine. An ad-hoc seal does not establish publisher trust. The protected signing workflow overrides this identity with the provisioned Developer ID. See [validation.md](validation.md) for executed job results and their source identities. These are repeatable source builds, not a claim of bit-for-bit identical signatures, timestamps or system packages.

`scripts/verify-native-bundle.py` extracts the Debian/AppImage packages, copies the macOS application from its `.app` or read-only DMG, or installs the Windows NSIS package. It checks the frozen engine payload against the source sidecar, runs the frozen import/interface smoke, then runs the **packaged native executable** with `--verify-engine --data-dir DIRECTORY`. That diagnostic uses the normal Rust supervisor and sidecar resolution, including restart and shutdown, without opening a webview. Paths include spaces and Unicode. Tauri places the macOS engine beside the application executable under `Contents/MacOS`, which the diagnostic verifies. The report records hashes and explicitly leaves native GUI rendering and hardware testing unverified.

For example, on Linux:

```sh
uv run --frozen python scripts/verify-native-bundle.py --artifact "src-tauri/target/release/bundle/deb/Q22 Flight Lab_0.1.0_amd64.deb" --report .local/native-verification-linux.json
```

Run Windows installer verification **only on a clean disposable runner or VM**. A temporary install directory alone does not isolate Windows registration or shortcuts. The verifier refuses existing Q22 uninstall registrations in both user/machine hives and registry views, cleans up partial installation failures when the uninstaller exists, and waits for the copied uninstaller to complete. CI applies this check to the actual NSIS artifact; it is not a claim that Windows verification ran in this Linux environment.

## Local data and failure recovery

The engine receives `--data-dir` pointing to Tauri's per-user application data directory:

| OS | Expected location |
| --- | --- |
| Windows | `%APPDATA%\com.q22.flightlab` |
| macOS | `~/Library/Application Support/com.q22.flightlab` |
| Linux | `$XDG_DATA_HOME/com.q22.flightlab`, normally `~/.local/share/com.q22.flightlab` |

Original source archives and metadata remain there across restarts. Engine diagnostics stay local in `logs/engine-stderr.log`; one previous log is retained, with each capped at 1 MiB. Logs may contain local paths. No log upload is enabled. File-write failures cannot block stderr drainage or contaminate RPC stdout.

The shell accepts only its enumerated engine operations and the local `engine.restart` control. It rejects requests above 1 MiB, responses above 4 MiB, mismatched IDs, malformed envelopes and incompatible handshakes. There are at most eight queued operations. Startup has a 15-second timeout and calls have a 120-second timeout. A crashed or timed-out engine is stopped, and the UI must request a restart explicitly. Failed requests are never replayed automatically, since an import or network change may already have completed. Inspect persisted state after restarting before retrying a mutation.

`logs.import` and `logs.session` return durable job IDs promptly; decoding and metric construction run on a bounded single-worker queue. The UI polls `jobs.get`, lists outcomes through `jobs.list`, and requests cooperative cancellation through `jobs.cancel`. A large import does not extend the native RPC timeout or emit unsolicited stdout frames. Restart interrupts pending jobs and preserves their journal; an operator reviews the evidence and explicitly retries. `network.recovery` reads persisted repair receipts and reconstructs only supported, freshly confirmed recovery actions.

Closing the desktop window stops accepting work, closes the engine input pipe, waits up to three seconds, and terminates an engine that does not exit. PyInstaller's launcher and Python child belong to a dedicated process group on Linux/macOS. Windows starts the launcher suspended, assigns a private job with kill-on-close, then resumes its initial thread. Because job termination is asynchronous, Windows waits up to three additional seconds for its active process count to reach zero before reporting shutdown or permitting restart. A shutdown timeout is reported instead of silently overlapping old and new engines. This covers normal application exit; OS termination or power loss still requires the engine's durable storage guarantees. Starting another application instance is not yet a supported concurrent recording workflow.

The local webview has a restrictive CSP, native open-file dialogs, and no shell, arbitrary process, HTTP or filesystem plugin permission. Requests do not accept an executable or command-line string. The engine validates its own method arguments. Imports currently accept local paths supplied by the local UI; per-file capability grants are future defense in depth. Network probes and repairs use the engine's separate explicit configuration and confirmation controls; desktop startup does not change host networking.

On macOS 15 and later, configured device probes can require Local Network permission. The application includes `NSLocalNetworkUsageDescription` explaining configured camera/digiQ checks and forwarded telemetry. If access is denied, enable Q22 Flight Lab under **System Settings → Privacy & Security → Local Network**, then retry the configured probe. Permission prompting has not been tested here; no sandbox exemptions or broad entitlements were added.

## Remaining qualification

The foundation includes bounded BIN/LOG/TLOG evidence ingestion and observed parameter timelines. Full live stream decoding, unattended live recording and advanced DSP remain outside this milestone. Parameter comparisons describe associations with explicit evidence scope; they do not prove causation or write aircraft parameters. No real aircraft, digiQ module, camera, Windows/macOS installation, signing, or hardware acceptance is implied by compilation or a synthetic fixture test. Record the actual tests performed with each preview.

## Manual signed candidate preparation

`.github/workflows/native-signing.yml` is a prepared capability, not evidence that anything was signed. It has only a manual trigger, read-only repository permissions and artifact retention; it never creates a GitHub Release, publishes an installer or submits to a store. Select an existing reviewed `vMAJOR.MINOR.PATCH` tag (optional prerelease suffix) when dispatching. It checks out that exact commit and rebuilds/tests before signing. There is no arbitrary ref input.

Before enabling it, an administrator must protect the release tags against creation/update/deletion by untrusted actors, configure the **release-signing** environment to allow only those protected tags with required human reviewers, and restrict its self-hosted `q22-release` runners to this trusted workflow. A YAML tag check alone does not establish trust. Use ephemeral clean runners, never shared PR runners: installers create Windows registration, and trusted build code executes where signing capabilities are available. Provision the pinned build tools and native/browser dependencies from the build section. Linux release runners must use the Ubuntu 22.04 baseline; both macOS CPU architectures have separate runners. Start from a checkout without existing bundle output.

Private signing material stays in the runner's OS store or agent. The required secrets/capabilities are provisioned out of band; do not put certificate files, passwords, Apple credentials, or GPG passphrases into workflow inputs, GitHub variables, command arguments, or this repository.

| Platform | Provisioned secret capability | Non-secret environment variables |
| --- | --- | --- |
| Windows x64 | Authenticode certificate/private key accessible to `signtool` in the dedicated runner user's certificate store or supported hardware provider | `Q22_WINDOWS_CERT_THUMBPRINT` (40 hex characters), `Q22_WINDOWS_TIMESTAMP_URL` (HTTPS RFC3161 service) |
| macOS arm64 and x64 | Developer ID Application identity/private key in an unlocked dedicated keychain; a `notarytool` keychain profile containing the Apple notarization credential | `APPLE_SIGNING_IDENTITY`, `APPLE_TEAM_ID`, `Q22_APPLE_NOTARY_PROFILE` |
| Linux x64 | Publisher GPG signing key available through the dedicated runner's GPG agent | `Q22_GPG_FINGERPRINT` |

Set the listed selectors as protected GitHub environment variables. The macOS packager signs embedded Python libraries with the same Developer ID as the application; no library-validation or sandbox exemption is introduced. The signing script notarizes/staples the app, creates a DMG containing the stapled app, then signs/notarizes/staples the DMG. Windows signs the frozen engine and lets Tauri sign the shell/NSIS installer. Verification requires the expected publisher and OS trust, and reruns the packaged runtime smoke. Linux packages receive detached GPG signatures verified against the configured fingerprint. Final artifact hashes, notices, inventories and verification/notarization reports are retained for qualification.

No signing credentials are configured or exercised by this cloud setup. Windows/macOS signatures, notarization and clean-machine GUI installation remain release prerequisites until the actual jobs and [release qualification procedure](release-qualification.md) pass. Retained workflow artifacts are candidates for review; publishing them remains a separate authorized action.

The managed cloud machine is Debian 13. Native dependencies are extracted into a workspace sysroot, with compiler activation in `scripts/cloud-native-env.sh`. This supports compilation, Rust/engine tests and Debian packaging. Build its package using `npm run tauri -- build --bundles deb --config src-tauri/tauri.debian13.conf.json --ci -- --locked`; the override declares the local binary's newer libc requirement. Its release WebKit build expects child executables at system paths absent from the isolated sysroot, so native webview rendering cannot be qualified there. Do not disable WebKit sandboxing to work around this. A locally generated Debian 13 preview still needs installation and GUI testing on a normal Debian 13 machine with the declared system dependencies. The Ubuntu CI job provides the separate older Linux build baseline; its recorded results are in [validation.md](validation.md).
