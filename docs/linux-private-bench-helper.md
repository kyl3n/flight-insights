# Linux private bench service

This is an explicit administrator-enrolled experimental service for a private
Linux bench. It is separate from ordinary app installation and from the helper's
safe `--capabilities` / `--stdio` diagnosis-only interface. No service is installed
or started by preview CI, the cloud setup, or the application. Public signing and
native hardware acceptance are not established by compiling this implementation.

The Linux service accepts only its enrolled non-root UID running the exact
root-protected frozen `q22-engine` image. It compares executable hashes and native
peer credentials for each connection. The currently enabled native writer is a
temporary host route through NetworkManager 1.42 or newer's versioned D-Bus `Reapply` operation.
It does not enable general Linux address/firewall repair. Provider, manager-version,
crash, privilege-denial and rollback cases still need a disposable physical/VM
bench with the actual network manager. Unsupported managers fail closed.

The app separately requires explicit private-bench session opt-in and approval of
each freshly prepared plan. Installation is not approval of a repair. A dedicated
native observer must receive current vehicle identity/boot/armed-state evidence
and recording status. The observer is passive; arrange forwarding out of band to
a separate port. Explicit `127.0.0.1` observer bind/sender addresses are supported
for local GCS forwarding; this does not allow loopback device/route or Internet
verification targets. A socket bound without valid fresh telemetry does not establish
that the aircraft is disarmed. Unsigned telemetry cannot prove authenticity or
exclude replay. Signed telemetry, when required by enrollment, uses a separately
provisioned root-private 32-byte key file under `/etc`, for example
`/etc/q22-observer-signing.key`; never place key bytes in JSON, chat or
source. Existing publisher signatures are not bypassed.

## Review before installation

Use the exact helper executable, installer, systemd unit and enrollment example
from the same preview. Python 3 is needed only for this administrator's installer;
ordinary application use needs neither Python nor Node. Install the application
normally under `/usr` or `/opt` first so its frozen `q22-engine` and every ancestor are root-owned and
not group/world writable. An AppImage extracted into a user-writable directory is
not an enrollable engine. Symlinks, interpreters, non-ELF images, multiply linked
images and mismatched hashes are rejected. `/root` and user home paths are not
accepted for the engine or observer key because the service hides home directories.
The supported frozen-engine mode is public read/execute (normally0755), with
traversable protected ancestors and no custom access ACL. This prevents a root-only
image from being enrolled for a desktop user who cannot execute it; custom ACLs
need separate access qualification.

Copy `enrollment-linux.example.json` into a new local review file. Its placeholders
and documentation-only addresses deliberately do not describe the user's bench.
Fill every identity, interface, device address/verification port, and explicit
Internet TCP probe endpoint from the actual bench. The Internet probe is the
operator-selected connectivity check after an approved change; the installer does
not contact it. Hash the exact engine/helper locally with `sha256sum`; set the
matching lowercase values. Use the actual non-root desktop UID from `id -u`.

Review without elevation or system changes:

```sh
python3 /absolute/preview/install-linux.py \
  --helper /absolute/preview/q22-network-helper \
  --engine /usr/bin/q22-engine \
  --enrollment /absolute/review/enrollment.json --start --enable
```

The command prints the complete bounded plan and `plan_sha256`. It does not execute
the helper or write installation files. Review the exact endpoints, hashes and
service policy. `--start` and `--enable` are explicit parts of the reviewed plan;
omit them to install without starting/enabling the service. If a private signing
key's directory prevents metadata inspection, an administrator may run this same
review mode; it still does not apply anything or read/print key bytes.

After review, an administrator can repeat those exact arguments with `--apply
--approve-plan-sha256 THE_REVIEWED_HASH` under the host's normal elevation method.
Changing any reviewed hash, policy, unit, startup or enable option invalidates the
approval. There is no sudo/password prompt or elevation mechanism inside the app
or installer. This installation command has **not** been run in the cloud.

## Installed files and recovery

The installer exclusively creates:

| Path | Owner/mode and purpose |
| --- | --- |
| `/usr/libexec/q22-network-helper` | root, 0755, exact helper executable |
| `/etc/q22-network-helper/enrollment.json` | root, 0644, policy without secret key bytes |
| `/etc/q22-network-helper/installation-review.json` | root, 0644, approved plan/hash evidence |
| `/etc/systemd/system/q22-network-helper.service` | root, 0644, reviewed service definition |
| `/var/lib/q22-network-helper` | root, 0700, durable supervisor journal |
| `/run/q22-network-helper/control.sock` | service-created socket; native credentials/image authorization applies to every request |

The systemd unit keeps the system filesystem read-only to the service except its
runtime/journal directories, retains native networking for approved verification,
and does not disable OS security. Restart supervision is independent of the app.
The default helper commands remain unprivileged diagnostics.

Existing helper, configuration, unit or durable state blocks replacement. Partial
installation failure leaves its evidence for review and reports failure; it does
not erase prior state or automatically retry a mutation. An installed service that
cannot confirm current safety/ownership must expose unresolved recovery rather
than invent successful rollback.

This installer deliberately has no destructive uninstall mode. Before an
administrator removes or replaces a service, review its recovery receipts through
the enrolled app, complete all safe rollback, confirm no unresolved repair or
recording, and preserve the configuration/journal for migration. Do not remove
state, kill supervision, or delete similarly named routes to clear a warning.
The current service has no SIGTERM drain or forced-undo handler: stopping it keeps
the journal, and restarting resumes safety/ownership-checked recovery. That may
remain unresolved until fresh trusted inactive/disarmed evidence returns.
The physical Windows/Linux cases remain in [release-qualification.md](release-qualification.md).
