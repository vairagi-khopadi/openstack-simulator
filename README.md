# OpenStack-Simulator — a lightweight OpenStack API simulator for local development and CI

Run a full OpenStack control plane on a laptop. Eleven services — Keystone, Nova, Cinder,
Glance, Neutron, Placement, Octavia, Swift, CloudKitty, a failure-injection API and a live
dashboard — answer on their **native OpenStack ports** from a single ~90 MB Python process.
No hypervisor, no virtual machines, no DevStack, no hardware.

It is a **DevStack alternative** for the cases DevStack is too heavy for: testing
`python-openstackclient`, the OpenStack SDK and the Terraform OpenStack provider in CI, on
an old laptop, or inside a container.

What makes it more than a set of stub endpoints is that it models **bare-metal resource
depletion and control-plane state**. Nothing is virtualised, but booting a `m1.medium`
deducts 2 vCPU, 4096 + 256 MB of RAM and 40 GB of disk from a simulated 256 GB node — and
the node fills up, refuses the next boot, and reports the shortfall exactly as Nova would.

![The status dashboard on port 10000: live capacity meters for vCPU, RAM, disk and
conntrack, instances across BUILD / ACTIVE / SHUTOFF / SHELVED_OFFLOADED, attached
volumes, load balancers and on-the-fly rating](docs/dashboard.png)

## What you get

| | |
| --- | --- |
| **11 services, one process** | Native OpenStack ports, one asyncio loop, ~90 MB RSS |
| **Real depletion** | A 256 GB node that actually fills up and refuses the next boot |
| **Upstream error bodies** | `itemNotFound`, `NeutronError`, swob HTML — the real dialect per service |
| **Failure injection** | 500s, 503s, 429s, latency, timeouts and quota exhaustion on demand |
| **On-the-fly billing** | CloudKitty rating computed from SQL aggregates, no collector |
| **Live dashboard** | Capacity meters, instance states, volumes, LBs and cost on port 10000 |
| **Real microversions** | The version header is negotiated and acted on, not echoed back |
| **Enforced quotas** | Per-project limits that bind before the hardware does |
| **Persistent state** | SQLite across restarts — or delete the file to start over |
| **In-process test suite** | 727 tests, no ports bound, fresh schema per test |

## Quick start

```bash
uv venv .venv && uv pip install -r requirements.txt   # or: python -m venv .venv; pip install -r requirements.txt
.venv/bin/python seed.py --reset                      # node, identity, catalog, flavors, images, networks
.venv/bin/python main.py                              # all 11 services, one process (~90 MB RSS)

source openrc.sh                                      # or: cp clouds.yaml ~/.config/openstack/
openstack server create --flavor m1.small --image cirros --network private vm1
open http://127.0.0.1:10000/                          # live capacity dashboard
```

Ctrl-C stops all eleven services. To run it in the background instead:

```bash
.venv/bin/python main.py --detach                     # or -d
.venv/bin/python main.py --status                     # running? on which ports?
.venv/bin/python main.py --stop                       # SIGTERM, then wait for a clean exit
```

## Starting and stopping

| Command | What it does |
| --- | --- |
| `main.py` | run in the foreground, Ctrl-C to stop |
| `main.py --detach` (`-d`) | run in the background, return once every port answers |
| `main.py --status` | is it running, and on which ports |
| `main.py --stop` | SIGTERM, then wait for a clean exit |
| `main.py --service nova --service keystone` | run a subset |
| `main.py --database dev.db` (`-D`) | run against a particular database — one per environment |
| `main.py --log-level info --access-log` | turn the noise up |
| `main.py --help` | all of the above |
| `seed.py --reset` | rebuild the node, identity, catalog, flavors, images, networks |
| `seed.py --database dev.db` (`-D`) | seed that environment instead of the default one |
| `seed.py --seed-data mycloud.json` (`-S`) | take the flavor and image lists from a JSON file |

`--detach` re-execs the entry point in its own session, so closing the terminal or
Ctrl-C'ing the shell that launched it leaves the simulator running. It does not return
until every port actually answers — a background start that returned earlier would just
move the startup race into your script:

