# Handoff provenance

[CODEX_START_HERE.txt](CODEX_START_HERE.txt) is the entry point for the seven files
in the canonical **Revision 2.0, 12 October 2026** design handoff. They were copied
unchanged from the supplied archive. These
documents describe requirements; they are not application source, completed
acceptance evidence, or permission to execute the example network changes.
Existing user instructions, repository work and platform requirements remain
applicable.

The four previously canonical **Revision 1.0, 3 October 2026** files are preserved
byte-for-byte in [revisions/2026-10-03/](revisions/2026-10-03/). Their declared
revision date names that directory. Each preserved file was also checked against
its member in the original uploaded archive. Earlier wording, including Brian
baseline names, remains unchanged in that historical copy.

| Uploaded archive | Revision | SHA-256 |
| --- | --- | --- |
| `Q22_Codex_Handoff.zip` | 1.0 | `4cf22de7fc5472ccfbb5c33a44db288c2ec4a80ca8487136ec6fa8475798eb93` |
| `Q22_Codex_Handoff.zip` | 2.0 | `c8869f1337af20508426fae4797eadcf83c9da5a3e003aa8c491b6d405040604` |

Both archive hashes were calculated from the uploaded ZIP bytes. The Revision 2
hash also matches its intake digest. Every canonical file matches its Revision 2
archive member. [SOURCE_HASHES.json](SOURCE_HASHES.json) records archive identity,
file byte counts and individual hashes for all eleven preserved/package files.
[SHA256SUMS](SHA256SUMS) provides the same file hashes in standard verification
format:

```bash
cd docs/handoff
sha256sum --check SHA256SUMS
```

The manifests cover the supplied documents and examples; they do not include
themselves or this explanatory provenance note.

[FLIGHT_MANIFEST_EXAMPLE.json](FLIGHT_MANIFEST_EXAMPLE.json) and
[NETWORK_REPAIR_PLAN_EXAMPLE.json](NETWORK_REPAIR_PLAN_EXAMPLE.json) are explicitly
**synthetic, illustrative design examples**. Both set `example_only` to `true`;
the repair example also sets `executable` to `false`. Example identifiers,
addresses, parameter-completeness labels and state names are not observations of
an actual aircraft or this computer. They must not be imported as real evidence
or treated as authorization to change the network.

Neither handoff archive supplies the accepted physical original/pre-FACt or
FACt/Brian parameter-baseline files. The updated display names and legacy aliases
describe how accepted files should eventually be represented; they do not
establish that those files exist in this repository. Preserve any independently
provided originals and their aliases, and do not synthesize a missing baseline
from these documents or the synthetic fixtures. No hardware validation is
established by installing this handoff.
