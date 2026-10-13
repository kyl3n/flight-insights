# Q22 private bench network helper

This crate implements the separate privileged Windows/Linux service used by explicit experimental bench repair enrollment. It is not automatically installed or enabled by the desktop app. Public signing and real adapter/aircraft qualification remain pending; capability `qualified` stays false.

Read [network diagnostics and recovery](../docs/network-diagnostics.md), [Linux installation](../docs/linux-private-bench-helper.md), and [Windows installation](docs/windows-service.md) before installation. Default command-line modes `--capabilities` and `--stdio` are read-only diagnostics. Only the administrator-installed Linux `--serve` or Windows `--service` host can mutate enrolled targets after authenticated app opt-in and exact plan approval.

Linux supports a temporary selected-device host route using NetworkManager >=1.42, its nonzero applied-connection version and preserve-external-IP flag. Windows supports temporary ActiveStore host routes and exact named inbound UDP rules. Other automatic operations are unsupported. No raw renderer/Python command text is accepted.

The native service owns its policy, process authentication, passive UID/boot/heartbeat observer, recording guard, typed plans, journal and rollback timer. Missing/armed/unknown evidence blocks both directions; timed recovery may remain unresolved rather than mutate an unsafe or externally changed link. Service restart invalidates previews and checks unresolved repairs immediately, never replaying apply.

Development checks (injected state/temp files only):

```sh
cargo fmt --manifest-path native-repair/Cargo.toml --check
cargo clippy --manifest-path native-repair/Cargo.toml --locked --all-targets -- -D warnings
cargo test --manifest-path native-repair/Cargo.toml --locked
```

These checks do not install the helper or change host networking. OS-specific read-only tests run only on their native platform; actual privileged fixture, power-loss, multi-adapter and aircraft bench acceptance is separate.
