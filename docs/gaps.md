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

- **Sorting.** No `sort_key` / `sort_dir` on any listing.
- **Field selection.** No `fields=` to trim a response.
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

Groups and group role assignments · application credentials · `/v3/credentials` · trusts
(OS-TRUST) · EC2 credentials · federation · system-scoped tokens · password change
(`POST /v3/users/{id}/password`) · project tags · project hierarchy (`parent_id`).

CRUD is read-only for services, endpoints, domains and regions. Role assignment has `PUT`
but no revoke, no `HEAD` check, no domain- or group-scoped assignments.

### Nova

**done** — Server groups: CRUD, the `group` scheduler hint at boot, membership on the
server body from 2.71, and the 2.64 change from `policies`/`metadata` to `policy`/`rules`.
Anti-affinity refuses a second member, since one node means there is nowhere else to put
it; soft policies degrade instead.

Still open: host aggregates · migrations (`/os-migrations`,
`/servers/{id}/migrations`) · instance actions (`/servers/{id}/os-instance-actions`) ·
remote consoles (`/servers/{id}/remote-consoles` — note `os-getConsoleOutput` *is*
supported as a server action) · server tags · interface attach/detach (only `GET
/os-interface` exists) · flavor update and `os-flavor-access` · extra-specs writes ·
volume-attachment update (swap) · server password.

Correctly absent: `os-floating-ips` and Nova-side security-group CRUD — real Nova removed
both at microversion 2.36.

### Cinder

**done** — Backups: create (full and incremental), list, detail, show, update, delete,
restore into a new or existing volume, and `os-reset_status` / `os-force_delete`. Bounded
by the `backups` and `backup_gigabytes` quota rather than the node's disk pool, since a
real backup lands in object storage.

Still open: volume and snapshot metadata · volume transfers · consistency groups and
group types · QoS specs · volume-type extra specs and encryption · `/v3/messages` ·
`os-services` / `os-hosts` · manage/unmanage · default types.

Volume actions stop at `os-attach`, `os-detach`, `os-extend`, `os-reset_status`,
`os-set_bootable` — no retype, migrate, upload-to-image or revert-to-snapshot.

### Glance

**done** — The import workflow (`/stage`, `/import` for glance-direct and web-download,
`/v2/info/import`, `/v2/info/stores`), the tasks API, image tags, and full member sharing
with the pending/accepted handshake.

Still open: metadata definitions (`/v2/metadefs/*`) · real multi-store (there is one
store) · cache API · task types other than import.

### Neutron

Trunks · QoS policies and rules · subnet pools · RBAC policies · address scopes and
groups · segments · FWaaS · VPNaaS · agents and agent scheduling · floating-IP port
forwarding · extra routes · network IP availability · auto-allocated topology.

Note `subnetpool` and `rbac_policy` are *counted in the quota response* while having no
endpoints behind them.

### Placement

Effectively read-only. No resource-provider create/update/delete, no inventory writes, no
trait writes, no aggregate writes, no `POST /reshaper`, no nested providers. Only
`/allocations/{consumer}` accepts `PUT` and `DELETE`.

### Octavia

L7 policies and rules · statistics (`/loadbalancers/{id}/stats`, `/listeners/{id}/stats`)
· amphorae · failover · quotas · flavor profiles · availability zones · health-monitor
update · batch member update.

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