```bash
.venv/bin/python main.py --detach && openstack server list   # no sleep needed
```

If the run dies on the way up, `--detach` exits non-zero and prints the tail of the log
rather than reporting a success you would only discover later. Output goes to
`openstack-simulator.log` (`OPENSTACK_SIMULATOR_LOG_FILE`), appended per run.

`main.py` writes its pid to `openstack-simulator.pid` on start and removes it on exit
(override the location with `OPENSTACK_SIMULATOR_PID_FILE`). A stale file left by a
`kill -9` is detected and cleaned up rather than trusted, and the pid is checked against
`/proc` before any signal is sent, so a recycled pid can never be signalled by mistake.
Starting a second instance is refused with a clear message instead of eleven
`Address already in use` errors, and a port held by some *other* process is named before
anything is spawned.

## Multiple environments

The whole cloud is one SQLite file, so an environment is a file. `--database` (`-D`)
picks which one, and everything else — capacity, quotas, instances, billing — follows
from it:

```bash
.venv/bin/python seed.py  --database prod.db    # build each environment once
.venv/bin/python seed.py  --database dev.db

.venv/bin/python main.py --detach --database dev.db     # bring one up
.venv/bin/python main.py --status                       # says which one is running
.venv/bin/python main.py --stop
.venv/bin/python main.py --detach --database prod.db    # same ports, other cloud
```

Seed each environment with the same value you run it with. The ports are identical in
every environment — only one can be up at a time — so the database is named in the
startup banner, in `--status`, and in the top-right corner of the dashboard, which is
the only way to tell at a glance which cloud you are pointed at.

`--database` takes three shapes:

| Value | Means |
| --- | --- |
| `dev.db`, `prod`, `~/clouds/staging.db` | a SQLite file; a bare name gains `.db`, and a missing parent directory is created |
| `:memory:` | a throwaway cloud, seeded automatically at startup and gone on exit |
| `postgresql+asyncpg://…` | a full SQLAlchemy url, for a backend other than SQLite |

`OPENSTACK_SIMULATOR_DATABASE` sets the same thing from the environment (as does
`OPENSTACK_SIMULATOR_DATABASE_URL`, which takes a url only and wins over both). The
default stays `openstack_simulator.db` in the working directory, so a run with no flag
behaves exactly as before.

A database that exists but was never seeded has no admin user, so every request would
come back `401`. Rather than let that look like a broken simulator, startup says so and
names the command that fixes it.

## Your own flavors and images

`--seed-data` (`-S`) points the seeder at a JSON file and uses the lists in it instead of
the built-in `m1.*` flavors and cirros/ubuntu images:

```json
{
  "flavors": [
    {"id": "10", "name": "c1.large", "vcpus": 8, "ram": 16384, "disk": 100},
    {"id": "11", "name": "c1.xlarge", "vcpus": 16, "ram": 32768, "disk": 200,
     "extra_specs": {"hw:numa_nodes": "2"}}
  ],
  "images": [
    {"name": "debian-12", "min_ram": 512, "min_disk": 10, "size": 350000000,
     "disk_format": "qcow2", "properties": {"os_distro": "debian"}}
  ]
}
```

```bash
.venv/bin/python seed.py --reset --seed-data mycloud.json
```

A section you leave out keeps its built-in list, so a file with only `images` still gets
`m1.tiny` and friends; `"flavors": []` seeds none at all. A flavor needs `name`, `vcpus`,
`ram` and `disk`, and may also carry `id`, `ephemeral`, `swap`, `rxtx_factor`,
`is_public`, `disabled`, `description` and `extra_specs`. An image needs `name`,
`min_ram`, `min_disk`, `size` and `disk_format`, and may carry `properties`; its id and
checksums are derived from its name, so they are stable across reseeds.

The whole file is checked before the first row is written — an unknown key, a missing
one, a wrong type or a repeated name is reported with the entry that caused it and
nothing is seeded. Both lists are matched by name, so adding an entry and rerunning
without `--reset` tops it up rather than duplicating anything.

## Services

