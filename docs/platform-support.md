# Windows, Linux and macOS

All three operating systems are required release platforms. The user's clarification supersedes the Windows-first rollout in the supplied reference handoff. Platform-specific work belongs behind the native shell and network adapters; it must not produce different flight calculations or change evidence semantics.

| Platform | Build target | Installer format | Native build command |
| --- | --- | --- | --- |
| Windows x64 | `x86_64-pc-windows-msvc` | NSIS `.exe`, with offline WebView2 runtime installation | `npm run desktop:build:windows` |
| Linux x64 | `x86_64-unknown-linux-gnu` | `.deb` and AppImage | `npm run desktop:build:linux` |
| macOS Apple Silicon | `aarch64-apple-darwin` | `.app` and `.dmg` | `npm run desktop:build:macos` on Apple Silicon |
| macOS Intel | `x86_64-apple-darwin` | `.app` and `.dmg` | `npm run desktop:build:macos` on Intel |

Build on the corresponding operating system and CPU architecture. The packaged Python runtime cannot be cross-compiled from Linux into a Windows or macOS executable. Separate Mac installers avoid claiming that a single-architecture runtime is universal. Installed users do not need Python, Node, uv or a terminal for normal app operation.

The Linux CI baseline is Ubuntu 22.04. The managed cloud is Debian 13; its locally generated preview has a higher declared glibc requirement and must use `src-tauri/tauri.debian13.conf.json` when bundling. It does not establish compatibility with every Linux distribution. macOS has an explicit 13.0 deployment target, with a build-time audit of the bundled runtime's Mach-O minimums. This is a compatibility target; actual macOS 13 installed-app qualification has not been performed. Minimum OS versions must agree with both the native shell and bundled Python/dependency binaries. See [native build details](native-build.md) for the current configuration.

## Shared workflows and OS integration

| Capability | Windows | Linux | macOS |
| --- | --- | --- | --- |
| Desktop UI, native file picker, local packaged engine | Implemented; see recorded CI results; installed-GUI qualification pending | Implemented; build/runtime checks passed; installed-GUI qualification pending | Implemented; see recorded CI results; installed-GUI qualification pending |
| Immutable files, parameter and BIN/LOG/TLOG imports, scoped metrics and comparisons | Shared engine | Shared engine | Shared engine |
| IPv4 adapter/subnet/route inspection | PowerShell NetTCPIP and psutil | iproute2 and psutil | netstat/route and psutil |
| Configured SkyPulse HTTP, RTSP and UDP checks | Shared bounded socket probes | Shared bounded socket probes | Shared bounded socket probes |
| Reviewed automatic network repair | Selected host routes and Windows Firewall rules | Selected host routes, address aliases and active firewalld rules | Selected device host routes and /32 address aliases; firewall changes unsupported |

Core app compatibility does not make every administrator operation interchangeable across operating systems. Unsupported repairs must be clearly shown with manual recovery guidance, while diagnostics and flight analysis remain available. Durable repair receipts support reviewed recovery across restarts; they never authorize automatic replay or removal of a changed setting. Native repair execution on each target remains a release gate. See [network capabilities and limitations](network-diagnostics.md).

On macOS versions with Local Network privacy controls, allow Q22 Flight Lab access under **System Settings → Privacy & Security → Local Network** when using configured LAN devices. A denied permission can prevent camera/telemetry access even when routing is correct. The application's privacy description explains the configured-device access; this permission prompt still needs native verification.

## Required qualification on every platform

CI runs separate native builds for all four target combinations. Before a release is declared compatible, each target must pass:

1. Locked dependency installation, engine tests, UI integration, Rust tests and packaged-runtime checks.
2. Installer/bundle inspection and the engine handshake from the actual packaged application layout.
3. Clean-machine install, launch, native file selection, immutable evidence import, comparison, shutdown, restart and upgrade without losing evidence.
4. Offline launch with no separately installed Python or Node; paths containing spaces and non-ASCII names must work.
5. Real host networking checks, occupied UDP ports, appropriate OS permission failures, and supported repair/rollback behavior on a separate test network.
6. Platform signing/notarization and a documented supported OS-version range before general distribution.

Native CI has executed on Windows, Linux and both Mac architectures. The [validation record](validation.md) links the actual runs and source commits and distinguishes them from the separate Debian 13 build. Automated checks include the bundled engine and native supervisor; clean installed GUI, supported minimum OS versions, real-device behavior and publisher signing remain release gates.
