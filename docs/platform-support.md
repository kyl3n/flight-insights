# Windows and Linux; macOS deferred

Windows x64 and Linux x64 are the current required release targets. The user's latest clarification defers macOS; existing Mac configurations are retained for optional future work. Platform-specific work belongs behind the native shell and network adapters; it must not produce different flight calculations or change evidence semantics.

| Platform | Build target | Installer format | Native build command |
| --- | --- | --- | --- |
| Windows x64 | `x86_64-pc-windows-msvc` | NSIS `.exe`, with offline WebView2 runtime installation | `npm run desktop:build:windows` |
| Linux x64 | `x86_64-unknown-linux-gnu` | `.deb` and AppImage | `npm run desktop:build:linux` |
| macOS Apple Silicon (deferred) | `aarch64-apple-darwin` | `.app` and `.dmg` | `npm run desktop:build:macos` on Apple Silicon |
| macOS Intel (deferred) | `x86_64-apple-darwin` | `.app` and `.dmg` | `npm run desktop:build:macos` on Intel |

Build on the corresponding operating system and CPU architecture. The packaged Python runtime cannot be cross-compiled from Linux into a Windows or macOS executable. Separate Mac installers avoid claiming that a single-architecture runtime is universal. Installed users do not need Python, Node, uv or a terminal for normal app operation.

The Linux CI baseline is Ubuntu 22.04. The managed cloud is Debian 13; its locally generated preview has a higher declared glibc requirement and must use `src-tauri/tauri.debian13.conf.json` when bundling. It does not establish compatibility with every Linux distribution. macOS has an explicit 13.0 deployment target, with a build-time audit of the bundled runtime's Mach-O minimums. This is a compatibility target; actual macOS 13 installed-app qualification has not been performed. Minimum OS versions must agree with both the native shell and bundled Python/dependency binaries. See [native build details](native-build.md) for the current configuration.

## Shared workflows and OS integration

| Capability | Windows | Linux | macOS |
| --- | --- | --- | --- |
| Desktop UI and local packaged engine | 0.3.0 NSIS install/uninstall and basic production WebView checks passed | 0.3.0 Debian/AppImage payload and basic production WebView checks passed | Deferred; older CI results only |
| Full native file picker, accessibility and upgrade qualification | Pending | Pending | Deferred |
| Immutable files, parameter and BIN/LOG/TLOG imports, scoped metrics and comparisons | Shared engine | Shared engine | Shared engine |
| Shared UDP recording/readback, armed-interval records, provisional associations and manifest history | Shared engine; GCS/hardware qualification required | Shared engine; GCS/hardware qualification required | Shared engine; deferred qualification |
| Chronological flight library, automatic analysis and compatible preceding-flight comparisons | Shared engine | Shared engine | Shared engine; deferred native qualification |
| Local insights, pinned guidance and explicit review-packet export | Shared engine; aircraft interpretation unqualified | Shared engine; aircraft interpretation unqualified | Shared engine; aircraft interpretation unqualified |
| Explicit parameter/log acquisition and content-based reconciliation | Shared engine; GCS/hardware qualification pending | Shared engine; GCS/hardware qualification pending | Shared engine; GCS/hardware qualification pending |
| IPv4 adapter/subnet/route inspection | PowerShell NetTCPIP and psutil | iproute2 and psutil | netstat/route and psutil |
| Configured SkyPulse HTTP/history, RTSP and UDP/RTP framing checks | Shared bounded socket probes | Shared bounded socket probes | Shared bounded socket probes |
| Experimental administrator-enrolled helper | Narrow temporary host routes and Windows Firewall rules | Host routes through NetworkManager 1.42 or newer; aliases and firewall writes unavailable | Deferred; no native execution helper |
| Default installation | Diagnosis and repair previews | Diagnosis and repair previews | Deferred diagnosis and repair previews |

Core app compatibility does not make every administrator operation interchangeable across operating systems. Default installations remain diagnosis-only. A separately installed, administrator-enrolled helper can enable experimental Windows/Linux bench repairs after session opt-in, independent fresh aircraft safety observations, a recording interlock and exact per-plan approval. The Ubuntu 22.04 app baseline does not establish Linux helper availability: its stock NetworkManager predates the helper's required 1.42 API. These helpers have not passed physical Q22 bench qualification. Durable repair receipts and conditional rollback never authorize replay or removal of a changed setting; unknown safety or conflicting host state blocks cleanup. See [network capabilities and limitations](network-diagnostics.md) and the [Linux bench setup](linux-private-bench-helper.md).

On macOS versions with Local Network privacy controls, allow Q22 Flight Lab access under **System Settings → Privacy & Security → Local Network** when using configured LAN devices. A denied permission can prevent camera/telemetry access even when routing is correct. The application's privacy description explains the configured-device access; this permission prompt still needs native verification.

## Required qualification on supported platforms

CI runs Windows and Linux by default. Manual dispatch can add both deferred Mac architectures. Before a release is declared compatible, each supported target must pass:

1. Locked dependency installation, engine tests, UI integration, Rust tests and packaged-runtime checks.
2. Installer/bundle inspection and the engine handshake from the actual packaged application layout.
3. Clean-machine install, launch, native file selection, immutable evidence import, comparison, shutdown, restart and upgrade without losing evidence.
4. Offline launch with no separately installed Python or Node; paths containing spaces and non-ASCII names must work.
5. Real host networking checks, occupied UDP ports, appropriate OS permission failures, and supported repair/rollback behavior on a separate test network.
6. Publisher signing and a documented supported OS-version range before general distribution. Signing is not required for private unsigned bench testing. Mac notarization is future work.

The [0.3.0 native CI run](https://github.com/kyl3n/flight-insights/actions/runs/38748020018)
passed on 15 October at clean source `77e39a0`, including three fresh production
GUI launches per packaged entrypoint. The current GUI contract requires exactly
**Flights** and **Setup**. Earlier 0.2.0 and Mac runs remain historical. The
[validation record](validation.md) gives exact target results and checksums.
Basic rendering/IPC checks do not replace the full desktop, upgrade, accessibility,
minimum-OS, real-device and publisher-signing release gates. Review the current
per-target dependency inventories and unresolved notice texts before release.
