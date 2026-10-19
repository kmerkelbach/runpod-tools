# Runpod REST API v2 reference notes

Compiled 2026-10-19 from the official docs and the published OpenAPI spec.
Nothing here was verified against the live API (no API key was used).
Where the docs are silent or contradict themselves, it says so under
**Gaps / ambiguities** instead of guessing.

Sources (all fetched 2026-10-19):

- Index: https://docs.runpod.io/llms.txt
- Overview: https://docs.runpod.io/api-reference-v2/overview
- Rate limits: https://docs.runpod.io/api-reference-v2/rate-limits
- Migration guide: https://docs.runpod.io/api-reference-v2/migrate-from-v1
- OpenAPI 3.1 spec (v2, `info.version` 2.0.0): https://api.runpod.io/v2/openapi.json
  (public, no auth; linked from the overview and the migration guide). **The
  spec is the most detailed source; the per-endpoint doc pages are rendered
  from it.**
- v1 OpenAPI spec (for the mapping): https://rest.runpod.io/v1/openapi.json
- GraphQL overview: https://docs.runpod.io/sdks/graphql/configurations
- REST v1 overview: https://docs.runpod.io/api-reference/overview

Field notation below: `name*` = required in the schema; types are JSON Schema
types; `T|null` = nullable.

---

## 1. Basics

Docs: https://docs.runpod.io/api-reference-v2/overview,
https://docs.runpod.io/api-reference-v2/rate-limits,
https://docs.runpod.io/api-reference-v2/migrate-from-v1

| Item | Value |
| --- | --- |
| Base URL | `https://api.runpod.io/v2` (spec `servers[0].url` is `https://api.runpod.io`; every path begins with `/v2/...`) |
| Auth | `Authorization: Bearer <RUNPOD_API_KEY>` ("Authentication is unchanged" from v1). Keys are permission-scoped; each operation carries `x-runpod-scope`, e.g. `pods:read`, `pods:write`, `templates:read`, `templates:write`, `volumes:read`, `catalog:read`, `secrets:read`. A valid key without the scope gets `403`. |
| Team header | `X-Team-Id: <team_id>` applies only to console (Clerk JWT) sessions; API keys created inside a team already act for that team. |
| Versioning | Path prefix `/v2`. No version header documented. |
| Content type | Requests `application/json`; errors `application/problem+json`. |
| Request size limit | `POST /v2/pods`: bodies over 102400 bytes get `413` before auth. The limit also applies to the expanded request after template inheritance, so a small body plus a large template env can 413. |
| Path params | Generic `{id}` for every resource (v1 used `{podId}`, `{templateId}`, ...). |

### Rate limits

- Per **user** (all keys and processes of one user share the quota), three
  windows at once: `minute`, `hour`, `day`. A request counts against all three;
  exhausting any one gives `429`.
