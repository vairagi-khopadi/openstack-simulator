# Roadmap

Where the work to close the gap with real OpenStack has got to, what is left in the
current phase, and the questions that need answering before parts of it can start.

`docs/gaps.md` is the inventory — every endpoint family real OpenStack has that this does
not. This file is the *plan*: what was grouped into which phase, what is done, and what is
blocked on a decision rather than on time.

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Cross-cutting: microversion negotiation, marker pagination | **complete** |
| 2 | Per-project quotas, enforced | **complete** |
| 3 | Missing resource families (six of them) | **complete** |
| 4 | The long tail | **~8 of 15 areas** |
| 5 | Configuration: the real `nova.conf` / `cinder.conf` / `neutron.conf` | planned |

Phases 1–3 landed in `467e0b4..1e3aab1`. Phase 4 is in progress.

---

## Phase 4 — done

| Area | What landed |
| --- | --- |
| Cross-cutting | `?sort_key=` / `?sort_dir=` on every paginated listing, composing with the marker keyset; `?fields=` on Neutron listings |
| Nova | interface attach/detach/show, server tags (2.26), flavor update (2.55), `os-flavor-access` |
| Cinder | volume + snapshot metadata, volume transfers, `os-retype`, `os-volume_upload_image`, `revert_to_snapshot` |
| Keystone | groups, group membership, group role assignments, application credentials |
| Placement | custom traits, provider traits, aggregates, custom resource classes |
| CloudKitty | the hashmap configuration tree, with service-level mappings applied during rating |
| Swift | bulk delete, server-side `COPY`, expiring objects |
| Glance | image tags (with the import work in phase 3) |

---

## Phase 4 — remaining

Roughly 55 items. Ordered by how much they are likely to matter to something written
against the simulator, not by size.

### Neutron (12) — the largest remaining block