| Service | Port | Base path | Notes |
| --- | --- | --- | --- |
| Keystone | 5000 | `/v3` | UUID tokens via `X-Subject-Token`, projects, users, roles, groups, application credentials |
| Nova | 8774 | `/v2.1` | servers, flavors, keypairs, hypervisors, diagnostics, console, server groups, interfaces, tags |
| Cinder | 8776 | `/v3/{project_id}` | volumes, types, snapshots, attachments, backups, transfers, metadata (`/v3/...` also works) |
| Glance | 9292 | `/v2` | image catalog, import workflow + tasks, tags, member sharing; uploads are hashed and discarded |
| Neutron | 9696 | `/v2.0` | networks, subnets, ports, routers, security groups, floating IPs, trunks, subnet pools |
| Placement | 8778 | `/` | resource providers, inventories, usages, allocations, traits, aggregates |
| Octavia | 9876 | `/v2/lbaas` | load balancers, listeners, pools, members, monitors, L7 policies + rules, statistics |
| Swift | 8080 | `/v1/AUTH_{project}` | containers + object metadata, bulk delete, COPY, expiry; bodies discarded |
| CloudKitty | 8889 | `/v1` | rating computed per request from SQL aggregates, configurable via the hashmap module |
| Scenarios | 8999 | `/v1/scenarios` | failure injection control plane |
| Dashboard | 10000 | `/` | live capacity bars, instances, volumes, LBs, billing |

## How it compares to DevStack

DevStack gives you a real OpenStack, virtual machines included. This gives you the control
plane only — which is the part most integrations actually talk to.

| | DevStack | OpenStack-Simulator |
| --- | --- | --- |
| Time to first API call | 30+ minutes | seconds |
| Footprint | several GB, dozens of processes | ~90 MB, one process |
| Needs a dedicated VM or host | yes | no — runs in a CI container |
| Boots real VMs | yes, libvirt/QEMU | no, nothing is virtualised |
| Resource limits | your actual hardware | a modelled node that fills and refuses boots |
| Inject a 503 from Nova | restart things and hope | one `curl`, expires on its own |
| Teardown | slow, and not always clean | delete one SQLite file |
| Tests policy files and RBAC | yes | **no** |
| Tests scheduling across hosts | yes | **no** — there is one node |