- Headers on **every authenticated response** (success and 429), following the
  IETF RateLimit header draft:
  - `RateLimit-Policy: "minute";q=60;w=60, "hour";q=3000;w=3600, "day";q=50000;w=86400`
    (`q` = quota, `w` = window seconds)
  - `RateLimit: "minute";r=0;t=12, "hour";r=2800;t=1812, "day";r=49500;t=45012`
    (`r` = remaining, `t` = seconds to reset)
  - The numbers are **illustrative**; the docs say read your real quota from
    `RateLimit-Policy` and do not hardcode it. Headers are absent for
    rate-limit-exempt callers ("If your responses don't include these headers,
    your requests aren't subject to these limits").
  - `401` responses carry no rate-limit headers in the spec; `413` and the
    cluster-`409`s also list none.
- `429 Too Many Requests` adds `Retry-After: <integer seconds>` and the body:

  ```json
  {"title": "Too Many Requests", "status": 429, "detail": "rate limit exceeded for the minute window"}
  ```

- Serverless endpoint calls (`https://api.runpod.ai/v2/ENDPOINT_ID/...`) have
  separate per-endpoint limits; not covered here.
- Docs guidance: honour `Retry-After` plus jitter; slow down when any `r`
  nears 0; retry `5xx`/timeouts only for `GET`; a `POST /v2/pods` that timed
  out may have succeeded, so list pods before retrying.

### Error body (RFC 9457)

Schema `ErrorResponse`, content type `application/problem+json`:

| Field | Type | Notes |
| --- | --- | --- |
| `title*` | string | e.g. `"Not Found"` |
| `status*` | integer | HTTP status |
| `detail*` | string | human-readable |
| `errors` | array<string> | validation failures, e.g. `"$: additional properties 'bogus' not allowed"` |

v1 returned `{"message": "..."}`. Status codes used: 400 (cross-field rule
or capacity), 401, 402 (insufficient balance, documented in createPod prose
only), 403, 404, 405, 409 (invalid state transition / cluster member), 413,
422 (schema validation; also bad cursor), 429, 5xx. Capacity exhaustion on
create is a **400 with only a human-readable `detail`**, no machine code.

### Pagination

- Cursor-based. Query params `cursor` (string, opaque, from the previous
  `pagination.nextCursor`; malformed/foreign cursor → 422) and `limit`
  (integer 1–1000, default 1000).
- Paginated list responses: `{"<resource>": [...], "pagination": {"nextCursor": string|null, "hasNextPage": boolean}}`.
- Paginated: `GET /v2/pods` (newest first), `/v2/templates`,
  `/v2/network-volumes` (newest first), `/v2/serverless`, `/v2/clusters`,
  `/v2/global-volumes`, endpoint releases/builds.
- **Not** paginated: `GET /v2/catalog/gpus` (`{"gpus": [...]}` only, no
  pagination block), `/v2/catalog/cpus`, `/v2/catalog/datacenters`,
  `/v2/clusters/{id}/pods`. `GET /v2/catalog/templates` returns at most 100
  with a permanently exhausted pagination marker.
- With the 1000 default, one request returns all pods for any realistic
  account.

---

## 2. Pods

Docs: https://docs.runpod.io/api-reference-v2/pods/list-pods,
https://docs.runpod.io/api-reference-v2/pods/get-a-pod,
https://docs.runpod.io/api-reference-v2/pods/create-a-pod,
https://docs.runpod.io/api-reference-v2/pods/update-a-pod,
https://docs.runpod.io/api-reference-v2/pods/trigger-a-pod-state-transition,
https://docs.runpod.io/api-reference-v2/pods/terminate-a-pod,
https://docs.runpod.io/api-reference-v2/pods/stream-pod-logs

| Operation | Method + path | Success |
| --- | --- | --- |
| List | `GET /v2/pods?includeClusterPods=&cursor=&limit=` | 200 `ListPodsResponse` |
| Get | `GET /v2/pods/{id}` | 200 `Pod` |
| Create | `POST /v2/pods` | 201 `Pod` (status starts `PROVISIONING`) |
| Update | `PATCH /v2/pods/{id}` | 200 `Pod` |
| Start / stop / restart / terminate | `POST /v2/pods/{id}/action` body `{"action": "start"\|"stop"\|"restart"\|"terminate"}` | 200 `Pod` (start/stop/restart), 204 no body (terminate) |
| Terminate | `DELETE /v2/pods/{id}` | 204 no body |
| Logs | `GET /v2/pods/{id}/logs` | 200 SSE stream (`id:` + JSON `data:` with `source`, `line`, `ts`) |

### List filters

Only `includeClusterPods` (boolean, default false), `cursor`, `limit`. **No
filters by status, name, GPU type or data center.**

### `Pod` object (response of list/get/create/update/action)

| Field | Type | Notes |
| --- | --- | --- |
| `id*` | string | |
| `name*` | string | |
| `status*` | enum `PROVISIONING`, `STARTING`, `RUNNING`, `EXITED` (stopped), `ERROR`, `TERMINATED` | Replaces v1/GraphQL `desiredStatus`. |
| `actions*` | array<enum start/stop/restart/terminate> | Valid transitions now. RUNNING → stop/restart/terminate; EXITED, ERROR → start/terminate; PROVISIONING, STARTING → stop/terminate. |
| `image*` | string | v1 `imageName`/`image` |
| `args*` | string | Stored container command (raw string, or JSON `{"entrypoint":[...],"cmd":[...]}`). |
| `entrypoint` | array<string> | Deconstructed from `args` ("Responses always return both representations"). |
| `cmd` | array<string> | Deconstructed from `args`. |
| `disk*` | integer ≥1 | Container disk GB (v1 `containerDiskInGb`). |
| `env*` | object<string,string> | v1/REST map form (GraphQL used `[{key,value}]`). |
| `ports*` | array<string> | `"8888/http"`, `"22/tcp"` |
| `registry*` | string\|null | Registry credential ID (v1 `containerRegistryAuthId`). |
| `mounts*` | object | `persistent: {size* int ≥10, path* string}` (pod volume; v1 `volumeInGb` + `volumeMountPath`), `network: [{volumeId*, path*}]` (max 1; v1 `networkVolumeId`), `global: [{volumeId*, path}]` (max 1). persistent and network are mutually exclusive (400). |
| `gpu` | object | GPU pods only: `id*` (GPU type id, e.g. `"NVIDIA GeForce RTX 4090"`), `count` (int, default 1), `vcpuCount*` (number), `memory*` (number, GB system RAM). |
| `cpu` | object | CPU pods only: `id*`, `vcpuCount*`, `memory*`. |
| `cloud*` | enum `SECURE`, `COMMUNITY` | |
| `dataCenterId*` | string\|null | |
| `cudaVersion*` | string\|null | Host CUDA. Kept while stopped ("a stopped pod keeps its machine assignment and resumes onto the same host"). |
| `ssh*` | object | `proxy*` and `direct*`, each `PodSshEndpoint {host*, port*, username*, command*}` or null. `direct` is null unless `22/tcp` is in `ports` and the running pod has a public port for it (null while provisioning or stopped). `proxy.username` is an opaque routing token. |
| `template*` | string\|null | See gaps: createPod says it stays null. |
| `cost*` | number (float) | **USD per hour now; 0.0 when EXITED or TERMINATED.** Replaces v1/GraphQL `costPerHr`. |
| `locked*` | boolean | |
| `globalNetworking*` | object | `enabled*`, `ip`, `internalDns` |
| `runtime*` | `PodRuntime`\|null | **Null when the pod is not RUNNING.** |
| `runtime.uptime` | integer | Seconds since container start (GraphQL `runtime.uptimeInSeconds`). |
| `runtime.gpus` | array<{util int, memoryUtil int}> | |
| `runtime.cpu`, `runtime.memory` | {util int} | percent |
| `runtime.ports` | array<{private int, public int\|null, type string, ip string\|null}> | GraphQL `privatePort`/`publicPort`/`type`/`ip`. **No `isIpPublic` field.** |
| `cluster` | object | Only on cluster members. |
| `createdAt*` | date-time | |
| `startedAt*` | date-time\|null | Closest to v1 `lastStartedAt`. |

Answers to the specific questions:

- **GPU display name:** not on the pod. The pod has only `gpu.id` (the GPU type
  id such as `"NVIDIA GeForce RTX 4090"`). The short display name (`"RTX 4090"`)
  is `name` in `GET /v2/catalog/gpus`; join on `gpu.id` = catalog `id`.
- **GPU type id:** yes, `gpu.id`; `gpu.count` is the count.
- **Public IP / 22/tcp mapping:** `ssh.direct.host` + `ssh.direct.port`
  (example `195.26.233.3:34446`), or the `runtime.ports` entry with
  `private == 22 && type == "tcp"` (`ip`, `public`). No `publicIp` or
  `portMappings` top-level field like v1, and no "is this IP public" flag.
- **Uptime / last start:** `runtime.uptime` (only while RUNNING) and
  `startedAt`.
- **Cost:** `cost` (USD/hr), no `adjustedCostPerHr`.
- **desiredStatus:** replaced by `status` (enum above).

### Create body (`CreatePodRequest`)

`name*` always; exactly one of `gpu` or `cpu`; `image` required unless
`templateId` is set. `additionalProperties` is not set false at the top level
in the spec, but the spec's `ErrorResponse.errors` example is
`"$: additional properties 'bogus' not allowed"`; assume unknown fields get
a 422.

| Field | Type | Default | Notes / v1 equivalent |
| --- | --- | --- | --- |
| `name*` | string, minLength 1 | | |
| `image` | string | | v1 `imageName` |
| `templateId` | string | | Resolved **once at create time** into image, args, disk, ports, env, registry, persistent mount, startSsh, startJupyter, allowedCudaVersions. Body fields override the template's, except `env`, which is merged per key (body wins). Sending either CUDA field replaces the template's CUDA constraint entirely. Public catalog templates allowed; serverless template → 422; unknown → 404. "The created pod does not retain a link to the template (`template` stays null)." |
| `gpu` | object | | `id*` string (single GPU type, v1 `gpuTypeIds` list), `count` int ≥1 default 1 (v1 `gpuCount`), `allowedCudaVersions` array<"major.minor"> (exact match; mutually exclusive with non-empty + `minCudaVersion`), `minCudaVersion` string (numeric compare, 12.11 > 12.2), `minRamPerGpu` int (v1 `minRAMPerGPU`), `minVcpuCountPerGpu` int (v1 `minVCPUPerGPU`). |
| `cpu` | object | | `id*` (CPU flavor from `/v2/catalog/cpus`), `vcpuCount*` int ≥2, power of two. Memory derived. |
| `cloud` | enum SECURE/COMMUNITY | `SECURE` | v1 `cloudType` |
| `dataCenterIds` | array<string> | scheduler chooses | "Preferred data centers for placement." |
| `disk` | integer ≥1 | | v1 `containerDiskInGb` |
| `mounts.persistent` | `{size* int ≥10, path* string}` | | v1 `volumeInGb` + `volumeMountPath`. Disallowed on CPU pods. Marked deprecated in favour of network volumes. |
| `mounts.network` | `[{volumeId*, path*}]`, max 1 | | v1 `networkVolumeId` + `volumeMountPath`. Volume must be in the pod's data center. No default path. |
| `mounts.global` | `[{volumeId*, path}]`, max 1 | | Global volumes; not with `persistent`. |
| `ports` | array<string> | | `"22/tcp"`, `"8888/http"` |
| `env` | object<string,string> | | `{{ RUNPOD_SECRET_<name> }}` placeholders allowed (substituted at boot). |
| `args` | string | | Raw command string or JSON `{"entrypoint":[...],"cmd":[...]}`. `""` clears. |
| `cmd` | array<string> | | v1 `dockerStartCmd` (GraphQL `dockerArgs` was a string). |
| `entrypoint` | array<string> | | v1 `dockerEntrypoint`. `args` plus `cmd`/`entrypoint` allowed only if they describe the same command. |
| `registry` | string\|null | | v1 `containerRegistryAuthId` |
| `globalNetworking` | boolean | false | NVIDIA + eligible DC only. |
| `startSsh` | boolean | false | Injects `PUBLIC_KEY` from account SSH keys (`PUT /v2/account/ssh-keys`) unless env sets one. Not readable later. |
| `startJupyter` | boolean | false | Injects `JUPYTER_PASSWORD` unless env sets one. |

**Dropped vs v1 `POST /v1/pods` (no v2 field):** `interruptible` (spot pods),
`supportPublicIp`, `gpuTypeIds` as a priority list and `gpuTypePriority`,
`dataCenterPriority`, `countryCodes`, `cpuFlavorIds` list /
`cpuFlavorPriority`, `computeType` (implied by gpu/cpu), `minDiskBandwidthMBps`,
`minDownloadMbps`, `minUploadMbps`, `locked` at create (PATCH-only now),
`volumeEncrypted`. The create endpoint "places one specific GPU type. It does
not search for capacity, and it does not fall back to a different GPU": the
caller iterates candidates (read `/v2/catalog/...`, then create per
(GPU, data center) until a 201). Retry table from the docs: 422 fix request;
400 try next candidate; 402 stop; 403 skip candidate; 429 back off with
`Retry-After`; 5xx retry same candidate.

### Update (`PATCH /v2/pods/{id}`, `UpdatePodRequest`)

Partial; omitted fields untouched. Mutable: `name`, `image`, `args`/`cmd`/
`entrypoint`, `disk`, `ports`, `env` (replaces the whole env map), `registry`,
`mounts` (network/global `volumeId` immutable; only `path` changes),
`locked`, `globalNetworking` (next start), `templateId` (applies the
template's container settings once; mounts not applied). Cluster members →
409. Whether a PATCH restarts the container is not stated in the spec (the
docs page title mentions "restart behavior", but the spec text says only
"Some changes apply immediately while others ... take effect on the pod's
next start/restart").

### Stop / start / restart / terminate

- `POST /v2/pods/{id}/action` body `PodActionRequest`
  (`additionalProperties: false`, `action*` only).
- **`start` takes no `gpuCount` or any other parameter.** There is no
  equivalent of GraphQL `podResume(input:{podId, gpuCount})`. The docs say a
  stopped pod keeps its machine assignment and resumes onto the same host;
  what happens when that host's GPUs are taken is undocumented.
- `stop`: releases compute, keeps disk, status → `EXITED`.
- `restart`: RUNNING only, container restart in place.
- Invalid for current status → 409.
- `terminate` (or `DELETE /v2/pods/{id}`): irreversible; persistent mount
  destroyed, network volume detached only. 204, no body.
- v1 `POST /pods/{podId}/reset` has **no v2 equivalent** (stated in the
  migration guide).

---

## 3. GPU types and prices

Docs: https://docs.runpod.io/api-reference-v2/catalog/list-gpu-types,
https://docs.runpod.io/api-reference-v2/catalog/get-a-gpu-type,
https://docs.runpod.io/api-reference-v2/catalog/list-data-centers

Yes, v2 replaces GraphQL `gpuTypes`: `GET /v2/catalog/gpus` (list,
`{"gpus": [...]}`, not paginated) and `GET /v2/catalog/gpus/{id}` (single).
Scope `catalog:read`.

Query params (both endpoints):

| Param | Type | Notes |
| --- | --- | --- |
| `include` | array, max 1, value `AVAILABILITY` | adds `availability`, `dataCenters`, `cudaVersions` |
| `product` | array of `POD`, `CLUSTER`, `SERVERLESS` | **required with** `include=AVAILABILITY` and only valid with it (400 otherwise) |
| `count` | int ≥1, default 1 | GPU count "for availability and lowest-price calculations"; only with AVAILABILITY |
| `cloud` | `SECURE` \| `COMMUNITY` | for availability and lowest price; only with AVAILABILITY; upstream default SECURE |
| `countryCodes` | array of ISO alpha-2 | only with AVAILABILITY; list drops GPU types with no DC there |
| `cudaVersions` | array "major.minor" | exact; exclusive with `minCudaVersion` |
| `minCudaVersion` | "12" or "12.1" | floor |

Arrays are comma-separated (`include=AVAILABILITY&product=POD`).

`GpuType` fields:

| Field | Type | GraphQL equivalent |
| --- | --- | --- |
| `id*` | string, e.g. `"NVIDIA GeForce RTX 4090"` | `id` |
| `name*` | string, e.g. `"RTX 4090"` | `displayName` |
| `pool*` | string\|null | serverless pool id |
| `manufacturer*` | enum NVIDIA/AMD/UNKNOWN | |
| `memory*` | integer GB VRAM | `memoryInGb` |
| `secure*` | boolean | `secureCloud` |
| `community*` | boolean | `communityCloud` |
| `price*` | `{secure* float, community* float, serverless float}` | per single GPU, USD/hr list price; closest to `lowestPrice.uninterruptablePrice` |
| `maxCount*` | `{secure* int, community* int}` | max GPUs per pod (ceiling, not stock) |
| `availability` | enum NONE/LOW/MEDIUM/HIGH | only with AVAILABILITY; replaces stock status |
| `dataCenters` | array<{id*, name*, availability*}> | only with AVAILABILITY; omitted when unavailable everywhere |
| `cudaVersions` | array<{version*, available* boolean}> | only with AVAILABILITY |

**No spot/bid price:** there is no field like `minimumBidPrice`, consistent
with v2 having no interruptible pods. Negotiated discounts are not reflected
(stated for `serverless`; presumably also pod list prices).

`GET /v2/catalog/datacenters?include=GPU_AVAILABILITY&regions=EUROPE`
returns per-DC `gpuAvailability: [{id, name, availability}]`; other filters
`networkVolumeTypes`, `compliance`, `globalNetwork`.

---

## 4. Templates

Docs: https://docs.runpod.io/api-reference-v2/templates/list-templates,
https://docs.runpod.io/api-reference-v2/templates/get-a-template,
https://docs.runpod.io/api-reference-v2/templates/create-a-template,
https://docs.runpod.io/api-reference-v2/templates/update-a-template,
https://docs.runpod.io/api-reference-v2/templates/delete-a-template,
https://docs.runpod.io/api-reference-v2/catalog/list-public-templates,
https://docs.runpod.io/api-reference-v2/account/list-secrets

| Operation | Method + path | Success |
| --- | --- | --- |
| List own | `GET /v2/templates?cursor=&limit=` | 200 `{"templates": [...], "pagination": {...}}` (includes team/org templates) |
| Get | `GET /v2/templates/{id}` | 200 `Template` (own or public catalog) |
| Create | `POST /v2/templates` | 201 `Template` |
| Update | `PATCH /v2/templates/{id}` | 200 full `Template` (owner only; public catalog → 404) |
| Delete | `DELETE /v2/templates/{id}` | 204; 400 if referenced by a pod or bound to an endpoint |
| Public catalog | `GET /v2/catalog/templates?source=official\|verified\|community` | max 100 |

`Template` fields: `id*`, `name*`, `image*` (v1 `imageName`), `args*` +
`cmd`/`entrypoint` (v1 `dockerStartCmd`/`dockerEntrypoint`, GraphQL
`dockerArgs`), `disk*` int (v1 `containerDiskInGb`), `mounts*`
(`TemplateMounts`, `additionalProperties: false`, **only** `persistent:
{size*, path*}`, i.e. v1 `volumeInGb` + `volumeMountPath`; `network` → 422),
`ports*` array<string>, `env*` object<string,string>, `registry*`
string|null, `serverless*` boolean, `public*` boolean (v1 `isPublic`),
`category*` enum CPU/NVIDIA/AMD, `startSsh*` boolean, `startJupyter*`
boolean, `allowedCudaVersions*` array<string>. No `readme` field (v1 had one).

Create (`CreateTemplateRequest`): `name*`, `image*`; optional `args`/`cmd`/
`entrypoint`, `disk`, `env`, `ports`, `registry`, `mounts.persistent`,
`allowedCudaVersions`, `category` (default NVIDIA), `public` (default false),
`serverless` (default false), **`startSsh` (default true)**, **`startJupyter`
(default true)**.

Update (`UpdateTemplateRequest`, all optional): `name`, `image`, `args`/
`cmd`/`entrypoint`, `disk`, `ports`, `env`, `registry`, `mounts`
(`{"persistent": {size, path}}`; omitting `mounts` or sending `{}` leaves it
unchanged), `allowedCudaVersions` (`[]` clears), `category`, `public`,
`serverless`, `startSsh`, `startJupyter`.

- **Ports, volume size (`mounts.persistent.size`), env, image, container disk
  (`disk`) and the start command (`args`/`cmd`/`entrypoint`, the v2 form of
  `dockerArgs`) are all PATCHable**, which the v1 notes said needed GraphQL
  `saveTemplate`.
- **No full-object save needed:** "This is a PATCH: only the fields present
  in the body are changed; omitted fields are left untouched." Caveat:
  `PersistentMount` requires both `size` and `path`, so changing the volume
  size means sending `{"mounts": {"persistent": {"size": N, "path": "/workspace"}}}`.
  `env` is one field: sending it replaces the whole env map (the spec states
  map replacement explicitly for pod PATCH; for templates it follows from
  PATCH-of-a-field semantics but is not spelled out).
- Edits do not affect pods/endpoints already created from the template.

### Secret placeholders on GET (critical)

**Not documented either way.** What the spec does say:

- Secrets are referenced from pod, serverless and template env vars as
  `{{ RUNPOD_SECRET_<name> }}` and "substituted with the secret's value when
  the pod or worker boots."
- Secret values are "write-only and never returned by the API" (`Secret`
  schema, list/get secret endpoints).
- The `GET /v2/templates/{id}` description and `Template.env` description say
  nothing about placeholder resolution. The example env holds no placeholder.

Reading "substituted ... when the pod boots" plus "never returned by the API"
suggests v2 returns the placeholder verbatim, but this is an inference, not a
documented guarantee. v1 `GET /v1/templates/{id}` was observed to resolve
them. **Verify against the live API with a throwaway template referencing a
dummy secret before trusting a v2 read-modify-write of template env.** Until
verified, avoid sending `env` in template PATCHes that don't need to change
it (PATCH makes that possible now).

New in v2: secrets can be managed by API (`GET/POST /v2/account/secrets`,
`GET/PATCH/DELETE /v2/account/secrets/{id}`; `CreateSecretRequest {name*,
value*}`, name pattern `^[a-zA-Z_][a-zA-Z0-9_.\-/]*$`, names starting with
`RUNPOD` rejected; `GET ?name=` filter is case-insensitive). The local notes
said secrets were console-only.

---

## 5. Network volumes

Docs: https://docs.runpod.io/api-reference-v2/network-volumes/list-network-volumes

`GET /v2/network-volumes?cursor=&limit=` → 200
`{"networkVolumes": [NetworkVolume], "pagination": {...}}`, newest first.
`NetworkVolume`: `id*` string, `name*` string, `size*` int GB (10–4096),
`dataCenter*` string (v1 `dataCenterId`), `type*` enum `STANDARD` |
`HIGH_PERFORMANCE`. Also `GET/PATCH/DELETE /v2/network-volumes/{id}`,
`POST /v2/network-volumes` (`dataCenter`, optional `type`). Global volumes:
`/v2/global-volumes`.

---

## 6. Mapping from v1 and GraphQL

### REST v1 (`https://rest.runpod.io/v1`)

| v1 | v2 | Notes |
| --- | --- | --- |
| `GET /pods` (bare array) | `GET /v2/pods` → `{"pods": [...], "pagination"}` | v1 `publicIp`, `portMappings` (`{"22": 10341}`), `costPerHr`, `adjustedCostPerHr`, `desiredStatus`, `lastStartedAt`, `machine`, `gpu`, `interruptible` → v2 `ssh.direct`/`runtime.ports`, `cost`, `status`, `startedAt`, `gpu {id,count}`; no `machine`, no `adjustedCostPerHr`, no `interruptible`. |
| `POST /pods` (flat) | `POST /v2/pods` (nested) | `imageName`→`image`; `gpuTypeIds[]`+`gpuCount`→`gpu.{id,count}` (single type, no fallback); `cloudType`→`cloud`; `containerDiskInGb`→`disk`; `volumeInGb`+`volumeMountPath`→`mounts.persistent.{size,path}`; `networkVolumeId`→`mounts.network[0].{volumeId,path}`; `dockerStartCmd`→`cmd`; `dockerEntrypoint`→`entrypoint`; `allowedCudaVersions`→`gpu.allowedCudaVersions`; `minRAMPerGPU`→`gpu.minRamPerGpu`; `minVCPUPerGPU`→`gpu.minVcpuCountPerGpu`; `containerRegistryAuthId`→`registry`; `dataCenterIds`, `templateId`, `env`, `ports`, `name`, `globalNetworking` keep names. **No v2 equivalent:** `supportPublicIp`, `interruptible`, `countryCodes`, `*Priority`, bandwidth minimums, `locked` at create. |
| `POST /pods/{podId}/stop` | `POST /v2/pods/{id}/action` `{"action":"stop"}` | 200 + Pod |
| `POST /pods/{podId}/start` | `POST /v2/pods/{id}/action` `{"action":"start"}` | no gpuCount |
| `POST /pods/{podId}/restart` | `POST /v2/pods/{id}/action` `{"action":"restart"}` | |
| `POST /pods/{podId}/reset` | **none** | stated in migration guide |
| `DELETE /pods/{podId}` | `DELETE /v2/pods/{id}` or action `terminate` | 204 |
| `POST /pods/{podId}/update` | `PATCH /v2/pods/{id}` | |
| `GET /networkvolumes` | `GET /v2/network-volumes` → `{"networkVolumes": [...]}` | `dataCenterId`→`dataCenter`, new `type` |
| `PATCH /templates/{templateId}` | `PATCH /v2/templates/{id}` | `imageName`→`image`, `containerDiskInGb`→`disk`, `volumeInGb`+`volumeMountPath`→`mounts.persistent`, `dockerStartCmd`/`dockerEntrypoint`→`cmd`/`entrypoint`, `isPublic`→`public`, `containerRegistryAuthId`→`registry`; `readme` dropped. |
| `GET /templates[/{id}]` | `GET /v2/templates[/{id}]` | secret resolution undocumented (section 4) |

### GraphQL (`https://api.runpod.io/graphql`)

| GraphQL | v2 | Notes |
| --- | --- | --- |
| `myself { pods { id name desiredStatus costPerHr ... } }` | `GET /v2/pods` | `desiredStatus`→`status`, `costPerHr`→`cost` (0 when stopped) |
| `... machine { gpuDisplayName }` | **not on pod**; `GET /v2/catalog/gpus` `name`, joined on `pod.gpu.id` | one extra (cacheable) call |
| `... runtime { uptimeInSeconds }` | `runtime.uptime` | null unless RUNNING; `startedAt` always present |
| `... runtime { ports { ip isIpPublic privatePort publicPort type } }` | `runtime.ports[] { ip, private, public, type }`; also `ssh.direct {host, port}` for 22/tcp | **`isIpPublic` has no equivalent**; v2 only fills `ssh.direct` when a public port exists |
| `podResume(input: {podId, gpuCount})` | `POST /v2/pods/{id}/action` `{"action":"start"}` | **no gpuCount**; cannot resume with a different GPU count |
| `podTerminate(input: {podId})` | `DELETE /v2/pods/{id}` (204) | |
| `gpuTypes { id displayName memoryInGb secureCloud communityCloud }` | `GET /v2/catalog/gpus` `{ id, name, memory, secure, community }` | |
| `lowestPrice(input:{gpuCount:1}) { uninterruptablePrice }` | `price.secure` / `price.community` (per GPU, list); `?include=AVAILABILITY&product=POD&count=N&cloud=...` for stock | exact equivalence of `price` to "lowest price" unclear (gaps) |
| `lowestPrice { minimumBidPrice }` | **none** (no spot in v2) | |
| `myself { podTemplates { id name imageName ports containerDiskInGb volumeInGb volumeMountPath dockerArgs env { key value } } }` | `GET /v2/templates` | `env` is a map, not `[{key,value}]`; `dockerArgs` string → `args` string (+ `cmd`/`entrypoint`); placeholder behaviour undocumented |
| `saveTemplate(input: SaveTemplateInput!)` (full object) | `PATCH /v2/templates/{id}` (partial) | ports, volume, env, image, disk, args all PATCHable; create via `POST /v2/templates` |
| `podFindAndDeployOnDemand` | `POST /v2/pods` | v2 does not "find"; iterate candidates yourself |

---

## 7. Deadlines (as stated)

- **REST v1:** "REST API v1 is deprecated and will be retired on November 15,
  2026. Migrate your integrations to v2 before that date."
  (https://docs.runpod.io/api-reference-v2/migrate-from-v1; same date in bold
  on https://docs.runpod.io/api-reference/overview).
- **GraphQL:** "The GraphQL API is deprecated and will be retired in early
  2027. For new integrations, use REST API v2."
  (https://docs.runpod.io/sdks/graphql/configurations). **The docs I read do
  not say "January 2027" or limit it to "external integrations"**; that
  wording must come from another source (email/announcement). Treat early
  2027 as the documented date.

---

## Gaps / ambiguities

1. **Template GET and `{{ RUNPOD_SECRET_* }}`**: undocumented. Must be
   tested live before any template env round-trip.
2. **Resume with GPU count**: `start` takes no parameters; behaviour when
   the original host has no free GPUs is undocumented (v1/GraphQL resume
   could fail with "not enough free GPUs" and needed `gpuCount`).
3. **`template` field contradiction**: the createPod `templateId` text says
   "`template` stays null", but the getPod example shows
   `"template": "9x4m2p7v"`. Don't rely on it.
4. **Public IP**: no `supportPublicIp` on create and no `isIpPublic`
   on read. For Community Cloud pods there is no documented way to request a
   public IP; `ssh.direct` being non-null is the only signal that a direct
   port exists.
5. **Interruptible/spot pods**: absent from v2 (no field anywhere in the
   spec; v1 had `interruptible`). Either unsupported or undocumented.
6. **Price semantics**: `price` is described as "list price" per GPU, but
   the `count`/`cloud` params say they affect "lowest-price calculations".
   Whether `price` changes with those params, or matches GraphQL
   `lowestPrice.uninterruptablePrice`, is unclear.
7. **Rate-limit quotas**: only illustrative numbers in the docs
   (60/min, 3000/h, 50000/day). Real values only from `RateLimit-Policy`.
   Whether v2 shares a bucket with GraphQL/v1 is not stated (our gotchas
   say v1+GraphQL share one; v2 says limits are per user).
8. **PATCH restart behaviour for pods** (image/env/ports changes): spec
   doesn't say which changes restart the container.
9. **`costPerHr` vs `cost` when stopped**: v2 reports 0.0 for EXITED pods;
   any stopped-pod storage cost must come from `/v2/billing/pods`.
10. Capacity errors are 400 with free-text `detail` only; no machine code.