QoS policies and rules · RBAC policies · address scopes and address groups · segments ·
FWaaS · VPNaaS · agents and agent scheduling · floating-IP port forwarding · extra routes ·
network IP availability · auto-allocated topology · resource tags
(`/v2.0/networks/{id}/tags` and the same on every other resource — Nova's and Glance's
tags exist, Neutron's do not).

### Cinder (8)

Consistency groups and group types · QoS specs · volume-type extra specs and encryption ·
`/v3/messages` · `os-services` / `os-hosts` · manage/unmanage · default types ·
volume migrate.

### Keystone (8)

`/v3/credentials` · trusts (OS-TRUST) · EC2 credentials · federation · system-scoped
tokens · password change (`POST /v3/users/{id}/password`) · project tags · project
hierarchy (`parent_id`). CRUD is also still read-only for services, endpoints, domains and
regions, and user role assignments have `PUT` but no revoke.

### Nova (7)

Host aggregates · migrations (`/os-migrations`, `/servers/{id}/migrations`) · instance
actions (`/servers/{id}/os-instance-actions`) · remote consoles
(`/servers/{id}/remote-consoles` — note `os-getConsoleOutput` already works as a server
action) · extra-specs writes · volume-attachment update (swap) · server password.

### Octavia (6)

Amphorae · failover · LB quotas (`/v2.0/lbaas/quotas`) · flavor profiles · availability
zones · batch member update.

### Glance (4)

Metadata definitions (`/v2/metadefs/*`) · real multi-store (there is one store) · cache
API · task types other than import.

### Swift (4)

Large objects (SLO/DLO manifests — see open question 2) · object versioning · temp URLs
and form POST · container ACLs and sync.

### CloudKitty (3)

Field-level mappings and thresholds are stored and served but **not applied during
rating** — only service-level mappings are · the pyscripts module · the v2 API
(`/v2/summary`, `/v2/dataframes`, scope state).

### Placement (2)

Inventory writes (inventory is currently derived from the node's hardware) ·
`POST /reshaper`.

### Quota leftovers

`os-quota-class-sets` on Nova and Cinder · Octavia quotas (`/v2.0/lbaas/quotas`).

Six quota'd resources have a limit that is stored and reported but **no usage counter**,
so the limit can never bind — in every case because the resource itself is not
implemented yet:

| Service | No counter |
| --- | --- |
| Nova | `key_pairs`, `metadata_items`, `server_group_members` |
| Cinder | `groups`, `per_volume_gigabytes` |
| Neutron | `rbac_policy` |

Each becomes a one-line entry in `_COUNTERS` (`app/services/quotas.py`) as soon as the
resource behind it exists.

---

## Phase 5 — configuration files

**Decided:** the simulator will read the same configuration files as real OpenStack —
`nova.conf`, `cinder.conf`, `neutron.conf` — in the same INI format, under the same
section and option names. Not a simulator-specific format that happens to hold the same
numbers.

**Why:** quota defaults are the case that forced it. They live in three Python dicts in
`app/services/quotas.py` and nothing but an editor changes them; the only environment
variable in the area is `OPENSTACK_SIMULATOR_ENFORCE_QUOTAS`, which is all-or-nothing.
Anyone arriving from a real deployment reaches for `[quota] instances` in `nova.conf`,
finds no such file, and has to be told the defaults are compiled in. Reading the real
files means a runbook written against OpenStack transfers unchanged, which is the point
of the simulator.

### The option names are not symmetric

The three services spell their quota options three different ways, and the mapping layer
has to carry that rather than inventing a uniform one:

| File | Section | Spelling | Example |
| --- | --- | --- | --- |
| `nova.conf` | `[quota]` | bare resource name | `instances = 10` |
| `cinder.conf` | `[DEFAULT]` | `quota_`-prefixed | `quota_volumes = 25` |
| `neutron.conf` | `[quotas]` | `quota_`-prefixed | `quota_floatingip = 15` |

Two names also do not translate one-for-one: Cinder's `per_volume_gigabytes` is
`per_volume_size_limit` in the file, and Neutron's `default_quota` is a catch-all with no
counterpart in `NEUTRON_DEFAULTS`. Both need a decision at implementation time rather than
a mechanical rename.

### Scope

Quotas first, because that is the concrete complaint. The same loader then has obvious
second users — `[DEFAULT] cpu_allocation_ratio` and `ram_allocation_ratio` in
`nova.conf` are already settings here under `OPENSTACK_SIMULATOR_*` names — so the
loader should be general from the start even if only `[quota]` is wired up in the first
pass.

### Precedence

Four layers, lowest first. This matches real OpenStack for the bottom two and keeps the
existing environment variables working, which is what the tests and `openrc.sh` use:

1. the dicts in `app/services/quotas.py` — the compiled-in default, unchanged
2. the conf file, when one is found
3. `OPENSTACK_SIMULATOR_*` environment variables
4. the per-project override in the `quotas` table — always wins, as it does today

Putting the environment above the file is the one departure from oslo.config, and it is
deliberate: a test that exports a variable should not be silently overridden by a file
left in the working directory.

### Open at implementation time

- **How the files are found.** Real OpenStack takes `--config-file` and falls back to
  `/etc/nova/nova.conf`. Reading `/etc` from a simulator that runs unprivileged in a
  working directory is wrong, so this most likely becomes a `--config-dir` flag defaulting
  to `./etc/`, with `--config-file` accepted per service.
- **Reload.** The dicts are module constants read at import, so a file change needs a
  restart. Live reload is a separate question and should not block the first pass.
- **Whether `openstack quota show --default` should reflect the file.** It reads
  `NOVA_DEFAULTS` directly (`app/api/nova.py:1643`, and the two siblings in Cinder and
  Neutron). If the loader mutates those dicts at startup the endpoints follow for free; if
  it layers on top, all three need rewiring.

---

## Open questions

These are blocked on a decision, not on time. Each changes the shape of the work enough
that guessing wrong means redoing it.

### 1. RBAC — how far should authorisation go?

**Today:** project isolation is modelled (another tenant's resource is a 404), but
per-role authorisation is not. Inside its own project a `reader` token can do everything
an `admin` token can.

**Why it is a question:** this is not an endpoint to add. Every handler would need a
policy check, which means touching all ~409 routes and deciding what the default policy
file looks like.

The options, roughly:

- **Leave it.** Documented in README Limitations. Cheapest, and multi-tenancy testing
  already works.
- **Three-tier check** (`reader` / `member` / `admin`) applied in the shared `require()`
  dependency, with a per-route override. Catches the common "my service account only has
  `reader`" bug without modelling oslo.policy.
- **Real policy files.** Faithful, and a large amount of work for a simulator whose users
  mostly do not test policy.

*Recommendation: the three-tier check, if anything. It catches the realistic failure at a
fraction of the cost.*

### 2. Swift large objects — do they fit the zero-storage design?

**Today:** every uploaded byte is hashed and discarded (README, "Zero-storage payloads").
That is a deliberate design choice and the reason 13 MB of uploads leaves the database at
~620 KB.

**Why it is a question:** SLO and DLO are *manifests* — a large object is defined by the
list of segments it is assembled from. Implementing them means storing object structure,
which is the first real exception to the zero-storage rule.

The options:

- **Store manifests only.** The segment list is metadata, not payload, so the rule mostly
  survives: a manifest records which segments exist and their sizes, and `GET` still
  returns no bytes. Consistent with how images and objects already work.
- **Decline and document.** SLO/DLO stays out of scope, listed in Limitations next to the
  zero-storage principle.

*Recommendation: store manifests only. It keeps the principle and makes the common client
path — `swift upload --segment-size` — work.*

### 3. Should ports be configurable?

**Today:** `PORTS` in `app/core/config.py` is hardcoded, and no environment variable moves
a port. But the pre-flight error message says:

> Free it first, or point the simulator elsewhere with `OPENSTACK_SIMULATOR_*` settings.

There is no such setting, so the message sends the reader looking for something that does
not exist. This is not hypothetical — Swift's 8080 is the default HTTP-alt port and
collides regularly.

The options:

- **Correct the message.** One line, pointing at `--service` and freeing the port. No new
  capability, and honest.
- **Make ports configurable** (`OPENSTACK_SIMULATOR_SWIFT_PORT=8081`), which makes the
  message true. Larger than it looks: the service catalog, `openrc.sh` and `clouds.yaml`
  all encode the ports and would have to follow.

*Recommendation: correct the message now; make ports configurable only if the collisions
become a real nuisance.*

### 4. Should `?status=` see unresolved transitions?

**Today:** transitions resolve lazily on read, so a server whose build window has elapsed
is still stored as `BUILD` until something reads it. `GET /v2.1/servers?status=ACTIVE`
filters in SQL and misses it; a real cloud would return it. Same for every other
status-filtered listing.

The options:

- **Resolve before filtering.** Load the candidate rows, settle them, then filter in
  Python. Correct, and gives up the SQL-side filter on large listings.
- **Filter on the effective status in SQL** with a `CASE` that accounts for an elapsed
  `transition_until`. Keeps it in SQL; the expression has to be repeated per model.
- **Leave it**, documented under Known behavioural quirks.

*Recommendation: resolve before filtering. The listings are small by construction — one
node — and correctness is the point of the simulator.*

---

## Decisions already made

Recorded so they are not re-opened by accident.

- **Placement resource-provider create/delete is out of scope.** The simulator has one
  node by design; a second provider that nothing could schedule to would be fiction rather
  than a gap. Traits, aggregates and custom resource classes — the things an operator
  actually sets on a single-provider cloud — are implemented.
- **The seeded `admin` project gets unlimited quotas.** Even the service defaults (10
  instances, 20 cores, 80 GB RAM) would bind long before a 256 GB node does, so a default
  install would never reach the depletion model the simulator exists to demonstrate. A
  project created afterwards gets the real defaults.
- **An unversioned request is served at the service minimum**, not the maximum, matching a
  real deployment — which is what makes a missing microversion pin visible.
