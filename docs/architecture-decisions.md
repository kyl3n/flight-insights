# Architecture decisions — foundation preview

The supplied [handoff](handoff/CODEX_START_HERE.txt) and [implementation contract](handoff/Q22_Implementation_Contract.json) are the reference design. They describe a complete future product. This checkout begins with a working milestone; it does not claim that the six implementation stages have passed.

## Desktop and local engine

Tauri 2 supervises a packaged Python executable. React invokes a narrow native command; newline-delimited JSON-RPC 2.0 carries request IDs and a protocol-version handshake. stdout contains protocol messages only. Requests and replies have size limits. No production HTTP API or remote service is required. Python/Node are development tools, not installed-user prerequisites.

The coordinator serializes short protocol requests. Log imports, scoped session construction and finalized capture archival run in a single background worker, with a bounded queue, cooperative cancellation and a durable job journal. A restart marks unfinished jobs interrupted and never replays an import or repair. The passive UDP recorder runs in a separate supervised process and persists raw UDP, TLOG chunks and an event journal independently of analysis. Closing the app ends its engine and recorder; no background lifetime is promised. Bounded network probes are explicit user actions and respect recorder socket ownership.

## Evidence and comparisons

Original files are archived by SHA-256 and catalogued locally with immutable snapshots. Parameter imports support actual text export formats; performance CSV is an explicitly documented interchange schema, not a replacement for a DataFlash decoder. Supplied fixtures are synthetic. No original/Brian tune baseline is seeded without the accepted files.

The numerical integration remains in a shared standard-library implementation. A bounded DataFlash reader preserves the file's schemas and scaling, while a pinned generated pymavlink dialect validates recorded MAVLink packets. PyArrow stores batches in bounded Parquet shards and filters source/boot-scoped queries without loading an entire flight into Python dictionaries. Closing each shard bounds retained footer metadata; reads scan one shard at a time. An immutable manifest preserves order and checksums. The full signal catalog, DuckDB query service, spectra, filter modeling and PID advice remain outstanding.

Comparisons associate actual imported snapshots with scoped flight measurements. A current readback does not prove which parameters were active during a historic flight. Configuration differences and performance differences are associations, never automatically causal conclusions or tuning instructions.

Log-derived snapshots contain observations available at the selected interval's start. Later observations remain in the timeline and cannot overwrite earlier settings. Partial parameter evidence, corruption boundaries, source identity and boot epochs constrain analysis; a log containing parameter records is not automatically a complete readback. See [log ingestion](log-import.md).

## Network repairs

Inspect configured targets and host interfaces; never sweep a subnet. Distinguish local route selection, successful HTTP/RTSP negotiation, actual UDP receipt, stream validation, and unknown firewall state. Receiving bytes does not establish decodable video or prove return routing on a radio.

Repair plans are generated and retained in the engine, reviewed before application, scoped to the selected target/interface, and accompanied by rollback instructions. They do not disable firewalls, change the default internet route, enable IP forwarding/NAT, or alter aircraft parameters. Privilege failures remain explicit. Physical topology, address uniqueness, and device return routes still require field verification.

## Release boundaries

The user's platform clarification makes Windows, Linux and macOS equally required release targets, superseding the Windows-first sequencing in the original handoff. Core evidence, import, comparison and network-diagnostic workflows share one implementation. OS integration uses explicit capability adapters and must expose unavailable operations with recovery guidance. A release is not qualified until native builds and installed-app checks pass on all three systems, including both configured Mac architectures.

CI definitions are build recipes, not evidence of executed Windows/macOS qualification. Unsigned milestone artifacts require the documented development workflow; signed installers and clean-machine tests are release work. Real Q22 flight bundles, exact firmware, ESC mappings, SkyPulse samples, and accepted baseline files are required for hardware claims. See the [platform matrix](platform-support.md).
