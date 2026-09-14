# Gaps against real OpenStack

What the real services expose that this simulator does not. Derived by enumerating the
router table (`main.build_apps()`) and comparing it to the upstream API reference, so it
describes routes that exist rather than features that were intended.

The point of the list is to be honest about the edges: code written against the simulator
should fail here, in a way you can see, rather than on a real cloud later.

Status key: **done** · **next** · *(no marker)* not started.

---

## Cross-cutting — done

These affected every service and are now implemented.

- **done** — Microversion negotiation. `OpenStack-API-Version` and
  `X-OpenStack-Nova-API-Version` are parsed, validated (`406` out of range, `400`
  malformed), `latest` resolves, and the response reports the version actually served. A
  request with no header is served at the service **minimum**, as a real deployment does.
  Response bodies branch on it: see `app/core/microversion.py` and the gates in
  `app/api/nova.py`.
- **done** — Marker pagination. `?limit=N&marker=<id>` with `<collection>_links`
  (Glance's flat `next`/`first`), keyset-based, on every major collection. See
  `app/core/pagination.py`.

Still open, cross-cutting:

- **done** — Sorting. `?sort_key=` / `?sort_dir=` on every paginated listing, resolved
  against the model with an unknown key ignored rather than refused. The marker keyset
  seeks on the sorted column, so sorting and pagination compose.
- **done** — Field selection. `?fields=` trims Neutron listings (comma-separated or
  repeated), always keeping `id`.
- **Tags.** No tags API on any resource — Neutron resource tags, Nova server tags
  (`/servers/{id}/tags`), Glance image tags.
- **Unified limits.** Keystone `/v3/limits` and `/v3/registered_limits`, the modern
  replacement for per-service quota APIs.
- **RBAC.** Project isolation is modelled; per-role authorisation is not. A `reader`
  token can do anything a `member` can inside its own project.

---

## Quotas — done

Per-project limits are stored (`app/models/quota.py`), served, and **enforced** on create
ahead of the capacity check (`app/services/quotas.py`). Nova and Cinder have `/detail`,
`/defaults`, `PUT` and `DELETE`; Neutron has the collection list, `/default`, `/details`,
`PUT` and `DELETE`.

Still open here:

- `os-quota-class-sets` — class-level defaults, on Nova and Cinder.
- Quotas for resources nothing counts yet: `key_pairs`, `metadata_items`, `server_groups`
  (Nova), `backups` (Cinder), `rbac_policy` and `subnetpool` (Neutron). The limits are
  stored and reported; there is no usage behind them because the resource itself is not
  implemented.
- Octavia quotas (`/v2.0/lbaas/quotas`).

---

## Per service

### Keystone

**done** — Groups (CRUD, membership, the `HEAD`/`GET` membership check) and group role
assignments, which resolve through membership at token issuance rather than being copied
onto users. Application credentials, with the secret returned only at creation.

Still open: `/v3/credentials` · trusts (OS-TRUST) · EC2 credentials · federation ·
system-scoped tokens · password change (`POST /v3/users/{id}/password`) · project tags ·
project hierarchy (`parent_id`).

CRUD is still read-only for services, endpoints, domains and regions, and user role
assignments have `PUT` but no revoke.

### Nova

**done** — Server groups: CRUD, the `group` scheduler hint at boot, membership on the
server body from 2.71, and the 2.64 change from `policies`/`metadata` to `policy`/`rules`.
Anti-affinity refuses a second member, since one node means there is nowhere else to put
it; soft policies degrade instead.

**done** — Interface attach/detach and show, server tags (gated on 2.26), flavor update
(2.55, description only) and `os-flavor-access` with `addTenantAccess` /
`removeTenantAccess`.

Still open: host aggregates · migrations (`/os-migrations`,
`/servers/{id}/migrations`) · instance actions (`/servers/{id}/os-instance-actions`) ·
remote consoles (`/servers/{id}/remote-consoles` — note `os-getConsoleOutput` *is*
supported as a server action) · extra-specs writes · volume-attachment update (swap) ·
server password.

Correctly absent: `os-floating-ips` and Nova-side security-group CRUD — real Nova removed
both at microversion 2.36.

### Cinder

**done** — Backups: create (full and incremental), list, detail, show, update, delete,
restore into a new or existing volume, and `os-reset_status` / `os-force_delete`. Bounded
by the `backups` and `backup_gigabytes` quota rather than the node's disk pool, since a
real backup lands in object storage.

**done** — Volume and snapshot metadata (whole-dict and per-key), volume transfers with
the auth-key handshake, and the volume actions `os-retype`, `os-volume_upload_image` and
`revert_to_snapshot`.

Still open: consistency groups and group types · QoS specs · volume-type extra specs and
encryption · `/v3/messages` · `os-services` / `os-hosts` · manage/unmanage · default types ·
volume migrate.

### Glance

**done** — The import workflow (`/stage`, `/import` for glance-direct and web-download,
`/v2/info/import`, `/v2/info/stores`), the tasks API, image tags, and full member sharing
with the pending/accepted handshake.

Still open: metadata definitions (`/v2/metadefs/*`) · real multi-store (there is one
store) · cache API · task types other than import.

### Neutron

**done** — Trunks (with subport add/remove and the exclusivity rules) and subnet pools
(with real non-overlapping allocation, so `subnetpool_id` on a subnet carves the next free
prefix out of the pool).

Still open: QoS policies and rules · RBAC policies · address scopes and groups · segments ·
FWaaS · VPNaaS · agents and agent scheduling · floating-IP port forwarding · extra routes ·
network IP availability · auto-allocated topology.

`rbac_policy` is still counted in the quota response with no endpoints behind it.

### Placement

**done** — Custom traits (`PUT`/`GET`/`DELETE /traits/{name}`), provider traits
(`PUT`/`DELETE /resource_providers/{uuid}/traits`), provider aggregates, and custom
resource classes. Standard names are protected and a trait in use cannot be deleted.

**Deliberately not implemented** — resource provider create/update/delete and nested
providers. The simulator has one node by design (see README Limitations), and a second
provider that nothing could ever schedule to would be a fiction rather than a gap.

Still open: inventory writes (inventory is derived from the node's hardware) ·
`POST /reshaper`.

### Octavia

**done** — L7 policies and rules (with position bookkeeping and per-action validation),
load balancer and listener statistics, and health-monitor update.

Still open: amphorae · failover · quotas (`/v2.0/lbaas/quotas`) · flavor profiles ·
availability zones · batch member update.

### Swift

Bulk delete · large objects (SLO/DLO manifests) · object versioning · temp URLs and form
POST · container ACLs and sync · `COPY` · expiring objects (`X-Delete-After`).

### CloudKitty

The entire rate-configuration surface: `/v1/rating/module_config/hashmap/*` (services,
fields, mappings, thresholds, groups) and the pyscripts module — so rates are settable
only through `OPENSTACK_SIMULATOR_RATE_*` environment variables. No v2 API
(`/v2/summary`, `/v2/dataframes`, scope state).

---

## Known behavioural quirks

Not missing endpoints, but places where a present endpoint behaves unlike the real one.

- **`?status=` filters on stored state.** Transitions resolve lazily at read time, so a
  server whose build window has elapsed is still stored as `BUILD` until something reads
  it. `GET /v2.1/servers?status=ACTIVE` filters in SQL and therefore misses it, where a
  real cloud would return it.
- Glance and Octavia do not scope listings to the caller's project (see README
  Limitations).