Reach for DevStack when what you are testing *is* OpenStack. Reach for this when what you
are testing is a **client of** OpenStack — an SDK call, a Terraform plan, a billing
integration, a retry path — and you want it to run on a laptop or in CI. See
[Limitations](#limitations) for the full list of what is not modelled.

## Client compatibility

| Client | Status |
| --- | --- |
| [`python-openstackclient`](https://docs.openstack.org/python-openstackclient/) 10.3.0 | **verified** — every command in the walkthrough below |
| [`openstacksdk`](https://docs.openstack.org/openstacksdk/) | **verified** end to end |
| `curl` / raw HTTP | **verified** — set `OPENSTACK_SIMULATOR_REQUIRE_AUTH=0` to skip tokens |
| [Terraform OpenStack provider](https://registry.terraform.io/providers/terraform-provider-openstack/openstack/latest) | design target, **not yet exercised** |
| [gophercloud](https://github.com/gophercloud/gophercloud) | untested |

Anything that speaks the OpenStack wire format should work — the catalog, microversion
headers and [error bodies](#error-formats) are the real ones. Only the first three rows
have actually been run, though, and the table says so rather than implying more.

## Using with the OpenStack CLI

Every command below is verified against this simulator with `python-openstackclient`
10.3.0. The catalog it returns points at the loopback ports, so no endpoint overrides are
needed.

```bash
pip install python-openstackclient python-octaviaclient
source openrc.sh

openstack token issue                       # 32-char UUID token
openstack catalog list                      # all 10 services
openstack flavor list
openstack image list

openstack server create --flavor m1.small --image cirros --network private web-01
openstack server list                       # BUILD, for a random 10-60s
openstack console log show web-01           # synthetic cloud-init boot log

# Instances and volumes are not actionable until they leave their transition window,
# exactly as on a real cloud -- acting too early returns 409, so wait for it:
until openstack server show web-01 -f value -c status | grep -qx ACTIVE; do sleep 5; done

openstack server stop web-01                # SHUTOFF still holds its cores and RAM

openstack volume create --size 25 data-vol
until openstack volume show data-vol -f value -c status | grep -qx available; do sleep 5; done
openstack server add volume web-01 data-vol
openstack floating ip create public
openstack security group create web-sg

openstack container create backups
openstack object create backups ./big.iso   # streamed, hashed, discarded

openstack hypervisor stats show             # watch the node deplete
openstack loadbalancer list
```

## API documentation

Each service serves its own interactive docs, because each one is a separate FastAPI app
on its own port:

| | | |
|---|---|---|
| Keystone `:5000/docs` | Nova `:8774/docs` | Cinder `:8776/docs` |
| Glance `:9292/docs` | Neutron `:9696/docs` | Placement `:8778/docs` |
| Octavia `:9876/docs` | Swift `:8080/docs` | CloudKitty `:8889/docs` |
| Scenarios `:8999/docs` | Dashboard `:10000/docs` | |

`/redoc` and `/openapi.json` are served alongside. The schema is generated lazily on first
request, so it costs nothing at startup.

```bash
curl -s localhost:8774/openapi.json | jq -r '.paths | keys[]'    # list Nova's paths
```

Note that most request bodies show as a free-form object rather than a typed schema. That
is deliberate: handlers accept the raw body and validate inside, because real OpenStack
payloads carry a long tail of vendor extensions that a strict signature would reject. For
endpoint *semantics*, [the official API reference](https://docs.openstack.org/api-ref/) is
authoritative — this simulator follows those wire formats.

## Error formats

Errors are returned in the dialect the real service speaks, not a house style — so a test
that asserts on an error body against a real cloud sees the same body here.

| Service | Body |
|---|---|
| Nova, Cinder | `{"itemNotFound": {"message": ..., "code": 404}}` |
| Neutron | `{"NeutronError": {"type": "NetworkNotFound", "message": ..., "detail": ""}}` |
| Keystone | `{"error": {"code": ..., "title": ..., "message": ...}}` |
| Placement | `{"errors": [{"status", "title", "detail", "code", "request_id"}]}` |
| Octavia | `{"faultcode": "Client", "faultstring": ..., "debuginfo": null}` |
| Glance | `{"message": ..., "code": ..., "title": ...}` |
| Swift | `text/html` — `<html><h1>Not Found</h1><p>The resource could not be found.</p></html>` |

Two cases are not what the addressed service would produce on its own, because in a real
deployment it never gets the chance:

- **Every 401 is Keystone-shaped.** `keystonemiddleware` sits in front of Nova, Cinder,
  Neutron, Glance, Placement and Octavia and rejects an unauthenticated request before it
  reaches the service, so the body is Keystone's and the challenge is
  `WWW-Authenticate: Keystone uri="http://127.0.0.1:5000"`.
- **Swift authenticates itself**, so it answers 401 with its own
  `WWW-Authenticate: Swift realm="AUTH_{project}"` and a swob HTML body.

Swift's HTML is canned per status code and has no room for a message, exactly as upstream.
The simulator's own explanation is kept on an `X-OpenStack-Simulator-Detail` header.

## The operating principles

**Stateless polling delays.** No worker threads, no background jobs. Creating a resource
stores a `transition_until` timestamp 10–60 s in the future. While `now() < transition_until`
reads return `BUILD` / `creating` / `PENDING_CREATE`; the first read after it returns
`ACTIVE` / `available` / `ACTIVE`+`ONLINE` and persists the flip.

**Zero-storage payloads.** `PUT /v2/images/{id}/file` and `PUT /v1/AUTH_x/{c}/{o}` stream
the body slice by slice, update an MD5 (so the ETag a client verifies is real), and drop
every byte. 13 MB of uploads leaves the SQLite file at ~620 KB.

**On-the-fly billing.** CloudKitty has no collector. Instance seconds are accrued lazily
onto the row at read time; volumes, floating IPs, load balancers and objects are priced
with `julianday()` age arithmetic inside SQLite. `(accumulated_seconds / 3600) * unit_cost`.

**Real microversion negotiation.** The `OpenStack-API-Version` header (and novaclient's
older `X-OpenStack-Nova-API-Version`) is parsed, validated and *acted on*: the response
carries the version actually served, an out-of-range version is refused with `406`, a
malformed one with `400`, and — as on a real deployment — a request with no version header
is served at the service **minimum**, not the maximum.

That last part is the one that catches bugs. Nova's response has grown a field at a time,
so a server read at 2.1 has no `locked`, `tags`, `description` or `host_status` and links
to its flavor instead of embedding it; `os-quota-sets` drops the network quotas at 2.36
and the personality-file quotas at 2.57. Code that forgets to pin a microversion sees
exactly what the real cloud would send it.

**Marker pagination.** Listings take `?limit=N&marker=<id>` and answer with
`<collection>_links` (Glance: a flat `next`) while more remain, so an SDK paging through
a collection terminates on a short page instead of re-reading page one forever. Paging is
keyset-based, so it stays correct when resources are created or deleted mid-walk, and an
unknown marker is a `400` rather than a silently empty page. `?sort_key=` / `?sort_dir=`
compose with it — the keyset seeks on whichever column the sort uses — and Neutron
listings honour `?fields=`.

## Quotas

Two different ceilings, and a create is checked against both — quota first, so the error
names the one you actually hit.

**Capacity** is the node: 192 allocatable vCPU, 256 GB RAM, 4 TB disk, shared by every
project. **Quota** is one project's policy limit, stored per project and settable:

```bash
openstack quota set --instances 2 admin
openstack server create ... # third boot: 403 Quota exceeded for instances: ... 2 of 2
openstack quota show --usage # limit, in use and reserved per resource
openstack quota delete admin # back to the defaults
```

Nova, Cinder and Neutron each own the quotas for their own resources, so one
`openstack quota set` fans out to three services. Going over reports in each service's own
dialect: Nova `403`, Cinder `413 VolumeLimitExceeded`, Neutron `409 OverQuota`.

A project with nothing set gets upstream's defaults (10 instances, 20 cores, 50 GB RAM,
10 volumes, 100 networks). **The seeded `admin` project is deliberately unlimited**, so a
default install still demonstrates the depletion model below rather than stopping at 10
instances — set a quota on it to see enforcement. `OPENSTACK_SIMULATOR_ENFORCE_QUOTAS=0`
leaves the quota APIs readable and writable but binding on nothing.

## Depletion model

Seeded node `node-01`: 2 sockets / 32 cores / 64 threads, 262144 MB RAM, 4096 GB disk,
65536 conntrack entries.

- **vCPU** — overcommitted 3.0x (192 allocatable).
- **RAM** — strictly 1.0x, plus **256 MB QEMU overhead per VM**. This is normally the
  binding constraint: 56 × `m1.medium` fills the node, then boots return `403 Quota exceeded`.
- **Disk** — instance root disks *and* Cinder volumes come out of the same 4 TB pool.
- **Conntrack** — one entry per security-group rule; exhaustion returns a `409 NeutronError`.

State affects the booking, exactly as on real hardware:

| State | vCPU | RAM | Disk |
| --- | --- | --- | --- |
| `ACTIVE` / `BUILD` | held | held | held |
| `SHUTOFF` | **held** | **held** | held |
| `SHELVED_OFFLOADED` | released | released | **held** |
| deleted | released | released | released |

Nova, Placement, `/v2.1/limits` and the dashboard all read the same aggregation, so they
cannot disagree.

## Failure injection

```bash
curl -X POST http://127.0.0.1:8999/v1/scenarios \
  -H 'Content-Type: application/json' \
  -d '{"service": "nova", "action": "500_error", "duration_seconds": 30}'
```

![The dashboard highlighting two active failure injections, with hit counts and the
time left on each](docs/dashboard-failure-injection.png)

Active rules surface on the dashboard with a live hit count, so you can see exactly how
many client calls each one intercepted.

Actions: `500_error`, `503_error`, `rate_limit` (429 + `Retry-After`), `latency`,
`timeout` (504), `quota_exhausted` (403). Narrow a rule with `path_contains`, `method`
and `probability`. Rules take effect within a second and expire on their own.
`GET /v1/scenarios/actions` documents them; `DELETE /v1/scenarios` clears everything.

## Versioning

Two numbers move independently, and `main.py --version` reports both:

```
$ python main.py --version
OpenStack-Simulator 0.1.0 (database schema v1)
```

The **project version** lives in one place, `app/__init__.py`, and is read from there by
the CLI, each service's OpenAPI metadata, and an `X-OpenStack-Simulator-Version` header on
every response — handy when a client reaches an endpoint unexpectedly and you want to know
at a glance what it is talking to. Releases follow [semver](https://semver.org/) and are
recorded in [CHANGELOG.md](CHANGELOG.md).

The **database schema version** is stamped into the SQLite file itself, in
`PRAGMA user_version`. This matters because `create_all` — which is all the simulator uses
— creates tables that are missing and never emits `ALTER`. Adding a new model is picked up
on an existing file automatically; adding a *column* to an existing model would be silently
ignored, and would surface much later as `no such column` from a live request. So:

| The file says | What happens on startup |
| --- | --- |
| nothing yet | tables created and stamped |
| the current version | started, nothing to do |
| no version at all | adopted as v1 and stamped — rows are kept |
| an older version | registered migrations applied in order |
| an older version, no migration for it | **refused**, naming the remedy |
| a newer version | **refused**, naming the build that wrote it |

A refusal is a message, not a traceback, and exits non-zero:

```
This database is at schema v99, but this build only understands v1.
It was written by OpenStack-Simulator 9.9.9; this is 0.1.0.
Upgrade the simulator, or start over with:
    python seed.py --reset
```

Migrations are declarative — a description and the SQL — registered in
`app/core/schema.py` under the version they upgrade *from*:

```python
MIGRATIONS: dict[int, Migration] = {
    1: Migration(
        "servers gained a description column",
        ("ALTER TABLE servers ADD COLUMN description VARCHAR",),
    ),
}
```

Leaving a gap is a deliberate option rather than an oversight: nothing here is precious,
so for an awkward change it is entirely reasonable to skip the migration and let startup
tell people to `seed.py --reset`.

## Configuration

Every knob is an `OPENSTACK_SIMULATOR_*` environment variable — see `app/core/config.py`. Useful ones:

```bash
OPENSTACK_SIMULATOR_CPU_ALLOCATION_RATIO=16.0   # more aggressive overcommit
OPENSTACK_SIMULATOR_TRANSITION_MIN=1            # fast transitions for CI
OPENSTACK_SIMULATOR_TRANSITION_MAX=3
OPENSTACK_SIMULATOR_HOST_RAM_MB=8192            # emulate a smaller node
OPENSTACK_SIMULATOR_REQUIRE_AUTH=0              # skip tokens for curl-driven demos
OPENSTACK_SIMULATOR_DATABASE=dev.db             # which environment to run (see above)
```

`python main.py --service nova --service keystone` runs a subset.

## Tests

```bash
uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest                       # whole suite
.venv/bin/python -m pytest tests/test_nova.py -v # one service
.venv/bin/python -m pytest -k transitions        # one theme
```

> **Coverage numbers are unreliable here.** `pytest-cov` is installed, but on this
> Python 3.12 / coverage 7.16 combination it fails to attribute lines executed inside the
> async endpoint bodies — it reports the import-time lines only, so a fully exercised
> module reads as ~50%. This reproduces without pytest at all (a plain `coverage run`
> that issues one request and gets a correct 200 back still records no body lines), and
> the tracer is demonstrably active during the request. Treat the percentage as noise.

The suite runs entirely **in-process**: httpx drives each ASGI app directly, so no ports
are bound and no server needs to be started. Every test gets a freshly built in-memory
SQLite schema.

Two details make it fast and deterministic:

- The 10-60 s transition windows collapse to zero by default. Tests that need to observe
  a pending state take the `slow_transitions` fixture, then use `expire` to rewind the
  stored deadline into the past — so the state machine is exercised in milliseconds
  rather than by sleeping. `test_transitions.py` separately asserts that the real
  randomised delay stays inside [10, 60] and uses the full window.
- `tests/conftest.py` seeds the node, identity, catalog, flavors, images and networks
  through `seed.py` itself, so the fixtures and the shipped seeder cannot drift apart.

## Project structure

```
app/__init__.py the project and schema version -- the one place either is written down
app/api/        one module per service, each exporting a `router`
app/static/     dashboard markup and client script (plain files, no template engine:
                the page has no server-side variables -- it renders itself from /api/stats)
app/core/       config (specs, ratios, rates), async engine, middleware + app factory,
                schema versioning, microversion negotiation, marker pagination
app/models/     typed SQLAlchemy 2.0 models
app/services/   capacity (depletion), quotas (per-project limits), telemetry
                (diagnostics/console), rating (billing), networking (IPAM)
main.py         runs every service on one asyncio loop
seed.py         idempotent seeder (`--reset` to start over, `--seed-data` for your own
                flavors and images)
tests/          pytest suite (unit + per-service API tests), in-process via httpx
CHANGELOG.md    what changed in each release, and which schema version it ships
docs/gaps.md    what real OpenStack has that this does not, and what is out of scope
docs/roadmap.md the phased plan, what is left, and the open design questions
```

## Limitations

Worth knowing before you trust it for something:

- **The Terraform OpenStack provider is a design target, not a verified one.**
  `python-openstackclient` 10.3.0 and the OpenStack SDK are tested end to end; Terraform
  has not been exercised yet.
- **Project isolation yes, RBAC no.** Resources are owned by the project their token was
  scoped to. Nova, Cinder and Neutron scope reads to the caller's project — another
  tenant's resource returns `404`, as Neutron does — while shared and external networks
  stay visible to everyone, and an admin-roled token sees all projects. What is *not*
  modelled is per-role authorisation: inside its own project, a `reader` token can do
  everything a `member` or `admin` token can. Use it to test multi-tenancy; do not use it
  to test policy files.
- **Glance and Octavia do not scope reads yet.** They stamp the owning project on create,
  but their listings return every project's images and load balancers. Nova, Cinder,
  Neutron, Swift and CloudKitty do scope correctly.
- **Uploaded bytes are gone.** Glance and Swift hash the payload for a correct ETag and
  then discard it. `GET` on an image returns `204`; `GET` on an object returns the real
  metadata with an empty body. Anything that reads its data back will fail.
- **Physics is not simulated.** No NUMA, ballooning, page sharing, fragmentation, CPU
  contention, IO throughput or network bandwidth. Diagnostics figures are plausible
  numbers derived from the instance UUID, not measurements. What *is* modelled faithfully
  is the control plane's accounting — which is what actually breaks integrations.
- **One of everything.** A single node, region (`RegionOne`), and domain (`Default`).
  There is no scheduler to test, because there is nowhere else to place an instance.
- **Generated keypairs are decorative.** Importing a public key works properly; asking
  Nova to generate one returns synthetic material. There is no VM to log in to either way.
- **Not for exposure.** Plain HTTP, tokens that are opaque UUIDs rather than Fernet, and
  a seeded password of `secret`. Bind it to loopback and keep it there.
- **Some API families are still missing** inside the services that are simulated —
  Nova host aggregates, migrations and remote consoles; Neutron QoS, FWaaS and agents;
  Glance metadefs; Cinder consistency groups and QoS specs; Octavia amphorae and
  failover; Swift large objects and temp URLs; CloudKitty's v2 API. `docs/gaps.md` tracks
  all of it, marking what has been closed and what is deliberately out of scope;
  `docs/roadmap.md` has the plan and the open design questions.
- **Services not simulated:** Heat, Barbican, Magnum, Manila, Ironic, Designate, Ceilometer.

## License

MIT — see [LICENSE](LICENSE).
